"""Read-only V2 facts for one follow job's coverage summary (EA-04).

The coverage engine (core.follow_coverage) judges three dimensions from a facts
dict.  Until this module existed the only producer was the legacy crawl-state
store (crawl_runs/artist_progress), which the desktop downloader no longer
writes: the current downloader records its work in the V2 tables of
library.sqlite3.  A summary that reads the legacy file therefore describes a
different database than the one the application is filling.

What V2 actually stores, and what it does not
---------------------------------------------
Measured on a synthetic job built through the real repository (see
.runtime/jpgpng_u1u2_e1_fix_20261115_01/probe/probe_v2_facts.py):

* the expected *page set* of a work is recorded: batch_items.required_pages_json
  ("[0,1]") or, when that is absent, page_count;
* the delivered page is recorded: deliveries(state, generation) plus
  artifacts(sha256, size, verification);
* the expected *content version* of a work page is **not** recorded anywhere.
  deliveries.source_identity looks like a digest but is a hash of
  {illust_id, page_index, url, original, validation_policy_version}
  (core/page_delivery.py:168), i.e. a reference identity, not a content digest;
  source_pages.content_digest is the digest of the follow *list* page.

So this module fills expected[].content_sha256 with None and observed[].content_sha256
with the artifact digest.  That is the honest pair: "we know which pages we
saved" and "we never recorded what the site said those pages should be".  The
engine turns that into delivery=unknown with the reason
expected_content_version_missing, which is why a V2 summary can report real page
numbers and still refuse to call the run complete.

Nothing here writes, creates a table, or switches a journal mode: every
connection is opened with SQLite's mode=ro URI plus PRAGMA query_only, and a
report reads inside one transaction so a batch that advances while it runs
cannot produce a mixed-generation answer.

"SQL read-only" is not the same claim as "the directory did not change", and that
difference is the whole of RA-02.  Measured on this host (transcripts in
.runtime/jpgpng_ra_fix_20261117_01/runs/c/evidence/probe_read_modes*.txt):
mode=ro against a WAL database creates the -shm lock index, and against a
cleanly closed WAL database it creates -wal (0 bytes) and -shm (32 KiB) as well.
read_plan() therefore decides between a direct locking read, a private copy, and
an immutable read of a caller-owned snapshot, and the connection carries both
the plan and the measured evidence of what the read did to the source.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import weakref
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

#: V2 job states mapped onto the engine's vocabulary.  Never pass a raw state
#: through: the engine treats "not failed/paused/partial/running" as complete,
#: so an unmapped "queued" would be read as a finished run.
RUN_STATE = {
    "completed": "completed",
    "running": "running",
    "partial": "partial",
    "failed": "failed",
    "paused": "paused",
    "blocked": "paused",
    "pausing": "paused",
    "cancelling": "paused",
    "settling": "paused",
    "cancelled": "paused",
    "queued": "running",
}

#: batch_items.result_state -> the artist-level vocabulary the engine knows
#: (completed/empty/pending/running/deferred/failed).  A work whose delivery
#: is unfinished keeps its artist unfinished; a work the filter decided is a
#: finished walk, not a missing one.
ARTIST_STATE = {
    "completed": "completed",
    "skipped_existing": "completed",
    "filtered": "empty",
    "metadata_unknown": "completed",
    "unavailable": "completed",
    "unsupported": "completed",
    "pending": "pending",
    "failed": "failed",
    "partial": "deferred",
    "needs_verification": "deferred",
    "cancelled": "deferred",
}

#: deliveries.state -> the engine's observed page state vocabulary.
PAGE_STATE = {
    "completed": "saved_new",
    "needs_verification": "pending_verify",
    "failed": "failed",
    "recovery_needed": "failed",
}

#: Delivery states that are known to be "not delivered yet": a page that still
#: owes work is parked for another look, never counted as saved.
PENDING_STATES = (
    "pending",
    "transferring",
    "validating",
    "prepared",
    "suspended",
    "retry_wait",
    "cancelled",
)

#: The state a known-pending delivery reports to the engine.
PENDING_PAGE = "pending_verify"


class V2FactsError(ValueError):
    """The V2 tables cannot answer for this job; never a coverage verdict."""

    def __init__(self, code: str, detail: object = "") -> None:
        self.code = str(code)
        self.detail = str(detail)
        super().__init__(self.code if not self.detail else "%s: %s" % (self.code, self.detail))


#: The SQL read modes a plan can use.  All of them mean "this connection cannot
#: write"; none of them is by itself a promise about the directory (RA-02).
#:
#: Measured on this host (.runtime/.../runs/c/evidence/probe_read_modes*.txt):
#: mode=ro on a WAL database creates the -shm lock index, and on a *cleanly
#: closed* WAL database it creates -wal and -shm as well.  So "SQL read-only"
#: and "no side effect on the source" are two different claims a plan has to
#: keep apart.
READ_ONLY = "ro"
READ_ONLY_IMMUTABLE = "ro+immutable"
READ_FROM_COPY = "copy"

#: What a plan promises about the source files themselves.
#:
#: names_and_bytes: nothing of the source moves because of this read - no name
#:                  is created or removed, no byte is rewritten (a rollback
#:                  database without a hot journal).
#: names_only:      a live WAL database's -shm is SQLite's lock index and a
#:                  reader marks it, so its bytes move while no name changes.
#: copy_verified:   the read happened against a private copy whose source
#:                  members were proven unchanged before and after the copy.
PROMISE_NAMES_AND_BYTES = "names_and_bytes"
PROMISE_NAMES_ONLY = "names_only"
PROMISE_COPY_VERIFIED = "copy_verified"

#: The only provenance that may select immutable=1.  It has to be *stated* by
#: the caller: "there is no -wal next to the file" was never a proof, and an
#: immutable connection ignores a WAL that does exist (measured: the rows in
#: the log disappear from the answer).
IMMUTABLE_CALLER_OWNED = "caller_owned_snapshot"

SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")
#: A private copy needs the main file and the log that holds frames.  -shm and
#: -journal are never copied: one is a lock index SQLite rebuilds, the other is
#: recovery state that belongs to the source.
COPY_SUFFIXES = ("",)

_SQLITE_HEADER = b"SQLite format 3\x00"
_SQLITE_HEADER_BYTES = 100
_ROLLBACK_JOURNAL_MAGIC = b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7"
_ROLLBACK_JOURNAL_HEADER = 28
_PRIVATE_PREFIX = "pixivkeep-read-"
_COPY_CHUNK = 1024 * 1024


def _plain(value: Any) -> Any:
    """A JSON-ready copy: mappings and sequences only, no live objects."""
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _stop_requested(stop: object) -> bool:
    """Did the caller ask this read to stop?

    An Event, a callable and a plain truth value are all accepted, because the
    maintenance CLI, a test and a future UI thread each have their own natural
    shape and none of them may be silently ignored.
    """
    if stop is None:
        return False
    checker = getattr(stop, "is_set", None)
    if callable(checker):
        return bool(checker())
    if callable(stop):
        return bool(stop())
    return bool(stop)


def _check_stop(stop: object, stage: str) -> None:
    if _stop_requested(stop):
        raise V2FactsError("read_cancelled", stage)


@dataclass(frozen=True)
class ReadPlan:
    """How this run reads one database, and what it promises about the source.

    Decided once, from the files on disk, before anything is opened, and handed
    back with the connection.  A caller must not re-inspect the directory
    afterwards and guess which mode was used: that guess was RA-02, and the
    evidence it produced ("there is a -wal, so the index already existed") was
    false for a quiescent WAL database.
    """

    database: str
    mode: str
    sql_mode: str
    immutable_source: str
    journal: str
    sidecars: Mapping[str, bool]
    promise: str
    strict_no_side_effects: bool
    copy: Mapping[str, Any] | None
    evidence: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "database": self.database,
            "mode": self.mode,
            "sql_mode": self.sql_mode,
            "immutable_source": self.immutable_source,
            "journal": self.journal,
            "sidecars": _plain(self.sidecars),
            "promise": self.promise,
            "strict_no_side_effects": bool(self.strict_no_side_effects),
            "copy": None if self.copy is None else _plain(self.copy),
            "evidence": _plain(self.evidence),
        }


def _member_facts(member: Path) -> dict[str, Any]:
    """(size, mtime_ns) of one file family member; never its content."""
    info = member.stat()
    return {"size": int(info.st_size), "mtime_ns": int(info.st_mtime_ns)}


def _family_names(database: Path) -> dict[str, dict[str, Any]]:
    """Every member of the database's file family, by name, with attributes."""
    names: dict[str, dict[str, Any]] = {}
    for suffix in ("",) + SIDECAR_SUFFIXES:
        member = Path(str(database) + suffix)
        try:
            names[member.name] = _member_facts(member)
        except FileNotFoundError:
            continue
        except OSError:
            names[member.name] = {"size": None, "mtime_ns": None}
    return names


