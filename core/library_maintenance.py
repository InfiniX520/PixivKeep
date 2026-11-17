"""Read-only preflight and file-set backups for the library database.

The download core validates every declared SQL object character by character
before it opens a library (LibraryRepository._inspect_existing), so an operator
needs one answer before switching builds: will this build accept this file, and
what does going back look like if it does not.  This module answers that
without writing to the inspected database and produces a backup a human can
restore by hand.

Safety rules this module keeps:

- inspect_library, verify_library_copy and switch_plan open the database
  through a 'file:...?mode=ro' URI.  They never execute a write PRAGMA (no
  wal_checkpoint, no journal_mode=, no VACUUM, no BEGIN IMMEDIATE) and they
  never create the file.  Opening a WAL database read-only still makes SQLite
  create its own bookkeeping beside it, an empty -wal and the -shm
  shared-memory index (measured).  Neither carries library content, both are
  rebuilt on the next open, and the database bytes never move.
- The declared schema is imported from core.library_repository.  A second copy
  of 49 statements would drift on the next schema change, and the drift would
  be invisible exactly where the check has to be exact.
- A backup has two modes.  'online_backup' is the default: it asks SQLite for
  a consistent snapshot through a read-only source connection, so a live app
  keeps writing while the snapshot stays consistent and uncommitted rows stay
  out.  'file_group' copies main, -wal and -shm byte for byte and therefore
  requires a quiet source.  Either way the manifest is written last and any
  failure removes the run's own directory, so a directory that contains a
  manifest is a directory that verified.

What "compatible" means here is what the code will actually do, not a tighter
rule of our own: extra objects are reported and tolerated (the repository only
iterates its own declaration), while a missing object, a one-character text
difference, a wrong format/version marker or a failed quick_check is not
compatible.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import hashlib
import json
import os
import re
import sqlite3
import struct
import sys
import tempfile
import time
import uuid
from pathlib import Path

from core.library_repository import (
    LibraryError,
    _DDL,
    _LIBRARY_FORMAT,
    _REQUIRED_TABLES,
    _SCHEMA_VERSION,
    _validate_database_path,
)
from core.task_contracts import ContractError
from core.workspace_ownership import (
    OWNED,
    OwnershipClaim,
    ParentPin,
    TemporaryWorkspace,
    WorkspaceError,
    verify_owned,
)
from core.workspace_ownership import describe as _describe_cleanup

MANIFEST_VERSION = 2
SUPPORTED_MANIFEST_VERSIONS = (1, 2)
MANIFEST_NAME = "manifest.json"
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
FINGERPRINT_VERSION = 1
FINGERPRINT_CHUNK_ROWS = 1000
_CHUNK_BYTES = 1024 * 1024
_READ_TIMEOUT_SECONDS = 2.0
_BACKUP_SIBLINGS = ("", "-wal", "-shm")
_BACKUP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
_MAX_COLLISION = 1000
_TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"
_OBJECT_RE = re.compile(
    r"\s*CREATE\s+(?:(UNIQUE)\s+)?(TABLE|INDEX)\s+(\w+)", re.IGNORECASE
)
_EXIT_BY_STATUS = {
    "compatible": 0,
    "missing": 0,
    "empty": 0,
    "incompatible": 2,
    "unreadable": 3,
}
_EXIT_REFUSED = 4
_EXIT_INCOMPLETE = 5
# Legacy rows only: a record that never carried a digest or a size cannot be
# verified by anyone, so --allow-unknown may skip exactly these reasons.  A
# malformed value stays a reason to report, never something to wave through.
_LEGACY_UNKNOWN_REASONS = frozenset({"no_recorded_size", "no_recorded_digest"})


class LibraryMaintenanceError(RuntimeError):
    """A preflight or backup that must never be reported as success."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        #: The cleanup of the workspace this call created, kept as a field of
        #: its own.  A failure and what was left behind are two facts; folding
        #: the second into the first's message makes it unreadable to a caller
        #: that wants to act on it.
        self.cleanup: dict[str, object] | None = None
        super().__init__(code if not detail else "%s: %s" % (code, detail))


class UnsupportedPathError(LibraryMaintenanceError):
    """A path this module refuses to open, copy from or write to."""


class BackupError(LibraryMaintenanceError):
    """A backup that could not be produced or verified."""


def _declared_objects() -> dict[str, tuple[str, str]]:
    """Map declared object name to (kind, normalised statement).

    The statements come from core.library_repository._DDL so this module and
    the open path can never disagree about the expected text.
    """
    declared: dict[str, tuple[str, str]] = {}
    for statement in _DDL:
        match = _OBJECT_RE.match(statement)
        if match is None:
            raise LibraryMaintenanceError("schema_declaration_unreadable")
        name = match.group(3)
        if name in declared:
            raise LibraryMaintenanceError("schema_declaration_duplicated", name)
        declared[name] = (match.group(2).upper(), " ".join(statement.split()))
    return declared


_DECLARED = _declared_objects()
_DECLARED_TABLES = tuple(
    name for name, (kind, _) in _DECLARED.items() if kind == "TABLE"
)


# ---------------------------------------------------------------------------
# Paths and connections
# ---------------------------------------------------------------------------


def _resolve_target(path: str | Path) -> Path:
    """Absolute, link-free path, using the same guard as the open path."""
    try:
        return _validate_database_path(path)
    except LibraryError as error:
        raise UnsupportedPathError("unsupported_path", str(error)) from None


def _resolve_destination(destination_dir: str | Path, source: Path) -> Path:
    raw = Path(destination_dir).expanduser()
    text = str(raw)
    if text.startswith("\\\\") or text.startswith("//"):
        raise UnsupportedPathError("unsupported_path", "UNC destinations are not supported")
    absolute = Path(os.path.abspath(text))
    for item in (absolute, *absolute.parents):
        if item.is_symlink() or (
            hasattr(item, "is_junction") and item.is_junction()
        ):
            raise UnsupportedPathError("unsupported_path", "Linked destinations are not supported")
    resolved = absolute.resolve()
    if resolved == source.parent or source.parent in resolved.parents:
        raise BackupError(
            "destination_inside_library",
            "the backup directory must live outside %s" % source.parent,
        )
    if resolved.exists() and not resolved.is_dir():
        raise BackupError("destination_not_a_directory", str(resolved))
    return resolved


def _connect_readonly(path: Path, *, snapshot: bool = True) -> sqlite3.Connection:
    """One read-only connection; SQLite may not create or write the file."""
    connection = sqlite3.connect(
        path.as_uri() + "?mode=ro",
        uri=True,
        timeout=_READ_TIMEOUT_SECONDS,
        isolation_level=None,
    )
    try:
        connection.execute("PRAGMA busy_timeout=%d" % int(_READ_TIMEOUT_SECONDS * 1000))
        connection.execute("PRAGMA query_only=ON")
        # One read transaction keeps counters, objects and checks in a single
        # snapshot instead of three drifting reads.  The online backup API
        # needs a source connection with no open transaction, so it asks for
        # snapshot=False.
        if snapshot:
            connection.execute("BEGIN")
    except BaseException:
        connection.close()
        raise
    return connection


def _sibling_stats(path: Path) -> dict[str, dict[str, int] | None]:
    stats: dict[str, dict[str, int] | None] = {}
    for suffix in _BACKUP_SIBLINGS:
        candidate = Path(str(path) + suffix)
        try:
            info = candidate.stat()
        except FileNotFoundError:
            stats[suffix] = None
            continue
        except OSError as error:
            raise LibraryMaintenanceError("library_unavailable", str(error)) from None
        if not candidate.is_file():
            raise LibraryMaintenanceError("library_unavailable", str(candidate))
        stats[suffix] = {"bytes": info.st_size, "mtime_ns": info.st_mtime_ns}
    return stats


def _sqlite_reason(error: BaseException) -> str:
    text = str(error).lower()
    if "not a database" in text or "file is encrypted" in text:
        return "not_a_sqlite_file"
    if "locked" in text or "busy" in text:
        return "library_locked"
    if "unable to open" in text or "cannot open" in text:
        return "library_unavailable"
    return "library_unreadable"


def _is_internal_object(name: object) -> bool:
    """True for SQLite's own schema objects, judged by its literal prefix.

    SQLite reserves the "sqlite_" prefix and compares it case insensitively,
    but the underscore is an ordinary character.  A LIKE pattern would read it
    as a single-character wildcard, so a legal user table such as
    "sqliteXuser" would silently drop out of every digest and two databases
    with different content in it would compare equal (PA-02).  Every object
    enumeration in this module goes through this one predicate.
    """
    return str(name)[:7].lower() == "sqlite_"


# ---------------------------------------------------------------------------
# Reading facts
# ---------------------------------------------------------------------------


def _normalized_objects(connection: sqlite3.Connection) -> dict[str, str]:
    return {
        str(row[0]): " ".join(str(row[1]).split())
        for row in connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type IN ('table','index')"
        )
        if not _is_internal_object(row[0])
    }


def _all_ok(rows: list[str]) -> bool:
    return bool(rows) and all(str(row) == "ok" for row in rows)


def _facts(connection: sqlite3.Connection) -> dict[str, object]:
    """Read every fact the verdict needs; sqlite errors escape to the caller."""
    tables = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    facts: dict[str, object] = {"tables": tables, "warnings": []}
    meta: dict[str, str] = {}
    if "schema_meta" in tables:
        for row in connection.execute('SELECT "key", "value" FROM schema_meta'):
            key = str(row[0])
            if key in meta:
                facts["warnings"].append("duplicate_schema_marker")
                continue
            meta[key] = str(row[1])
    facts["format"] = meta.get("format")
    facts["schema_version"] = meta.get("schema_version")
    # A read-only query: no value is assigned, so no journal is converted.
    facts["journal_mode"] = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
    if "schema_meta" not in tables:
        facts["warnings"].append("no_library_schema_marker")
    actual = _normalized_objects(connection)
    facts["objects_actual"] = len(actual)
    facts["missing_objects"] = sorted(set(_DECLARED) - set(actual))
    facts["extra_objects"] = sorted(set(actual) - set(_DECLARED))
    facts["mismatched_objects"] = sorted(
        name
        for name in set(_DECLARED) & set(actual)
        if _DECLARED[name][1] != actual[name]
    )
    facts["missing_required_tables"] = sorted(_REQUIRED_TABLES - tables)
    facts["quick_check"] = [
        str(row[0]) for row in connection.execute("PRAGMA quick_check")
    ]
    facts["integrity_check"] = [
        str(row[0]) for row in connection.execute("PRAGMA integrity_check")
    ]
    facts["foreign_key_violations"] = len(
        connection.execute("PRAGMA foreign_key_check").fetchall()
    )
    counts: dict[str, int | None] = {}
    for name in _DECLARED_TABLES:
        if name not in tables:
            continue
        try:
            counts[name] = int(
                connection.execute('SELECT COUNT(*) FROM "%s"' % name).fetchone()[0]
            )
        except sqlite3.Error:
            counts[name] = None
            facts["warnings"].append("counts_unavailable")
    facts["counts"] = counts
    return facts


def inspect_library(
    path: str | Path, *, connection: sqlite3.Connection | None = None
) -> dict[str, object]:
    """Read-only verdict on one library file.

    status is 'missing', 'empty', 'compatible', 'incompatible' or
    'unreadable'.  'healthy' covers the file's own integrity only, so an
    incompatible-but-intact database still reports healthy true and a corrupted
    one reports a reason instead of a plain verdict.

    'connection' lets a caller that already holds one read transaction ask for
    the health facts of that same snapshot; the default opens a read-only
    connection of its own, which is what a plain inspect wants.
    """
    target = _resolve_target(path)
    report: dict[str, object] = {
        "path": str(target),
        "status": "missing",
        "format": None,
        "schema_version": None,
        "journal_mode": None,
        "objects_declared": len(_DECLARED),
        "objects_actual": 0,
        "missing_objects": [],
        "extra_objects": [],
        "mismatched_objects": [],
        "missing_required_tables": [],
        "quick_check": [],
        "integrity_check": [],
        "foreign_key_violations": 0,
        "counts": {},
        "size_bytes": 0,
        "wal_bytes": 0,
        "shm_bytes": 0,
        "healthy": False,
        "reasons": [],
        "warnings": [],
    }
    siblings = _sibling_stats(target)
    for key, suffix in (("size_bytes", ""), ("wal_bytes", "-wal"), ("shm_bytes", "-shm")):
        entry = siblings[suffix]
        report[key] = 0 if entry is None else int(entry["bytes"])
    if siblings[""] is None:
        report["warnings"].append("library_missing")
        return report
    if report["size_bytes"] == 0:
        report["status"] = "empty"
        report["warnings"].append("empty_file")
        return report
    try:
        if connection is not None:
            facts = _facts(connection)
        else:
            with contextlib.closing(_connect_readonly(target)) as opened:
                facts = _facts(opened)
    except sqlite3.Error as error:
        report["status"] = "unreadable"
        report["warnings"].append(_sqlite_reason(error))
        return report
    report.update(facts)
    tables = report.pop("tables", set())
    if not (set(tables) - {"sqlite_sequence"}):
        # The open path treats a database without tables of its own as a new
        # library and initialises it, so the preflight has to say the same
        # instead of calling an empty file incompatible.
        report["status"] = "empty"
        report["warnings"] = sorted(
            {str(item) for item in report["warnings"]} | {"sqlite_file_without_tables"}
        )
        report["healthy"] = True
        return report
    report["warnings"] = sorted({str(item) for item in report["warnings"]})
    report["healthy"] = (
        _all_ok(report["quick_check"])
        and _all_ok(report["integrity_check"])
        and int(report["foreign_key_violations"]) == 0
    )
    reasons: list[str] = []
    if report["format"] != _LIBRARY_FORMAT:
        reasons.append("format_mismatch")
    if report["schema_version"] != _SCHEMA_VERSION:
        reasons.append("schema_version_mismatch")
    if report["missing_required_tables"]:
        reasons.append("missing_required_tables")
    if report["missing_objects"]:
        reasons.append("missing_objects")
    if report["mismatched_objects"]:
        reasons.append("mismatched_objects")
    if int(report["foreign_key_violations"]):
        reasons.append("foreign_key_violations")
    if not _all_ok(report["quick_check"]):
        reasons.append("quick_check_failed")
    if not _all_ok(report["integrity_check"]):
        reasons.append("integrity_check_failed")
    if report["extra_objects"]:
        report["warnings"] = sorted(
            set(report["warnings"]) | {"extra_objects", "tolerated_by_open_path"}
        )
    report["reasons"] = reasons
    report["status"] = "compatible" if not reasons else "incompatible"
    report.pop("tables", None)
    return report


# ---------------------------------------------------------------------------
# Source snapshots and file copies
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _file_fact(path: Path) -> dict[str, object]:
    """Stat one group file, and hash it when the bytes are readable.

    A live writer may hold the WAL open exclusively on Windows, so hashing is
    best effort: a permission error is reported as an unreadable hash instead
    of escaping.  The file-group path treats that as proof the source is busy.
    """
    try:
        info = path.stat()
    except FileNotFoundError:
        return {"name": path.name, "present": False, "bytes": 0, "mtime_ns": 0,
                "sha256": None}
    except OSError as error:
        raise LibraryMaintenanceError("library_unavailable", str(error)) from None
    fact: dict[str, object] = {
        "name": path.name,
        "present": True,
        "bytes": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "sha256": None,
    }
    try:
        fact["sha256"] = _sha256_file(path)
    except OSError:
        fact["sha256"] = None
    return fact


def _group(path: Path) -> dict[str, dict[str, object]]:
    return {suffix: _file_fact(Path(str(path) + suffix)) for suffix in _BACKUP_SIBLINGS}


def _comparable(group: dict[str, dict[str, object]]) -> dict[str, object]:
    """What must not move: bytes and mtime of the database and its WAL.

    Two files are deliberately outside the comparison, both measured rather
    than assumed.  The -shm shared-memory index is excluded because SQLite
    rewrites its header and lock bytes whenever any connection opens or closes
    and rebuilds it from the WAL.  An empty -wal is treated like an absent one
    because a reader creates it and the last close can remove it again; it
    carries no frames, so it cannot hold a fact the copy would miss.  A WAL
    with content is compared by byte count and mtime.
    """
    comparable: dict[str, object] = {}
    for suffix in ("", "-wal"):
        entry = group[suffix]
        size = int(entry["bytes"])
        comparable[suffix] = (
            None if (not entry["present"] or size == 0)
            else (size, entry["mtime_ns"])
        )
    return comparable


