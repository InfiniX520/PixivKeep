"""EA-04: the coverage summary must read the V2 library the downloader fills.

Synthetic only: a temporary library built through the real repository and the
real delivery executor.  The assertions are about which store was read, which
pages were counted, and which facts V2 does not record - never about a real
account, gallery or network.
"""
import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.collection_desktop import DesktopRuntime
from core.follow_coverage import build_coverage
from core.follow_facts_v2 import (
    READ_ONLY,
    READ_ONLY_IMMUTABLE,
    V2FactsError,
    coverage_facts,
    read_only_connection,
    read_only_mode,
)
from core.library_repository import LibraryRepository
from core.report_commands import ReportError
from delivery_fixtures import Response, Transport, executor, make_job, run


def _finished_job(root, *, artwork="900", pages=2):
    """One job with every page delivered, built through the public API."""
    library = root / "library.sqlite3"
    with LibraryRepository(library) as repository:
        job, deliveries, _ = make_job(repository, artwork=artwork, count=pages)
        service = executor(repository, root, Transport(*[Response() for _ in range(pages)]))
        outcomes = [run(repository, job, delivery, service) for delivery in deliveries]
        assert [outcome.state for outcome in outcomes] == ["completed"] * pages
    return library, job


def _facts(library, job_id):
    connection = read_only_connection(library)
    try:
        connection.execute("BEGIN")
        return coverage_facts(connection, job_id)
    finally:
        connection.close()


def test_expected_pages_come_from_the_batch_and_carry_no_content_version(tmp_path):
    library, job = _finished_job(tmp_path)
    facts = _facts(library, job["job_id"])
    expected = {(item["work_id"], item["page"]) for item in facts["pages"]["expected"]}
    assert expected == {("900", 0), ("900", 1)}
    # V2 stores a reference identity for a page, never its expected content
    # version; inventing one here would let the report claim a match that was
    # never observed.
    assert [item["content_sha256"] for item in facts["pages"]["expected"]] == [None, None]


def test_observed_pages_use_the_artifact_digest_and_are_counted_as_saved(tmp_path):
    library, job = _finished_job(tmp_path)
    facts = _facts(library, job["job_id"])
    observed = facts["pages"]["observed"]
    assert {(item["work_id"], item["page"]) for item in observed} == {("900", 0), ("900", 1)}
    assert {item["state"] for item in observed} == {"saved_new"}
    with sqlite3.connect(library) as connection:
        digests = {row[0] for row in connection.execute("SELECT sha256 FROM artifacts")}
    assert {item["content_sha256"] for item in observed} == digests


def test_counts_are_real_numbers_not_placeholders(tmp_path):
    library, job = _finished_job(tmp_path, pages=3)
    facts = _facts(library, job["job_id"])
    totals = facts["run"]["totals"]
    assert totals == {
        "artists_total": 1,
        "works_seen": 1,
        "excluded_by_filter": 0,
        "unknown_fields": 0,
    }
    assert facts["artists"][0]["artist_id"] == "7"
    assert facts["artists"][0]["state"] == "completed"


def test_a_run_that_is_still_running_is_not_reported_as_complete(tmp_path):
    library, job = _finished_job(tmp_path)
    facts = _facts(library, job["job_id"])
    assert facts["run"]["status"] == "running"
    coverage = build_coverage(facts, scope={"kind": "follow_job", "job_id": job["job_id"]})
    assert coverage.overall != "complete"


def test_missing_expected_content_version_keeps_delivery_unknown(tmp_path):
    library, job = _finished_job(tmp_path)
    facts = _facts(library, job["job_id"])
    # The run finished, so only the page side can hold the verdict back.
    facts["run"]["status"] = "completed"
    coverage = build_coverage(facts, scope={"kind": "follow_job", "job_id": job["job_id"]})
    assert coverage.delivery["state"] != "complete"


def test_unknown_job_is_a_typed_refusal(tmp_path):
    library, _ = _finished_job(tmp_path)
    with pytest.raises(V2FactsError) as raised:
        _facts(library, "no-such-job")
    assert raised.value.code == "job_not_found"


def test_missing_database_is_reported_not_created(tmp_path):
    with pytest.raises(V2FactsError) as raised:
        read_only_connection(tmp_path / "absent" / "library.sqlite3")
    assert raised.value.code == "database_missing"
    assert not (tmp_path / "absent").exists()


def test_reading_the_facts_neither_writes_nor_leaves_a_journal(tmp_path):
    library, job = _finished_job(tmp_path)
    before = (hashlib.sha256(library.read_bytes()).hexdigest(), library.stat().st_mtime_ns)
    listing_before = sorted(path.name for path in library.parent.iterdir())
    _facts(library, job["job_id"])
    after = (hashlib.sha256(library.read_bytes()).hexdigest(), library.stat().st_mtime_ns)
    assert after == before
    assert sorted(path.name for path in library.parent.iterdir()) == listing_before