def _family_difference(
    before: Mapping[str, Mapping[str, Any]], after: Mapping[str, Mapping[str, Any]]
) -> dict[str, list[str]]:
    """Which names appeared, disappeared, or moved between two observations."""
    return {
        "created": sorted(set(after) - set(before)),
        "removed": sorted(set(before) - set(after)),
        "changed": sorted(
            name for name in set(before) & set(after) if dict(before[name]) != dict(after[name])
        ),
    }


def _copy_hashed(source: Path, destination: Path, *, stop: object = None) -> str:
    """Copy one member and return the digest of the bytes that were copied."""
    hasher = hashlib.sha256()
    with source.open("rb") as origin, destination.open("wb") as target:
        while True:
            _check_stop(stop, "private_copy:" + source.name)
            chunk = origin.read(_COPY_CHUNK)
            if not chunk:
                break
            hasher.update(chunk)
            target.write(chunk)
    return hasher.hexdigest()


def _read_header(database: Path) -> tuple[bool, str]:
    """(is_sqlite, journal_mode) from the 100-byte header - no connection."""
    try:
        with database.open("rb") as stream:
            header = stream.read(_SQLITE_HEADER_BYTES)
    except OSError as error:
        raise V2FactsError(
            "database_unreadable", "%s (%s)" % (database, type(error).__name__)
        ) from None
    if len(header) < 20 or not header.startswith(_SQLITE_HEADER):
        return False, "unknown"
    # Bytes 18 and 19 are the file format write/read versions; 2 means WAL.
    if header[18] == 2 or header[19] == 2:
        return True, "wal"
    return True, "rollback"


