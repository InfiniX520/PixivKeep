"""RA-02: what "read-only" is allowed to mean, measured on the real filesystem.

Synthetic databases only, all inside tmp_path.  Every case compares the source
file family (names plus size/mtime of each member) before and after the read,
because "the SQL connection was read-only" and "the directory did not change"
are two different promises and the second one is what a user can check.
"""
import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest

from core.collection_desktop import DesktopRuntime
from core.follow_facts_v2 import (
    READ_FROM_COPY,
    READ_ONLY,
    READ_ONLY_IMMUTABLE,
    V2FactsError,
    coverage_facts,
    read_only_connection,
    read_only_mode,
    read_plan,
)
from core.library_repository import LibraryRepository
from core.report_commands import ReportError, coverage_lines, coverage_report_v2
from delivery_fixtures import Response, Transport, executor, make_job, run

_JOURNAL_MAGIC = bytes((0xD9, 0xD5, 0x05, 0xF9, 0x20, 0xA1, 0x63, 0xD7))


def _family(path):
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


def _make_rollback(path):
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE t(v)")
    connection.execute("INSERT INTO t VALUES (1)")
    connection.commit()
    connection.close()


def _make_wal_closed(path):
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE t(v)")
    connection.execute("INSERT INTO t VALUES (1)")
    connection.close()


def _make_hot_journal(path):
    """A journal with real content: pages were flushed mid-transaction."""
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("PRAGMA cache_size=2")
    connection.execute("CREATE TABLE t(v)")
    connection.execute("BEGIN IMMEDIATE")
    for index in range(400):
        connection.execute("INSERT INTO t VALUES (?)", ("x" * 2000,))
    copy = path.parent / ("hot-" + path.name)
    copy.write_bytes(path.read_bytes())
    Path(str(copy) + "-journal").write_bytes(Path(str(path) + "-journal").read_bytes())
    connection.execute("ROLLBACK")
    connection.close()
    return copy


def _rows(connection):
    return [list(row) for row in connection.execute("SELECT v FROM t ORDER BY v")]


def _copy_wal_family(source, target, *, with_shm):
    target.write_bytes(source.read_bytes())
    for suffix in ("-wal", "-shm"):
        origin = Path(str(source) + suffix)
        if origin.is_file() and (suffix != "-shm" or with_shm):
            Path(str(target) + suffix).write_bytes(origin.read_bytes())
    return target


def _finished_job(root, *, artwork="930", pages=1):
    library = root / "library.sqlite3"
    with LibraryRepository(library) as repository:
        job, deliveries, _ = make_job(repository, artwork=artwork, count=pages)
        service = executor(repository, root, Transport(*[Response() for _ in range(pages)]))
        outcomes = [run(repository, job, delivery, service) for delivery in deliveries]
        assert [outcome.state for outcome in outcomes] == ["completed"] * pages
    return library, job


def test_a_rollback_database_is_read_directly_and_its_family_never_moves(tmp_path):
    """Measured: mode=ro on a rollback database creates and removes nothing."""
    library = tmp_path / "rollback.db"
    _make_rollback(library)
    before = _family(library)
    plan = read_plan(library)
    assert plan.mode == READ_ONLY
    assert plan.sql_mode == READ_ONLY
    assert plan.promise == "names_and_bytes"
    assert plan.strict_no_side_effects is True
    assert plan.journal == "rollback"
    assert plan.copy is None
    connection = read_only_connection(library)
    try:
        assert _rows(connection) == [[1]]
        evidence = connection.read_evidence()
    finally:
        connection.close()
    assert evidence["source"]["created"] == []
    assert evidence["source"]["removed"] == []
    assert evidence["source"]["ok"] is True
    assert _family(library) == before


def test_a_hot_journal_is_refused_before_anything_is_opened(tmp_path):
    """A database that needs a rollback is not a database this command reads."""
    source = tmp_path / "hot.db"
    copy = _make_hot_journal(source)
    assert Path(str(copy) + "-journal").stat().st_size > 0
    before = _family(copy)
    with pytest.raises(V2FactsError) as raised:
        read_plan(copy)
    assert raised.value.code == "source_hot_journal"
    with pytest.raises(V2FactsError) as again:
        read_only_connection(copy)
    assert again.value.code == "source_hot_journal"
    # Nothing was opened, so the recovery state is exactly as it was.
    assert _family(copy) == before


