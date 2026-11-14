"""Who created which temporary file, and what may therefore be removed again.

A maintenance run creates a private directory and fills it with files.  When
it fails, or when it is finished with the copy, it wants to take those files
away again -- but "the directory is mine" is not the same statement as
"everything inside this directory is mine".  Another process can drop a file
there, a directory entry can be replaced by a junction, and Windows reports a
junction as a directory even with follow_symlinks=False.

This module keeps the two statements apart.  A TemporaryWorkspace records the
identity of the directory when it is adopted and the identity of every member
the caller says it just created.  discard() removes only those members, only
while they still describe the same object, and never walks into a reparse
point.  Anything else stays on disk and is reported.

Two rules follow from that, and both are about evidence rather than trust:

* A member whose identity the volume cannot report is never deleted.  It is
  kept, with reason identity_unavailable, even when its size and time differ
  from the record: a half-written member may be removed only once its object
  is proven, and a name plus a guess is not proof.
* A directory whose own identity cannot be reported has none of its members
  touched at all; the whole workspace is kept and named.

The module deliberately has no knowledge of libraries, manifests or SQLite: it
only answers "is this still the thing I created".
"""
from __future__ import annotations

import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO

#: Windows marks every reparse point (symlink, junction, mount point) with this.
FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class WorkspaceError(RuntimeError):
    """The directory cannot be described, so nothing inside it may be removed."""


def _attributes(info: os.stat_result) -> int:
    return int(getattr(info, "st_file_attributes", 0) or 0)


def is_reparse_point(info: os.stat_result) -> bool:
    """True for symlinks, junctions and every other reparse point.

    os.DirEntry.is_dir(follow_symlinks=False) answers "is this a directory
    entry that is not a symlink"; on Windows a junction is a directory entry
    *and* a reparse point, so that answer is not a permission to walk into it.
    This predicate is the one the cleanup asks.
    """
    if stat.S_ISLNK(info.st_mode):
        return True
    return bool(_attributes(info) & FILE_ATTRIBUTE_REPARSE_POINT)


def _key(info: os.stat_result) -> tuple[int, int]:
    return (int(getattr(info, "st_dev", 0) or 0), int(getattr(info, "st_ino", 0) or 0))


def key_available(key: tuple[int, int]) -> bool:
    """True only when the platform really reported an identity.

    A zero device or inode is not 'the same file': it is 'this volume did not
    say'.  Treating it as an identity would let any file with the same name look
    like the one this run created, which is exactly the guess the cleanup must
    not make.  Members whose identity is unavailable are therefore never
    deleted, and a directory in that state has none of its members touched.
    """
    return key[0] != 0 and key[1] != 0


@dataclass(frozen=True)
class _Member:
    name: str
    kind: str  # "file" or "dir"
    key: tuple[int, int]
    size: int
    mtime_ns: int
    provable: bool


#: The three answers a claim check may give.  "unknown" is not "owned": a
#: volume that does not report an identity has not told us this is our file.
OWNED = "owned"
FOREIGN = "foreign"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class OwnershipClaim:
    """What this run created, and the facts that let it recognise it again.

    The handle is deliberately not part of the claim: a claim is a fact about
    an object, and the handle is the capability to write to it.  Keeping them
    apart is what lets a caller hand the claim to a checker without also
    handing it the right to write.
    """

    path: Path
    name: str
    identity: tuple[int, int]
    parent_identity: tuple[int, int]
    created_at: float
    purpose: str
    provable: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "name": self.name,
            "identity": list(self.identity),
            "parent_identity": list(self.parent_identity),
            "created_at": self.created_at,
            "purpose": self.purpose,
            "provable": self.provable,
        }


