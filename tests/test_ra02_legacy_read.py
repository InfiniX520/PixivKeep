"""A read-only coverage command must not change the directory it reports on.

The legacy crawl store is opened for reading too.  Before this file it was read
through a plain mode=ro connection, which on a cleanly closed WAL store makes
SQLite create its -wal/-shm beside the user's file while the text said
"read-only" (RA-02, adjacent store; S0_CONTRACTS_R3 section 3).
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.follow_crawl_state import FollowArtistSnapshot, FollowCrawlStateRepository
from core.report_commands import ReportError, coverage_lines, coverage_report


def _crawl_db(tmp_path: Path) -> tuple[Path, str]:
    """A crawl store as the writer leaves it: WAL mode, closed cleanly."""
    database = tmp_path / "crawl.sqlite3"
    repository = FollowCrawlStateRepository(database)
    run = repository.create_run(
        account_id="acct-synthetic",
        config_key="follow-default",
        artists=[FollowArtistSnapshot(artist_id=str(1000 + index)) for index in range(2)],
        start_mode="history",
    )
    repository.close()
    return database, run.run_id


def _family(directory: Path) -> dict[str, int]:
    return {path.name: path.stat().st_size for path in sorted(directory.iterdir())}


def test_a_quiescent_wal_store_is_read_from_a_copy_and_the_directory_is_untouched(tmp_path):
    database, run_id = _crawl_db(tmp_path)
    assert not Path(str(database) + "-wal").exists()
    assert not Path(str(database) + "-shm").exists()
    before = _family(tmp_path)

    report = coverage_report(database, run_id)

    assert _family(tmp_path) == before
    assert report["provider"] == "legacy"
    assert report["read"]["plan"]["mode"] == "copy"
    assert report["facts_notes"]["read_mode"] == "copy"
    assert report["facts_notes"]["read_promise"] == "copy_verified"
    text = "\n".join(coverage_lines(report))
    assert "私有副本" in text
    assert "只读，legacy" not in text


def test_a_rollback_store_is_read_directly_and_creates_nothing(tmp_path):
    database, run_id = _crawl_db(tmp_path)
    # Seal the WAL into a rollback-journal database: the one shape where a
    # direct read was measured to create and remove nothing.
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.execute("PRAGMA journal_mode=DELETE")
    connection.close()
    before = _family(tmp_path)

    report = coverage_report(database, run_id)

    assert _family(tmp_path) == before
    assert report["read"]["plan"]["mode"] == "ro"
    assert report["facts_notes"]["read_promise"] == "names_and_bytes"
    assert report["read"]["source"]["ok"] is True


def test_a_missing_store_is_a_typed_refusal(tmp_path):
    with pytest.raises(ReportError) as caught:
        coverage_report(tmp_path / "nothing.sqlite3", "run-1")
    assert caught.value.code == "database_missing"


def test_a_store_without_the_expected_tables_is_a_typed_refusal(tmp_path):
    database = tmp_path / "empty.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE unrelated(value INTEGER)")
    connection.close()
    with pytest.raises(ReportError) as caught:
        coverage_report(database, "run-1")
    assert caught.value.code == "schema_missing"