def test_a_zero_length_journal_is_not_hot_and_is_left_alone(tmp_path):
    """journal_mode=PERSIST leaves an empty -journal; that is not recovery state."""
    library = tmp_path / "persist.db"
    _make_rollback(library)
    journal = Path(str(library) + "-journal")
    journal.write_bytes(b"")
    before = _family(library)
    assert read_plan(library).mode == READ_ONLY
    connection = read_only_connection(library)
    try:
        assert _rows(connection) == [[1]]
    finally:
        connection.close()
    assert journal.is_file()
    assert _family(library) == before


def test_a_direct_read_of_a_closed_wal_database_would_create_sidecars(tmp_path):
    """The control for the copy: mode=ro is not side-effect free for WAL files."""
    library = tmp_path / "closed.db"
    _make_wal_closed(library)
    assert sorted(_family(library)) == ["closed.db"]
    # The same URI a "read-only" connection uses, on a copy of the same bytes.
    control = tmp_path / "control.db"
    control.write_bytes(library.read_bytes())
    connection = sqlite3.connect(
        "file:%s?mode=ro" % control, uri=True, timeout=5.0, isolation_level=None
    )
    try:
        assert _rows(connection) == [[1]]
    finally:
        connection.close()
    assert sorted(_family(control)) == ["control.db", "control.db-shm", "control.db-wal"]


def test_a_cleanly_closed_wal_database_is_read_from_a_private_copy(tmp_path):
    """Quiescent WAL: direct mode=ro would create -wal and -shm in the library."""
    library = tmp_path / "closed.db"
    _make_wal_closed(library)
    before = _family(library)
    private_root = tmp_path / "private"
    private_root.mkdir()
    plan = read_plan(library)
    assert plan.mode == READ_FROM_COPY
    assert plan.sql_mode == READ_ONLY
    assert plan.promise == "copy_verified"
    assert plan.copy["members"] == {}  # not claimed before the copy happened
    connection = read_only_connection(library, private_root=private_root)
    try:
        assert _rows(connection) == [[1]]
        realised = connection.plan
        evidence = connection.read_evidence()
        assert sorted(p.name for p in private_root.iterdir()) == [realised.copy["dir"].split("\\")[-1]]
    finally:
        connection.close()
    copy = realised.copy
    assert copy["verified"] is True
    assert list(copy["members"]) == ["closed.db"]
    assert copy["members"]["closed.db"] == before["closed.db"][2]
    assert copy["source_members"]["closed.db"] == [before["closed.db"][0], before["closed.db"][1]]
    assert realised.strict_no_side_effects is True
    assert evidence["source"]["ok"] is True
    assert evidence["source"]["measured"] == "source_family_during_copy"
    # The library directory is exactly as it was: no -shm, no -wal, no copy.
    assert _family(library) == before
    assert sorted(p.name for p in private_root.iterdir()) == []


def test_a_wal_log_without_its_index_is_read_from_a_copy(tmp_path):
    """RA-02 itself: -wal without -shm used to make the reader create -shm."""
    source = tmp_path / "live.db"
    writer = sqlite3.connect(source, isolation_level=None)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE t(v)")
    writer.execute("INSERT INTO t VALUES (1)")
    copy = _copy_wal_family(source, tmp_path / "kept.db", with_shm=False)
    writer.close()
    assert sorted(_family(copy)) == ["kept.db", "kept.db-wal"]
    before = _family(copy)
    plan = read_plan(copy)
    assert plan.mode == READ_FROM_COPY
    assert plan.sidecars == {"-journal": False, "-wal": True, "-shm": False}
    connection = read_only_connection(copy)
    try:
        assert _rows(connection) == [[1]]  # the rows live in the -wal
    finally:
        connection.close()
    assert _family(copy) == before  # no -shm appeared next to the source