def verify_owned(claim: OwnershipClaim, *, handle: int | None = None) -> dict[str, object]:
    """Is this still the object the run created: owned, foreign or unknown?

    Two independent checks live here, because two different things can go
    wrong.  With a handle, the question is "is the object I am about to write
    to the one I created" -- that answer comes from fstat and cannot be fooled
    by renaming or replacing the name.  Without one, the question is "does the
    name still point at my object", and the answer is a fact about the
    directory entry, so a reparse point is foreign by construction.

    unknown is returned whenever the volume does not report an identity: not
    knowing is not permission.
    """
    if not isinstance(claim, OwnershipClaim):
        raise WorkspaceError("claim_invalid: %r" % (claim,))
    if not claim.provable:
        return {"state": UNKNOWN, "reason": "identity_unavailable", "via": "claim"}
    if handle is not None:
        try:
            info = os.fstat(handle)
        except OSError as error:
            return {"state": UNKNOWN, "reason": "handle_unreadable",
                    "error": type(error).__name__, "via": "handle"}
        if _key(info) != claim.identity:
            return {"state": FOREIGN, "reason": "handle_replaced", "via": "handle"}
        return {"state": OWNED, "reason": "", "via": "handle"}
    try:
        info = os.lstat(claim.path)
    except FileNotFoundError:
        return {"state": FOREIGN, "reason": "path_missing", "via": "path"}
    except OSError as error:
        return {"state": UNKNOWN, "reason": "path_unreadable",
                "error": type(error).__name__, "via": "path"}
    if is_reparse_point(info):
        return {"state": FOREIGN, "reason": "reparse", "via": "path"}
    if _key(info) != claim.identity:
        return {"state": FOREIGN, "reason": "path_replaced", "via": "path"}
    return {"state": OWNED, "reason": "", "via": "path"}


class ClaimedFile:
    """The name this run created, together with the proof that it is still it.

    A bare Path would carry no evidence, so a copy that re-opened the name
    could not tell "the file I made" from "whatever that name points at now".
    This object carries the claim, and that is what makes the two-argument
    copy interfaces safe: the proof travels with the name.

    The creation handle is deliberately not kept open.  Measured on this host,
    Python's os.open does not share the file for deletion, so holding the
    handle would make the name unreplaceable -- a takeover attempt would fail
    with "another program is using this file" instead of being *detected*,
    which hides the very window this record exists to expose.  Ownership is
    proven where it matters instead: at the write, on the descriptor that is
    about to be written through (see writer()).
    """

    def __init__(self, claim: OwnershipClaim) -> None:
        self._claim = claim

    @property
    def claim(self) -> OwnershipClaim:
        return self._claim

    @property
    def path(self) -> Path:
        return self._claim.path

    @property
    def name(self) -> str:
        return self._claim.name

    @property
    def parent(self) -> Path:
        return self._claim.path.parent

    @property
    def suffix(self) -> str:
        return self._claim.path.suffix

    def __fspath__(self) -> str:
        return str(self._claim.path)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "ClaimedFile(%r)" % (self._claim.path,)

    def resolve(self) -> Path:
        return self._claim.path.resolve()

    def unlink(self, missing_ok: bool = False) -> None:
        """Remove the *name*.  The claim stays, so verify() reports the loss."""
        try:
            os.unlink(self._claim.path)
        except FileNotFoundError:
            if not missing_ok:
                raise

    def verify(self) -> dict[str, object]:
        """Does the name still point at the object this run created?"""
        return verify_owned(self._claim)

    def writer(self) -> IO[bytes]:
        """A writer proven to be the claimed object before anything is truncated.

        Open the name, fstat the very descriptor that will be written through,
        compare it with the claim, and only then seek and truncate.  A name
        replaced in the meantime makes the descriptor describe a stranger, the
        comparison fails, and the stranger's bytes are untouched.
        """
        if not self._claim.provable:
            raise WorkspaceError(
                "claim_unprovable: %s (identity_unavailable)" % self._claim.path
            )
        flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
        try:
            handle = os.open(self._claim.path, flags)
        except FileNotFoundError:
            raise WorkspaceError("claim_name_missing: %s" % self._claim.path) from None
        except OSError as error:
            raise WorkspaceError(
                "claim_unwritable: %s (%s)" % (self._claim.path, type(error).__name__)
            ) from None
        state = verify_owned(self._claim, handle=handle)
        if state["state"] != OWNED:
            os.close(handle)
            raise WorkspaceError(
                "claim_not_owned: %s (%s)"
                % (self._claim.path, state.get("reason") or state["state"])
            )
        try:
            stream = os.fdopen(handle, "r+b", buffering=0)
            stream.seek(0)
            stream.truncate(0)
        except OSError:
            try:
                os.close(handle)
            except OSError:
                pass
            raise
        return stream