def test_the_read_only_mode_follows_the_evidence_on_disk(tmp_path):
    """Quiescent store: no lock and no index file.  Live writer: take part in
    the locking protocol instead, because a -shm already exists there."""
    library, _ = _finished_job(tmp_path)
    assert read_only_mode(library) == READ_ONLY_IMMUTABLE
    (Path(str(library) + "-shm")).write_bytes(b"")
    assert read_only_mode(library) == READ_ONLY


def test_a_file_that_changes_between_the_two_stamps_is_refused(tmp_path, monkeypatch):
    """The before/after comparison is wired, and it refuses rather than reports.

    A concurrently rewritten main file means the numbers describe no single
    moment; that is not a coverage verdict.  The stamp is injected here because
    making a real writer collide inside our own read transaction would only
    prove that SQLite takes locks.
    """
    from core import report_commands as commands

    library, job = _finished_job(tmp_path)
    seen = []
    real = commands._file_stamp

    def stamp(path):
        value = real(path)
        seen.append(value)
        return value if len(seen) == 1 else (value[0], value[1] + 1)

    monkeypatch.setattr(commands, "_file_stamp", stamp)
    with pytest.raises(ReportError) as raised:
        commands.coverage_report_v2(library, job["job_id"])
    assert raised.value.code == "database_changed_during_read"
    assert len(seen) == 2


def test_desktop_summary_reads_the_v2_library_and_names_its_provider(tmp_path):
    logs = tmp_path / "download_logs"
    logs.mkdir()
    with LibraryRepository(logs / "library.sqlite3") as repository:
        job, deliveries, _ = make_job(repository, artwork="901", count=1)
        service = executor(repository, logs, Transport(Response()))
        assert run(repository, job, deliveries[0], service).state == "completed"
        runtime = object.__new__(DesktopRuntime)
        runtime.attach_paths(SimpleNamespace(download_logs_dir=logs))
        summary = runtime._coverage_summary(job_id=job["job_id"])
    assert summary["provider"] == "v2"
    assert summary["database"] == str(logs / "library.sqlite3")
    assert summary["report"]["coverage"]["scope"]["provider"] == "v2"
    assert summary["exit_code"] != 0  # never 0 while the page side is unknown


def test_desktop_summary_without_a_selected_task_refuses(tmp_path):
    logs = tmp_path / "download_logs"
    logs.mkdir()
    runtime = object.__new__(DesktopRuntime)
    runtime.attach_paths(SimpleNamespace(download_logs_dir=logs))
    with pytest.raises(ReportError) as raised:
        runtime._coverage_summary()
    assert raised.value.code == "job_required"


def test_desktop_summary_keeps_the_legacy_store_available_by_request(tmp_path):
    logs = tmp_path / "download_logs"
    logs.mkdir()
    (logs / "follow_crawl_state.sqlite3").write_bytes(b"not a database")
    runtime = object.__new__(DesktopRuntime)
    runtime.attach_paths(SimpleNamespace(download_logs_dir=logs))
    with pytest.raises(ReportError) as raised:
        runtime._coverage_summary(run_id="run-1", provider="legacy")
    assert raised.value.code in {"database_unreadable", "query_failed", "database_missing"}
def test_the_notes_follow_the_facts_instead_of_a_hard_coded_gap(tmp_path, monkeypatch):
    """When V2 starts recording a required version, the note must stop saying
    "missing" by itself - a hard-coded string would become a lie silently."""
    from core import report_commands as commands

    library, job = _finished_job(tmp_path)
    real = commands.coverage_facts

    def with_versions(connection, job_id):
        facts = real(connection, job_id)
        for item in facts["pages"]["expected"]:
            item["content_sha256"] = item["content_sha256"] or "a" * 64
        return facts

    monkeypatch.setattr(commands, "coverage_facts", with_versions)
    payload = commands.coverage_report_v2(library, job["job_id"])
    assert payload["facts_notes"]["expected_content_version"] == "present"


def test_works_without_an_expected_page_are_counted_and_explained(tmp_path):
    """The "deliverable works" counter and the expected page set are different
    populations; the payload has to say so rather than leave the gap implicit."""
    library, job = _finished_job(tmp_path)
    facts = _facts(library, job["job_id"])
    assert facts["works_without_expected_pages"] == 0
    payload = pytest.importorskip("core.report_commands").coverage_report_v2(
        library, job["job_id"]
    )
    assert "works_without_expected_pages" not in payload["facts_notes"]

