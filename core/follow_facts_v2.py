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
#: Independent attempts one private copy gets before it refuses to answer.  A
#: retry re-observes the source, so two are enough to survive a writer that
#: commits once; a store that keeps moving under us gets no number at all.
_COPY_ATTEMPTS = 2


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


def _copy_members(family: Mapping[str, Mapping[str, Any]], database: Path) -> list[str]:
    """The suffixes one attempt has to copy, read from that attempt's snapshot.

    Only the main file and an existing -wal carry database content.  The list is
    derived from the same observation the attempt compares against afterwards, so
    a -wal that appeared after the previous attempt is part of this attempt's
    input instead of being silently dropped (R4-01).
    """
    suffixes = list(COPY_SUFFIXES)
    if (database.name + "-wal") in family:
        suffixes.append("-wal")
    return suffixes


def _private_copy(
    database: Path, *, private_root: object = None, stop: object = None
) -> tuple[Path, dict[str, Any], dict[str, list[str]]]:
    """Copy the members a reader needs into a directory this run owns.

    Only the main file and an existing -wal are copied.  The copy is usable only
    if it is a *moment*: every family member's (size, mtime_ns) is taken before
    and after, and no member that did not exist may appear.  Either moves and the
    attempt is thrown away and rebuilt; still moving after the last attempt means
    source_changed_during_copy - never a number that is "probably right".

    Every attempt is independent (R4-01).  Its input is the family as observed at
    that attempt's start, it is written into its own subdirectory, and its result
    is what the returned facts describe.  A retry that reused the first attempt's
    member list would copy a database that never existed: the first attempt's
    observed change is usually *the* -wal that carries a committed transaction,
    so the second attempt has to include it.  The successful attempt is promoted
    to the private root and the losing attempts are removed, so the directory the
    connection opens never contains a member from an earlier attempt.

    The copy's digests are computed while it is written, so the source is read
    exactly once per attempt (the before/after attributes prove it did not move
    meanwhile).  That is also this function's honest boundary: consistency is
    argued from names and (size, mtime_ns), not from a second full read, so a
    writer that rewrites the file in place and restores the exact modification
    time can still pass the check.

    SQLite then gets the copy, not the library: whatever it creates there (a
    -shm index, an empty -wal) lives in our own directory and goes away with it.
    """
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
    difference = {"created": [], "removed": [], "changed": []}
    attempts = 0
    dropped: list[str] = []
    previous_names: list[str] = []
    try:
        for attempt in range(1, _COPY_ATTEMPTS + 1):
            attempts = attempt
            before = _family_names(database)
            members = _copy_members(before, database)
            names = [database.name if suffix == "" else database.name + suffix
                     for suffix in members]
            dropped = sorted(set(previous_names) - set(names))
            attempt_dir = root / ("attempt-%d" % attempt)
            try:
                attempt_dir.mkdir()
            except OSError as error:
                raise V2FactsError(
                    "private_copy_failed", "%s (%s)" % (database, type(error).__name__)
                ) from None
            digests: dict[str, str] = {}
            source_members: dict[str, list[int]] = {}
            try:
                for suffix in members:
                    _check_stop(stop, "private_copy")
                    source = Path(str(database) + suffix)
                    facts = before.get(source.name)
                    if facts is None:
                        # The member vanished between the family observation and
                        # the copy: that is a change, and the retry decides.
                        raise OSError("%s disappeared during the copy" % source)
                    digests[source.name] = _copy_hashed(
                        source, attempt_dir / source.name, stop=stop
                    )
                    source_members[source.name] = [
                        int(facts["size"]),
                        int(facts["mtime_ns"]),
                    ]
            except OSError as error:
                raise V2FactsError(
                    "private_copy_failed", "%s (%s)" % (database, type(error).__name__)
                ) from None
            after = _family_names(database)
            difference = _family_difference(before, after)
            # A copied member whose (size, mtime_ns) moved is not the file we
            # copied any more, even though its name is still there: that is the
            # difference between "the copy has a name" and "the copy is the
            # database".
            moved = [name for name in digests if before.get(name) != after.get(name)]
            difference["changed"] = sorted(set(difference["changed"]) | set(moved))
            if difference["created"] or difference["removed"] or moved:
                # The source moved under this attempt: the next one observes the
                # family again, so the new member list includes what appeared.
                previous_names = names
                continue
            for name in digests:
                # Same volume, same directory: a rename, not a second copy.
                (attempt_dir / name).replace(root / name)
            for stale in root.glob("attempt-*"):
                shutil.rmtree(str(stale), ignore_errors=True)
            return root, {
                "dir": str(root),
                "members": digests,
                "verified": True,
                "source_members": source_members,
                "reason": "复制前后源文件族的名字集合与 (size, mtime_ns) 未变",
                "attempts": attempts,
                "dropped_members": dropped,
            }, difference
        raise V2FactsError(
            "source_changed_during_copy",
            "%s 在复制期间被改写（%s），私有副本不能代表任何单一时刻"
            % (database, ", ".join(difference["created"] + difference["removed"])),
        )
    except BaseException:
        # Cancelled, failed or refused: nothing this run created stays behind.
        _discard_private(root)
        raise


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