def test_a_live_wal_database_is_read_through_the_locking_protocol(tmp_path):
    """With a writer present, -shm exists and the reader only marks it."""
    library = tmp_path / "live.db"
    _make_wal_closed(library)
    writer = sqlite3.connect(library, isolation_level=None)
    writer.execute("SELECT COUNT(*) FROM t").fetchone()
    try:
        plan = read_plan(library)
        assert plan.mode == READ_ONLY
        assert plan.promise == "names_only"
        assert plan.strict_no_side_effects is False
        before = sorted(_family(library))
        connection = read_only_connection(library)
        try:
            assert _rows(connection) == [[1]]
            connection.execute("BEGIN")
            seen_in_transaction = _rows(connection)
            writer.execute("INSERT INTO t VALUES (2)")  # commits while we read
            still_the_same = _rows(connection)
            connection.execute("COMMIT")
            connection.execute("BEGIN")
            next_transaction = _rows(connection)
            connection.execute("COMMIT")
            evidence = connection.read_evidence()
        finally:
            connection.close()
        # Taken while the writer is still open: closing it checkpoints the log
        # and removes -wal/-shm, which is the writer's doing, not the reader's.
        names_after = sorted(_family(library))
    finally:
        writer.close()
    assert seen_in_transaction == [[1]]
    assert still_the_same == [[1]]  # one read transaction sees one moment
    assert next_transaction == [[1], [2]]
    assert names_after == before  # names unchanged while the writer is open
    assert evidence["source"]["created"] == []
    assert evidence["source"]["removed"] == []
    assert evidence["source"]["ok"] is True


def test_the_private_copy_is_removed_on_close_and_never_leaks(tmp_path):
    library = tmp_path / "closed.db"
    _make_wal_closed(library)
    private_root = tmp_path / "private"
    private_root.mkdir()
    connection = read_only_connection(library, private_root=private_root)
    realised = connection.plan
    assert Path(realised.copy["dir"]).is_dir()
    connection.close()
    assert not Path(realised.copy["dir"]).exists()
    assert sorted(p.name for p in private_root.iterdir()) == []
    connection.close()  # idempotent: the second close must not raise


def test_a_stopped_copy_leaves_no_directory_and_no_report(tmp_path):
    library = tmp_path / "closed.db"
    _make_wal_closed(library)
    private_root = tmp_path / "private"
    private_root.mkdir()

    class Stop:
        def is_set(self):
            return True

    with pytest.raises(V2FactsError) as raised:
        read_only_connection(library, private_root=private_root, stop=Stop())
    assert raised.value.code == "read_cancelled"
    assert sorted(p.name for p in private_root.iterdir()) == []


def test_a_copy_that_moves_under_the_copy_is_refused(tmp_path, monkeypatch):
    """A copy of a database that is being written is not "the" database."""
    from core import follow_facts_v2 as facts

    library = tmp_path / "closed.db"
    _make_wal_closed(library)
    private_root = tmp_path / "private"
    private_root.mkdir()
    real = facts._copy_hashed

    def copy_and_touch(source, destination, *, stop=None):
        digest = real(source, destination, stop=stop)
        Path(str(source)).touch()  # the source moves while we copy it
        return digest

    monkeypatch.setattr(facts, "_copy_hashed", copy_and_touch)
    with pytest.raises(V2FactsError) as raised:
        read_only_connection(library, private_root=private_root)
    assert raised.value.code == "source_changed_during_copy"
    assert sorted(p.name for p in private_root.iterdir()) == []


def test_a_missing_database_or_a_stranger_file_is_a_typed_refusal(tmp_path):
    absent = tmp_path / "absent" / "library.sqlite3"
    with pytest.raises(V2FactsError) as missing:
        read_only_connection(absent)
    assert missing.value.code == "database_missing"
    assert not absent.parent.exists()
    stranger = tmp_path / "stranger.db"
    stranger.write_bytes(b"not a database" * 64)
    before = _family(stranger)
    with pytest.raises(V2FactsError) as unreadable:
        read_only_connection(stranger)
    assert unreadable.value.code == "database_unreadable"
    assert _family(stranger) == before
    directory = tmp_path / "a-directory.db"
    directory.mkdir()
    with pytest.raises(V2FactsError) as refused:
        read_only_connection(directory)
    assert refused.value.code == "database_missing"