def _owned_writer(destination: object) -> "object":
    """A writer for the claimed destination, proven before a byte is truncated.

    The copy functions take "a destination that may or may not carry a claim",
    so this is where that shape is checked; the proof itself is
    ClaimedFile.writer(), and there is exactly one of it.
    """
    claim = getattr(destination, "claim", None)
    writer = getattr(destination, "writer", None)
    if not isinstance(claim, OwnershipClaim) or not callable(writer):
        # No claim means no proof.  Refusing is the only honest answer: writing
        # would be writing into a file this run cannot show it created.
        raise WorkspaceError("copy_requires_claim: %r" % (destination,))
    # The proof itself lives in exactly one place (ClaimedFile.writer).  Two
    # copies of "open, fstat the descriptor, compare, then truncate" would
    # drift the day one of them is fixed and the other is not.
    return writer()


def _copy_file(source: Path, destination: object) -> dict[str, object]:
    """Copy into the object this run created, never into whatever the name says.

    destination is a name the caller claimed exclusively; the claim is what
    proves the name still describes that object.  The writer is opened and
    verified before the first byte, so a replaced name receives nothing.
    """
    digest = hashlib.sha256()
    total = 0
    # The source is opened first: if it cannot be read, the destination has not
    # been truncated yet and the caller's "this member vanished" path still
    # holds a claimed member it can release.
    with source.open("rb") as reader:
        with _owned_writer(destination) as writer:  # type: ignore[union-attr]
            while True:
                chunk = reader.read(_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
                writer.write(chunk)
                total += len(chunk)
            writer.flush()
            os.fsync(writer.fileno())
    return {
        "name": getattr(destination, "name", str(destination)),
        "bytes": total,
        "sha256": digest.hexdigest(),
    }


def _discard(workspace: TemporaryWorkspace) -> dict[str, object]:
    """Remove what this operation created and still owns; never anything else.

    The old body of this function walked the directory and deleted whatever it
    found.  A verifier does not own whatever happens to sit next to its copy, and
    on Windows a junction is listed as a directory, so that walk could delete
    files in another directory.  Ownership now lives in the workspace record;
    this name stays as the one place a caller (and a test) can replace the
    cleanup step.
    """
    return workspace.discard()


def _inspect_sidecars(path: Path) -> dict[str, object]:
    """Look at the SQLite sidecars beside one database; adopt none of them.

    SQLite creates -wal/-shm beside a database it opens, and a cleanup would
    like to remove them again.  A name is not a provenance: anything can put a
    file with exactly that name there between the open and the cleanup, and a
    suffix test would then delete a stranger while reporting success.

    So this function only reports.  A sidecar is *retained* with the reason
    that it cannot be proven to be this run's, and its identity is read
    explicitly -- if even that fails, the failure is typed and travels into the
    command's exit code instead of escaping from a finally block.
    """
    retained: list[dict[str, object]] = []
    errors: list[str] = []
    for suffix in ("-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        try:
            info = os.lstat(candidate)
        except FileNotFoundError:
            continue
        except OSError as error:
            retained.append({"name": candidate.name, "reason": "identity_unavailable"})
            errors.append(
                "sidecar_identity_unavailable: %s (%s)"
                % (candidate.name, type(error).__name__)
            )
            continue
        retained.append(
            {
                "name": candidate.name,
                "reason": "sidecar_not_proven",
                "bytes": int(info.st_size),
            }
        )
    return {"retained": retained, "errors": errors}


def _safe_cleanup(workspace: TemporaryWorkspace) -> dict[str, object]:
    """Cleanup must never replace the failure that made it necessary."""
    try:
        return _discard(workspace)
    except Exception as error:  # a cleanup failure is data, not a new crash
        return {
            "state": "failed",
            "path": str(workspace.path),
            "removed": [],
            "kept": [],
            "errors": ["cleanup_failed: %s: %s" % (type(error).__name__, error)],
        }


def _backup_name(label: str | None, destination_root: Path) -> Path:
    if label is not None:
        if not isinstance(label, str) or _BACKUP_NAME_RE.fullmatch(label) is None:
            raise BackupError(
                "invalid_label", "use 1-63 letters, digits, dot, dash or underscore"
            )
        target = destination_root / label
        if target.exists():
            raise BackupError("backup_target_exists", str(target))
        return target
    stem = "library-backup-" + datetime.datetime.now().strftime(_TIMESTAMP_FORMAT)
    for index in range(_MAX_COLLISION):
        candidate = destination_root / (
            stem if index == 0 else "%s-%d" % (stem, index + 1)
        )
        if not candidate.exists():
            return candidate
    raise BackupError("backup_target_exists", stem)


#: The names SQLite itself creates, truncates and removes beside a database.
#: Authorising the database file is not authorising these: they are the rest of
#: the *file family*, and a backup that removes one of them has removed somebody
#: else's file.  Measured on this host: SQLite unlinks a pre-existing -journal
#: and a pre-existing -wal while opening and closing the database, without any
#: Python code asking for it.
_FAMILY_SUFFIXES = ("-journal", "-wal", "-shm")

#: Prefix of the private directory one backup is built in before it is published.
_STAGING_PREFIX = ".staging-"

#: How many times a unique staging name is tried before giving up.
_STAGING_ATTEMPTS = 8


def _family_members(target: Path) -> dict[str, dict[str, object]]:
    """What sits at the three sidecar names beside one database, if anything.

    Size, time and digest travel with the fact so a refusal can prove the file
    was left alone byte for byte instead of merely claiming it.
    """
    found: dict[str, dict[str, object]] = {}
    for suffix in _FAMILY_SUFFIXES:
        candidate = Path(str(target) + suffix)
        try:
            info = os.lstat(candidate)
        except FileNotFoundError:
            continue
        except OSError as error:
            found[suffix] = {
                "name": candidate.name,
                "state": "unreadable",
                "reason": type(error).__name__,
            }
            continue
        entry: dict[str, object] = {
            "name": candidate.name,
            "state": "present",
            "bytes": int(info.st_size),
            "mtime_ns": int(info.st_mtime_ns),
            "sha256": None,
        }
        try:
            entry["sha256"] = _sha256_file(candidate)
        except OSError:
            # A file that cannot be read is still a file that must not be
            # removed; the hash is unknown, the name and size are not.
            entry["sha256"] = None
        found[suffix] = entry
    return found


def _require_empty_family(target: Path, *, stage: str) -> dict[str, object]:
    """Refuse to let SQLite touch the family while a stranger owns a member.

    This runs *before* the connection is opened, because an open is enough:
    SQLite deletes a pre-existing -journal/-wal on its own.  It is a check in a
    directory this run created and restricted, not a substitute for the
    exclusive creation of every name the run itself writes -- both are needed,
    and the report says which one was in force.
    """
    members = _family_members(target)
    if members:
        detail = ", ".join(
            "%s(state=%s,bytes=%s,sha256=%s)"
            % (
                item["name"],
                item.get("state"),
                item.get("bytes", "?"),
                str(item.get("sha256"))[:12],
            )
            for _suffix, item in sorted(members.items())
        )
        raise BackupError(
            "backup_target_family_occupied",
            "%s: %s (kept; nothing was opened; stage=%s)"
            % (target, detail, stage),
        )
    return {
        "precheck": "pass",
        "checked": [Path(str(target) + suffix).name for suffix in _FAMILY_SUFFIXES],
        "present": [],
    }


def _staging_directory(destination_root: Path) -> Path:
    """A private, empty directory this run owns; the artifact is built inside."""
    for _attempt in range(_STAGING_ATTEMPTS):
        candidate = destination_root / (_STAGING_PREFIX + uuid.uuid4().hex[:12])
        try:
            candidate.mkdir(parents=False, exist_ok=False)
        except FileExistsError:
            continue
        except OSError as error:
            raise BackupError("backup_staging_failed", str(error)) from None
        return candidate
    raise BackupError("backup_staging_failed", "no unique staging name available")


def _publish(shipping: Path, target: Path, workspace: TemporaryWorkspace) -> None:
    """Give the finished directory its final name with one directory rename.

    Two things are refused here, both before the rename: a name inside the
    staging directory that this run did not create, and a final name that
    already exists.  The rename itself is the only moment the final name comes
    into being, so a reader can never see a half-written backup there, and no
    file at the final name is ever opened by SQLite.
    """
    try:
        present = sorted(entry.name for entry in os.scandir(shipping))
    except OSError as error:
        raise BackupError(
            "backup_publish_failed", "staging unreadable: %s" % error
        ) from None
    foreign = [name for name in present if name not in set(workspace.members)]
    if foreign:
        raise BackupError(
            "backup_target_family_foreign",
            "%s: %s (kept; nothing published)" % (shipping, ",".join(foreign)),
        )
    if os.path.lexists(target):
        raise BackupError("backup_target_exists", str(target))
    try:
        os.rename(shipping, target)
    except OSError as error:
        raise BackupError("backup_publish_failed", "%s: %s" % (target, error)) from None


def _source_counts(path: Path) -> dict[str, int] | None:
    try:
        with contextlib.closing(_connect_readonly(path)) as connection:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            counts: dict[str, int] = {}
            for name in _DECLARED_TABLES:
                if name in tables:
                    counts[name] = int(
                        connection.execute(
                            'SELECT COUNT(*) FROM "%s"' % name
                        ).fetchone()[0]
                    )
            return counts
    except (sqlite3.Error, OSError):
        return None


def _write_manifest(directory: Path, manifest: dict[str, object]) -> Path:
    data = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary = directory / (MANIFEST_NAME + ".tmp")
    final = directory / MANIFEST_NAME
    with temporary.open("wb") as stream:
        stream.write(data.encode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, final)
    return final


def _own_manifest(workspace: TemporaryWorkspace, directory: Path) -> None:
    """Record the manifest this run just wrote as one of its own members.

    _write_manifest leaves a fresh object behind (it writes a temporary name and
    replaces), so there is no claim to inherit.  Recording it is still not a
    guess: the bytes are read before and after the record, the name has to keep
    pointing at the object that was recorded, and any difference drops the claim
    again instead of leaving the cleanup a file it cannot prove it made.
    """
    path = directory / MANIFEST_NAME
    try:
        before = _sha256_file(path)
    except OSError as error:
        raise BackupError(
            "backup_manifest_failed", "%s: %s" % (path, type(error).__name__)
        ) from None
    if not workspace.record(MANIFEST_NAME):
        raise BackupError("backup_target_replaced", "%s (identity_changed)" % path)
    try:
        after = _sha256_file(path)
    except OSError:
        after = None
    if after != before or workspace.verify_member(MANIFEST_NAME)["state"] != OWNED:
        # The name no longer describes the manifest that was just written, so
        # the claim is dropped and the file is kept: a cleanup that removes it
        # would be removing something it cannot show it created.
        workspace.forget(MANIFEST_NAME)
        raise BackupError("backup_target_replaced", "%s (changed while recording)" % path)


def _restore_steps(
    source: Path, name: str, mode: str, files: list[dict[str, object]]
) -> list[str]:
    """Restore instructions that always keep a way back.

    The previous text told the operator to overwrite the database and then
    delete the -wal/-shm beside it.  That destroys the only copy of whatever
    the old WAL still held before anyone proved the replacement opens, and it
    cannot be undone.  This protocol moves the current group aside first, then
    puts a verified group in its place, so every step is reversible (WR-01).
    """
    group = ", ".join(str(item["name"]) for item in files)
    if mode == "file_group":
        prepared = "整套文件（%s）" % group
        placement = (
            "file_group 备份必须整套一起放回；禁止只放主文件，也禁止用新主库配旧 WAL。"
        )
    else:
        prepared = "自包含的 %s（online 快照）" % name
        placement = "online 快照是自包含单文件，放回后旁边不得再留 -wal/-shm。"
    return [
        "1. 关闭 JpgPng 主程序与所有辅助进程，确认没有进程或连接还持有 %s；"
        "maintain.py inspect 报 library_locked 就说明还没关干净，不要继续。" % name,
        "2. 先校验备份本身：python maintain.py verify <备份目录>，必须 ok=true；"
        "记下清单里的模式（%s）与成员（%s）。" % (mode, group),
        "3. 在暂存目录准备与备份模式匹配的恢复组：%s；对暂存副本运行 inspect 与 compare，"
        "确认可用后才进入下一步。" % prepared,
        "4. 把当前库文件组（%s 以及同目录的 -wal/-shm）整组移入同卷独立保留目录"
        "（例如 record_backups/restore_rollback_<时间>）：移动，不删除、不覆盖。" % name,
        "5. 只把第 3 步校验过的恢复组放进原位置：%s" % placement,
        "6. 重新打开并对照：maintain.py inspect %s 须 compatible；"
        "maintain.py compare <恢复后库> <备份里的库> 须内容一致；"
        "restore_check 须无 orphan_wal。" % source,
        "7. 任一步失败：把第 4 步保留的整组移回原位置，保留保留目录与报告，不删除现场。",
    ]


def _journal_header(path: Path) -> tuple[int, int] | None:
    """The two journal-format bytes of a database header (offsets 18 and 19).

    1 is the legacy rollback journal, 2 is write-ahead logging.  This is a
    fact about the file itself; PRAGMA journal_mode reports "wal" as soon as a
    -wal sits beside a database SQLite can open with one, which is exactly the
    mixed group the restore check has to refuse.
    """
    try:
        with path.open("rb") as stream:
            header = stream.read(20)
    except OSError:
        return None
    if len(header) < 20 or header[:15] != b"SQLite format 3":
        return None
    return (header[18], header[19])


def restore_check(path: str | Path) -> dict[str, object]:
    """Is this file group one consistent state that may be opened?

    Four questions are answered separately, because they fail differently:

    content_healthy   the database's own integrity checks pass;
    openable          this build would open it (status == compatible);
    group_consistent  the files beside it belong to the same state: a
                      rollback-journal database with a non-empty -wal is a mixed
                      group, and SQLite would silently ignore that log;
    observed          all of the above could actually be read.

    The journal header (bytes 18 and 19) says which mode the file itself is in.
    It cannot prove where a stray -wal came from, so a group with an unexplained
    log is refused rather than guessed at.  The WAL is examined before the
    database, because an unreadable log is a specific fact (wal_unreadable) and
    not the same thing as a database nobody could open.
    """
    target = _resolve_target(path)
    wal_path = Path(str(target) + "-wal")
    wal_bytes: int | None = 0
    wal_readable = True
    try:
        wal_bytes = int(wal_path.stat().st_size) if wal_path.is_file() else 0
    except OSError:
        wal_bytes = None
        wal_readable = False
    if not wal_readable:
        # The log beside the database cannot be read, so the group cannot be
        # judged at all.  This is a specific fact, not 'the database is bad'.
        return {
            "path": str(target),
            "state": "wal_unreadable",
            "status": "unknown",
            "journal_mode": None,
            "wal_mode": None,
            "header_journal": None,
            "wal_bytes": None,
            "reasons": ["wal_unreadable"],
            "content_healthy": None,
            "openable": None,
            "group_consistent": False,
            "exit_code": 2,
        }
    try:
        report = inspect_library(target)
    except Exception as error:  # noqa: BLE001 - observation failure is a state
        return {
            "path": str(target),
            "state": "unobservable",
            "status": "unobservable",
            "journal_mode": None,
            "wal_mode": None,
            "header_journal": None,
            "wal_bytes": wal_bytes,
            "reasons": ["inspect_failed"],
            "content_healthy": None,
            "openable": None,
            "group_consistent": None,
            "exit_code": 3,
            "detail": "%s: %s" % (type(error).__name__, error),
        }
    header = _journal_header(target)
    wal_mode = header == (2, 2)
    status = str(report["status"])
    reasons: list[str] = []
    # A -wal that could not be read returned above; reaching this point means
    # the log was measurable, so what is left is a question about the database.
    if status == "missing":
        state = "library_missing"
        reasons.append("library_missing")
    elif status in {"unreadable", "empty", "incompatible", "not_a_sqlite_file"}:
        state = "not_usable"
        reasons.append("status_" + status)
    elif not wal_mode and wal_bytes:
        state = "orphan_wal"
        reasons.append("orphan_wal")
    else:
        state = "ok"
    exit_code = 0 if state == "ok" else 2
    return {
        "path": str(target),
        "state": state,
        "status": status,
        "journal_mode": report["journal_mode"],
        "wal_mode": wal_mode,
        "header_journal": list(header) if header is not None else None,
        "wal_bytes": wal_bytes,
        "reasons": reasons,
        "content_healthy": bool(report.get("healthy")),
        "openable": status == "compatible",
        "group_consistent": state == "ok",
        "exit_code": exit_code,
    }


def _stop_requested(stop: object) -> bool:
    """Did the caller ask this run to stop?

    An Event, a callable and a plain truth value are all accepted, because the
    maintenance CLI, the tests and a future UI thread each have their own
    natural shape; none of them may be silently ignored.
    """
    if stop is None:
        return False
    checker = getattr(stop, "is_set", None)
    if callable(checker):
        return bool(checker())
    if callable(stop):
        return bool(stop())
    return bool(stop)


def _stop_check(stop: object, stage: str) -> None:
    if _stop_requested(stop):
        raise BackupError("backup_cancelled", "stopped before %s" % (stage,))


def _progress_guard(stop: object):
    """A backup progress callback that aborts the copy when asked to stop."""
    if stop is None:
        return None

    def progress(status, remaining, total):  # noqa: ANN001 - SQLite's shape
        if _stop_requested(stop):
            raise BackupError("backup_cancelled", "stopped during the online snapshot")
        return None

    return progress


#: Workspace refusals that mean "the name no longer describes our member".
_TARGET_REFUSALS = frozenset({"claim_not_owned", "claim_name_missing"})
#: A volume that reports no identity: nothing is written, and it is not the
#: same fact as a replacement.
_TARGET_UNPROVABLE = "claim_unprovable"
#: Workspace refusals that mean "the directory is not the adopted one".
_WORKSPACE_REFUSALS = frozenset({"parent_replaced", "parent_unreadable"})


def _online_snapshot(
    source: Path,
    target: Path,
    workspace: TemporaryWorkspace,
    *,
    stop: object = None,
    family: dict[str, object] | None = None,
) -> list[dict[str, object]]:
    """Consistent snapshot through the SQLite online backup API.

    The source connection is read-only and holds no transaction, so the API
    reads the last committed state while other connections keep writing: a
    writer's uncommitted rows are not part of the snapshot, and a source that
    is mid-commit still yields one consistent state.  The source is never
    checkpointed, journalled or written.

    The target is claimed exclusively, then *leased*: the descriptor is opened
    and compared with the claimed identity, and it stays open until SQLite has
    finished writing.  Measured on this host, that descriptor makes the name
    unreplaceable (unlink, os.replace and unlink+hardlink all fail) while SQLite
    can still open, journal and write the file.  A replacement attempted before
    the lease is caught by the fstat comparison and nothing is written; one
    attempted afterwards is refused by the system.  Verifying after the write,
    as the previous version did, could only report the damage.

    The lease covers the database *file*; SQLite also owns three names beside it
    and removes two of them by itself while opening and closing (measured on
    this host: a pre-existing -journal and a pre-existing -wal are unlinked by
    SQLite alone).  Those names are checked here, after the claim and before the
    connection, so a member this run did not create is never opened, journalled
    over or unlinked.  The check is meaningful only against a directory this run
    created and restricted; the residual window between the check and the
    connection is stated in docs/optimization/2026-11-17/S0_CONTRACTS_R3.md.
    """
    claim = workspace.claim(target.name, purpose="online_snapshot")
    if family is not None:
        family.update(_require_empty_family(target, stage="before_connect"))
    pin = ParentPin(state="unknown", identity="", error="not_observed")
    try:
        with workspace.parent_pin() as pin:
            with claim.lease() as lease:
                _stop_check(stop, "online_backup")
                destination = sqlite3.connect(
                    str(target), timeout=_READ_TIMEOUT_SECONDS, isolation_level=None
                )
                try:
                    with contextlib.closing(
                        _connect_readonly(source, snapshot=False)
                    ) as origin:
                        origin.backup(
                            destination, pages=256, progress=_progress_guard(stop)
                        )
                    # Pages are copied verbatim, so the snapshot inherits the
                    # source's journal header.  A restored backup must never
                    # depend on a -wal that belongs to a different file, so the
                    # snapshot is normalised into one self-contained
                    # rollback-journal database.
                    destination.execute("PRAGMA journal_mode=DELETE")
                finally:
                    destination.close()
                if lease.verify()["state"] != OWNED:
                    # Unreachable while the descriptor is held on a working
                    # filesystem; kept so a platform where the share mode does
                    # not hold is refused instead of trusted.
                    raise BackupError("snapshot_target_replaced", str(target))
                # The snapshot is complete now: record its final size and time so
                # a later cleanup can tell an unfinished copy from this one.  A
                # name that changed identity under the lease is not ours, and the
                # record refuses to adopt it -- that refusal is raised here
                # instead of being left as a note in a successful result.
                if not workspace.record(target.name):
                    raise BackupError(
                        "snapshot_target_replaced", "%s (identity_changed)" % target
                    )
    except WorkspaceError as error:
        code = str(error).split(":", 1)[0]
        if code in _TARGET_REFUSALS:
            raise BackupError("snapshot_target_replaced", str(error)) from None
        if code == _TARGET_UNPROVABLE:
            raise BackupError("backup_target_unprovable", str(error)) from None
        if code in _WORKSPACE_REFUSALS:
            raise BackupError("backup_workspace_replaced", str(error)) from None
        raise
    leftovers = [
        suffix
        for suffix in ("-wal", "-shm")
        if Path(str(target) + suffix).exists()
        and Path(str(target) + suffix).stat().st_size > 0
    ]
    if leftovers:
        # Whatever wrote them, they were not proven to be this run's members, so
        # they are kept and named instead of adopted or deleted.
        raise BackupError("snapshot_not_self_contained", ",".join(leftovers))
    fact = _file_fact(target)
    fact["parent_pin"] = pin.state
    fact["parent_identity"] = pin.identity
    if pin.error:
        fact["parent_pin_error"] = pin.error
    return [fact]


def _file_group_copy(
    source: Path,
    target: Path,
    group: dict[str, dict[str, object]],
    workspace: TemporaryWorkspace,
    *,
    stop: object = None,
) -> list[dict[str, object]]:
    """Byte-for-byte copy of the database file set; needs a quiet source.

    Every destination name is created exclusively (O_EXCL) by workspace.claim,
    so a name somebody else already owns is refused by the create itself.  The
    three family names this function never writes are checked by the caller
    before the copy starts: a -journal left in the staging directory belongs to
    nobody here, and nothing in this run may remove it.
    """
    _stop_check(stop, "file_group_copy")
    for suffix in ("", "-wal"):
        entry = group[suffix]
        if entry["present"] and int(entry["bytes"]) and entry["sha256"] is None:
            raise BackupError(
                "source_busy",
                "the source %s cannot be read whole; close the application first"
                % entry["name"],
            )
    files: list[dict[str, object]] = []
    for suffix in _BACKUP_SIBLINGS:
        entry = group[suffix]
        if not entry["present"]:
            continue
        _stop_check(stop, "file_group_copy:" + (suffix or "main"))
        source_path = Path(str(source) + suffix)
        if not source_path.is_file():
            # Same three cases the copy used to discover by opening: a -shm
            # disappears first, an empty -wal may vanish, anything else means
            # the source moved and no copy may vouch for it.
            if suffix == "-shm":
                continue
            if suffix == "-wal" and int(entry["bytes"]) == 0:
                continue
            raise FileNotFoundError(str(source_path))
        destination = workspace.claim(source.name + suffix, purpose="backup_copy")
        try:
            fact = _copy_file(source_path, destination)
        except FileNotFoundError:
            # The source vanished between the check and the open: the empty
            # member we just claimed was never written, so it goes away again.
            workspace.release(source.name + suffix)
            if suffix == "-shm":
                continue
            if suffix == "-wal" and int(entry["bytes"]) == 0:
                continue
            # A log that held frames cannot vanish without the source having
            # moved; no copy may vouch for that.
            raise
        files.append(fact)
        # Recording again refreshes size and time of the same object.  If the
        # name no longer describes it, the copy is not this run's and claiming
        # it back would be exactly the laundering the audit found.
        if not workspace.record(fact["name"]):
            raise BackupError("backup_target_replaced", str(fact["name"]))
    return files


def backup_library(
    path: str | Path,
    destination_dir: str | Path,
    *,
    label: str | None = None,
    mode: str = "auto",
    stop: object = None,
) -> dict[str, object]:
    """Copy the library into a fresh, verified directory.

    Two modes exist because they answer different questions:

    'online_backup' (the default through 'auto') asks SQLite for a consistent
    snapshot.  It is the only safe choice while the application is running:
    a writer's uncommitted rows stay out, and no byte of the source is
    touched.  The result is one self-contained file.

    'file_group' copies main, -wal and -shm byte for byte.  It preserves the
    source exactly, including a pending WAL, and therefore requires a quiet
    source: the caller must close the application.  'auto' picks it only when
    the source is not healthy enough for the online API.

    The manifest is written last, so its presence proves the copy verified.
    From the moment the staging directory exists to the moment the manifest is
    in place, one failure boundary covers every stage.  A failure keeps the
    primary error and attaches the cleanup result to it; the cleanup only
    removes members this run recorded and still owns, so a failed backup never
    looks like a successful one and never deletes somebody else's file.

    Where the files live while they are written is part of the guarantee, not
    an implementation detail.  Every run builds its artifact inside a private
    staging directory (".staging-<id>") whose ACL is restricted to this account
    and SYSTEM when the filesystem allows it, and the finished directory gets
    its final name through one atomic rename.  SQLite therefore never opens a
    database whose family names are the ones a reader will see, and a name that
    already sits beside the final name - a foreign -journal, -wal or -shm - is
    never opened, journalled over or removed by this run.  The rename refuses to
    publish when anything in the staging directory is not this run's, and it
    refuses to replace an existing final name.
    """
    if mode not in {"auto", "online_backup", "file_group"}:
        raise BackupError("invalid_mode", mode)
    source = _resolve_target(path)
    destination_root = _resolve_destination(destination_dir, source)
    siblings = _sibling_stats(source)
    # The family as this run finds it, recorded before anything opens the
    # database.  Opening a WAL database read-only makes SQLite create its own
    # bookkeeping beside it when none exists yet (measured on this host: an empty
    # -wal and a 32 KiB -shm).  No library byte moves, but the directory listing
    # does, and a maintenance command must not leave that for an operator to
    # discover: the difference is reported by name.
    family_before = {
        suffix: Path(str(source) + suffix).exists() for suffix in _FAMILY_SUFFIXES
    }
    if siblings[""] is None:
        raise BackupError("library_missing", str(source))
    if int(siblings[""]["bytes"]) == 0:
        raise BackupError("library_empty", str(source))
    source_report = inspect_library(source)
    if source_report["status"] == "unreadable":
        # A database this module cannot read cannot be verified either, so it
        # refuses instead of producing a manifest nobody can check.  A plain
        # file copy is the honest tool for that case.
        raise BackupError(
            "source_unreadable",
            "copy %s with the file manager; this tool cannot verify it" % source,
        )
    effective = mode
    if mode == "auto":
        effective = "online_backup" if source_report["healthy"] else "file_group"
    try:
        destination_root.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise BackupError("destination_unavailable", str(error)) from None
    target = _backup_name(label, destination_root)
    before = _group(source)
    before_counts = _source_counts(source)
    # The artifact is built in a private directory of this run and only gets its
    # final name through one directory rename.  That is what keeps SQLite away
    # from the names a reader will later see: the database is opened at a
    # staging name, never at <target>/<library name>, so no file that already
    # sits beside the final name can be opened, journalled over or removed.
    staging = _staging_directory(destination_root)
    try:
        workspace = TemporaryWorkspace.adopt(staging, purpose="backup")
    except WorkspaceError as error:
        try:
            os.rmdir(staging)
        except OSError:
            pass
        raise BackupError("backup_directory_failed", str(error)) from None
    family: dict[str, object] = {
        "root": str(staging),
        "publish": "atomic_directory_rename",
        "published": True,
        "foreign": [],
    }
    workspace.restrict()
    family["acl"] = workspace.acl
    stage = "copy"
    try:
        _stop_check(stop, "copy")
        copy_path = staging / source.name
        if effective == "online_backup":
            files = _online_snapshot(
                source, copy_path, workspace, stop=stop, family=family
            )
        else:
            family.update(
                _require_empty_family(
                    staging / source.name, stage="before_group_copy"
                )
            )
            files = _file_group_copy(source, staging, before, workspace, stop=stop)
        stage = "verify"
        _stop_check(stop, "verify")

        after = _group(source)
        source_sidecars_created = [
            suffix
            for suffix in _FAMILY_SUFFIXES
            if not family_before[suffix] and Path(str(source) + suffix).exists()
        ]
        after_counts = _source_counts(source)
        copy_report = inspect_library(copy_path)
        sidecar_report = _inspect_sidecars(copy_path)
        if sidecar_report["errors"]:
            raise BackupError(
                "backup_sidecar_unavailable", "; ".join(sidecar_report["errors"])
            )
        # Compare the two sides with each other, not each side against the shared
        # declaration: two databases can deviate from it in different ways and
        # still look identical to a name-set comparison (WC-01).
        source_definitions = _definitions_or_none(source, source_report["status"])
        copy_definitions = _definitions_or_none(copy_path, copy_report["status"])
        objects_match = (
            source_definitions is not None and source_definitions == copy_definitions
        )
        counts_match = (
            before_counts is not None and copy_report["counts"] == before_counts
        )
        main_unchanged = (
            before[""]["bytes"] == after[""]["bytes"]
            and before[""]["mtime_ns"] == after[""]["mtime_ns"]
            and before[""]["sha256"] == after[""]["sha256"]
        )
        group_unchanged = _comparable(before) == _comparable(after)
        warnings: list[str] = []
        problems: list[str] = []
        checks_match = (
            copy_report["quick_check"] == source_report["quick_check"]
            and copy_report["integrity_check"] == source_report["integrity_check"]
            and copy_report["foreign_key_violations"]
            == source_report["foreign_key_violations"]
        )
        if copy_report["status"] in {"missing", "empty", "unreadable"}:
            problems.append("copy_unreadable")
        if not objects_match:
            problems.append("copy_objects_mismatch")
        if not checks_match:
            # A faithful copy reproduces the source's integrity facts exactly.  A
            # damaged source therefore still gets a usable backup; what is refused
            # is a copy whose checks disagree with the source it came from.
            problems.append("copy_integrity_differs")
        if not main_unchanged:
            # Only a concurrent checkpoint by another connection can move the main
            # file while we read it; the online API has no way to write the source.
            if effective == "file_group":
                problems.append("library_changed_during_copy")
            else:
                warnings.append("source_changed_during_backup")
        if not group_unchanged and effective == "file_group":
            problems.append("library_changed_during_copy")
        if not counts_match:
            if effective == "file_group":
                problems.append("counts_mismatch")
            else:
                warnings.append("counts_advanced_during_backup")
        for item in files:
            # -shm is SQLite bookkeeping rebuilt on the next open; every other
            # member carries content, so it is sealed byte for byte.
            item["sealed"] = not str(item["name"]).endswith("-shm")
        try:
            fingerprint = logical_fingerprint(copy_path)
        except LibraryMaintenanceError as error:
            fingerprint = None
            problems.append("copy_logical_unreadable")
            warnings.append(getattr(error, "code", "library_unreadable"))
        if problems:
            raise BackupError("backup_verification_failed", ",".join(sorted(set(problems))))
        manifest: dict[str, object] = {
            "manifest_version": MANIFEST_VERSION,
            "status": "ok",
            "mode": effective,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(
                timespec="seconds"
            ),
            "label": label,
            "source": str(source),
            "library_name": source.name,
            "source_bytes": int(siblings[""]["bytes"]),
            "source_wal_bytes": 0
            if siblings["-wal"] is None
            else int(siblings["-wal"]["bytes"]),
            "source_shm_bytes": 0
            if siblings["-shm"] is None
            else int(siblings["-shm"]["bytes"]),
            # Named, not hidden: these are files that did not exist before this
            # run opened the source and do exist after it.  They carry no library
            # content (an empty -wal and SQLite's shared-memory index), and this
            # run does not delete them - deleting a name beside the user's library
            # is exactly what a backup must not do on its own initiative.
            "source_sidecars_created": source_sidecars_created,
            "source_mtime_ns": int(siblings[""]["mtime_ns"]),
            "journal_mode_source": source_report["journal_mode"],
            "journal_mode_copy": copy_report["journal_mode"],
            "files": files,
            "members": [
                {
                    "name": str(item["name"]),
                    "bytes": int(item["bytes"]),
                    "sha256": item["sha256"],
                }
                for item in files
            ],
            "sealed_members": [
                str(item["name"]) for item in files if item.get("sealed")
            ],
            "logical_fingerprint": fingerprint,
            "copy_name": source.name,
            "copy_bytes": sum(int(item["bytes"]) for item in files),
            "objects_declared": copy_report["objects_declared"],
            "objects_actual": copy_report["objects_actual"],
            "missing_objects": copy_report["missing_objects"],
            "extra_objects": copy_report["extra_objects"],
            "mismatched_objects": copy_report["mismatched_objects"],
            "counts": copy_report["counts"],
            "quick_check": copy_report["quick_check"],
            "integrity_check": copy_report["integrity_check"],
            "foreign_key_violations": copy_report["foreign_key_violations"],
            "healthy": bool(copy_report["healthy"]),
            "verified": True,
            "objects_match_source": bool(objects_match),
            "counts_match_source": bool(counts_match),
            "source_checks": {
                "main_unchanged": bool(main_unchanged),
                "group_unchanged": bool(group_unchanged),
                "main_sha256_before": before[""]["sha256"],
                "main_sha256_after": after[""]["sha256"],
                "wal_bytes_before": before["-wal"]["bytes"],
                "wal_bytes_after": after["-wal"]["bytes"],
                "shm_excluded": True,
            },
            "reasons": [],
            "warnings": sorted(set(warnings)),
            "finalizer": {
                "format": MANIFEST_NAME,
                "written_at": _now_utc(),
                "sealed_by": "backup_library",
            },
            "restore_steps": _restore_steps(source, source.name, effective, files),
        }
        stage = "manifest"
        _stop_check(stop, "manifest")
        # The final path is recorded in the manifest because the manifest only
        # becomes readable after the rename: a manifest inside a directory is a
        # manifest whose directory has already been published.
        manifest["manifest_path"] = str(target / MANIFEST_NAME)
        manifest["family"] = family
        _write_manifest(staging, manifest)
        _own_manifest(workspace, staging)
        stage = "publish"
        _stop_check(stop, "publish")
        _publish(staging, target, workspace)
        return manifest
    except BaseException as error:
        # The primary failure is saved first; the cleanup result is attached to
        # it and can never replace it.  A real interrupt keeps its own type.
        # No sidecar is adopted here: a file this run cannot prove it created
        # stays on disk and the cleanup result names it.
        cleanup = _safe_cleanup(workspace)
        summary = _describe_cleanup(cleanup)
        if not isinstance(error, Exception):
            raise
        if isinstance(error, LibraryMaintenanceError):
            error.detail = ("%s; %s" % (error.detail, summary)).strip("; ")
            error.cleanup = cleanup
            raise error
        code = "backup_manifest_failed" if stage == "manifest" else "backup_failed"
        failure = BackupError(code, "%s: %s; %s" % (type(error).__name__, error, summary))
        # The cleanup travels as its own field even when the failure was not one
        # of ours: the reason the run stopped and what it left behind are two
        # facts, and a caller may need to act on the second one.
        failure.cleanup = cleanup
        raise failure from None


def _now_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="microseconds")


# ---------------------------------------------------------------------------
# Logical content fingerprint (contract v5 section 1.3)
# ---------------------------------------------------------------------------

_TAG_NULL = b"\x00"
_TAG_INTEGER = b"\x01"
_TAG_REAL = b"\x02"
_TAG_TEXT = b"\x03"
_TAG_BLOB = b"\x04"


def _quote_identifier(name: object) -> str:
    return '"%s"' % str(name).replace('"', '""')


def _quote_literal(name: object) -> str:
    return "'%s'" % str(name).replace("'", "''")


def _length_prefix(payload: bytes) -> bytes:
    return len(payload).to_bytes(8, "big") + payload


def _encode_value(value: object) -> bytes:
    """Type-preserving encoding: NULL, '' and b'' must never collide."""
    if value is None:
        return _TAG_NULL + _length_prefix(b"")
    if isinstance(value, bool):
        # SQLite has no boolean storage class; a bool never comes back from a
        # cursor, so encode it as the integer it means instead of guessing.
        value = int(value)
    if isinstance(value, int):
        try:
            payload = int(value).to_bytes(8, "big", signed=True)
        except OverflowError:
            raise LibraryMaintenanceError("integer_out_of_range") from None
        return _TAG_INTEGER + _length_prefix(payload)
    if isinstance(value, float):
        return _TAG_REAL + _length_prefix(struct.pack(">d", float(value)))
    if isinstance(value, str):
        return _TAG_TEXT + _length_prefix(value.encode("utf-8"))
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _TAG_BLOB + _length_prefix(bytes(value))
    raise LibraryMaintenanceError("unsupported_value_type", type(value).__name__)


def _object_rows(connection: sqlite3.Connection) -> list[tuple[str, str, str, str | None]]:
    """Every non-internal schema object, in a deterministic order.

    The internal-name test is applied here rather than in SQL, so tables,
    views, triggers and indexes all use the same definition (PA-02).
    """
    return [
        (str(row[0]), str(row[1]), str(row[2]), None if row[3] is None else str(row[3]))
        for row in connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        )
        if not _is_internal_object(row[1])
    ]