#: Event kinds this module reconstructs a job's real relations from.  They are
#: written by the follow repository (core/follow_repository.py) and are the only
#: record of *which* snapshot a job walked and which artists it reached.
SNAPSHOT_SELECTED_EVENT = "follow_snapshot_selected"
ARTIST_ADMITTED_EVENT = "source_artist_admitted"

#: One IN(...) list never carries more ids than this.  SQLite's default limit is
#: 999 bound parameters, and a job can own more batches than that after a long
#: series of resumes, so the queries are chunked instead of assumed small.
_SQL_CHUNK = 400

#: What kind of artist cohort this job has.  The three answers differ: a
#: snapshot gives the frozen member set, a non-follow job enumerates its own
#: artists, and a follow job that recorded no snapshot has an *unknown* cohort -
#: which is why artists_total is None there and never 0.
SCOPE_SNAPSHOT = "snapshot"
SCOPE_NOT_APPLICABLE = "not_applicable"
SCOPE_UNKNOWN = "unknown"

#: How heavy an artist-level result_state is.  One unfinished work keeps its
#: artist unfinished, and a later completed row cannot hide an earlier failure;
#: filtering is a finished walk (0) but is still distinguished from a save.
_HEAVY = {"completed": 0, "empty": 0, "pending": 1, "deferred": 2, "failed": 3}


def _chunks(values, size: int = _SQL_CHUNK):
    """Split one id list into query-sized pieces; never an unbounded IN(...)."""
    items = list(values)
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _stream(connection, sql: str, params: tuple = (), *, stop: object = None, every: int = 500):
    """Rows one at a time, with a stop check every few hundred.

    A ten-thousand-work job is a real shape (a followed artist can have that
    many works), and materialising every row of it would make the report's
    memory a function of the library size.  The caller still owns the read
    transaction; this only avoids the list.
    """
    try:
        cursor = connection.execute(sql, params)
    except sqlite3.Error as error:
        raise V2FactsError("query_failed", error) from None
    seen = 0
    while True:
        try:
            row = cursor.fetchone()
        except sqlite3.Error as error:
            raise V2FactsError("query_failed", error) from None
        if row is None:
            return
        seen += 1
        if every and seen % every == 0:
            _check_stop(stop, "coverage_facts")
        yield row


def _request_source(job) -> str:
    """The request's source ("follow", "artist", ...) or "" when unreadable.

    Only used to tell "this job never had a follow cohort" from "this job had
    one and did not record which snapshot it used": the two are different
    unknowns and must not be merged.
    """
    try:
        payload = json.loads(str(job["request_json"]))
    except (TypeError, ValueError):
        return ""
    if not isinstance(payload, Mapping):
        return ""
    query = payload.get("query")
    if not isinstance(query, Mapping):
        return ""
    return str(query.get("source") or "")


