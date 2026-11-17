"""R4-01: a private copy retry rebuilds its input instead of reusing it.

Every attempt of `_private_copy` observes the source file family again and copies
what that observation says is there.  The counterexample that made this a finding
is in the frozen R4 file; this module covers the surrounding window: a -wal that
appears, one that moves, one that is dropped, and every failure path that must
leave nothing behind (S0_CONTRACTS_R4 section 1).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from core import follow_facts_v2 as facts
from core.follow_facts_v2 import (
    READ_FROM_COPY,
    READ_ONLY,
    V2FactsError,
    read_only_connection,
    read_plan,
)
from core.library_repository import LibraryRepository
from delivery_fixtures import Response, Transport, executor, make_job, run


def _family(path: Path) -> dict[str, tuple[int, int, str]]:
    """Name -> (size, mtime_ns, sha256) for every member of the family."""
    members = {}
    for suffix in ("", "-journal", "-wal", "-shm"):
        member = Path(str(path) + suffix)
        if not member.is_file():
            continue
        info = member.stat()
        members[member.name] = (
            int(info.st_size),
            int(info.st_mtime_ns),
            hashlib.sha256(member.read_bytes()).hexdigest(),
        )
    return members


def _make_closed_wal(path: Path, rows=(1,)) -> None:
    """A WAL database closed cleanly: main file only, no sidecars."""
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE t(v)")
    for value in rows:
        connection.execute("INSERT INTO t VALUES (?)", (value,))
    connection.close()


def _crash_shape(path: Path, rows=(1,)) -> None:
    """main + -wal, no -shm: the shape a killed process leaves behind."""
    live = path.parent / ("live-" + path.name)
    writer = sqlite3.connect(live, isolation_level=None)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE t(v)")
    for value in rows:
        writer.execute("INSERT INTO t VALUES (?)", (value,))
    path.write_bytes(live.read_bytes())
    Path(str(path) + "-wal").write_bytes(Path(str(live) + "-wal").read_bytes())
    writer.close()


def _rows(connection) -> list:
    return [row[0] for row in connection.execute("SELECT v FROM t ORDER BY v")]


def _private_root(tmp_path: Path) -> Path:
    root = tmp_path / "private"
    root.mkdir(exist_ok=True)
    return root


def _leftovers(root: Path) -> list:
    return sorted(path.name for path in root.iterdir())


class _Counted:
    """Wrap the real copy so a test can act at the copy boundary."""

    def __init__(self, action=None):
        self.calls = []
        self.action = action

    def __call__(self, source, destination, *, stop=None):
        names_before = list(self.calls)
        real = facts.__dict__["_real_copy_hashed"]
        digest = real(source, destination, stop=stop)
        self.calls.append(source.name)
        if self.action is not None:
            self.action(self, source, destination, names_before)
        return digest


@pytest.fixture
def copy_hook(monkeypatch):
    """Patch the copy boundary while keeping the real byte copy underneath."""

    def install(action=None):
        monkeypatch.setattr(facts, "_real_copy_hashed", facts._copy_hashed, raising=False)
        hook = _Counted(action)
        monkeypatch.setattr(facts, "_copy_hashed", hook)
        return hook

    return install


def test_a_wal_that_appears_after_the_first_attempt_is_copied(tmp_path, copy_hook):
    """The R4-01 shape: the commit lands in a -wal the first attempt never saw."""
    database = tmp_path / "source.sqlite3"
    _make_closed_wal(database)
    holder = {}

    def commit_after_first_copy(hook, source, destination, before):
        if len(hook.calls) != 1:
            return
        writer = sqlite3.connect(database)
        writer.execute("INSERT INTO t VALUES (2)")
        writer.commit()
        holder["writer"] = writer

    hook = copy_hook(commit_after_first_copy)
    root = _private_root(tmp_path)
    before = _family(database)
    connection = read_only_connection(database, private_root=root)
    try:
        rows = _rows(connection)
        plan = connection.plan
        after = _family(database)  # while the writer is still open
    finally:
        connection.close()
        if "writer" in holder:
            holder["writer"].close()

    assert rows == [1, 2]
    assert sorted(plan.copy["members"]) == ["source.sqlite3", "source.sqlite3-wal"]
    assert plan.copy["verified"] is True
    assert plan.copy["attempts"] == 2
    assert plan.copy["dropped_members"] == []
    assert hook.calls == ["source.sqlite3", "source.sqlite3", "source.sqlite3-wal"]
    # The read itself never rewrote the library's main file: the writer's commit
    # lives in the -wal, and closing the writer is what checkpoints it later.
    assert after["source.sqlite3"] == before["source.sqlite3"]
    assert _leftovers(root) == []


def test_a_wal_that_moves_during_the_copy_is_copied_again(tmp_path, copy_hook):
    """A crash-shape store whose -wal grows while attempt 1 is copying it."""
    database = tmp_path / "source.sqlite3"
    _crash_shape(database)

    def append_to_the_log(hook, source, destination, before):
        if source.name.endswith("-wal") and len(hook.calls) == 2:
            # A real file append on the source, not a mock: the size and the
            # modification time move, so the attempt cannot be trusted.
            with open(str(source), "ab") as log:
                log.write(b"\x00" * 8)

    hook = copy_hook(append_to_the_log)
    root = _private_root(tmp_path)
    connection = read_only_connection(database, private_root=root)
    try:
        rows = _rows(connection)
        plan = connection.plan
    finally:
        connection.close()

    assert rows == [1]
    assert plan.copy["attempts"] == 2
    assert plan.copy["verified"] is True
    assert sorted(plan.copy["members"]) == ["source.sqlite3", "source.sqlite3-wal"]
    assert hook.calls == [
        "source.sqlite3",
        "source.sqlite3-wal",
        "source.sqlite3",
        "source.sqlite3-wal",
    ]
    assert _leftovers(root) == []


def test_a_member_dropped_between_attempts_is_not_read_from_the_old_one(tmp_path, copy_hook):
    """A -wal that a checkpoint removed must not survive in the final copy."""
    database = tmp_path / "source.sqlite3"
    _crash_shape(database, rows=(1, 2))
    holder = {}

    def checkpoint_and_drop(hook, source, destination, before):
        if not source.name.endswith("-wal") or len(hook.calls) != 2:
            return
        writer = sqlite3.connect(database, isolation_level=None)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        writer.close()
        holder["dropped"] = True

    hook = copy_hook(checkpoint_and_drop)
    root = _private_root(tmp_path)
    connection = read_only_connection(database, private_root=root)
    try:
        rows = _rows(connection)
        plan = connection.plan
        copy_dir = Path(plan.copy["dir"])
        copied_names = sorted(path.name for path in copy_dir.iterdir())
        own_wal = copy_dir / "source.sqlite3-wal"
        own_wal_size = own_wal.stat().st_size if own_wal.is_file() else None
    finally:
        connection.close()

    assert holder.get("dropped") is True
    assert rows == [1, 2]  # the rows moved into the main file, not lost
    assert plan.copy["attempts"] == 2
    assert list(plan.copy["members"]) == ["source.sqlite3"]
    assert plan.copy["dropped_members"] == ["source.sqlite3-wal"]
    # The first attempt did copy a -wal; it must not survive into the copy the
    # connection reads.  SQLite itself creates an empty -wal and a -shm beside
    # the copy the moment it opens it - those are bookkeeping in our own
    # directory, and the empty log is how the leak would NOT look: a promoted
    # attempt-1 log would still hold the checkpointed frames.
    assert "attempt-1" not in copied_names and "attempt-2" not in copied_names
    # Measured while the connection was open: SQLite's own log is empty.  A
    # promoted attempt-1 log would still hold the checkpointed frames.
    assert own_wal_size == 0
    assert _leftovers(root) == []


def test_a_store_that_keeps_moving_is_refused_without_a_number(tmp_path, copy_hook):
    database = tmp_path / "source.sqlite3"
    _make_closed_wal(database)
    holder = {}

    def always_move(hook, source, destination, before):
        holder.setdefault("writer", sqlite3.connect(database))
        writer = holder["writer"]
        writer.execute("INSERT INTO t VALUES (?)", (len(hook.calls) + 10,))
        writer.commit()

    copy_hook(always_move)
    root = _private_root(tmp_path)
    try:
        with pytest.raises(V2FactsError) as raised:
            read_only_connection(database, private_root=root)
    finally:
        if "writer" in holder:
            holder["writer"].close()

    assert raised.value.code == "source_changed_during_copy"
    assert "private root" not in str(raised.value)
    assert _leftovers(root) == []


def test_the_source_family_is_untouched_across_a_retry(tmp_path, copy_hook):
    database = tmp_path / "source.sqlite3"
    _make_closed_wal(database)
    holder = {}

    def commit_once(hook, source, destination, before):
        if len(hook.calls) != 1:
            return
        writer = sqlite3.connect(database)
        writer.execute("INSERT INTO t VALUES (2)")
        writer.commit()
        holder["writer"] = writer

    copy_hook(commit_once)
    before = {name: value[2] for name, value in _family(database).items()}
    root = _private_root(tmp_path)
    connection = read_only_connection(database, private_root=root)
    try:
        assert _rows(connection) == [1, 2]
        evidence = connection.read_evidence()
        after = {name: value[2] for name, value in _family(database).items()}
    finally:
        connection.close()
        if "writer" in holder:
            holder["writer"].close()

    # The reader copied the main file twice and never wrote to it: same bytes,
    # same attributes.  The -wal is the writer's, and closing it checkpoints.
    assert after["source.sqlite3"] == before["source.sqlite3"]
    assert evidence["source"]["ok"] is True
    assert _leftovers(root) == []


def test_a_stop_during_the_second_attempt_leaves_nothing(tmp_path, copy_hook):
    database = tmp_path / "source.sqlite3"
    _make_closed_wal(database)
    holder = {}

    class Stop:
        def is_set(self):
            return holder.get("armed", False)

    def move_then_arm(hook, source, destination, before):
        if len(hook.calls) == 1:
            # Both things are needed to reach the second attempt: a change that
            # forces the retry, and the stop flag that must end it.
            Path(str(source)).touch()
            holder["armed"] = True

    copy_hook(move_then_arm)
    root = _private_root(tmp_path)
    with pytest.raises(V2FactsError) as raised:
        read_only_connection(database, private_root=root, stop=Stop())
    assert raised.value.code == "read_cancelled"
    assert _leftovers(root) == []


def test_a_copy_error_leaves_nothing(tmp_path, monkeypatch):
    database = tmp_path / "source.sqlite3"
    _make_closed_wal(database)

    def boom(source, destination, *, stop=None):
        raise OSError("simulated copy failure")

    monkeypatch.setattr(facts, "_copy_hashed", boom)
    root = _private_root(tmp_path)
    with pytest.raises(V2FactsError) as raised:
        read_only_connection(database, private_root=root)
    assert raised.value.code == "private_copy_failed"
    assert _leftovers(root) == []


def test_a_connection_that_cannot_open_leaves_nothing(tmp_path, monkeypatch):
    database = tmp_path / "source.sqlite3"
    _make_closed_wal(database)

    def refuse(*args, **kwargs):
        raise sqlite3.OperationalError("simulated open failure")

    root = _private_root(tmp_path)
    monkeypatch.setattr(sqlite3, "connect", refuse)
    with pytest.raises(V2FactsError) as raised:
        read_only_connection(database, private_root=root)
    monkeypatch.undo()
    assert raised.value.code == "database_unreadable"
    assert _leftovers(root) == []


def test_the_three_read_modes_are_still_the_measured_ones(tmp_path):
    rollback = tmp_path / "rollback.db"
    sqlite3.connect(rollback).execute("CREATE TABLE t(v)").connection.close()
    assert read_plan(rollback).mode == READ_ONLY

    live = tmp_path / "live.db"
    writer = sqlite3.connect(live, isolation_level=None)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE t(v)")
    try:
        plan = read_plan(live)
        assert plan.mode == READ_ONLY
        assert plan.promise == "names_only"
    finally:
        writer.close()

    closed = tmp_path / "closed.db"
    _make_closed_wal(closed)
    plan = read_plan(closed)
    assert plan.mode == READ_FROM_COPY
    assert plan.promise == "copy_verified"


def _finished_job(root: Path, *, artwork="940"):
    library = root / "library.sqlite3"
    with LibraryRepository(library) as repository:
        job, deliveries, _ = make_job(repository, artwork=artwork, count=1)
        service = executor(repository, root, Transport(Response()))
        outcomes = [run(repository, job, delivery, service) for delivery in deliveries]
        assert [outcome.state for outcome in outcomes] == ["completed"]
    return library, job


def test_the_v2_report_is_built_from_the_retried_copy(tmp_path, copy_hook, capsys):
    import core.library_maintenance as maintenance

    logs = tmp_path / "download_logs"
    logs.mkdir()
    library, job = _finished_job(logs)

    def touch_once(hook, source, destination, before):
        if len(hook.calls) == 1:
            Path(str(source)).touch()  # one move, then the retry is clean

    hook = copy_hook(touch_once)
    code = maintenance.main(
        ["coverage", "--database", str(library), "--job", job["job_id"], "--json"]
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["read"]["plan"]["mode"] == READ_FROM_COPY
    assert payload["read"]["plan"]["copy"]["attempts"] == 2
    assert payload["read"]["plan"]["copy"]["verified"] is True
    assert payload["read"]["source"]["ok"] is True
    assert code == payload["exit_code"]
    assert len(hook.calls) == 2  # main file copied twice, once per attempt


def test_the_legacy_report_reads_through_the_same_copy_path(tmp_path, capsys):
    import core.library_maintenance as maintenance
    from test_report_commands import _crawl_db

    database, run_id = _crawl_db(tmp_path)
    # _crawl_db seals the log into rollback mode; turn it back into a cleanly
    # closed WAL store so the read has to go through the private copy.
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.close()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["crawl.sqlite3"]

    code = maintenance.main(
        ["coverage", "--database", str(database), "--run", run_id, "--json"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["facts_notes"]["read_mode"] == READ_FROM_COPY
    assert payload["read"]["plan"]["copy"]["verified"] is True
    assert code == payload["exit_code"]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["crawl.sqlite3"]


def test_the_desktop_summary_carries_the_copy_attempts(tmp_path):
    from types import SimpleNamespace

    from core.collection_desktop import DesktopRuntime

    logs = tmp_path / "download_logs"
    logs.mkdir()
    library, job = _finished_job(logs)
    runtime = object.__new__(DesktopRuntime)
    runtime.attach_paths(SimpleNamespace(download_logs_dir=logs))
    summary = runtime._coverage_summary(job_id=job["job_id"])
    copy = summary["report"]["read"]["plan"]["copy"]
    assert copy["attempts"] >= 1
    assert copy["verified"] is True
    assert summary["report"]["read"]["source"]["ok"] is True
    family = sorted(
        path.name for path in logs.glob("library.sqlite3*")
    )
    assert family == ["library.sqlite3"]