def _sidecars(database: Path) -> dict[str, bool]:
    present: dict[str, bool] = {}
    for suffix in SIDECAR_SUFFIXES:
        try:
            present[suffix] = Path(str(database) + suffix).is_file()
        except OSError:
            present[suffix] = False
    return present


def _journal_is_hot(database: Path) -> bool:
    """A rollback journal that would have to be replayed before reading.

    Measured: a genuine hot journal starts with the magic and records a non-zero
    page count, and a read-only connection handed one refuses with "attempt to
    write a readonly database".  A zero-length journal (journal_mode=PERSIST) or
    a file that is not a journal at all is not hot, and reading past it is fine.
    """
    journal = Path(str(database) + "-journal")
    try:
        if journal.stat().st_size < _ROLLBACK_JOURNAL_HEADER:
            return False
        with journal.open("rb") as stream:
            header = stream.read(_ROLLBACK_JOURNAL_HEADER)
    except FileNotFoundError:
        return False
    except OSError:
        # An unreadable journal cannot be proven not to be hot, so it is
        # treated as hot: reporting past recovery state would be a guess.
        return True
    if not header.startswith(_ROLLBACK_JOURNAL_MAGIC):
        return False
    return int.from_bytes(header[8:12], "big") > 0


def read_plan(
    path: str | Path,
    *,
    immutable_source: str = "",
    allow_private_copy: bool = True,
) -> ReadPlan:
    """Decide how to read this database, from the files on disk only.

    The table (each row measured on this host; transcripts are in
    .runtime/jpgpng_ra_fix_20261117_01/runs/c/evidence/):

        rollback, no hot journal      -> mode=ro,   promise=names_and_bytes
        rollback, hot journal         -> refuse source_hot_journal
        WAL, -wal and -shm present    -> mode=ro,   promise=names_only
        WAL, either of them missing   -> mode=copy, promise=copy_verified
        missing / not a database      -> refuse

    mode=ro is used directly only where the measurement says it creates and
    removes nothing.  Every other WAL shape is read from a private copy, because
    SQLite would otherwise create the -shm index (and, for a cleanly closed WAL
    database, an empty -wal too) inside the user's directory.

    immutable=1 is never chosen here.  It skips locking and change detection, so
    it may only be used for a snapshot the *caller* owns and has said so
    (immutable_source) - not for a live library whose file may move underneath.
    """
    database = Path(path)
    if not database.is_file():
        raise V2FactsError("database_missing", database)
    if type(allow_private_copy) is not bool:
        raise V2FactsError("read_option_invalid", "allow_private_copy")
    is_sqlite, journal = _read_header(database)
    checks = ["header"]
    if not is_sqlite:
        raise V2FactsError(
            "database_unreadable", "%s (not a sqlite database)" % database
        )
    sidecars = _sidecars(database)
    checks.append("sidecars")
    if immutable_source:
        if immutable_source != IMMUTABLE_CALLER_OWNED:
            raise V2FactsError("immutable_source_invalid", immutable_source)
        return ReadPlan(
            database=str(database),
            mode=READ_ONLY_IMMUTABLE,
            sql_mode=READ_ONLY_IMMUTABLE,
            immutable_source=IMMUTABLE_CALLER_OWNED,
            journal=journal,
            sidecars=sidecars,
            promise=PROMISE_NAMES_AND_BYTES,
            strict_no_side_effects=True,
            copy=None,
            evidence={
                "checked": checks,
                "created": [],
                "removed": [],
                "changed": [],
                "why": "调用方声明这是它自己的冻结副本，因此可以跳过锁与变更检测",
            },
        )
    if journal == "rollback":
        hot = _journal_is_hot(database)
        checks.append("journal_header")
        if hot:
            raise V2FactsError(
                "source_hot_journal",
                "%s 的 -journal 需要回滚才能读；先让应用完成恢复再报告" % database,
            )
        return ReadPlan(
            database=str(database),
            mode=READ_ONLY,
            sql_mode=READ_ONLY,
            immutable_source="",
            journal=journal,
            sidecars=sidecars,
            promise=PROMISE_NAMES_AND_BYTES,
            strict_no_side_effects=True,
            copy=None,
            evidence={
                "checked": checks,
                "created": [],
                "removed": [],
                "changed": [],
                "why": "rollback 库且没有热 journal：实测直接只读不会新建或删除成员",
            },
        )
    if sidecars["-wal"] and sidecars["-shm"]:
        return ReadPlan(
            database=str(database),
            mode=READ_ONLY,
            sql_mode=READ_ONLY,
            immutable_source="",
            journal=journal,
            sidecars=sidecars,
            promise=PROMISE_NAMES_ONLY,
            strict_no_side_effects=False,
            copy=None,
            evidence={
                "checked": checks,
                "created": [],
                "removed": [],
                "changed": [],
                "why": "活的 WAL 库：读连接加入锁协议并标记 -shm；不新建、不改名，"
                       "但 -shm 的锁字节会变",
            },
        )
    if not allow_private_copy:
        raise V2FactsError(
            "source_sidecar_missing",
            "%s 是 WAL 库但缺少 -shm；直接只读打开会新建旁车，而调用方禁止私有副本"
            % database,
        )
    members = ["(main)"]
    if sidecars["-wal"]:
        members.append("-wal")
    return ReadPlan(
        database=str(database),
        mode=READ_FROM_COPY,
        sql_mode=READ_ONLY,
        immutable_source="",
        journal=journal,
        sidecars=sidecars,
        promise=PROMISE_COPY_VERIFIED,
        # Not claimed until the copy has been proven: see read_only_connection.
        strict_no_side_effects=False,
        copy={
            "dir": None,
            "members": {},
            "verified": False,
            "source_members": {},
            "reason": "WAL 库没有活写者：直接只读会在源目录新建 -shm（干净关闭的库还会新建 "
                      "0 字节 -wal），所以先把主文件与存在的 -wal 复制到私有目录再读",
        },
        evidence={
            "checked": checks,
            "created": [],
            "removed": [],
            "changed": [],
            "why": "WAL 库没有活写者：读私有副本，源目录不出现任何新名字",
        },
    )