def _selected_snapshot(connection, job_id: str):
    """The snapshot this job said it was walking, from its own event stream.

    _begin_follow_snapshot records {batch_id, fingerprint, snapshot_id} as a
    follow_snapshot_selected event, whether it created a new snapshot or reused
    the account's latest one.  That event - not "the newest snapshot in the
    table" - is the only honest answer to "which member set did this job use":
    the newest snapshot may belong to a later job entirely.
    """
    rows = _rows(
        connection,
        "SELECT batch_id, json_extract(safe_payload,'$.snapshot_id') AS snapshot_id "
        "FROM events WHERE job_id = ? AND kind = ? ORDER BY event_seq DESC LIMIT 1",
        (job_id, SNAPSHOT_SELECTED_EVENT),
    )
    if not rows:
        return None
    snapshot_id = rows[0]["snapshot_id"]
    if not isinstance(snapshot_id, str) or not snapshot_id.strip():
        raise V2FactsError("snapshot_event_invalid", rows[0]["batch_id"])
    return {"snapshot_id": snapshot_id.strip(), "batch_id": rows[0]["batch_id"]}


def _snapshot_scope(connection, job) -> dict[str, Any]:
    """Which cohort this job walked, and how sure we are that it is the cohort."""
    job_id = str(job["job_id"])
    account_ref = str(job["account_ref"])
    selected = _selected_snapshot(connection, job_id)
    if selected is None:
        if _request_source(job) != "follow":
            return {
                "scope": SCOPE_NOT_APPLICABLE,
                "reason": "request_is_not_a_follow_walk",
                "snapshot_id": "",
                "batch_id": "",
                "state": "",
                "coverage_complete": None,
                "members": None,
                "members_active": None,
            }
        return {
            "scope": SCOPE_UNKNOWN,
            "reason": "snapshot_selection_not_recorded",
            "snapshot_id": "",
            "batch_id": "",
            "state": "",
            "coverage_complete": None,
            "members": None,
            "members_active": None,
        }
    rows = _rows(
        connection,
        "SELECT snapshot_id, account_ref, origin_job_id, state, expected_total, "
        "coverage_complete FROM follow_snapshots WHERE snapshot_id = ?",
        (selected["snapshot_id"],),
    )
    if not rows:
        return {
            "scope": SCOPE_UNKNOWN,
            "reason": "snapshot_event_names_a_missing_snapshot",
            "snapshot_id": selected["snapshot_id"],
            "batch_id": selected["batch_id"],
            "state": "",
            "coverage_complete": None,
            "members": None,
            "members_active": None,
        }
    row = rows[0]
    if str(row["account_ref"]) != account_ref:
        # A snapshot of another account is a different cohort: counting its
        # members here would answer about somebody else's follows.
        return {
            "scope": SCOPE_UNKNOWN,
            "reason": "snapshot_belongs_to_another_account",
            "snapshot_id": selected["snapshot_id"],
            "batch_id": selected["batch_id"],
            "state": str(row["state"]),
            "coverage_complete": row["coverage_complete"],
            "members": None,
            "members_active": None,
        }
    members = _rows(
        connection,
        "SELECT m.artist_id, m.display_name, m.ordinal, "
        "a.is_following AS is_following FROM follow_snapshot_members AS m "
        "LEFT JOIN follow_artists AS a "
        "ON a.account_ref = ? AND a.artist_id = m.artist_id "
        "WHERE m.snapshot_id = ? ORDER BY m.ordinal",
        (account_ref, selected["snapshot_id"]),
    )
    return {
        "scope": SCOPE_SNAPSHOT,
        "reason": "",
        "snapshot_id": selected["snapshot_id"],
        "batch_id": selected["batch_id"],
        "state": str(row["state"]),
        "coverage_complete": row["coverage_complete"],
        "members": len(members),
        "members_active": sum(1 for item in members if item["is_following"]),
        "rows": members,
    }


def _admitted_artists(connection, job_id: str, *, stop: object = None) -> set[str]:
    """Artists this job provably reached, even when they produced no work row.

    Two records exist and neither is "a batch_items row": admit_source_artist
    writes a source_artist_admitted event, and a walked artist leaves a
    checkpoint.  Without them, "no works row" and "never visited" would be the
    same fact - which is exactly the missing-artist hole of RA-03.
    """
    reached: set[str] = set()
    for row in _stream(
        connection,
        "SELECT json_extract(safe_payload,'$.artist_id') AS artist_id "
        "FROM events WHERE job_id = ? AND kind = ?",
        (job_id, ARTIST_ADMITTED_EVENT),
        stop=stop,
    ):
        value = row["artist_id"]
        if isinstance(value, str) and value:
            reached.add(value)
    for row in _stream(
        connection, "SELECT artist_id, entity_id FROM checkpoints WHERE job_id = ?",
        (job_id,), stop=stop,
    ):
        for name in ("artist_id", "entity_id"):
            value = row[name]
            if isinstance(value, str) and value:
                reached.add(value)
    return reached


