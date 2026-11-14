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

The module deliberately has no knowledge of libraries, manifests or SQLite: it
only answers "is this still the thing I created".
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

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


@dataclass(frozen=True)
class _Member:
    name: str
    kind: str  # "file" or "dir"
    key: tuple[int, int]
    size: int
    mtime_ns: int


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
        self._members: dict[str, _Member] = {}

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

    def record(self, name: str, *, kind: str = "file", required: bool = True) -> bool:
        """Record one member this run has just created; True when it exists.

        Only a name the caller itself created may be recorded: the record is
        the ownership claim, so recording something the run did not make would
        hand the cleanup a permission it never earned.
        """
        if not isinstance(name, str) or not name or name in {".", ".."}:
            raise WorkspaceError("workspace_member_invalid: %r" % (name,))
        if "/" in name or "\\" in name:
            raise WorkspaceError("workspace_member_invalid: %r" % (name,))
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
        self._members[name] = _Member(
            name=name,
            kind="dir" if stat.S_ISDIR(info.st_mode) and not is_reparse_point(info) else kind,
            key=_key(info),
            size=int(info.st_size),
            mtime_ns=int(getattr(info, "st_mtime_ns", 0) or 0),
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
        }
        removed: list[str] = result["removed"]  # type: ignore[assignment]
        kept: list[dict[str, str]] = result["kept"]  # type: ignore[assignment]
        errors: list[str] = result["errors"]  # type: ignore[assignment]

        try:
            current = os.lstat(self.path)
        except FileNotFoundError:
            result["state"] = "ok"
            return result
        except OSError as error:
            errors.append("directory_unreadable: %s" % error)
            result["state"] = "failed"
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
            if is_reparse_point(info):
                kept.append({"name": name, "reason": "reparse"})
                continue
            if not _same(member, info):
                kept.append({"name": name, "reason": "replaced"})
                continue
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
    if kept:
        reasons = ",".join(sorted({str(item.get("reason")) for item in kept}))
        text += " kept=%d (%s) residual=%s" % (len(kept), reasons, cleanup.get("path"))
    return text