def read_only_mode(path: str | Path, *, immutable_source: str = "") -> str:
    """The plan's mode for this database.  Kept for callers that only ask that.

    It used to answer from "is there a -wal or -shm next to the file?", which
    measured wrong in both directions; the decision now lives in read_plan().
    """
    return read_plan(path, immutable_source=immutable_source).mode


def _private_copy(
    database: Path, *, private_root: object = None, stop: object = None
) -> tuple[Path, dict[str, Any], dict[str, list[str]]]:
    """Copy the members a reader needs into a directory this run owns.

    Only the main file and an existing -wal are copied.  The copy is usable only
    if it is a *moment*: every family member's (size, mtime_ns) is taken before
    and after, and no member that did not exist may appear.  Either moves and
    the copy is thrown away and tried once more; still moving means
    source_changed_during_copy - never a number that is "probably right".

    The copy's digests are computed while it is written, so the source is read
    exactly once (the before/after attributes prove it did not move meanwhile).

    SQLite then gets the copy, not the library: whatever it creates there (a
    -shm index, an empty -wal) lives in our own directory and goes away with it.
    """
    members = list(COPY_SUFFIXES)
    if Path(str(database) + "-wal").is_file():
        members.append("-wal")
    try:
        root = Path(
            tempfile.mkdtemp(
                prefix=_PRIVATE_PREFIX,
                dir=None if private_root is None else str(private_root),
            )
        )
    except OSError as error:
        raise V2FactsError(
            "private_copy_failed", "%s (%s)" % (database, type(error).__name__)
        ) from None
    target = root / database.name
    before = _family_names(database)
    difference = {"created": [], "removed": [], "changed": []}
    for _attempt in (1, 2):
        digests: dict[str, str] = {}
        source_members: dict[str, list[int]] = {}
        try:
            for suffix in members:
                _check_stop(stop, "private_copy")
                source = Path(str(database) + suffix)
                digests[source.name] = _copy_hashed(
                    source, Path(str(target) + suffix), stop=stop
                )
                source_members[source.name] = [
                    int(before[source.name]["size"]),
                    int(before[source.name]["mtime_ns"]),
                ]
        except OSError as error:
            _discard_private(root)
            raise V2FactsError(
                "private_copy_failed", "%s (%s)" % (database, type(error).__name__)
            ) from None
        except BaseException:
            _discard_private(root)
            raise
        after = _family_names(database)
        difference = _family_difference(before, after)
        # A copied member whose (size, mtime_ns) moved is not the file we copied
        # any more, even though its name is still there: that is the difference
        # between "the copy has a name" and "the copy is the database".
        moved = [
            name for name in digests if before.get(name) != after.get(name)
        ]
        difference["changed"] = sorted(set(difference["changed"]) | set(moved))
        if not difference["created"] and not difference["removed"] and not moved:
            return root, {
                "dir": str(root),
                "members": digests,
                "verified": True,
                "source_members": source_members,
                "reason": "复制前后源文件族的名字集合与 (size, mtime_ns) 未变",
            }, difference
        # The source moved under the copy: try once more from the new state.
        before = after
    _discard_private(root)
    raise V2FactsError(
        "source_changed_during_copy",
        "%s 在复制期间被改写（%s），私有副本不能代表任何单一时刻"
        % (database, ", ".join(difference["created"] + difference["removed"])),
    )