def _cohort_total(scope: Mapping[str, Any], count: int):
    """(total | None, state, reason): the only three answers a cohort can have.

    A *complete* follow snapshot is the cohort, so its member count is a real
    denominator.  Everything else is an unknown denominator, and the reason says
    which unknown it is, because "the job recorded no snapshot", "the snapshot
    was still being built", and "this is not a follow walk at all" are three
    different facts that must not collapse into one blank:

        complete snapshot      -> count, "recorded", ""
        building / uncertain   -> None,  "unknown",  "snapshot_not_complete:<state>"
        not a follow walk      -> None,  "unknown",  "snapshot_scope_not_applicable"
        no snapshot recorded   -> None,  "unknown",  scope reason
    """
    if scope["scope"] == SCOPE_SNAPSHOT:
        complete = (
            str(scope["state"]) == "complete"
            and int(scope["coverage_complete"] or 0) == 1
        )
        if complete:
            return count, "recorded", ""
        return None, "unknown", "snapshot_not_complete:%s" % (str(scope["state"]) or "unknown")
    if scope["scope"] == SCOPE_NOT_APPLICABLE:
        return None, "unknown", "snapshot_scope_not_applicable"
    return None, "unknown", str(scope["reason"] or "snapshot_selection_not_recorded")


def _batches(connection, job_id: str) -> list:
    """The job's batches in the order the contract fixes: created_at, batch_id."""
    return _rows(
        connection,
        "SELECT batch_id, phase, stop_reason, coverage_complete, created_at "
        "FROM batches WHERE job_id = ? ORDER BY created_at, batch_id",
        (job_id,),
    )


def _items(connection, batch_ids: list, *, stop: object = None) -> list:
    """Every batch_items row of this job, in one pass per chunk of batches."""
    items: list = []
    for chunk in _chunks(batch_ids):
        marks = ",".join("?" for _ in chunk)
        items.extend(
            _stream(
                connection,
                "SELECT batch_id, illust_id, artist_id, page_count, decision, "
                "required_pages_json, result_state FROM batch_items "
                "WHERE batch_id IN (%s)" % marks,
                tuple(chunk),
                stop=stop,
            )
        )
    return items


def _work_groups(items: list, order: dict) -> dict[str, list]:
    """illust_id -> its rows, oldest batch first (the representative is last)."""
    groups: dict[str, list] = {}
    for item in items:
        groups.setdefault(str(item["illust_id"]), []).append(item)
    for rows in groups.values():
        rows.sort(key=lambda row: (order[str(row["batch_id"])], str(row["batch_id"])))
    return groups


def _group_conflict(rows: list, created: dict) -> bool:
    """Two rows of one work taken at the same moment that disagree about it.

    A later batch overriding an earlier decision is an ordinary supersede, not a
    contradiction.  Two batches created in the same instant (same created_at)
    that decide differently are a contradiction: no order between them is
    justified by the data, so it is counted instead of silently picking one.
    """
    seen: dict[str, str] = {}
    for row in rows:
        stamp = created.get(str(row["batch_id"]), "")
        decision = str(row["decision"])
        if stamp and stamp in seen and seen[stamp] != decision:
            return True
        seen.setdefault(stamp, decision)
    return False


def _pages_of(rows: list):
    """The newest page list this work records, or None when none of them does.

    The representative row decides the work's fate, but a resumed row may have
    been written without a page list; the newest row that does record one is
    still the honest expected page set for the work.
    """
    for row in reversed(rows):
        if str(row["decision"]) != "download":
            continue
        pages = _required_pages(row["required_pages_json"], row["page_count"])
        if pages is not None:
            return pages
    return None