def test_immutable_is_only_used_when_the_caller_declares_a_frozen_snapshot(tmp_path):
    """An immutable connection ignores a -wal, so it may never be inferred."""
    library = tmp_path / "closed.db"
    _make_wal_closed(library)
    with pytest.raises(V2FactsError) as invalid:
        read_plan(library, immutable_source="no-wal-next-to-it")
    assert invalid.value.code == "immutable_source_invalid"
    plan = read_plan(library, immutable_source="caller_owned_snapshot")
    assert plan.mode == READ_ONLY_IMMUTABLE
    assert plan.immutable_source == "caller_owned_snapshot"
    assert plan.promise == "names_and_bytes"
    before = _family(library)
    connection = read_only_connection(library, immutable_source="caller_owned_snapshot")
    try:
        assert _rows(connection) == [[1]]
    finally:
        connection.close()
    assert _family(library) == before


def test_an_immutable_snapshot_with_a_wal_loses_those_rows(tmp_path):
    """Why immutable is opt-in: measured, it does not read the log at all."""
    source = tmp_path / "live.db"
    writer = sqlite3.connect(source, isolation_level=None)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE t(v)")
    writer.execute("INSERT INTO t VALUES (1)")
    kept = _copy_wal_family(source, tmp_path / "kept.db", with_shm=True)
    writer.close()
    # The connection opens; what it cannot see is the table that only exists in
    # the -wal, which is exactly the mistake an inferred immutable would make.
    plan = read_plan(kept, immutable_source="caller_owned_snapshot")
    assert plan.sql_mode == READ_ONLY_IMMUTABLE
    connection = read_only_connection(kept, immutable_source="caller_owned_snapshot")
    try:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("SELECT v FROM t").fetchall()
    finally:
        connection.close()


def test_the_report_names_the_promise_it_made(tmp_path):
    library, job = _finished_job(tmp_path)
    payload = coverage_report_v2(library, job["job_id"])
    read = payload["read"]
    assert read["plan"]["mode"] == READ_FROM_COPY
    assert read["plan"]["promise"] == "copy_verified"
    assert read["source"]["ok"] is True
    assert payload["facts_notes"]["read_mode"] == READ_FROM_COPY
    assert payload["facts_notes"]["read_promise"] == "copy_verified"
    lines = coverage_lines(payload)
    assert any(line.startswith("读取方式: 私有副本") for line in lines)


def test_a_report_whose_source_family_moves_is_refused(tmp_path, monkeypatch):
    """The promise is checked, not assumed: a new member is a refusal."""
    from core import follow_facts_v2 as facts

    library, job = _finished_job(tmp_path)
    real = facts.ReadConnection.read_evidence

    def with_a_stranger(self):
        evidence = real(self)
        evidence["source"]["created"] = ["library.sqlite3-shm"]
        evidence["source"]["ok"] = False
        return evidence

    monkeypatch.setattr(facts.ReadConnection, "read_evidence", with_a_stranger)
    with pytest.raises(ReportError) as raised:
        coverage_report_v2(library, job["job_id"])
    assert raised.value.code == "source_side_effects_observed"


def test_the_cli_reports_a_copy_read_and_leaves_the_library_alone(tmp_path, capsys):
    import json

    from core import library_maintenance as maintenance

    logs = tmp_path / "download_logs"
    logs.mkdir()
    library, job = _finished_job(logs)
    before = _family(library)
    code = maintenance.main(
        ["coverage", "--database", str(library), "--job", job["job_id"], "--json"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["provider"] == "v2"
    assert payload["read"]["plan"]["mode"] == READ_FROM_COPY
    assert payload["read"]["plan"]["promise"] == "copy_verified"
    assert payload["read"]["source"]["ok"] is True
    assert code == payload["exit_code"]
    assert _family(library) == before


def test_the_desktop_summary_carries_the_read_facts(tmp_path):
    from types import SimpleNamespace

    logs = tmp_path / "download_logs"
    logs.mkdir()
    library, job = _finished_job(logs)
    runtime = object.__new__(DesktopRuntime)
    runtime.attach_paths(SimpleNamespace(download_logs_dir=logs))
    summary = runtime._coverage_summary(job_id=job["job_id"])
    assert summary["report"]["read"]["plan"]["mode"] == READ_FROM_COPY
    assert any(line.startswith("读取方式:") for line in summary["lines"])