def _objects_digest(connection: sqlite3.Connection) -> tuple[int, str]:
    """Fingerprint the schema definitions, triggers and views included."""
    digest = hashlib.sha256()
    rows = _object_rows(connection)
    for kind, name, table, sql in rows:
        for part in (kind, name, table, sql):
            digest.update(_encode_value(part))
    return len(rows), digest.hexdigest()


def _object_definitions(connection: sqlite3.Connection) -> dict[str, dict[str, str | None]]:
    """Name to full definition, so two sides are compared with each other."""
    return {
        name: {"type": kind, "table": table, "sql": sql}
        for kind, name, table, sql in _object_rows(connection)
    }


def _table_columns(connection: sqlite3.Connection, name: str) -> list[tuple[str, int]]:
    return [
        (str(row[1]), int(row[5]))
        for row in connection.execute(
            "PRAGMA table_info(%s)" % _quote_literal(name)
        )
    ]


def _has_rowid(connection: sqlite3.Connection, name: str) -> bool:
    try:
        connection.execute(
            "SELECT rowid FROM %s LIMIT 0" % _quote_identifier(name)
        ).fetchall()
    except sqlite3.Error:
        return False
    return True


def _table_order(connection: sqlite3.Connection, name: str) -> tuple[str, str]:
    """Deterministic row order for one table, named for the record."""
    if _has_rowid(connection, name):
        return "rowid", "ORDER BY rowid"
    columns = _table_columns(connection, name)
    primary = [column for column, position in columns if position > 0]
    if primary:
        return "primary_key", "ORDER BY " + ", ".join(
            _quote_identifier(column) for column in primary
        )
    return "all_columns", "ORDER BY " + ", ".join(
        _quote_identifier(column) for column, _ in columns
    )