def _links(connection, batch_ids: list, job_id: str, *, stop: object = None) -> dict:
    """(illust_id, page) -> every selected-page link this job's batches carry.

    The link row (batch_item_pages) is the only place that says which page of
    which work this batch selected, and it may point at a delivery of this job,
    at a delivery another job completed, or at nothing but an artifact (the carry
    path).  Reading deliveries by job_id instead - the previous behaviour -
    cannot see the artifact-only link at all, which is RA-03's third case.
    """
    found: dict[tuple, list] = {}
    for chunk in _chunks(batch_ids):
        marks = ",".join("?" for _ in chunk)
        for row in _stream(
            connection,
            "SELECT bip.batch_id, bip.illust_id, bip.page_index, "
            "bip.delivery_id, bip.artifact_id, "
            "d.state AS delivery_state, d.generation AS delivery_generation, "
            "d.job_id AS delivery_job, "
            "a.sha256 AS artifact_sha256, a.job_id AS artifact_job, "
            "a.verification AS artifact_verification "
            "FROM batch_item_pages AS bip "
            "LEFT JOIN deliveries AS d ON d.delivery_id = bip.delivery_id "
            "LEFT JOIN artifacts AS a ON a.artifact_id = bip.artifact_id "
            "WHERE bip.batch_id IN (%s)" % marks,
            tuple(chunk),
            stop=stop,
        ):
            key = (str(row["illust_id"]), int(row["page_index"]))
            found.setdefault(key, []).append(row)
    return found


def _link_entry(row, job_id: str, unmapped: set) -> dict[str, Any] | None:
    """One observed page from one link row, or None when the link says nothing.

    A delivery state is the strongest fact and is mapped through PAGE_STATE; an
    unknown state stays a parked page and its name is recorded.  A link with no
    delivery is an artifact-only reference (a page another job saved and this one
    carried): the artifact proves the bytes were captured and hashed, so it is a
    save that already happened - but only when its verification says so.
    """
    raw = row["delivery_state"]
    if raw is not None and str(raw):
        state = PAGE_STATE.get(str(raw))
        if state is None:
            state = PENDING_PAGE
            if str(raw) not in PENDING_STATES:
                unmapped.add(str(raw))
        link = "delivery"
        owner = row["delivery_job"]
    elif row["artifact_id"] is not None:
        verification = str(row["artifact_verification"] or "")
        state = "already_verified" if verification == "structure_checked" else PENDING_PAGE
        if verification != "structure_checked" and verification:
            unmapped.add("artifact_verification:" + verification)
        link = "artifact"
        owner = row["artifact_job"]
    else:
        # The table's CHECK forbids a row with neither link; a row that has
        # neither after the joins means the delivery/artifact itself is gone.
        return None
    digest = row["artifact_sha256"]
    return {
        "work_id": str(row["illust_id"]),
        "page": int(row["page_index"]),
        "state": state,
        "content_sha256": str(digest) if digest else None,
        "link": link,
        "origin_job": "self" if (owner is None or str(owner) == job_id) else "other",
    }