def _discard_private(root: object) -> None:
    """Remove a private copy this run created.  Never touches anything else."""
    if root is None:
        return
    path = Path(str(root))
    if not path.name.startswith(_PRIVATE_PREFIX):
        return
    shutil.rmtree(str(path), ignore_errors=True)


class ReadConnection(sqlite3.Connection):
    """A read-only connection that carries the plan it was opened under.

    The plan travels with the connection because a caller that re-derived it
    afterwards would be guessing again.  Closing the connection also removes the
    private copy the connection was reading from, so a report leaves nothing
    behind even when a caller forgets to close it.
    """

    _read_plan: ReadPlan | None = None
    _read_private: object = None
    _read_source: object = None
    _read_before: object = None
    _read_copy_window: object = None

    @property
    def plan(self) -> ReadPlan | None:
        return self._read_plan

    def read_evidence(self) -> dict[str, Any]:
        """What this read did to the source, measured - never inferred.

        For a copy read the window is the copy itself (the source is not touched
        afterwards); for a direct read it is the time the connection was open.
        `changed` is reported as a fact and is not a failure: a live database
        may commit while we read, and a read transaction still sees one moment.
        """
        plan = self._read_plan
        if plan is None:  # pragma: no cover - a connection we did not build
            raise V2FactsError("read_plan_missing", "connection has no plan")
        if plan.mode == READ_FROM_COPY:
            window = dict(self._read_copy_window or {})
            difference = {
                "created": list(window.get("created") or []),
                "removed": list(window.get("removed") or []),
                "changed": list(window.get("changed") or []),
            }
            ok = bool((plan.copy or {}).get("verified"))
            measured = "source_family_during_copy"
        else:
            before = dict(self._read_before or {})
            difference = _family_difference(
                before, _family_names(Path(str(self._read_source)))
            )
            ok = not difference["created"] and not difference["removed"]
            measured = "source_family_while_the_connection_was_open"
        return {
            "plan": plan.as_dict(),
            "source": {
                "created": difference["created"],
                "removed": difference["removed"],
                "changed": difference["changed"],
                "promise": plan.promise,
                "strict": bool(plan.strict_no_side_effects),
                "ok": ok,
                "moved": bool(difference["changed"]),
                "measured": measured,
                "checked": "names+size+mtime_ns",
                "note": (
                    "名字与属性在读取前后一致"
                    if ok and not difference["changed"]
                    else (
                        "名字未变；读取期间有其他连接改写了源库（读事务内完成，"
                        "摘要仍属于单一时刻）"
                        if ok
                        else "读取期间源文件族的名字发生了变化（不一定由本次读取造成）"
                    )
                ),
            },
        }

    def close(self) -> None:  # noqa: D401 - sqlite3.Connection's own signature
        try:
            super().close()
        finally:
            private = self._read_private
            self._read_private = None
            _discard_private(private)