def _same(member: _Member, info: os.stat_result) -> bool:
    return (
        _key(info) == member.key
        and int(info.st_size) == member.size
        and int(getattr(info, "st_mtime_ns", 0) or 0) == member.mtime_ns
    )


class TemporaryWorkspace:
    """A directory this run created, plus the members this run created in it."""

    def __init__(self, path: Path, purpose: str, key: tuple[int, int]):
        self.path = path
        self.purpose = purpose
        self._key = key
        self._key_provable = key_available(key)
        self._members: dict[str, _Member] = {}
        self._claims: dict[str, OwnershipClaim] = {}
        #: Names whose entry turned into another object while this run worked.
        #: A conflict is data: the record keeps the original claim and the
        #: cleanup reports the name as replaced instead of adopting it.
        self._conflicts: list[dict[str, object]] = []
        self._closed = False

    @classmethod
    def adopt(cls, path: str | Path, *, purpose: str):
        """Take ownership of a directory this run has just created."""
        directory = Path(path)
        try:
            info = os.lstat(directory)
        except OSError as error:
            raise WorkspaceError(
                "workspace_unreadable: %s (%s)" % (directory, error)
            ) from None
        if is_reparse_point(info) or not stat.S_ISDIR(info.st_mode):
            raise WorkspaceError("workspace_not_a_plain_directory: %s" % directory)
        return cls(directory, purpose, _key(info))

    def release(self, name: str) -> None:
        """Forget a claimed member that was never written, and remove it.

        claim() creates the file before the caller knows the source can be read.
        When the source turns out to be gone, the empty file must not stay in a
        successful run: it is still the object this run created (same key), so
        removing it here is within the same ownership claim.

        The claim is the only thing that decides; the name is removed only
        while it still describes the object this run created.
        """
        member = self._members.get(name)
        if member is None:
            return
        target = self.path / name
        try:
            info = os.lstat(target)
        except OSError:
            return
        if not member.provable or is_reparse_point(info) or _key(info) != member.key:
            return
        try:
            os.unlink(target)
        except OSError:
            # The file is still there, so its ownership record must stay: a later
            # discard() has to keep reporting it as our half-written member
            # instead of relabelling it as a stranger.
            return
        self._members.pop(name, None)
        self._claims.pop(name, None)

    def _valid_name(self, name: object) -> str:
        if not isinstance(name, str) or not name or name in {".", ".."}:
            raise WorkspaceError("workspace_member_invalid: %r" % (name,))
        if "/" in name or "\\" in name:
            raise WorkspaceError("workspace_member_invalid: %r" % (name,))
        return name

    def claim(self, name: str, *, kind: str = "file", purpose: str = "write") -> ClaimedFile:
        """Create one member exclusively, record its identity, return its name.

        A copy that fails halfway leaves a file this run created.  Creating it
        through the workspace means the cleanup already knows it, so a failure
        cannot turn our own half-written file into an unowned stranger that has
        to be kept forever.

        The name is returned, not a held handle, and that is deliberate.  On
        this host Python's os.open does not share the file for deletion, so a
        held handle would make the name unreplaceable: a replacement attempt
        would fail with "another program is using this file" instead of being
        detected, which hides the very window this record exists to expose.
        Ownership is therefore proven where it matters -- at the write, on the
        descriptor that is about to be written through (see _copy_and_hash).
        """
        name = self._valid_name(name)
        target = self.path / name
        try:
            parent = os.lstat(self.path)
        except OSError as error:
            # Without a parent identity there is no claim to make, so nothing
            # is created: an empty file that nobody can prove is ours is worse
            # than a typed refusal.
            raise WorkspaceError(
                "ownership_unavailable: %s (%s)" % (self.path, type(error).__name__)
            ) from None
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_BINARY", 0)
        handle = os.open(target, flags, 0o600)
        try:
            info = os.fstat(handle)
        except OSError as error:
            os.close(handle)
            raise WorkspaceError(
                "ownership_unavailable: %s (%s)" % (target, type(error).__name__)
            ) from None
        key = _key(info)
        # The creation handle is closed immediately (see the docstring above);
        # the identity taken from fstat is what proves the object later.
        os.close(handle)
        claim = OwnershipClaim(
            path=target,
            name=name,
            identity=key,
            parent_identity=_key(parent),
            created_at=time.time(),
            purpose=purpose,
            provable=key_available(key),
        )
        self._members[name] = _Member(
            name=name,
            kind=kind,
            key=key,
            size=int(info.st_size),
            mtime_ns=int(getattr(info, "st_mtime_ns", 0) or 0),
            provable=claim.provable,
        )
        self._claims[name] = claim
        return ClaimedFile(claim)

    def claim_of(self, name: str) -> OwnershipClaim | None:
        """The claim recorded for one member, or None when it was never claimed."""
        return self._claims.get(name)

    def verify_member(self, name: str) -> dict[str, object]:
        """Does the name still point at the object this run created?"""
        claim = self._claims.get(name)
        if claim is None:
            return {"state": UNKNOWN, "reason": "not_claimed", "via": "path"}
        return verify_owned(claim)




    def record(self, name: str, *, kind: str = "file", required: bool = True) -> bool:
        """Refresh a member this run already owns; never adopt another object.

        Only a name the caller itself created may be recorded: the record is
        the ownership claim, so recording something the run did not make would
        hand the cleanup a permission it never earned.

        Refreshing size and time is legitimate -- the same object was just
        written.  Changing the *identity* is not: it means the name now
        describes a file this run did not create.  That case keeps the original
        claim, records a conflict, and returns False, so a copy that re-records
        after a replacement cannot launder a stranger into an owned member.
        """
        name = self._valid_name(name)
        target = self.path / name
        try:
            info = os.lstat(target)
        except FileNotFoundError:
            if required:
                raise WorkspaceError("workspace_member_missing: %s" % target) from None
            return False
        except OSError as error:
            raise WorkspaceError(
                "workspace_member_unreadable: %s (%s)" % (target, error)
            ) from None
        key = _key(info)
        existing = self._members.get(name)
        if existing is not None and existing.key != key:
            self._conflicts.append(
                {
                    "name": name,
                    "reason": "identity_changed",
                    "expected": list(existing.key),
                    "actual": list(key),
                }
            )
            return False
        self._members[name] = _Member(
            name=name,
            kind="dir" if stat.S_ISDIR(info.st_mode) and not is_reparse_point(info) else kind,
            key=key,
            size=int(info.st_size),
            mtime_ns=int(getattr(info, "st_mtime_ns", 0) or 0),
            provable=key_available(key),
        )
        return True

    @property
    def members(self) -> tuple[str, ...]:
        return tuple(sorted(self._members))

    def discard(self) -> dict[str, object]:
        """Remove the recorded members that still match; never anything else.

        The result is a fact about the cleanup, not about the work the caller
        did: state is ok only when every recorded member was removed and the
        directory itself is gone.  Anything kept is named with a reason so the
        caller can print where the leftovers are.
        """
        result: dict[str, object] = {
            "state": "ok",
            "path": str(self.path),
            "removed": [],
            "kept": [],
            "errors": [],
            "incomplete": [],
        }
        if self._conflicts:
            result["conflicts"] = list(self._conflicts)
        removed: list[str] = result["removed"]  # type: ignore[assignment]
        kept: list[dict[str, str]] = result["kept"]  # type: ignore[assignment]
        errors: list[str] = result["errors"]  # type: ignore[assignment]
        incomplete: list[str] = result["incomplete"]  # type: ignore[assignment]

        try:
            current = os.lstat(self.path)
        except FileNotFoundError:
            result["state"] = "ok"
            return result
        except OSError as error:
            errors.append("directory_unreadable: %s" % error)
            result["state"] = "failed"
            return result
        if not self._key_provable:
            # The volume did not report an identity for this directory, so no
            # member can be shown to belong to this run.  Nothing is touched.
            kept.append({"name": ".", "reason": "identity_unavailable"})
            result["state"] = "kept"
            return result
        if is_reparse_point(current) or _key(current) != self._key:
            kept.append({"name": ".", "reason": "directory_replaced"})
            result["state"] = "kept"
            return result

        for name in sorted(self._members, reverse=True):
            member = self._members[name]
            target = self.path / name
            try:
                info = os.lstat(target)
            except FileNotFoundError:
                continue
            except OSError as error:
                kept.append({"name": name, "reason": "unreadable"})
                errors.append("%s: %s" % (name, error))
                continue
            if not member.provable:
                # No identity was recorded when this member was created, so
                # nothing can prove the file under that name is the one this
                # run made.  A half-written member stays for the same reason:
                # 'size and time differ' is only evidence *after* the object
                # itself is proven.
                kept.append({"name": name, "reason": "identity_unavailable"})
                continue
            if is_reparse_point(info):
                kept.append({"name": name, "reason": "reparse"})
                continue
            if _key(info) != member.key:
                # A different object now sits under that name: not ours.
                kept.append({"name": name, "reason": "replaced"})
                continue
            if not _same(member, info):
                # Same object, different bytes: this run created it and wrote
                # into it, then stopped.  It is ours to remove, and the report
                # says it was incomplete rather than borrowed.
                incomplete.append(name)
            try:
                if member.kind == "dir":
                    os.rmdir(target)
                else:
                    os.unlink(target)
            except OSError as error:
                kept.append({"name": name, "reason": "remove_failed"})
                errors.append("%s: %s" % (name, error))
                continue
            removed.append(name)

        try:
            leftovers = sorted(entry.name for entry in os.scandir(self.path))
        except OSError as error:
            errors.append("scan_failed: %s" % error)
            leftovers = []
        for name in leftovers:
            if name not in self._members:
                kept.append({"name": name, "reason": "unowned"})

        if leftovers:
            # Something is still there.  An error while trying to remove it is
            # a different fact from an item the cleanup never owned.
            result["state"] = "partial" if removed else ("failed" if errors else "kept")
            return result
        try:
            os.rmdir(self.path)
        except OSError as error:
            errors.append("directory_remove_failed: %s" % error)
            result["state"] = "partial"
            return result
        if errors:
            result["state"] = "partial" if removed else "failed"
        elif kept:
            result["state"] = "kept"
        elif incomplete:
            result["state"] = "partial"
        else:
            result["state"] = "ok"
        return result


def describe(cleanup: object) -> str:
    """One-line, human-readable summary of a cleanup result."""
    if not isinstance(cleanup, dict):
        return "cleanup=unknown"
    state = str(cleanup.get("state") or "unknown")
    removed = len(cleanup.get("removed") or [])
    kept = cleanup.get("kept") or []
    text = "cleanup=%s removed=%d" % (state, removed)
    incomplete = cleanup.get("incomplete") or []
    if incomplete:
        text += " incomplete=%d (%s)" % (len(incomplete), ",".join(incomplete[:5]))
    if kept:
        reasons = ",".join(sorted({str(item.get("reason")) for item in kept}))
        text += " kept=%d (%s) residual=%s" % (len(kept), reasons, cleanup.get("path"))
    return text