def coverage_facts(
    connection: sqlite3.Connection, job_id: object, *, stop: object = None
) -> dict[str, Any]:
    """Build the coverage facts dict for one job from the V2 tables.

    The caller owns the transaction; this function only reads.  An empty cohort
    is a legitimate answer (a job that followed nobody), a job that is not in
    the table is not.

    The facts follow the relations V2 actually records, not the shape the
    summary used to assume (RA-03):

        job -> batches -> batch_items        (works, one row per work per batch)
        job -> events(follow_snapshot_selected) -> follow_snapshots
            -> follow_snapshot_members       (the frozen artist cohort)
        job -> events(source_artist_admitted) / checkpoints (who was reached)
        batch -> batch_item_pages -> deliveries | artifacts   (selected pages)

    Three consequences are deliberate:

    * a work counts once per job even when several batches mention it (a resume
      re-queues the same work), and the newest batch is the representative row;
    * an artist the snapshot lists but this job never reached is reported with
      works_seen=0 instead of disappearing from the cohort; and a follow job that
      recorded no snapshot reports artists_total=None - never 0;
    * a page observed through an artifact-only link (another job saved it and
      this one carried it) is counted, with link="artifact" and origin_job.

    Still unknown, and kept so: V2 stores no expected *content version* for a
    work page (see the module docstring), so expected[].content_sha256 stays None
    and the delivery dimension stays unknown.  Copying the observed digest into
    the expected field would prove the page against itself.
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
    batches = _batches(connection, job_id)
    order = {str(row["batch_id"]): index for index, row in enumerate(batches)}
    created = {str(row["batch_id"]): str(row["created_at"] or "") for row in batches}
    batch_ids = [str(row["batch_id"]) for row in batches]

    scope = _snapshot_scope(connection, job)
    members = scope.pop("rows", []) or []
    reached = _admitted_artists(connection, job_id, stop=stop) if batch_ids else set()
    items = _items(connection, batch_ids, stop=stop) if batch_ids else []
    groups = _work_groups(items, order)

    # ---- works: one row per work, the newest batch represents it -----------
    works_seen = len(groups)
    multi_batch = 0
    works_conflict = 0
    superseded = 0
    excluded = unknown_fields = 0
    expected: list[dict[str, Any]] = []
    page_gaps: list[str] = []
    artist_works: dict[str, list] = {}
    artist_rows: dict[str, dict[str, Any]] = {}
    for work_id in sorted(groups):
        rows = groups[work_id]
        if len({str(row["batch_id"]) for row in rows}) > 1:
            multi_batch += 1
        if _group_conflict(rows, created):
            works_conflict += 1
        elif len({str(row["decision"]) for row in rows}) > 1:
            superseded += 1
        representative = rows[-1]
        decision = str(representative["decision"])
        if decision == "filtered":
            excluded += 1
        elif decision == "metadata_unknown":
            unknown_fields += 1
        artist_id = ""
        for row in reversed(rows):
            if row["artist_id"]:
                artist_id = str(row["artist_id"])
                break
        if artist_id:
            artist_works.setdefault(artist_id, []).append(work_id)
            entry = artist_rows.setdefault(
                artist_id,
                {
                    "artist_id": artist_id,
                    "state": "completed",
                    "works_seen": 0,
                    "visited": False,
                    "visit_state": "not_visited",
                    "in_snapshot": False,
                    "following": None,
                    "sources": ["items"],
                    # Filled per deduplicated work, then resolved once below:
                    # an artist is not "done" because its other works are.
                    "_states": [],
                },
            )
            entry["works_seen"] += 1
            entry["visited"] = True
            entry["visit_state"] = "has_items"
            entry["_states"].append(
                ARTIST_STATE.get(str(representative["result_state"]), "deferred")
            )
        if decision != "download":
            continue
        pages = _pages_of(rows)
        if pages is None:
            page_gaps.append(work_id)
            continue
        for page in pages:
            expected.append(
                {
                    "work_id": work_id,
                    "page": page,
                    # Not "unknown because we forgot": V2 has no expected
                    # content version for a work page (see the module docstring).
                    "content_sha256": None,
                }
            )

    # ---- the frozen cohort: snapshot members the job may not have reached ---
    for member in members:
        artist_id = str(member["artist_id"])
        entry = artist_rows.get(artist_id)
        if entry is None:
            entry = artist_rows.setdefault(
                artist_id,
                {
                    "artist_id": artist_id,
                    "state": "pending",
                    "works_seen": 0,
                    "visited": False,
                    "visit_state": "not_visited",
                    "in_snapshot": True,
                    "following": None,
                    "sources": ["snapshot"],
                },
            )
        entry["in_snapshot"] = True
        entry["following"] = member["is_following"]
        if "snapshot" not in entry["sources"]:
            entry["sources"].append("snapshot")
        if entry["visited"]:
            continue
        # No work row: either the job reached this artist and found nothing to
        # download, or it never got there.  Both are stated, neither is a 0.
        if artist_id in reached:
            entry["visited"] = True
            entry["visit_state"] = "visited_no_items"
            entry["state"] = "empty"
        else:
            entry["state"] = "pending"
            entry["visit_state"] = "not_visited"

    for row in artist_rows.values():
        states = row.pop("_states", [])
        if not states:
            # An artist the snapshot lists but this job never reached: the
            # state was decided by the cohort loop above, not by any work row.
            continue
        heavy = max(states, key=lambda name: _HEAVY.get(name, 0))
        row["state"] = (
            heavy
            if _HEAVY.get(heavy, 0) > 0
            else ("empty" if all(name == "empty" for name in states) else "completed")
        )
    artists_rows = [artist_rows[key] for key in sorted(artist_rows)]
    visited = sum(1 for row in artists_rows if row["visited"])
    total, total_state, total_reason = _cohort_total(scope, len(artists_rows))
    totals = {
        "artists_total": total,
        "artists_visited": visited,
        "artists_total_state": total_state,
        "artists_members": scope["members"],
        "artists_members_active": scope["members_active"],
        "works_seen": works_seen,
        "works_multi_batch": multi_batch,
        "excluded_by_filter": excluded,
        "unknown_fields": unknown_fields,
        # Always present: "" when the denominator is known, otherwise the code
        # that says which of the three unknowns this is.  A missing key would
        # make a consumer guess, and guessing here means writing 0.
        "unknown_artists_reason": total_reason,
    }

    # ---- observed pages: through the selected-page links --------------------
    observed: list[dict[str, Any]] = []
    unmapped: set[str] = set()
    links = _links(connection, batch_ids, job_id, stop=stop) if batch_ids else {}
    expected_keys = {(item["work_id"], item["page"]) for item in expected}
    seen_observations: set[tuple] = set()
    for key in sorted(links):
        for row in links[key]:
            entry = _link_entry(row, job_id, unmapped)
            if entry is None:
                continue
            # Two batches naming the *same* delivery are one observation written
            # twice (a resume re-links the page); only a genuinely different
            # observation - another delivery, another digest - is a second one,
            # and that is what the engine must keep apart and park.
            signature = (
                entry["work_id"], entry["page"], entry["state"],
                entry["content_sha256"], entry["link"], entry["origin_job"],
            )
            if signature in seen_observations:
                continue
            seen_observations.add(signature)
            observed.append(entry)
    # A delivery this job completed but never linked (older rows, or a batch
    # whose link was written later) is still a saved page: it is counted, and
    # named so the report can say the link is missing rather than pretend the
    # page was never saved.
    linked = {key for key in links}
    unlinked = 0
    for row in _stream(
        connection,
        "SELECT delivery_id, illust_id, page_index, state FROM deliveries WHERE job_id = ?",
        (job_id,),
        stop=stop,
    ):
        key = (str(row["illust_id"]), int(row["page_index"]))
        if key in linked or key not in expected_keys:
            continue
        raw = str(row["state"])
        state = PAGE_STATE.get(raw)
        if state is None:
            state = PENDING_PAGE
            if raw not in PENDING_STATES:
                unmapped.add(raw)
        unlinked += 1
        observed.append(
            {
                "work_id": key[0],
                "page": key[1],
                "state": state,
                "content_sha256": None,
                "link": "delivery_unlinked",
                "origin_job": "self",
            }
        )

    incomplete = [
        {
            "artist_id": row["artist_id"],
            # "not reached" and "reached, still unfinished" are different gaps.
            "reason": "not_visited" if not row["visited"] else "state_%s" % row["state"],
        }
        for row in artists_rows
        if row["state"] not in ("completed", "empty")
    ]
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
    pages: dict[str, Any] = {
        "expected": expected,
        "observed": observed,
        "links": {
            "linked_pages": len(links),
            "unlinked_deliveries": unlinked,
            "artifact_only": sum(
                1 for entry in observed if entry.get("link") == "artifact"
            ),
            "from_another_job": sum(
                1 for entry in observed if entry.get("origin_job") == "other"
            ),
        },
    }
    facts: dict[str, Any] = {
        "run": run,
        "artists": artists_rows,
        "incomplete": incomplete,
        "budget": None,
        "snapshot": scope,
        "pages": pages,
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
        1 for work_id in groups if str(groups[work_id][-1]["decision"]) not in ("download", "filtered")
    )
    if multi_batch:
        facts["works_multi_batch"] = multi_batch
    if works_conflict:
        facts["works_conflict"] = works_conflict
    if superseded:
        facts["works_superseded"] = superseded
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