def _table_digest(
    connection: sqlite3.Connection, name: str, order_sql: str
) -> tuple[int, str]:
    """Stream one table in bounded batches; never fetch the whole table."""
    cursor = connection.execute(
        "SELECT * FROM %s %s" % (_quote_identifier(name), order_sql)
    )
    digest = hashlib.sha256()
    rows = 0
    while True:
        batch = cursor.fetchmany(FINGERPRINT_CHUNK_ROWS)
        if not batch:
            break
        for record in batch:
            rows += 1
            for value in record:
                digest.update(_encode_value(value))
    return rows, digest.hexdigest()


def _fingerprint_from_connection(connection: sqlite3.Connection) -> dict[str, object]:
    objects_count, objects_digest = _objects_digest(connection)
    tables: dict[str, dict[str, object]] = {}
    ordered: list[tuple[str, int, str]] = []
    for kind, name, _table, _sql in _object_rows(connection):
        if kind != "table":
            continue
        order, order_sql = _table_order(connection, name)
        rows, digest = _table_digest(connection, name, order_sql)
        tables[name] = {"rows": rows, "digest": digest, "order": order}
        ordered.append((name, rows, digest))
    combined = hashlib.sha256()
    combined.update(_encode_value("fp%d" % FINGERPRINT_VERSION))
    for name, rows, digest in sorted(ordered):
        combined.update(_encode_value(name))
        combined.update(_encode_value(rows))
        combined.update(_encode_value(digest))
    combined.update(_encode_value(objects_digest))
    return {
        "version": FINGERPRINT_VERSION,
        "digest": combined.hexdigest(),
        "rows": {name: int(item["rows"]) for name, item in sorted(tables.items())},
        "objects_digest": objects_digest,
        "objects_count": objects_count,
        "computed_at": _now_utc(),
        "tables": tables,
    }


def logical_fingerprint(
    path: str | Path, *, connection: sqlite3.Connection | None = None
) -> dict[str, object]:
    """Full logical content of one database, readable by any SQLite build.

    The digest covers the object definitions and, for every table, the typed
    encoding of every value in a deterministic row order.  Two databases that
    hold the same content therefore produce the same digest whatever journal
    mode, page layout or file size they ended up with.
    """
    if connection is not None:
        return _fingerprint_from_connection(connection)
    target = _resolve_target(path)
    try:
        with contextlib.closing(_connect_readonly(target)) as opened:
            return _fingerprint_from_connection(opened)
    except sqlite3.Error as error:
        raise LibraryMaintenanceError(_sqlite_reason(error), str(error)) from None


def _definitions(path: Path) -> dict[str, dict[str, str | None]]:
    with contextlib.closing(_connect_readonly(path)) as opened:
        return _object_definitions(opened)


def _definitions_or_none(
    path: Path, status: object
) -> dict[str, dict[str, str | None]] | None:
    """Definitions of a readable database, or None instead of an exception."""
    if status not in {"compatible", "incompatible"}:
        return None
    try:
        return _definitions(path)
    except (sqlite3.Error, LibraryMaintenanceError, OSError):
        return None


# ---------------------------------------------------------------------------
# Manifest reading and sealing checks (contract v5 section 1.4)
# ---------------------------------------------------------------------------


def _load_manifest(directory: Path) -> dict[str, object]:
    path = directory / MANIFEST_NAME
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        raise BackupError("manifest_missing", str(path)) from None
    except OSError as error:
        raise BackupError("manifest_unreadable", str(error)) from None
    if size > MAX_MANIFEST_BYTES:
        raise BackupError("manifest_too_large", str(path))
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        raise BackupError("manifest_unreadable", str(path)) from None
    if not isinstance(manifest, dict):
        raise BackupError("manifest_unreadable", str(path))
    version = manifest.get("manifest_version")
    if type(version) is not int or version not in SUPPORTED_MANIFEST_VERSIONS:
        raise BackupError("manifest_version_unsupported", str(version))
    return manifest


def _safe_member(directory: Path, name: object) -> Path:
    """Resolve one member name inside the backup directory, or refuse it."""
    if not isinstance(name, str) or not name or len(name) > 200:
        raise BackupError("manifest_member_invalid", repr(name)[:80])
    if any(ord(character) < 32 or ord(character) == 127 for character in name):
        raise BackupError("manifest_member_invalid", repr(name)[:80])
    if name in {".", ".."} or "/" in name or "\\" in name:
        raise BackupError("manifest_member_invalid", name)
    if Path(name).is_absolute() or (len(name) > 1 and name[1] == ":"):
        raise BackupError("manifest_member_invalid", name)
    candidate = directory / name
    try:
        resolved = candidate.resolve()
    except OSError as error:
        raise BackupError("manifest_member_invalid", str(error)) from None
    if resolved != directory.resolve() and directory.resolve() not in resolved.parents:
        raise BackupError("manifest_member_outside", name)
    if candidate.is_symlink() or (
        hasattr(candidate, "is_junction") and candidate.is_junction()
    ):
        raise BackupError("manifest_member_linked", name)
    return candidate


_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_BACKUP_MODES = ("online_backup", "file_group")


def _recorded_bytes(item: dict[str, object], position: int) -> int:
    """The recorded size, or a typed refusal; a string is never a byte count."""
    value = item.get("bytes")
    if type(value) is not int or value < 0:
        raise BackupError(
            "manifest_unreadable",
            "member[%d].bytes is not a byte count: %r" % (position, value),
        )
    return int(value)


def _recorded_digest(item: dict[str, object], position: int) -> str | None:
    value = item.get("sha256")
    if value is None:
        return None
    if not isinstance(value, str) or _HEX64_RE.fullmatch(value) is None:
        raise BackupError(
            "manifest_unreadable",
            "member[%d].sha256 is not a sha256 digest: %r" % (position, value),
        )
    return value


def _member_entries(
    directory: Path, manifest: dict[str, object]
) -> list[tuple[dict[str, object], Path]]:
    """Validated (record, path) pairs; duplicate or colliding names refuse.

    Every field a later stage relies on is checked here, before any file is
    opened or copied, so a malformed manifest becomes one typed error instead
    of a ValueError from whichever stage reads the field first (WR-04).
    """
    raw = manifest.get("members")
    if not isinstance(raw, list):
        raw = manifest.get("files")
    if not isinstance(raw, list) or not raw:
        raise BackupError("manifest_without_members")
    seen: set[str] = set()
    entries: list[tuple[dict[str, object], Path]] = []
    for position, item in enumerate(raw):
        if not isinstance(item, dict):
            raise BackupError("manifest_member_invalid", repr(item)[:80])
        name = item.get("name")
        path = _safe_member(directory, name)
        key = str(name).casefold()
        if key in seen:
            raise BackupError("manifest_member_duplicate", str(name))
        seen.add(key)
        _recorded_bytes(item, position)
        _recorded_digest(item, position)
        entries.append((item, path))
    return entries


def _sealed_names(
    manifest: dict[str, object],
    entries: list[tuple[dict[str, object], Path]],
    library_name: str,
    version: int,
) -> list[str]:
    """The sealed member list, checked against the member list itself."""
    raw = manifest.get("sealed_members")
    if raw is None or not raw:
        return []
    if version < 2:
        raise BackupError("manifest_unreadable", "a v1 manifest cannot seal members")
    if not isinstance(raw, list):
        raise BackupError("manifest_unreadable", "sealed_members is not a list")
    known = {str(item.get("name")) for item, _ in entries}
    sealed: list[str] = []
    for name in raw:
        if not isinstance(name, str):
            raise BackupError(
                "manifest_unreadable", "sealed_members holds a non-string entry"
            )
        if name not in known:
            raise BackupError("manifest_member_missing", name)
        if name.endswith("-shm"):
            raise BackupError(
                "manifest_unreadable",
                "%s cannot be sealed: SQLite rebuilds the shared-memory file" % name,
            )
        if name in sealed:
            raise BackupError("manifest_unreadable", "sealed_members repeats %s" % name)
        sealed.append(name)
    if library_name not in sealed:
        # Sealing a marker file beside an unsealed database proves nothing
        # about the database, which is the only member that matters (WR-04).
        raise BackupError(
            "manifest_member_missing",
            "%s is not sealed; sealing other files cannot vouch for the database"
            % library_name,
        )
    return sealed


def _check_mode_members(
    manifest: dict[str, object],
    entries: list[tuple[dict[str, object], Path]],
    library_name: str,
    version: int,
) -> None:
    """The recorded mode decides which sidecars may appear in the members."""
    if version < 2:
        return
    mode = manifest.get("mode")
    if mode not in _BACKUP_MODES:
        raise BackupError(
            "manifest_unreadable",
            "mode is not one of %s: %r" % (", ".join(_BACKUP_MODES), mode),
        )
    if library_name.endswith(("-wal", "-shm")):
        raise BackupError(
            "manifest_unreadable",
            "copy_name %s is a SQLite sidecar, not the database" % library_name,
        )
    if mode == "online_backup":
        for item, _ in entries:
            name = str(item.get("name"))
            if name.endswith("-wal") and int(item["bytes"]):
                raise BackupError(
                    "manifest_unreadable",
                    "an online snapshot is self-contained; %s cannot hold frames"
                    % name,
                )


def _check_fingerprint(recorded: object, version: int) -> None:
    """Shape of the recorded fingerprint, before it is compared with anything."""
    if version < 2 or recorded is None:
        return
    if not isinstance(recorded, dict):
        raise BackupError("manifest_unreadable", "logical_fingerprint is not an object")
    value = recorded.get("version")
    if type(value) is not int or value != FINGERPRINT_VERSION:
        raise BackupError(
            "manifest_unreadable",
            "logical_fingerprint.version is not %d: %r" % (FINGERPRINT_VERSION, value),
        )
    digest = recorded.get("digest")
    if not isinstance(digest, str) or _HEX64_RE.fullmatch(digest) is None:
        raise BackupError(
            "manifest_unreadable", "logical_fingerprint.digest is not a sha256 digest"
        )
    tables = recorded.get("tables")
    if not isinstance(tables, dict):
        raise BackupError(
            "manifest_unreadable", "logical_fingerprint.tables is not an object"
        )
    for name, entry in tables.items():
        if not isinstance(entry, dict):
            raise BackupError(
                "manifest_unreadable",
                "logical_fingerprint.tables[%r] is not an object" % str(name),
            )
        entry_digest = entry.get("digest")
        if not isinstance(entry_digest, str) or _HEX64_RE.fullmatch(entry_digest) is None:
            raise BackupError(
                "manifest_unreadable",
                "logical_fingerprint.tables[%r] has no sha256 digest" % str(name),
            )