def read_only_connection(
    path: str | Path,
    *,
    immutable_source: str = "",
    allow_private_copy: bool = True,
    private_root: object = None,
    stop: object = None,
) -> ReadConnection:
    """A connection that cannot write, plus the plan it was opened under.

    A missing file is reported, not created: a reporting command that created
    the database it describes would answer about itself.  A WAL database that
    nobody has open is copied first (see _private_copy); a live one is read
    through the locking protocol, where the only thing that moves is SQLite's
    own -shm index.

    stop is accepted and checked per member and per chunk while copying.  The
    coverage command has no cancel entry today, so its callers pass nothing;
    the parameter exists so a UI thread can stop a large copy.
    """
    database = Path(path)
    plan = read_plan(
        database, immutable_source=immutable_source, allow_private_copy=allow_private_copy
    )
    _check_stop(stop, "read_only_connection")
    before = _family_names(database)
    target = database
    private = None
    window: dict[str, list[str]] = {}
    if plan.mode == READ_FROM_COPY:
        private, copy_facts, window = _private_copy(
            database, private_root=private_root, stop=stop
        )
        target = private / database.name
        plan = replace(plan, copy=copy_facts, strict_no_side_effects=True)
    suffix = "&immutable=1" if plan.sql_mode == READ_ONLY_IMMUTABLE else ""
    try:
        connection = sqlite3.connect(
            "file:%s?mode=ro%s" % (quote(str(target)), suffix),
            uri=True,
            timeout=5.0,
            isolation_level=None,
            factory=ReadConnection,
        )
    except sqlite3.Error as error:
        _discard_private(private)
        raise V2FactsError("database_unreadable", error) from None
    except BaseException:
        _discard_private(private)
        raise
    connection.row_factory = sqlite3.Row
    connection._read_plan = plan
    connection._read_private = private
    connection._read_source = database
    connection._read_before = before
    connection._read_copy_window = window
    if private is not None:
        # Even a caller that never closes the connection must not leave the copy
        # behind; close() removes it first, and this is the backstop.
        weakref.finalize(connection, _discard_private, private)
    try:
        connection.execute("PRAGMA query_only=1")
    except sqlite3.Error:
        connection.close()
        raise
    return connection