def _differing_tables(
    recorded: dict[str, object], actual: dict[str, object]
) -> list[str]:
    """Table names whose per-table digest differs from the sealed one."""
    recorded_tables = recorded.get("tables")
    actual_tables = actual.get("tables")
    names: set[str] = set(actual_tables or {})
    if isinstance(recorded_tables, dict):
        names |= set(recorded_tables)
    differing: list[str] = []
    for name in sorted(names):
        left = None
        right = None
        if isinstance(recorded_tables, dict):
            entry = recorded_tables.get(name)
            if isinstance(entry, dict):
                left = entry.get("digest")
        if isinstance(actual_tables, dict):
            entry = actual_tables.get(name)
            if isinstance(entry, dict):
                right = entry.get("digest")
        if left != right:
            differing.append(name)
    return differing


def _identity(info: os.stat_result) -> dict[str, int]:
    """The four facts that must not move while one member is read."""
    return {
        "device": int(getattr(info, "st_dev", 0)),
        "file_id": int(getattr(info, "st_ino", 0)),
        "bytes": int(info.st_size),
        "mtime_ns": int(info.st_mtime_ns),
    }


def _path_identity(path: Path) -> dict[str, object]:
    """Identity of the name that still points at the member, or why it is gone."""
    try:
        return _identity(os.stat(path))
    except FileNotFoundError:
        return {"present": False}
    except OSError as error:
        return {"present": None, "error": type(error).__name__}