def _rows(connection: sqlite3.Connection, sql: str, params: tuple = ()) -> list:
    try:
        return list(connection.execute(sql, params))
    except sqlite3.Error as error:
        raise V2FactsError("query_failed", error) from None


def _required_pages(value: object, page_count: object) -> tuple[int, ...] | None:
    """The work's page indices, or None when V2 does not say how many there are."""
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            raise V2FactsError("required_pages_invalid", value[:80]) from None
        if isinstance(parsed, list) and all(type(item) is int and item >= 0 for item in parsed):
            return tuple(sorted(set(parsed)))
        raise V2FactsError("required_pages_invalid", value[:80])
    if type(page_count) is int and page_count > 0:
        return tuple(range(page_count))
    return None


def coverage_facts(connection: sqlite3.Connection, job_id: object) -> dict[str, Any]:
    """Build the coverage facts dict for one job from the V2 tables.

    The caller owns the transaction; this function only reads.  An empty cohort
    is a legitimate answer (a job that followed nobody), a job that is not in
    the table is not.
    """
    if not isinstance(job_id, str) or not job_id.strip():
        raise V2FactsError("job_id_invalid", job_id)
    job_id = job_id.strip()
    jobs = _rows(
        connection,
        "SELECT job_id, account_ref, state, request_json FROM jobs WHERE job_id = ?",
        (job_id,),
    )
    if not jobs:
        raise V2FactsError("job_not_found", job_id)
    job = jobs[0]
    batches = _rows(
        connection,
        "SELECT batch_id, phase, stop_reason, coverage_complete FROM batches "
        "WHERE job_id = ? ORDER BY rowid",
        (job_id,),
    )
    batch_ids = [row["batch_id"] for row in batches]
    items: list = []
    if batch_ids:
        marks = ",".join("?" for _ in batch_ids)
        items = _rows(
            connection,
            "SELECT batch_id, illust_id, artist_id, page_count, decision, "
            "required_pages_json, result_state FROM batch_items "
            "WHERE batch_id IN (%s) ORDER BY batch_id, illust_id" % marks,
            tuple(batch_ids),
        )
    deliveries = {
        (str(row["illust_id"]), int(row["page_index"])): row
        for row in _rows(
            connection,
            "SELECT delivery_id, illust_id, page_index, state, generation, completed_batch_id "
            "FROM deliveries WHERE job_id = ? ORDER BY generation",
            (job_id,),
        )
    }
    artifacts = {
        row["delivery_id"]: row
        for row in _rows(
            connection,
            "SELECT delivery_id, illust_id, page_index, sha256 FROM artifacts WHERE job_id = ?",
            (job_id,),
        )
    }

    expected: list[dict[str, Any]] = []
    page_gaps: list[str] = []
    artists: dict[str, dict[str, Any]] = {}
    works_seen = excluded = unknown_fields = 0
    for item in items:
        works_seen += 1
        decision = str(item["decision"])
        if decision == "filtered":
            excluded += 1
        elif decision == "metadata_unknown":
            unknown_fields += 1
        artist_id = "" if item["artist_id"] is None else str(item["artist_id"])
        if artist_id:
            entry = artists.setdefault(
                artist_id, {"artist_id": artist_id, "state": "completed", "works_seen": 0}
            )
            entry["works_seen"] += 1
            mapped = ARTIST_STATE.get(str(item["result_state"]), "deferred")
            # One unfinished work is enough to keep the artist unfinished: the
            # artist is not "done" just because its other works are.
            if entry["state"] == "completed" and mapped != "completed":
                entry["state"] = mapped
            elif entry["state"] == "empty" and mapped in ("pending", "failed", "deferred"):
                entry["state"] = mapped
        if decision != "download":
            continue
        pages = _required_pages(item["required_pages_json"], item["page_count"])
        if pages is None:
            page_gaps.append(str(item["illust_id"]))
            continue
        for page in pages:
            expected.append(
                {
                    "work_id": str(item["illust_id"]),
                    "page": page,
                    # Not "unknown because we forgot": V2 has no expected
                    # content version for a work page (see the module docstring).
                    "content_sha256": None,
                }
            )

    observed: list[dict[str, Any]] = []
    unmapped: set[str] = set()
    for (illust_id, page_index), row in sorted(deliveries.items()):
        raw_state = str(row["state"])
        state = PAGE_STATE.get(raw_state)
        if state is None:
            # An unknown state is not evidence of "waiting for a look", so the
            # name is recorded as well: the page is parked, but the report can
            # say which state V2 used instead of pretending it recognised it.
            state = PENDING_PAGE
            if raw_state not in PENDING_STATES:
                unmapped.add(raw_state)
        artifact = artifacts.get(str(row["delivery_id"]))
        observed.append(
            {
                "work_id": illust_id,
                "page": page_index,
                "state": state,
                # The digest of the file this run saved.  It is an *observed*
                # version; the expected one is not recorded anywhere in V2.
                "content_sha256": (
                    str(artifact["sha256"])
                    if artifact is not None and artifact["sha256"]
                    else None
                ),
            }
        )

    artists_rows = [artists[key] for key in sorted(artists)]
    incomplete = [
        {"artist_id": row["artist_id"], "reason": "state_%s" % row["state"]}
        for row in artists_rows
        if row["state"] not in ("completed", "empty")
    ]
    totals = {
        "artists_total": len(artists_rows),
        "works_seen": works_seen,
        "excluded_by_filter": excluded,
        "unknown_fields": unknown_fields,
    }
    stop_reason = ""
    for row in batches:
        if row["stop_reason"]:
            stop_reason = str(row["stop_reason"])
    run = {
        "job_id": job_id,
        "account_ref": str(job["account_ref"]),
        "status": RUN_STATE.get(str(job["state"]), "partial"),
        "state_v2": str(job["state"]),
        "stop_reason": stop_reason,
        "totals": totals,
    }
    facts: dict[str, Any] = {
        "run": run,
        "artists": artists_rows,
        "incomplete": incomplete,
        "budget": None,
        "pages": {"expected": expected, "observed": observed},
        "page_end_evidence": _page_end(batches, page_gaps),
    }
    if page_gaps:
        facts["page_gaps"] = page_gaps
    if unmapped:
        facts["unmapped_delivery_states"] = sorted(unmapped)
    # Works whose decision was not "download" contribute no expected page, while
    # the counter side calls every work minus the filtered ones deliverable.
    # The two populations differ; the count travels so a report can say so.
    # The filtered works are already the excluded_by_filter counter, so they are
    # not part of the difference this number explains.
    facts["works_without_expected_pages"] = sum(
        1
        for item in items
        if str(item["decision"]) not in ("download", "filtered")
    )
    return facts


def _page_end(batches: list, page_gaps: list) -> dict[str, str]:
    """Whether the run's batches claim their page lists were complete."""
    if page_gaps:
        return {"state": "partial", "reason": "pages_expected_missing:%d" % len(page_gaps)}
    declared = [row["coverage_complete"] for row in batches]
    if not declared or any(value is None for value in declared):
        return {"state": "unknown", "reason": "batch_coverage_not_recorded"}
    if all(int(value) == 1 for value in declared):
        return {"state": "complete", "reason": "every_batch_complete"}
    return {"state": "partial", "reason": "some_batch_incomplete"}