def _copy_and_hash(source: Path, destination: object) -> dict[str, object]:
    """Copy one member byte for byte and hash exactly the bytes written.

    The identity of the open source is read before and after the copy, and the
    name is stat-ed once more afterwards.  A member rewritten while it is
    being read therefore reports identities that disagree instead of a digest
    assembled from two versions (PA-01).

    The destination is written through a descriptor that is proven against the
    claim before it is truncated.  Two facts are returned about it: whether the
    descriptor described the claimed object when the writer was opened, and
    whether the *name* still pointed at it afterwards -- a replaced name is
    reported, never silently adopted.
    """
    digest = hashlib.sha256()
    total = 0
    with source.open("rb") as reader:
        before = _identity(os.fstat(reader.fileno()))
        with _owned_writer(destination) as writer:  # type: ignore[union-attr]
            while True:
                chunk = reader.read(_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
                writer.write(chunk)
                total += len(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        after = _identity(os.fstat(reader.fileno()))
    return {
        "name": getattr(destination, "name", str(destination)),
        "bytes": total,
        "sha256": digest.hexdigest(),
        "before": before,
        "after": after,
        "path_now": _path_identity(source),
        "destination": getattr(destination, "verify", lambda: {"state": "unknown"})()
        if hasattr(destination, "verify") else {"state": "unknown", "reason": "no_claim"},
    }


def _readback_digest(path: Path) -> str:
    """Hash the bytes as they now sit on disk, not as they were streamed."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _verification_workspace(root: Path) -> tuple[Path, str]:
    """A private directory for the copy, preferring the backup's own volume.

    A backup on a read-only volume (an external disk mounted read-only, an
    ACL that allows only reading) still has to be verifiable, so the system
    temporary directory is the fallback.  The returned label says where the
    copy really was, because "which volume did this proof land on" is part of
    the evidence.  Neither location is ever inside the verified directory.
    """
    name = "%s.verify-%s" % (root.name, uuid.uuid4().hex)
    try:
        sibling = root.parent / name
        sibling.mkdir(parents=False, exist_ok=False)
    except OSError:
        try:
            return Path(tempfile.mkdtemp(prefix="pixivkeep-verify-")), "system_temp"
        except OSError as error:
            raise OSError(
                "no writable place for the verification copy: %s" % error
            ) from None
    return sibling, "sibling"


def _seal_members(
    entries: list[tuple[dict[str, object], Path]],
    copy_names: list[str],
    sealed_names: list[str],
    workspace: Path,
    ws: TemporaryWorkspace,
) -> tuple[list[dict[str, object]], str, dict[str, object]]:
    """Copy each member once, then judge exactly the bytes that were read.

    Every copied member gets a record; only the sealed ones carry a verdict.
    'changed' means the bytes or the file identity moved while the copy was
    taken, so no single version can be described and the check must fail.
    """
    by_name = {str(item.get("name")): (item, path) for item, path in entries}
    sealed = set(sealed_names)
    facts: list[dict[str, object]] = []
    states: list[str] = []
    before: dict[str, object] = {}
    after: dict[str, object] = {}
    copy_seconds = 0.0
    hash_seconds = 0.0
    copy_bytes = 0
    for name in copy_names:
        record, path = by_name[name]
        recorded_bytes = int(record["bytes"])
        recorded_digest = record.get("sha256")
        before[name] = _path_identity(path)
        fact: dict[str, object] = {
            "name": name,
            "state": "missing",
            "bytes_recorded": recorded_bytes,
            "bytes_actual": 0,
            "sha256_recorded": recorded_digest,
            "sha256_actual": None,
        }
        if not path.is_file():
            after[name] = _path_identity(path)
            facts.append(fact)
            states.append("missing")
            continue
        # perf_counter is the measurement clock: the Windows monotonic clock
        # has a ~15.6 ms tick, which quantises a small copy into 0.0 and makes
        # the evidence unreadable.
        started = time.perf_counter()
        destination = ws.claim(name, purpose="verify_copy")
        try:
            copied = _copy_and_hash(path, destination)
        except FileNotFoundError:
            # The source vanished between the check and the copy: the member
            # we claimed was never written, so it is released, not kept.
            ws.release(name)
            after[name] = _path_identity(path)
            facts.append(fact)
            states.append("missing")
            continue
        except OSError as error:
            # An unreadable member is an expected external condition, not a
            # crash: it enters the public error protocol under the existing
            # "this file cannot be read" code so the documented exit codes hold.
            raise LibraryMaintenanceError(
                "library_unavailable", "%s: %s" % (name, error)
            ) from None
        except WorkspaceError as error:
            # The object we claimed could not be proven ours, so not one byte
            # was written.  That is a typed failure, not a coverage verdict.
            raise LibraryMaintenanceError(
                "library_unavailable", "%s: %s" % (name, error)
            ) from None
        copy_seconds += time.perf_counter() - started
        copy_bytes += int(copied["bytes"])
        # Refreshing the record is legitimate only while the name still
        # describes the object this run created; record() refuses otherwise.
        replaced = str((copied.get("destination") or {}).get("state")) != "owned"
        if not ws.record(name):
            replaced = True
        fact["destination"] = copied.get("destination")
        after[name] = copied["path_now"]
        started = time.perf_counter()
        readback = _readback_digest(workspace / name)
        hash_seconds += time.perf_counter() - started
        fact["bytes_actual"] = int(copied["bytes"])
        fact["sha256_actual"] = copied["sha256"]
        if (
            copied["before"] != copied["after"]
            or copied["path_now"] != copied["before"]
            or readback != copied["sha256"]
            or replaced
        ):
            fact["state"] = "changed"
        elif name not in sealed:
            fact["state"] = "not_checked"
        elif int(copied["bytes"]) != recorded_bytes or copied["sha256"] != recorded_digest:
            fact["state"] = "mismatch"
        else:
            fact["state"] = "ok"
        states.append(str(fact["state"]))
        facts.append(fact)
    changed = any(state == "changed" for state in states)
    if not changed:
        # One more look after every member was copied: a writer that moved an
        # earlier member while a later one was being read is caught here.
        for index, name in enumerate(copy_names):
            current = _path_identity(by_name[name][1])
            after[name] = current
            if current != before[name]:
                changed = True
                if states[index] == "ok":
                    states[index] = "changed"
                    facts[index]["state"] = "changed"
    timings: dict[str, object] = {
        "before": before,
        "after": after,
        "copy_seconds": round(copy_seconds, 6),
        "hash_seconds": round(hash_seconds, 6),
        "copy_bytes": copy_bytes,
    }
    return facts, ("changed" if changed else "stable"), timings


def _sealed_verdict(
    facts: list[dict[str, object]], sealed_names: list[str]
) -> tuple[str, list[dict[str, object]]]:
    """The worst state among the sealed members, and those members."""
    sealed = set(sealed_names)
    members = [fact for fact in facts if fact["name"] in sealed]
    states = [str(fact["state"]) for fact in members]
    if "changed" in states:
        return "changed", members
    if "missing" in states:
        return "missing", members
    if "mismatch" in states:
        return "mismatch", members
    if states and all(state == "ok" for state in states):
        return "ok", members
    return "not_checked", members


def _recheck_members(
    entries: list[tuple[dict[str, object], Path]],
    copy_names: list[str],
    before: dict[str, object],
) -> tuple[bool, dict[str, object]]:
    """Compare each member's identity with the one recorded before the copy."""
    by_name = {str(item.get("name")): path for item, path in entries}
    moved = False
    after: dict[str, object] = {}
    for name in copy_names:
        current = _path_identity(by_name[name])
        after[name] = current
        if current != before.get(name):
            moved = True
    return moved, after


def _verify_exit_code(ok: bool, reasons: list[str]) -> int:
    """0 when the four facts passed, 3 when they could not be observed, else 2.

    A staging area that cannot be created is not a finding about the backup:
    nothing was compared, so the command reports an unreadable condition (3)
    instead of dressing it up as a content mismatch (2).

    The same holds for a staging area whose own contents cannot be described:
    if a sidecar's identity cannot be read, this run cannot say what it left
    behind, so it does not get to answer "the backup is fine" either.
    """
    blocking = {"verify_workspace_unavailable", "workspace_sidecar_unavailable"}
    if blocking & set(reasons):
        return 3
    return 0 if ok else 2


def verify_backup(
    directory: str | Path, *, source: str | Path | None = None
) -> dict[str, object]:
    """Does this backup still hold what its manifest sealed?

    Four facts are reported apart and none of them defaults to true:

    sealed      every sealed member still matches the manifest byte for byte
    logical     the copy still holds the sealed object definitions and the
                sealed value of every row, at the same row counts
    health      the copy passes this build's own integrity checks
    compatible  this build would open the copy

    All of them describe one isolated copy: the member group is copied once,
    hashed while it is read, and every later question is asked of that copy
    through a single read transaction.  Reading the backup three times instead
    let a file that changed between the reads satisfy each stage with a
    different version, which is the PA-01 defect.

    The guarantee has a named end.  A write that lands between the copy and
    the final stat of every member is reported as 'changed' (the members are
    re-checked after every read); a write after that last stat, including one
    after this function returns, is outside the check.  The report says so and
    names the copy it describes, so nobody can read it as a statement about a
    later moment.

    'ok' is true only when all four ran and passed on a copy that did not move
    while it was taken.  'source_equivalent' is a fifth, separate question
    (does the *current* source still hold the sealed content) and never turns
    a sound backup into a broken one.
    """
    root = Path(directory).expanduser()
    if not root.exists():
        raise BackupError("backup_directory_missing", str(root))
    if not root.is_dir():
        raise BackupError("backup_not_a_directory", str(root))
    root = root.resolve()
    # Measurement only; nothing here decides a timeout.
    started = time.perf_counter()
    manifest = _load_manifest(root)
    version = int(manifest["manifest_version"])
    reasons: list[str] = []
    warnings: list[str] = []
    entries = _member_entries(root, manifest)
    library_name = manifest.get("copy_name") or manifest.get("library_name")
    if not isinstance(library_name, str):
        raise BackupError("manifest_without_library_name")
    # Resolved with the same guard as every other member; a name that leaves
    # the directory is refused before anything is opened.
    _safe_member(root, library_name)
    known = {str(item.get("name")) for item, _ in entries}
    if library_name not in known:
        raise BackupError(
            "manifest_member_missing",
            "the database %s is not in the member list" % library_name,
        )
    sealed_names = _sealed_names(manifest, entries, library_name, version)
    _check_mode_members(manifest, entries, library_name, version)
    recorded = manifest.get("logical_fingerprint")
    _check_fingerprint(recorded, version)

    evidence: dict[str, object] = {
        "scope": "manifest_only",
        "snapshot_id": uuid.uuid4().hex,
        "workspace": "none",
        "path": None,
        "cleanup": "not_needed",
        "stability": "unknown",
        "copy_seconds": 0.0,
        "hash_seconds": 0.0,
        "read_seconds": 0.0,
        "copy_bytes": 0,
        "source_state_before": {},
        "source_state_after": {},
        "started_at": _now_utc(),
    }
    sealed_state = "not_checked"
    sealed_members: list[dict[str, object]] = []
    stability = "unknown"
    copy_report: dict[str, object] | None = None
    actual_fingerprint: dict[str, object] | None = None
    logical_error: str | None = None

    unexpected: list[str] = []
    try:
        for entry in sorted(root.iterdir(), key=lambda item: item.name):
            if entry.name in known:
                continue
            if entry.name in {MANIFEST_NAME, MANIFEST_NAME + ".tmp"}:
                continue
            unexpected.append(entry.name)
    except OSError:
        unexpected = []
    if unexpected:
        # A stranger in the directory is not a change to the sealed facts, so
        # it is reported and never deleted.
        warnings.append("unexpected_members")
    evidence["unexpected_members"] = unexpected

    workspace: Path | None = None
    ws: TemporaryWorkspace | None = None
    try:
        try:
            workspace, where = _verification_workspace(root)
            ws = TemporaryWorkspace.adopt(workspace, purpose="verify")
        except (OSError, WorkspaceError) as error:
            # The directory exists but cannot be described (or was never
            # created).  Either way it is not ours to remove: name it and stop
            # using it, so the finally below has nothing to clean.
            if workspace is not None:
                warnings.append("verify_workspace_unowned")
                evidence["left_behind"] = str(workspace)
                workspace = None
            reasons.append("verify_workspace_unavailable")
            warnings.append("verify_workspace_unavailable")
            evidence["workspace"] = "unavailable"
            evidence["detail"] = str(error)
        else:
            evidence["workspace"] = where
            evidence["path"] = str(workspace)
            copy_names = sealed_names or [library_name]
            facts, stability, timings = _seal_members(
                entries, copy_names, sealed_names, workspace,
                ws,
            )
            evidence["source_state_before"] = timings["before"]
            evidence["source_state_after"] = timings["after"]
            evidence["copy_seconds"] = timings["copy_seconds"]
            evidence["hash_seconds"] = timings["hash_seconds"]
            evidence["copy_bytes"] = timings["copy_bytes"]
            evidence["stability"] = stability
            evidence["scope"] = "isolated_copy"
            # What this run claimed, and the identity it claimed it under: the
            # record a reader needs to check that the cleanup judged the same
            # objects the copy created.
            evidence["claims"] = [
                claim.as_dict()
                for claim in (ws.claim_of(name) for name in copy_names)
                if claim is not None
            ]
            isolated = workspace / library_name
            if isolated.is_file():
                read_started = time.perf_counter()
                try:
                    with contextlib.closing(_connect_readonly(isolated)) as opened:
                        # One connection and one read transaction: the object
                        # and row digest and the integrity checks describe the
                        # same snapshot of the same copy.
                        if version >= 2 and isinstance(recorded, dict):
                            actual_fingerprint = _fingerprint_from_connection(opened)
                        copy_report = inspect_library(isolated, connection=opened)
                except sqlite3.Error as error:
                    logical_error = _sqlite_reason(error)
                    warnings.append("copy_unreadable")
                except LibraryMaintenanceError as error:
                    logical_error = str(getattr(error, "code", "library_unreadable"))
                    warnings.append("copy_logical_unreadable")
                evidence["read_seconds"] = round(time.perf_counter() - read_started, 6)
                # SQLite may have left -wal/-shm beside the copy.  They are
                # reported, never adopted: this run cannot prove it created
                # them, so it may not delete them either.
                sidecars = _inspect_sidecars(isolated)
                evidence["sidecars"] = sidecars
                if sidecars["errors"]:
                    reasons.append("workspace_sidecar_unavailable")
                    warnings.append("workspace_sidecar_unavailable")
            else:
                warnings.append("copy_missing")
            # The last look comes after every read, so a member rewritten at any
            # point of this check is reported as changed instead of described.
            moved, final_states = _recheck_members(
                entries, copy_names, timings["before"]
            )
            evidence["source_state_after"] = final_states
            if moved and stability != "changed":
                stability = "changed"
                for fact in facts:
                    if fact["state"] == "ok":
                        fact["state"] = "changed"
            evidence["stability"] = stability
            evidence["scope"] = "isolated_copy"
            evidence["member_states"] = {
                str(fact["name"]): str(fact["state"]) for fact in facts
            }
            sealed_state, sealed_members = _sealed_verdict(facts, sealed_names)
            if version >= 2 and not sealed_names:
                warnings.append("manifest_without_sealed_members")
            if sealed_state != "ok":
                reasons.append("sealed_" + sealed_state)
            if stability == "changed":
                reasons.append("changed_during_check")
    finally:
        if ws is not None:
            # The cleanup is a fact about this run's own directory, not about
            # the backup: it is reported next to the verdict and never turns a
            # sound backup into a broken one.  Nothing is recorded here: a
            # finally block must not adopt a name and must not raise.
            cleanup = _safe_cleanup(ws)
            evidence["cleanup"] = cleanup
            if cleanup.get("state") != "ok":
                warnings.append("verify_cleanup_incomplete")
                if cleanup.get("state") == "failed":
                    warnings.append("verify_cleanup_failed")
    if sealed_state == "not_checked":
        reasons.append("sealed_not_checked")

    logical_state = "not_checked"
    logical: dict[str, object] = {"state": "not_checked"}
    if version < 2:
        logical = {"state": "not_checked", "reason": "manifest_too_old"}
        reasons.append("manifest_too_old")
    elif not isinstance(recorded, dict):
        logical = {"state": "not_checked", "reason": "manifest_without_fingerprint"}
    elif copy_report is None:
        logical = {
            "state": "not_checked",
            "reason": logical_error or "copy_missing",
        }
    elif actual_fingerprint is None:
        logical = {
            "state": "not_checked",
            "reason": logical_error or "copy_logical_unreadable",
            "recorded_digest": recorded.get("digest"),
        }
    else:
        recorded_digest = recorded.get("digest")
        digest_ok = recorded_digest == actual_fingerprint["digest"]
        logical_state = "ok" if digest_ok else "mismatch"
        logical = {
            "state": logical_state,
            "recorded_digest": recorded_digest,
            "actual_digest": actual_fingerprint["digest"],
            "rows_recorded": recorded.get("rows"),
            "rows_actual": actual_fingerprint["rows"],
            "tables_differing": _differing_tables(recorded, actual_fingerprint),
            "checked_at": actual_fingerprint["computed_at"],
        }
        if logical_state == "mismatch":
            reasons.append("logical_mismatch")

    if copy_report is None:
        health_state = "not_checked"
        compatible_ok = False
        health: dict[str, object] = {
            "state": "not_checked",
            "reason": logical_error or "copy_unreadable",
        }
        compatible: dict[str, object] = {
            "state": "not_checked",
            "declared_ok": False,
            "reasons": [],
        }
    else:
        health_state = "ok" if copy_report["healthy"] else "failed"
        if health_state != "ok":
            reasons.append("copy_unhealthy")
        compatible_ok = copy_report["status"] == "compatible"
        if not compatible_ok:
            reasons.append("copy_not_compatible")
        health = {
            "state": health_state,
            "status": copy_report["status"],
            "quick_check": copy_report["quick_check"],
            "integrity_check": copy_report["integrity_check"],
            "foreign_key_violations": copy_report["foreign_key_violations"],
        }
        compatible = {
            "state": "ok" if compatible_ok else "mismatch",
            "status": copy_report["status"],
            "declared_ok": bool(compatible_ok),
            "reasons": copy_report["reasons"],
        }

    source_equivalent: dict[str, object] = {"state": "not_checked",
                                            "reason": "no_source_given"}
    if source is not None:
        source_path = _resolve_target(source)
        anchor = recorded.get("digest") if isinstance(recorded, dict) else None
        if not isinstance(anchor, str):
            source_equivalent = {
                "state": "not_checked",
                "reason": "manifest_without_fingerprint",
            }
        else:
            try:
                source_fingerprint = logical_fingerprint(source_path)
            except LibraryMaintenanceError as error:
                warnings.append("source_unreadable")
                source_equivalent = {
                    "state": "not_checked",
                    "reason": getattr(error, "code", "library_unreadable"),
                }
            else:
                same = source_fingerprint["digest"] == anchor
                source_equivalent = {
                    "state": "ok" if same else "mismatch",
                    "source": str(source_path),
                    "source_digest": source_fingerprint["digest"],
                    "sealed_digest": anchor,
                }
                if not same:
                    warnings.append("source_changed_since_backup")

    ok = (
        sealed_state == "ok"
        and logical_state == "ok"
        and health_state == "ok"
        and compatible_ok
        and stability == "stable"
    )
    evidence["seconds"] = round(time.perf_counter() - started, 6)
    evidence["checked_at"] = _now_utc()
    report = {
        "mode": "manifest",
        "question": "sealed_backup",
        "backup": str(root),
        "manifest_path": str(root / MANIFEST_NAME),
        "manifest_version": version,
        "mode_recorded": manifest.get("mode"),
        "created_at": manifest.get("created_at"),
        "label": manifest.get("label"),
        "library_name": library_name,
        "member_count": len(entries),
        "sealed": {"state": sealed_state, "members": sealed_members},
        "logical": logical,
        "health": health,
        "compatible": compatible,
        "source_equivalent": source_equivalent,
        "evidence": evidence,
        "ok": bool(ok),
        "exit_code": _verify_exit_code(bool(ok), reasons),
        # exit_code 3 means 'nothing was compared'.  The report keeps its facts
        # and also carries the error marker the other commands use, so a reader
        # does not mistake an unobserved backup for a disagreement about content.
        "reasons": sorted(set(reasons)),
        "warnings": sorted(set(warnings)),
        "checked_at": _now_utc(),
    }
    if report["exit_code"] == 3:
        # Only an unobserved backup carries the error marker; a clean run must
        # not look like an error to a reader that keys on the presence of status.
        report["status"] = "error"
    return report


def compare_library(
    source: str | Path, copy_path: str | Path
) -> dict[str, object]:
    """Do these two databases hold the same content right now?

    This is the operator's comparison, not the restore gate: call
    verify_backup(directory) when the question is whether a sealed backup can
    be trusted.  Object definitions are compared with each other (not each
    side against the declaration), so two databases that deviate in different
    ways are still reported as different.
    """
    source_path = _resolve_target(source)
    copy = _resolve_target(copy_path)
    source_report = inspect_library(source_path)
    copy_report = inspect_library(copy)
    reasons: list[str] = []
    warnings: list[str] = []
    readable = (
        source_report["status"] in {"compatible", "incompatible"}
        and copy_report["status"] in {"compatible", "incompatible"}
    )
    objects_identical = False
    logical_state = "not_checked"
    logical: dict[str, object] = {"state": "not_checked",
                                  "reason": "database_unreadable"}
    if readable:
        objects_identical = _definitions(source_path) == _definitions(copy)
        source_fingerprint = logical_fingerprint(source_path)
        copy_fingerprint = logical_fingerprint(copy)
        digest_ok = source_fingerprint["digest"] == copy_fingerprint["digest"]
        logical_state = "ok" if digest_ok else "mismatch"
        logical = {
            "state": logical_state,
            "source_digest": source_fingerprint["digest"],
            "copy_digest": copy_fingerprint["digest"],
            "rows": copy_fingerprint["rows"],
            "tables_differing": _differing_tables(source_fingerprint,
                                                   copy_fingerprint),
            "checked_at": _now_utc(),
        }
        if not objects_identical:
            reasons.append("copy_objects_mismatch")
        if not digest_ok:
            reasons.append("copy_content_mismatch")
    else:
        reasons.append("database_unreadable")

    counts_match = source_report["counts"] == copy_report["counts"]
    if not counts_match:
        reasons.append("counts_mismatch")
    checks_match = (
        copy_report["quick_check"] == source_report["quick_check"]
        and copy_report["integrity_check"] == source_report["integrity_check"]
        and copy_report["foreign_key_violations"]
        == source_report["foreign_key_violations"]
    )
    if not checks_match:
        reasons.append("copy_integrity_differs")
    declared_ok = (
        copy_report["format"] == _LIBRARY_FORMAT
        and copy_report["schema_version"] == _SCHEMA_VERSION
        and not copy_report["missing_required_tables"]
        and not copy_report["missing_objects"]
        and not copy_report["mismatched_objects"]
    )
    main_sha256_match: bool | None = None
    try:
        main_sha256_match = _sha256_file(source_path) == _sha256_file(copy)
    except OSError:
        warnings.append("copy_unreadable_bytes")
    healthy_ok = bool(copy_report["healthy"])
    if not healthy_ok:
        reasons.append("copy_unhealthy")
    compatible_ok = copy_report["status"] == "compatible"
    if not compatible_ok:
        reasons.append("copy_not_compatible")
    ok = bool(
        objects_identical
        and logical_state == "ok"
        and healthy_ok
        and compatible_ok
    )
    return {
        "mode": "compare",
        "question": "same_content_as_source",
        "source": str(source_path),
        "copy": str(copy),
        "source_status": source_report["status"],
        "copy_status": copy_report["status"],
        "sealed": {"state": "not_checked", "reason": "no_manifest"},
        "logical": logical,
        "health": {
            "state": "ok" if healthy_ok else "failed",
            "quick_check": copy_report["quick_check"],
            "integrity_check": copy_report["integrity_check"],
            "foreign_key_violations": copy_report["foreign_key_violations"],
        },
        "compatible": {
            "state": "ok" if compatible_ok else "mismatch",
            "declared_ok": bool(declared_ok),
        },
        "source_equivalent": {"state": "not_checked", "reason": "is_the_source"},
        "objects_match_source": bool(objects_identical),
        "logical_state": logical_state,
        "declared_ok": bool(declared_ok),
        "checks_match": bool(checks_match),
        "healthy": healthy_ok,
        "counts_match": bool(counts_match),
        "main_sha256_match": main_sha256_match,
        "counts": copy_report["counts"],
        "quick_check": copy_report["quick_check"],
        "integrity_check": copy_report["integrity_check"],
        "reasons": sorted(set(reasons)),
        "warnings": sorted(set(warnings)),
        "ok": ok,
        "checked_at": _now_utc(),
    }


def verify_library_copy(
    path: str | Path, copy_path: str | Path
) -> dict[str, object]:
    """Deprecated alias of compare_library; it is not a backup gate.

    It answers whether two databases hold the same content right now.  It
    cannot answer whether a backup is trustworthy, because a copy nobody
    sealed can still match a source that has since drifted.  Use
    verify_backup(directory) for the restore decision.
    """
    return compare_library(path, copy_path)


def switch_plan(path: str | Path) -> dict[str, object]:
    """Ordered, read-only switch instructions for one library file."""
    report = inspect_library(path)
    status = str(report["status"])
    library = str(report["path"])
    steps: list[str] = []
    blockers: list[str] = []
    if status in {"missing", "empty"}:
        steps = [
            "库还不存在（或为 0 字节）：首次启动会初始化，不需要备份。",
            "如果这里原本有记录，先确认路径是否正确，再继续。",
        ]
    elif status == "compatible":
        steps = [
            "关闭 JpgPng 主程序，确认没有下载/整理任务在运行。",
            "运行 python maintain.py backup %s <备份目录> --label before-w5 生成快照。"
            % library,
            "运行 python maintain.py verify <备份目录>，必须 ok=true：它按清单校验封存内容，"
            "而不是只比表计数。",
            "在副本上运行 python maintain.py inspect <备份目录>/<库文件名>，"
            "预期 status=compatible。",
            "记录当前版本（git rev-parse HEAD）与本文件字节数 %d。" % report["size_bytes"],
            "切换入口后启动；启动后再次运行 maintain.py inspect，状态仍须是 compatible。",
            "回退：关闭程序后按恢复协议执行——先把当前文件组（主库与同目录的 -wal/-shm）"
            "整组移入同卷保留目录，再放回备份组（online 快照是自包含单文件，file_group "
            "整组放回）；不要只覆盖主文件，也不要直接删除 -wal。",
        ]
        if report["wal_bytes"]:
            steps.append(
                "默认的 online_backup 会读取已提交状态并把快照整理成单文件；"
                "当前 WAL 非空（%d 字节），这属于正常情况。" % report["wal_bytes"]
            )
        if report["extra_objects"]:
            steps.append(
                "库里有本版本未声明的对象（%s）：新代码容忍，旧代码可能不认识。"
                % ", ".join(str(item) for item in report["extra_objects"])
            )
    else:
        blockers = [str(item) for item in report["reasons"]] or [status]
        steps = [
            "先做字节备份：python maintain.py backup %s <备份目录> --mode file_group。"
            % library,
            "用仍能打开它的版本修复，或换回上一个已知良好的备份。",
            "重新运行 python maintain.py inspect %s，直到 status=compatible。" % library,
        ]
    return {
        "path": library,
        "status": status,
        "compatible": status in {"compatible", "missing", "empty"},
        "healthy": bool(report["healthy"]),
        "blockers": blockers,
        "steps": steps,
        "size_bytes": report["size_bytes"],
        "wal_bytes": report["wal_bytes"],
        "journal_mode": report["journal_mode"],
        "objects_declared": report["objects_declared"],
        "objects_actual": report["objects_actual"],
        "reasons": report["reasons"],
        "warnings": report["warnings"],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_COMMON = argparse.ArgumentParser(add_help=False)
_COMMON.add_argument(
    "--json", action="store_true", default=argparse.SUPPRESS, help="输出 JSON，便于机器读取"
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="maintain.py",
        description="库预检、备份、副本校验与切换计划（只读、不联网、不写库）",
    )
    parser.add_argument(
        "--json", action="store_true", default=False, help="输出 JSON，便于机器读取"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser(
        "inspect", parents=[_COMMON], help="只读检查库是否与本版本兼容"
    )
    inspect.add_argument("database", type=Path)
    backup = commands.add_parser(
        "backup", parents=[_COMMON], help="生成可还原的备份（默认在线快照）"
    )
    backup.add_argument("database", type=Path)
    backup.add_argument("destination", type=Path)
    backup.add_argument("--label", help="目标目录名（1-63 位字母数字点横线）；已存在则拒绝")
    backup.add_argument(
        "--mode",
        choices=("auto", "online_backup", "file_group"),
        default="auto",
        help="auto=健康库走在线快照、损坏库走整组字节复制",
    )
    verify = commands.add_parser(
        "verify",
        parents=[_COMMON],
        help="按清单校验备份是否仍与封存时一致（恢复门槛）",
    )
    verify.add_argument("target", type=Path, help="备份目录（含 manifest.json）")
    verify.add_argument(
        "copy",
        type=Path,
        nargs="?",
        default=None,
        help="已弃用：给出第二个路径时按 compare 处理",
    )
    verify.add_argument(
        "--source",
        type=Path,
        default=None,
        help="同时对照当前源库是否仍与封存内容相同（不影响 ok）",
    )
    compare = commands.add_parser(
        "compare",
        parents=[_COMMON],
        help="比较两个库当前内容是否相同（不是备份门槛）",
    )
    compare.add_argument("source", type=Path)
    compare.add_argument("copy", type=Path)
    plan = commands.add_parser(
        "plan", parents=[_COMMON], help="输出切换与回退顺序，不做任何写操作"
    )
    plan.add_argument("database", type=Path)
    restore = commands.add_parser(
        "restore-check", parents=[_COMMON],
        help="恢复前检查这份文件组是不是一个一致状态（回滚库旁边留着非空 -wal 就是混合组）",
    )
    restore.add_argument("database", type=Path)
    decode = commands.add_parser(
        "decode-check", parents=[_COMMON],
        help="只读检查图片的结构与解码维度；没有解码组件时如实报能力不可用",
    )
    decode.add_argument("paths", nargs="+", type=Path)
    decode.add_argument("--structure-only", action="store_true", default=False,
                        help="只做结构级，不要求解码（默认要求解码；没有解码组件时如实报能力不可用）")
    decode.add_argument("--max-pixels", type=int, default=None, help="单文件像素上限")
    decode.add_argument("--max-bytes", type=int, default=None, help="单文件字节上限")
    decode.add_argument("--max-seconds", type=float, default=None, help="单文件解码秒数上限")
    coverage = commands.add_parser(
        "coverage", parents=[_COMMON],
        help="只读汇总一次关注遍历的覆盖情况（不写库、不推进游标、不改断点）",
    )
    coverage.add_argument(
        "--database",
        type=Path,
        required=True,
        help="要读的库（--job 时是 library.sqlite3，--run 时是旧 crawl 状态库），以只读方式打开",
    )
    coverage.add_argument(
        "--job",
        default=None,
        help="当前下载器写的 V2 任务 id（默认来源，读 library.sqlite3）",
    )
    coverage.add_argument(
        "--run",
        default=None,
        help="旧 crawl 状态库里的 run_id（legacy 来源，需显式给出旧库路径）",
    )
    explain_report = commands.add_parser(
        "filter-explain", parents=[_COMMON],
        help="只读解释每个候选为什么被排除、为什么判不了（不发请求、不读图库）",
    )
    explain_report.add_argument(
        "--candidates", type=Path, required=True, help="候选 JSONL：每行一个对象"
    )
    explain_report.add_argument(
        "--conditions", type=Path, required=True,
        help="条件 JSON：{min_bookmarks: int|null, exclude_ai: bool}",
    )
    scan = commands.add_parser(
        "scan", parents=[_COMMON], help="只读巡检已交付的文件是否还和记录一致"
    )
    scan.add_argument("database", type=Path)
    scan.add_argument(
        "--root", type=Path, default=None,
        help="项目根目录；外部根从 <root>/config/output_roots_v1.json 只读加载。默认取库文件所在项目的根",
    )
    scan.add_argument(
        "--page", action="store_true", default=False,
        help="只检查一页（默认走全程）；单页不声明全库结论",
    )
    scan.add_argument(
        "--limit", type=int, default=200, help="每页大小（1-200，默认 200），不是全库总量"
    )
    scan.add_argument(
        "--after", default=None, help="续跑游标：上一次输出的 next_cursor（completed_at,artifact_id）"
    )
    scan.add_argument("--max-bytes", type=int, default=None, help="整趟最多读多少字节")
    scan.add_argument("--max-seconds", type=float, default=None, help="整趟最多用多少秒")
    scan.add_argument(
        "--allow-unknown",
        action="store_true",
        default=False,
        help="只有“无尺寸/无摘要”的旧行可被跳过；坏记录与未覆盖仍然不通过",
    )
    return parser


def _emit(payload: dict[str, object], as_json: bool, lines: list[str]) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
        return
    for line in lines:
        print(line)


def _inspect_lines(report: dict[str, object]) -> list[str]:
    lines = [
        "库: %s" % report["path"],
        "状态: %s" % report["status"],
        "格式: %s   schema: %s   journal_mode: %s"
        % (report["format"], report["schema_version"], report["journal_mode"]),
        "对象: %s/%s  缺失 %d  文本不符 %d"
        % (
            report["objects_actual"],
            report["objects_declared"],
            len(report["missing_objects"]),
            len(report["mismatched_objects"]),
        ),
        "完整性: quick_check=%s integrity_check=%s 外键冲突=%s"
        % (
            ",".join(str(item) for item in report["quick_check"]) or "-",
            ",".join(str(item) for item in report["integrity_check"]) or "-",
            report["foreign_key_violations"],
        ),
        "字节: 主 %s  WAL %s  SHM %s"
        % (report["size_bytes"], report["wal_bytes"], report["shm_bytes"]),
    ]
    counts = report["counts"]
    if counts:
        lines.append(
            "表计数: "
            + ", ".join("%s=%s" % (name, counts[name]) for name in sorted(counts))
        )
    if report["reasons"]:
        lines.append("原因: " + ", ".join(str(item) for item in report["reasons"]))
    if report["warnings"]:
        lines.append("提示: " + ", ".join(str(item) for item in report["warnings"]))
    return lines


_SCAN_FINDINGS = (
    "missing",
    "changed",
    "unreadable",
    "root_unavailable",
    "changed_during_check",
)


def _restore_lines(report: dict[str, object]) -> list[str]:
    """Say what the file group is, and never suggest deleting the log.

    Four questions, four lines: content health, whether this build would open
    it, whether the group is one state, and whether it could be observed at
    all.  They fail in different ways, so they are never merged into one
    reassuring word.
    """
    def fact(value: object) -> str:
        if value is None:
            return "未能观察"
        return "是" if value else "否"

    wal_bytes = report.get("wal_bytes")
    lines = [
        "库: %s" % report["path"],
        "状态: %s" % report["state"],
        "内容健康: %s  当前版本可打开: %s  文件组一致: %s"
        % (
            fact(report.get("content_healthy")),
            fact(report.get("openable")),
            fact(report.get("group_consistent")),
        ),
        "status: %s  journal_mode: %s  header: %s"
        % (
            report.get("status") or "-",
            report.get("journal_mode") or "-",
            report.get("header_journal") or "-",
        ),
        "WAL: %s%s"
        % (
            "未能读取" if wal_bytes is None else "%d 字节" % wal_bytes,
            "（WAL 模式）" if report.get("wal_mode") else "",
        ),
    ]
    if report.get("reasons"):
        lines.append("原因: " + ", ".join(str(item) for item in report["reasons"]))
    if report["state"] == "orphan_wal":
        lines.append(
            "这是混合文件组：回滚日志库旁边留着非空 -wal，SQLite 会忽略它，"
            "磁盘上已经不是同一个状态。按 runbook 第 5 步整组替换。"
        )
    elif report["state"] == "library_missing":
        lines.append("库文件不存在；不要在这里新建空库。")
    elif report["state"] == "not_usable":
        lines.append("这个文件不是一份可用的库：损坏、空白或与当前 schema 不兼容。")
    elif report["state"] == "wal_unreadable":
        lines.append("主库旁边有 -wal，但读不到它的大小；无法判断文件组是否一致。")
    elif report["state"] == "unobservable":
        lines.append("无法观察这个库：%s" % (report.get("detail") or "未知原因"))
    return lines


def _scan_coverage(report: dict[str, object]) -> str:
    """The engine's own coverage verdict, or a conservative reading of it.

    The audit module reports coverage itself from contract v5 on.  While that
    fix is still in flight the field can be absent, and the fallback must never
    claim more coverage than the evidence supports.
    """
    coverage = report.get("coverage")
    if isinstance(coverage, str) and coverage:
        return coverage
    stats = report.get("stats") or {}
    if stats.get("budget_exhausted_reason"):
        return "budget"
    if int(stats.get("examined") or 0) == 0:
        return "empty"
    return "page" if report.get("truncated") else "complete"


def _scan_is_empty(report: dict[str, object], *, continuation: bool = False) -> bool:
    """True only when the ledger itself holds no delivered artifact.

    Coverage 'empty' can also mean "the cursor sits at the end", so the row
    count is the fact that decides whether the library holds any work at all.
    """
    stats = report.get("stats") or {}
    library_rows = stats.get("library_rows")
    if type(library_rows) is int:
        return library_rows == 0
    # On a continuation page the row count is not computed, so an empty
    # page only means "the cursor is at the end", never "the library is empty".
    return _scan_coverage(report) == "empty" and not continuation


def _scan_findings(report: dict[str, object]) -> list[dict[str, object]]:
    return [
        row
        for row in (report.get("rows") or [])
        if row.get("verdict") in _SCAN_FINDINGS
    ]


def _scan_unknown_reasons(report: dict[str, object]) -> dict[str, int]:
    """Reason histogram for the rows nobody could verify.

    The audit module reports this itself from contract v5 on.  The per-row
    reasons are the same facts, so the CLI also reads them directly: a summary
    that cannot name why a row was skipped is a summary nobody can act on.
    """
    stats = report.get("stats") or {}
    raw = stats.get("unknown_reasons")
    if isinstance(raw, dict) and raw:
        counts: dict[str, int] = {}
        for key, value in raw.items():
            try:
                counts[str(key)] = int(value)
            except (TypeError, ValueError):
                continue
        return counts
    counts = {}
    for row in report.get("rows") or []:
        if row.get("verdict") != "unknown":
            continue
        reason = str(row.get("reason") or "unknown")
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def _scan_lines(report: dict[str, object], *, continuation: bool = False) -> list[str]:
    """Report exactly what was verified; never upgrade unknown into a match."""
    stats = report.get("stats") or {}
    coverage = _scan_coverage(report)
    examined = int(stats.get("examined") or 0)
    matched = int(stats.get("matched") or 0)
    excluded = int(stats.get("excluded") or 0)
    unknown = int(stats.get("unknown") or 0)
    considered = int(stats.get("considered") or examined)
    findings = _scan_findings(report)
    ledger = report.get("ledger")
    if isinstance(ledger, dict) and ledger.get("state") == "error":
        cursor = ledger.get("last_safe_cursor")
        lines_out = [
            "巡检失败: 账本读不出来（%s）。" % (ledger.get("code") or "ledger_error"),
            "最后安全游标: %s"
            % ("%s,%s" % (cursor[0], cursor[1]) if cursor else "-"),
        ]
        if findings:
            # Earlier pages did produce findings; a ledger that dies later
            # does not erase them, and the exit code still reports the failure.
            lines_out.append(
                "账本中断前已核验的部分里有 %d 行与记录不一致。" % len(findings)
            )
            for row in findings[:20]:
                lines_out.append(
                    "  %s p%s  %s  %s"
                    % (row["illust_id"], row["page_index"], row["verdict"], row["reason"])
                )
            if len(findings) > 20:
                lines_out.append("  … 还有 %d 行未列出" % (len(findings) - 20))
            lines_out.append("结论: 本次未能覆盖全库，退出码按账本失败处理。")
        else:
            lines_out.append("本次没有给出任何核验结论。")
        return lines_out
    if _scan_is_empty(report, continuation=continuation):
        return ["库里没有已登记的成品可核验。"]
    if _scan_coverage(report) == "empty" and continuation:
        return ["游标已在末尾，本次没有可核验的行。"]
    lines = [
        "核验: 覆盖 %d 行（匹配 %d，排除 %d，无法核验 %d）"
        % (examined, matched, excluded, unknown),
        "字节: 读取 %s，用时 %.2f 秒"
        % (stats.get("bytes_hashed", 0), float(stats.get("seconds") or 0)),
    ]
    if unknown:
        reasons = _scan_unknown_reasons(report)
        detail = "，".join(
            "%s: %d" % (name, count) for name, count in sorted(reasons.items())
        )
        lines.append("无法核验: " + (detail or "原因未记录"))
    if findings:
        # The listed rows are bounded, the counter is not: name both so a
        # large library never sounds smaller than it is.
        exact = int(stats.get("findings") or len(findings))
        lines.append("需要你知道的 %d 行（下面最多列出 20 行）:" % exact)
        for row in findings[:20]:
            lines.append(
                "  %s p%s  %s  %s"
                % (row["illust_id"], row["page_index"], row["verdict"], row["reason"])
            )
        if len(findings) > 20:
            lines.append("  … 还有 %d 行未列出" % (len(findings) - 20))
    if coverage != "complete":
        cursor = report.get("next_cursor")
        lines.append(
            "覆盖: 未完成（%s）；续跑游标 %s"
            % (coverage, "%s,%s" % (cursor[0], cursor[1]) if cursor else "-")
        )
    if findings:
        tail = "结论: 本次核验发现 %d 行与记录不一致" % len(findings)
        if unknown:
            tail += "；另有 %d 行无法核验" % unknown
        lines.append(tail + "。")
    elif coverage == "complete" and unknown == 0:
        lines.append("结论: 本次核验覆盖全部 %d 行，全部与记录一致。" % examined)
    else:
        lines.append(
            "结论: 本次只核验了 %d/%d 行，%d 行无法核验。"
            % (matched + excluded, max(considered, examined), unknown)
        )
    return lines


def _decode_outcome(reports: list[dict[str, object]]) -> int:
    """2 when a file is known bad, 5 when it could not be judged, 0 when all passed.

    "Passed" means the verdict still describes the file at that path.  A file
    that changed while it was being checked is not a failure (nothing was
    proven bad) and it is not a pass either (the verdict is about bytes that
    are no longer there), so it lands on 5 with the other not-proven cases.

    A verdict also has to name the bytes it is about.  check_file() already
    demotes a decode whose receipt does not match the version it hashed, and
    this line keeps a future edit from turning that demotion into the only
    guard: a report that says "ok" while its content block says the bytes are
    unknown is a 5, not a pass (RA-06, S0_CONTRACTS_R3 section 2).
    """
    structure = [
        str((item.get("quality") or {}).get("structure", {}).get("state"))
        for item in reports
    ]
    decode = [
        str((item.get("quality") or {}).get("decode", {}).get("state"))
        for item in reports
    ]
    stability = [str(item.get("path_stability") or "unknown") for item in reports]
    if "failed" in structure or "failed" in decode:
        return 2
    if any(state != "ok" for state in structure) or any(state != "ok" for state in decode):
        return _EXIT_INCOMPLETE
    if any(state != "unchanged" for state in stability):
        return _EXIT_INCOMPLETE
    # The five dimensions answer "what was checked"; content answers "which
    # bytes".  A report with no content block, or one whose content state is not
    # "observed", cannot say its decode describes this file - so it is not a
    # pass, whatever the dimension says.
    if not all(bool(item.get("content_bound")) for item in reports):
        return _EXIT_INCOMPLETE
    return 0


_STABILITY_WORDS = {"unchanged": "未变动", "changed": "已变动", "unknown": "未证明"}
_STABILITY_BYTES = {"unchanged": "是", "changed": "否（内容已变）", "unknown": "未证明"}


def _decode_lines(reports: list[dict[str, object]], code: int) -> list[str]:
    """One block per file; the words come from the five dimensions."""
    lines: list[str] = []
    for item in reports:
        quality = item.get("quality") or {}
        lines.append("文件: %s" % item["path"])
        for name, label in (("structure", "结构"), ("decode", "解码"), ("pages", "页")):
            entry = quality.get(name) or {}
            reason = entry.get("reason")
            lines.append(
                "  %s: %s%s"
                % (label, entry.get("state"), ("（%s）" % reason) if reason else "")
            )
        content = item.get("content") or {}
        content_state = str(content.get("state") or "unknown")
        if content_state == "observed":
            digest = str(content.get("sha256") or "")
            lines.append(
                "  内容: 已绑定 %s（读取 %s%s）"
                % (
                    digest[:12] if digest else "未知",
                    "未知" if content.get("bytes") is None else "%d 字节" % int(content["bytes"]),
                    "，整文件" if content.get("complete") else "，未读完",
                )
            )
        else:
            content_reason = content.get("reason")
            lines.append(
                "  内容: 未绑定（%s%s）"
                % (content_state, ("，" + str(content_reason)) if content_reason else "")
            )
        stability = str(item.get("path_stability") or "unknown")
        stability_reason = item.get("path_stability_reason")
        lines.append(
            "  路径稳定: %s%s"
            % (
                _STABILITY_WORDS.get(stability, stability),
                ("（%s）" % stability_reason) if stability_reason else "",
            )
        )
        if item.get("receipt"):
            receipt = item["receipt"]
            # A receipt may say "not measured" (None) now that the receipt type
            # stopped writing zeros for it.  Printing the Python literal "None"
            # would be worse than saying so in words: a reader cannot tell
            # "None" from a broken format string.
            frames = receipt.get("frames")
            pixels = receipt.get("pixels")
            seconds = receipt.get("seconds")
            lines.append(
                "  解码收据: %s 帧 %s 像素 用时 %s"
                % (
                    "未知" if frames is None else frames,
                    "未知" if pixels is None else pixels,
                    "未知" if seconds is None else "%ss" % (seconds,),
                )
            )
        # "unchanged" alone cannot carry this: a check that never afforded a
        # hash still reports unchanged=True.  The stability field is the
        # answer, and "we did not prove it" is a different word from "no".
        lines.append("  字节未变: %s" % _STABILITY_BYTES.get(stability, "未证明"))
        # The human block has to say what the JSON says: this budget belongs to
        # one file call, and an unmeasured count is 未知, never 0.
        budget = item.get("budget") or {}
        limits = budget.get("limits") or {}
        spent = budget.get("bytes")
        ceiling = limits.get("max_bytes")
        lines.append(
            "  预算: %s 实测%s / 上限%s"
            % (
                str(budget.get("scope") or "unknown"),
                "未知" if spent is None else "%d 字节" % int(spent),
                "未知" if ceiling is None else "%d 字节" % int(ceiling),
            )
        )
    if reports:
        # The contract asks a multi-file run to say how many files it covered and
        # what they added up to - and to say that this is a sum of per-file
        # budgets, not one shared allowance.  A missing count stays 未知.
        total = 0
        missing = False
        for item in reports:
            value = (item.get("budget") or {}).get("bytes")
            if value is None:
                missing = True
            else:
                total += int(value)
        lines.append(
            "预算汇总: files=%d 各文件实测合计=%s（每个文件各自一份预算，不是共享总额）"
            % (len(reports), "未知" if missing else "%d 字节" % total)
        )
    if code == 0:
        lines.append("结论: 每个文件的解码都真的执行并通过。")
    elif code == 2:
        lines.append("结论: 有文件解码失败；这不是能力缺失，是内容没读通。")
    elif code == _EXIT_INCOMPLETE:
        lines.append(
            "结论: 没有全部解码通过——原因见上面的「解码」与「路径稳定」维度"
            "（未请求/能力不可用/超限/取消/检查期间被改动）。"
        )
    return lines


def _report_scan_error(as_json: bool, code: str, detail: str) -> None:
    """One error shape for the whole scan command, text and JSON alike."""
    if as_json:
        print(json.dumps({"status": "error", "code": code, "detail": detail},
                         ensure_ascii=False, indent=2))
    else:
        print("巡检失败: %s（%s）" % (code, detail), file=sys.stderr)


def _scan_scope_lines(report: dict[str, object]) -> list[str]:
    """Name the scope and every external root, so a page is never the library."""
    scope = {"full_walk": "全程", "continuation": "续跑（--after）", "page": "单页（--page）"}
    raw = report.get("scope")
    lines = ["范围: " + scope.get(str(raw), str(raw))]
    roots = report.get("roots")
    if isinstance(roots, dict):
        for key in sorted(roots):
            item = roots[key] if isinstance(roots[key], dict) else {}
            state = "正常" if item.get("state") == "ok" else "目录暂不可用"
            lines.append("外部根: %s → %s（%s）" % (key, item.get("path"), state))
    return lines


def _verify_lines(result: dict[str, object]) -> list[str]:
    sealed = result["sealed"]
    logical = result["logical"]
    members = sealed.get("members") or []
    lines = [
        "备份: %s（清单 v%s，生成于 %s）"
        % (
            result["backup"],
            result["manifest_version"],
            result.get("created_at") or "-",
        ),
        "成员: %d 个，封存 %d 个，封存校验 %s"
        % (int(result.get("member_count") or 0), len(members), sealed["state"]),
    ]
    for member in members:
        if member["state"] == "ok":
            continue
        lines.append(
            "  %s  %s  记录 %s 字节 / 实际 %s 字节"
            % (
                member["name"],
                member["state"],
                member["bytes_recorded"],
                member["bytes_actual"],
            )
        )
    lines.append(
        "逻辑内容: %s%s"
        % (
            logical["state"],
            ("（%s）" % logical["reason"]) if logical.get("reason") else "",
        )
    )
    differing = logical.get("tables_differing") or []
    if differing:
        lines.append("  内容不同的表: " + ", ".join(str(name) for name in differing))
    evidence = result.get("evidence") or {}
    if evidence.get("scope") == "isolated_copy":
        lines.append(
            "证据: 隔离副本（%s）已复制 %d 字节，复制 %.4f 秒 / 回读哈希 %.4f 秒 / 读取 %.4f 秒"
            % (
                "同级目录" if evidence.get("workspace") == "sibling" else "系统临时目录",
                int(evidence.get("copy_bytes") or 0),
                float(evidence.get("copy_seconds") or 0.0),
                float(evidence.get("hash_seconds") or 0.0),
                float(evidence.get("read_seconds") or 0.0),
            )
        )
        if evidence.get("stability") == "changed":
            lines.append("注意: 复制期间备份文件被改动，本次核验不能描述任何一个版本。")
        cleanup = evidence.get("cleanup")
        if isinstance(cleanup, dict):
            state = str(cleanup.get("state") or "unknown")
            removed = len(cleanup.get("removed") or [])
            kept = cleanup.get("kept") or []
            if state == "ok":
                lines.append("清理: ok（删除 %d 项）" % removed)
            else:
                reasons = ",".join(sorted({str(item.get("reason")) for item in kept}))
                lines.append(
                    "清理: 保留 %d 项（%s）；核验结论不受影响，残留位置见 JSON: %s"
                    % (len(kept), reasons or state, cleanup.get("path"))
                )
        elif cleanup:
            lines.append("清理: %s" % cleanup)
    lines.append("健康: %s" % result["health"]["state"])
    lines.append("兼容: %s" % result["compatible"]["state"])
    lines.append("与当前源: %s" % result["source_equivalent"]["state"])
    if result["reasons"]:
        lines.append("原因: " + ", ".join(result["reasons"]))
    if result["warnings"]:
        lines.append("提示: " + ", ".join(result["warnings"]))
    lines.append(
        "边界: 以上结论描述那份隔离副本；核验返回之后备份目录再被改动，不在本次保证内。"
    )
    lines.append("结论: ok=%s" % result["ok"])
    if int(result["manifest_version"]) < 2:
        lines.append(
            "提示: 这份备份的清单是旧版本，无法证明内容一致；请重新运行 backup。"
        )
    return lines


def _compare_lines(result: dict[str, object]) -> list[str]:
    lines = [
        "源: %s (%s)" % (result["source"], result["source_status"]),
        "副本: %s (%s)" % (result["copy"], result["copy_status"]),
        "对象定义一致=%s 内容一致=%s 计数一致=%s 完整性一致=%s 本版本可打开=%s"
        % (
            result["objects_match_source"],
            result["logical"]["state"] == "ok",
            result["counts_match"],
            result["checks_match"],
            result["declared_ok"],
        ),
        "健康: %s" % result["health"]["state"],
        "结论: ok=%s" % result["ok"],
    ]
    differing = result["logical"].get("tables_differing") or []
    if differing:
        lines.append("内容不同的表: " + ", ".join(str(name) for name in differing))
    if result["reasons"]:
        lines.append("原因: " + ", ".join(result["reasons"]))
    if result["warnings"]:
        lines.append("提示: " + ", ".join(result["warnings"]))
    return lines


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    as_json = bool(getattr(arguments, "json", False))
    try:
        if arguments.command == "inspect":
            report = inspect_library(arguments.database)
            _emit(report, as_json, _inspect_lines(report))
            return _EXIT_BY_STATUS.get(str(report["status"]), _EXIT_REFUSED)
        if arguments.command == "backup":
            manifest = backup_library(
                arguments.database,
                arguments.destination,
                label=arguments.label,
                mode=arguments.mode,
            )
            _emit(
                manifest,
                as_json,
                [
                    "备份: %s" % Path(str(manifest["manifest_path"])).parent,
                    "方式: %s" % manifest["mode"],
                    "文件: "
                    + ", ".join(str(item["name"]) for item in manifest["files"]),
                    "对象: %s/%s  表计数 %d 项"
                    % (
                        manifest["objects_actual"],
                        manifest["objects_declared"],
                        len(manifest["counts"]),
                    ),
                    "校验: verified=%s 对象一致=%s 计数一致=%s"
                    % (
                        manifest["verified"],
                        manifest["objects_match_source"],
                        manifest["counts_match_source"],
                    ),
                    "源库: 主文件未变=%s  整组未变=%s"
                    % (
                        manifest["source_checks"]["main_unchanged"],
                        manifest["source_checks"]["group_unchanged"],
                    ),
                ]
                + (
                    [
                        "注意: 本次读取让 SQLite 在源库旁新建了 %s（无库内容，未删除）"
                        % ", ".join(manifest["source_sidecars_created"])
                    ]
                    if manifest.get("source_sidecars_created")
                    else []
                ),
            )
            return 0
        if arguments.command == "verify":
            if arguments.copy is not None:
                # The old two-path form was a comparison, not a seal check.
                result = compare_library(arguments.target, arguments.copy)
                _emit(
                    result,
                    as_json,
                    _compare_lines(result)
                    + ["提示: verify 的两个路径写法已弃用，请改用 compare。"],
                )
                return 0 if result["ok"] else 2
            result = verify_backup(arguments.target, source=arguments.source)
            _emit(result, as_json, _verify_lines(result))
            return int(result.get("exit_code", 0 if result["ok"] else 2))
        if arguments.command == "compare":
            result = compare_library(arguments.source, arguments.copy)
            _emit(result, as_json, _compare_lines(result))
            return 0 if result["ok"] else 2
        if arguments.command == "scan":
            from core.app_paths import AppPaths
            from core.library_audit import LibraryAuditError, ScanBudget
            from core.library_paths import PathResolver
            from core.library_scan import ScanMappingError, scan_library_report

            database = Path(arguments.database).expanduser().resolve()
            raw_root = arguments.root if arguments.root is not None else database.parent.parent
            # --root names the project root only; recorded external roots come
            # from <root>/config/output_roots_v1.json and are never rebound.
            root = Path(raw_root).expanduser().resolve()
            budget = ScanBudget(
                **{
                    key: value
                    for key, value in (
                        ("max_bytes", arguments.max_bytes),
                        ("max_seconds", arguments.max_seconds),
                    )
                    if value is not None
                }
            )
            cursor = None
            if arguments.after:
                parts = str(arguments.after).split(",", 1)
                if len(parts) != 2 or not all(parts):
                    raise LibraryMaintenanceError(
                        "scan_cursor_invalid", "游标格式应为 completed_at,artifact_id"
                    )
                cursor = (parts[0].strip(), parts[1].strip())
            if cursor is not None:
                # A cursor is a continuation, never a statement about the library.
                kind = "continuation"
            elif arguments.page:
                kind = "page"
            else:
                kind = "full_walk"
            try:
                report = scan_library_report(
                    (database, PathResolver(AppPaths(root))),
                    project_root=root,
                    after=cursor,
                    limit=arguments.limit,
                    budget=budget,
                    kind=kind,
                    allow_unknown=arguments.allow_unknown,
                )
            except ScanMappingError as error:
                _report_scan_error(as_json, "mapping_unreadable", error.detail)
                return 3
            except LibraryAuditError as error:
                _report_scan_error(as_json, "scan_ledger_unreadable", str(error))
                return 3
            if (
                report["coverage"] == "error"
                and report["pages_examined"] == 0
                and not report["rows"]
            ):
                # Nothing was examined at all, so this is not a scan report
                # with a bad ending: the ledger could not be opened.
                ledger = report.get("ledger") or {}
                _report_scan_error(
                    as_json,
                    "scan_ledger_unreadable",
                    str(ledger.get("code") or "ledger_unreadable"),
                )
                return 3
            # The walk owns the answer.  The old per-page outcome helper that
            # re-derived it was deleted with the WR-03 hole (a bare continuation
            # at the end of the ledger came back as 0).
            code = int(report["exit_code"])
            lines = _scan_scope_lines(report) + _scan_lines(
                report, continuation=cursor is not None
            )
            if code == 0 and arguments.allow_unknown and report["stats"]["unknown"]:
                lines.append(
                    "提示: 已按 --allow-unknown 跳过 %d 行无摘要旧行。"
                    % report["stats"]["unknown"]
                )
            _emit(report, as_json, lines)
            return code
        if arguments.command == "decode-check":
            from core.decode_limits import DecodeLimits
            from core.quality_check import DEFAULT_LIMITS, check_file

            # "not given" and "given as zero" are different answers: the old
            # "x or default" spelling turned --max-bytes 0 into 256 MiB, i.e. it
            # silently ignored an explicit zero.  A limit that cannot be honoured
            # is a rejected argument (exit 4), never a traceback.
            try:
                limits = DecodeLimits(
                    max_bytes=DEFAULT_LIMITS.max_bytes
                    if arguments.max_bytes is None
                    else arguments.max_bytes,
                    max_pixels=DEFAULT_LIMITS.max_pixels
                    if arguments.max_pixels is None
                    else arguments.max_pixels,
                    max_frames=DEFAULT_LIMITS.max_frames,
                    max_seconds=DEFAULT_LIMITS.max_seconds
                    if arguments.max_seconds is None
                    else arguments.max_seconds,
                )
            except ValueError as error:
                if as_json:
                    print(
                        json.dumps(
                            {
                                "status": "error",
                                "code": "invalid_argument",
                                "detail": str(error),
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                else:
                    print("参数被拒: %s" % error, file=sys.stderr)
                return _EXIT_REFUSED
            policy = "structure" if arguments.structure_only else "strict"
            reports = []
            for target in arguments.paths:
                try:
                    reports.append(check_file(target, decode_policy=policy, limits=limits))
                except OSError as error:
                    _report_scan_error(as_json, "image_unreadable", "%s: %s" % (target, error))
                    return 3
            code = _decode_outcome(reports)
            payload = {
                "question": "image_quality",
                "decode_policy": policy,
                "limits": limits.as_dict(),
                "files": reports,
                "exit_code": code,
            }
            _emit(payload, as_json, _decode_lines(reports, code))
            return code
        if arguments.command in {"coverage", "filter-explain"}:
            from core.report_commands import (
                ReportError,
                coverage_lines,
                coverage_report,
                coverage_report_v2,
                error_payload,
                filter_explain_lines,
                filter_explain_report,
            )

            # Both commands answer with a conclusion, so 3 is "we could not even
            # read the input" and never a stand-in for "we are not sure".
            try:
                if arguments.command == "coverage":
                    # Exactly one task, and the flag says which store to read:
                    # a V2 job is not a legacy run id, and guessing would make
                    # the report describe a database nobody asked for.
                    if bool(arguments.job) == bool(arguments.run):
                        raise ReportError(
                            "coverage_scope_required",
                            "给 --job（V2 任务）或 --run（旧 crawl 状态库）之一，且只给一个",
                        )
                    if arguments.job:
                        report = coverage_report_v2(arguments.database, str(arguments.job))
                    else:
                        report = coverage_report(arguments.database, str(arguments.run))
                    lines = coverage_lines(report)
                else:
                    report = filter_explain_report(arguments.candidates, arguments.conditions)
                    lines = filter_explain_lines(report)
            except ReportError as error:
                if as_json:
                    print(json.dumps(error_payload(error), ensure_ascii=False, indent=2))
                else:
                    print(
                        "失败: %s%s"
                        % (error.code, (" " + error.detail) if error.detail else ""),
                        file=sys.stderr,
                    )
                return 3
            _emit(report, as_json, lines)
            return int(report["exit_code"])
        if arguments.command == "restore-check":
            report = restore_check(arguments.database)
            _emit(report, as_json, _restore_lines(report))
            return int(report.get("exit_code", 0 if report["state"] == "ok" else 2))

        plan = switch_plan(arguments.database)
        _emit(
            plan,
            as_json,
            ["库: %s" % plan["path"], "状态: %s" % plan["status"]]
            + (["阻塞: " + ", ".join(plan["blockers"])] if plan["blockers"] else [])
            + [""]
            + ["%d. %s" % (index + 1, step) for index, step in enumerate(plan["steps"])],
        )
        return 0 if plan["compatible"] else 2
    except ContractError as error:
        # A rejected argument (bad limit, bad budget) is not a crash: the
        # documented exit codes are 0/2/3/4 and a traceback would break them.
        if as_json:
            print(
                json.dumps(
                    {"status": "error", "code": "invalid_argument", "detail": str(error)},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print("参数被拒: %s" % error, file=sys.stderr)
        return _EXIT_REFUSED
    except LibraryMaintenanceError as error:
        if as_json:
            payload: dict[str, object] = {
                "status": "error",
                "code": error.code,
                "detail": error.detail,
            }
            cleanup = getattr(error, "cleanup", None)
            if cleanup is not None:
                # A separate field, not more prose: what failed and what was
                # left behind are two facts a caller acts on differently.
                payload["cleanup"] = cleanup
                payload["retained"] = [
                    item.get("name") for item in (cleanup.get("kept") or [])
                ]
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(
                "失败: %s%s" % (error.code, (" " + error.detail) if error.detail else ""),
                file=sys.stderr,
            )
            if error.code in {"backup_failed", "backup_verification_failed"}:
                print("提示: 可改用 --mode file_group 做整组字节复制。", file=sys.stderr)
        if error.code in {
            "library_missing",
            "library_unavailable",
            "library_locked",
            "library_unreadable",
            "not_a_sqlite_file",
            "source_unreadable",
            "backup_directory_missing",
            "backup_not_a_directory",
            "manifest_missing",
            "manifest_unreadable",
            "manifest_too_large",
            "manifest_version_unsupported",
            "manifest_without_members",
            "manifest_without_library_name",
            "manifest_member_missing",
            "manifest_member_invalid",
            "verify_workspace_unavailable",
            "backup_manifest_failed",
        }:
            return 3
        return _EXIT_REFUSED


if __name__ == "__main__":  # pragma: no cover - thin script guard
    raise SystemExit(main())
