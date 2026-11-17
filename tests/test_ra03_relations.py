"""RA-03: the coverage facts must follow the relations V2 really records.

Synthetic only: temporary libraries built through the real repository, the real
delivery executor and the real follow-snapshot API.  No real account, gallery,
network or business database is touched.

The three frozen counterexamples of the audit are replayed here so they stay in
the suite, and the neighbouring boundaries are pinned beside them: the three
answers a cohort can have (recorded / unknown / not applicable), a snapshot that
is still building, an empty snapshot, a filtered work, an artifact-only link, a
delivery another job still has parked, a second generation of the same page, and
the size at which the queries have to stay affordable.
"""
import sqlite3
import time
import tracemalloc

from core.candidate_filter import CandidateDecision
from core.follow_coverage import build_coverage
from core.follow_coverage_counts import coverage_counts
from core.follow_facts_v2 import coverage_facts, read_only_connection
from core.library_repository import LibraryRepository
from core.report_commands import coverage_lines, coverage_report_v2
from core.task_contracts import (
    BatchBudget,
    CollectionRequest,
    OutputSpec,
    PathRef,
    PageSpec,
    QuerySpec,
    SourcePage,
)
from delivery_fixtures import Response, Transport, executor, run as run_page
from test_follow_repository import page as follow_page
from test_follow_repository import snapshot as follow_snapshot

ACCOUNT = "synthetic-account"


# ---------------------------------------------------------------------------
# fixtures built through the public API
# ---------------------------------------------------------------------------


def _submit(repo, *, request_id="job", source="follow", dedup="trusted", works=100,
            output_root=""):
    query = QuerySpec(source=source, mode="history" if source == "follow" else "query")
    request = CollectionRequest(
        request_id,
        ACCOUNT,
        query=query,
        output=OutputSpec(
            root_ref=PathRef(relative_path=output_root) if output_root else PathRef(),
            folder_by_artist=False,
        ),
        dedup_policy=dedup,
        budget=BatchBudget(max_works=works, max_requests=2 * works, max_artists=works),
    )
    job = repo.submit(request, "submit-" + request_id)
    assert repo.activate_next()["batch_id"] == job["batch_id"]
    return job


def _candidate(artwork, pages=1, artist="7", decision="download", root=""):
    if decision != "download":
        # Only a download decision may carry page tasks (task_contracts).
        return CandidateDecision(artwork, decision, artist_id=artist)
    prefix = (root + "/") if root else ""
    specs = tuple(
        PageSpec(
            index,
            PathRef(relative_path=prefix + "downl_Pixiv/%s_p%d.png" % (artwork, index)),
            "https://i.pximg.net/img-original/2026/%s_p%d.png" % (artwork, index),
        )
        for index in range(pages)
    )
    return CandidateDecision(
        artwork, decision, artist_id=artist, page_count=pages, pages=specs
    )


def _accept(
    repo, job, artwork="900", pages=1, artist="7", *, batch_id=None, decision="download",
    root=""
):
    source = SourcePage(
        "follow",
        "history",
        artist,
        "source-" + artwork + ("-" + root if root else ""),
        (_candidate(artwork, pages, artist, decision, root),),
        exhausted=True,
    )
    return repo.accept_source_page(batch_id or job["batch_id"], source)


def _facts(path, job_id):
    connection = read_only_connection(path)
    try:
        connection.execute("BEGIN")
        return coverage_facts(connection, job_id)
    finally:
        connection.close()


def _resume(repo, job):
    repo.request_stop(job["job_id"], "pause")
    assert repo.settle_stop(job["batch_id"])
    resumed = repo.resume(job["job_id"], "resume")
    assert repo.activate_next()["batch_id"] == resumed["batch_id"]
    return resumed


def _insert_like(connection, table, row, changes):
    """INSERT a row shaped like another one, with the named fields replaced."""
    values = {name: row[name] for name in row.keys()}
    values.update(changes)
    connection.execute(
        "INSERT INTO %s(%s) VALUES (%s)"
        % (table, ",".join(values), ",".join("?" for _ in values)),
        tuple(values.values()),
    )


def _second_generation(path, job_id, *, digest, batch_id):
    """A synthetic second generation of one page, hung on the real rows.

    The public resume path *carries* the same delivery into the new batch, so
    two generations of one page cannot be produced through the API today.  The
    shape is therefore built here from a real delivery and a real artifact row
    (same job, same foreign keys), with only the generation, the output key and
    the digest replaced - the DDL stays the authority.
    """
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        delivery = connection.execute(
            "SELECT * FROM deliveries WHERE job_id = ? ORDER BY rowid LIMIT 1", (job_id,)
        ).fetchone()
        artifact = connection.execute(
            "SELECT * FROM artifacts WHERE delivery_id = ?", (delivery["delivery_id"],)
        ).fetchone()
        new_delivery = "second-" + delivery["delivery_id"]
        new_artifact = "second-" + artifact["artifact_id"]
        _insert_like(
            connection,
            "deliveries",
            delivery,
            {
                "delivery_id": new_delivery,
                "generation": int(delivery["generation"]) + 1,
                "state": "completed",
                "output_key": delivery["output_key"] + "-generation-2",
            },
        )
        _insert_like(
            connection,
            "artifacts",
            artifact,
            {
                "artifact_id": new_artifact,
                "delivery_id": new_delivery,
                "sha256": digest,
                "output_key": delivery["output_key"] + "-generation-2",
            },
        )
        connection.execute(
            "UPDATE batch_item_pages SET delivery_id = ?, artifact_id = ? "
            "WHERE batch_id = ? AND illust_id = ? AND page_index = ?",
            (new_delivery, new_artifact, batch_id, delivery["illust_id"], delivery["page_index"]),
        )
        connection.commit()


# ---------------------------------------------------------------------------
# the three frozen counterexamples, kept in the suite
# ---------------------------------------------------------------------------


def test_a_snapshot_member_without_work_rows_stays_in_the_cohort(tmp_path):
    """RA-03 #1: a full snapshot of two artists with no works at all.

    The old facts walked batch_items only, so the cohort disappeared
    (artists_total=0).  The snapshot is the cohort; its members are counted even
    when this job never reached them - and "never reached" is stated, not 0.
    """
    path = tmp_path / "library.sqlite3"
    with LibraryRepository(path) as repo:
        job = _submit(repo, request_id="snapshot-only")
        follow_snapshot(repo, job, 7, 8)
        assert len(repo.follow_artists(ACCOUNT)) == 2
        facts = _facts(path, job["job_id"])
    totals = facts["run"]["totals"]
    assert totals["artists_total"] == 2
    assert totals["artists_total_state"] == "recorded"
    assert totals["artists_members"] == 2
    assert totals["artists_visited"] == 0
    assert totals["works_seen"] == 0
    assert [row["artist_id"] for row in facts["artists"]] == ["7", "8"]
    assert [row["state"] for row in facts["artists"]] == ["pending", "pending"]
    assert [row["visit_state"] for row in facts["artists"]] == ["not_visited"] * 2
    assert facts["incomplete"] == [
        {"artist_id": "7", "reason": "not_visited"},
        {"artist_id": "8", "reason": "not_visited"},
    ]


def test_a_page_reused_from_another_job_is_observed(tmp_path):
    """RA-03 #3: the second task carries the first task's file, no delivery.

    The batch records an artifact-only selected-page link.  Reading deliveries by
    job_id cannot see that link at all, which is why the summary used to report
    no observed page for a page that is demonstrably on disk.
    """
    import test_u1a_j01_health as health

    path = tmp_path / "library.sqlite3"
    with LibraryRepository(path) as repo:
        health.first_task(repo, tmp_path)
        job, accepted = health.second_task(repo)
        assert accepted == ()
        result = health.settle(repo, job)
        assert result["pages"]["existing"] == 1
        facts = _facts(path, job["job_id"])
    observed = facts["pages"]["observed"]
    assert len(observed) == 1
    assert (observed[0]["work_id"], observed[0]["page"]) == ("123", 0)
    assert observed[0]["link"] == "artifact"
    assert observed[0]["origin_job"] == "other"
    assert observed[0]["state"] == "already_verified"
    assert observed[0]["content_sha256"] and len(observed[0]["content_sha256"]) == 64
    assert facts["pages"]["links"] == {
        "linked_pages": 1,
        "unlinked_deliveries": 0,
        "artifact_only": 1,
        "from_another_job": 1,
    }


def test_one_work_stays_one_work_after_a_resume(tmp_path):
    """RA-03 #2: a paused-and-resumed job re-queues the same work.

    batch_items has one row per (batch, work), so counting rows counted the same
    work twice: works_seen 2 and two expected pages for a one-page work.  The
    work is counted once, the newest batch represents it, and the page set is
    de-duplicated with it.
    """
    path = tmp_path / "library.sqlite3"
    with LibraryRepository(path) as repo:
        job = _submit(repo, request_id="resume")
        ids = _accept(repo, job)
        before = _facts(path, job["job_id"])
        assert before["run"]["totals"]["works_seen"] == 1
        resumed = _resume(repo, job)
        assert repo.claim_delivery(ids[0], resumed["batch_id"])
        repo.release_delivery(ids[0], resumed["batch_id"], state="suspended")
        facts = _facts(path, job["job_id"])
    totals = facts["run"]["totals"]
    assert totals["works_seen"] == 1
    assert totals["works_multi_batch"] == 1
    assert len(facts["pages"]["expected"]) == 1
    # The same delivery is linked by both batches; that is one observation
    # written twice, not two observations of the page.
    assert len(facts["pages"]["observed"]) == 1
    assert coverage_counts(facts)["counts"]["duplicate_pages"] == 0


# ---------------------------------------------------------------------------
# the three answers a cohort can have
# ---------------------------------------------------------------------------


def test_a_complete_snapshot_is_the_denominator_and_names_the_unreached(tmp_path):
    """(a) Two members, one of them reached: 2 artists, 1 visited, recorded."""
    path = tmp_path / "library.sqlite3"
    with LibraryRepository(path) as repo:
        job = _submit(repo, request_id="reached-one")
        follow_snapshot(repo, job, 7, 8)
        _accept(repo, job, artwork="700", artist="7")
        facts = _facts(path, job["job_id"])
    totals = facts["run"]["totals"]
    assert totals["artists_total"] == 2
    assert totals["artists_total_state"] == "recorded"
    assert totals["artists_visited"] == 1
    assert totals["unknown_artists_reason"] == ""
    rows = {row["artist_id"]: row for row in facts["artists"]}
    assert rows["7"]["visit_state"] == "has_items"
    assert rows["7"]["works_seen"] == 1
    assert rows["8"]["visit_state"] == "not_visited"
    assert rows["8"]["works_seen"] == 0
    assert rows["8"]["following"] == 1
    assert facts["snapshot"]["scope"] == "snapshot"
    assert facts["snapshot"]["members"] == 2
    assert facts["snapshot"]["members_active"] == 2
    assert facts["snapshot"]["state"] == "complete"


def test_a_follow_job_without_a_snapshot_record_has_an_unknown_denominator(tmp_path):
    """(b) A follow job that recorded no snapshot selection: never 0, never 1."""
    path = tmp_path / "library.sqlite3"
    with LibraryRepository(path) as repo:
        job = _submit(repo, request_id="no-snapshot")
        ids = _accept(repo, job)
        run_page(repo, job, ids[0], executor(repo, tmp_path, Transport(Response())))
        facts = _facts(path, job["job_id"])
        report = coverage_report_v2(path, job["job_id"])
    totals = facts["run"]["totals"]
    assert totals["artists_total"] is None
    assert totals["artists_total_state"] == "unknown"
    assert totals["unknown_artists_reason"] == "snapshot_selection_not_recorded"
    assert totals["artists_visited"] == 1
    assert totals["works_seen"] == 1
    assert facts["snapshot"] == {
        "scope": "unknown",
        "reason": "snapshot_selection_not_recorded",
        "snapshot_id": "",
        "batch_id": "",
        "state": "",
        "coverage_complete": None,
        "members": None,
        "members_active": None,
    }
    assert report["coverage"]["denominators"]["artists_total"] is None
    assert "denominator_unknown:artists_total" in report["coverage"]["reasons"]
    assert report["exit_code"] != 0
    text = "\n".join(coverage_lines(report))
    assert "画师总数未知" in text
    assert "画师总数 0" not in text


def test_a_non_follow_job_reports_its_own_unknown(tmp_path):
    """(c) Not a follow walk at all: the same None with a different reason.

    The two branches must not collapse into one blank: "this job never had a
    follow cohort" and "it had one and did not record it" are different facts.
    """
    path = tmp_path / "library.sqlite3"
    with LibraryRepository(path) as repo:
        job = _submit(repo, request_id="artist-job", source="artist")
        _accept(repo, job, artwork="701")
        facts = _facts(path, job["job_id"])
    totals = facts["run"]["totals"]
    assert totals["artists_total"] is None
    assert totals["artists_total_state"] == "unknown"
    assert totals["unknown_artists_reason"] == "snapshot_scope_not_applicable"
    assert facts["snapshot"]["scope"] == "not_applicable"
    assert facts["snapshot"]["reason"] == "request_is_not_a_follow_walk"
    assert totals["artists_visited"] == 1


def test_an_incomplete_snapshot_cannot_supply_the_denominator(tmp_path):
    """A snapshot still being built lists *some* members, not the whole account."""
    path = tmp_path / "library.sqlite3"
    with LibraryRepository(path) as repo:
        job = _submit(repo, request_id="building")
        began = repo.begin_follow_snapshot(job["batch_id"], "sync")
        repo.accept_following_page(
            job["batch_id"],
            began["snapshot_id"],
            "page-1",
            "null",
            follow_page(7, next_cursor='{"offset":1}', exhausted=False),
        )
        facts = _facts(path, job["job_id"])
    totals = facts["run"]["totals"]
    assert totals["artists_total"] is None
    assert totals["artists_total_state"] == "unknown"
    assert totals["unknown_artists_reason"] == "snapshot_not_complete:building"
    assert totals["artists_members"] == 1
    assert facts["snapshot"]["state"] == "building"
    assert [row["artist_id"] for row in facts["artists"]] == ["7"]


def test_an_empty_snapshot_is_zero_artists_and_not_unknown(tmp_path):
    """The one case where 0 is the measured answer: a complete empty cohort."""
    path = tmp_path / "library.sqlite3"
    with LibraryRepository(path) as repo:
        job = _submit(repo, request_id="empty")
        began = repo.begin_follow_snapshot(job["batch_id"], "sync")
        repo.accept_following_page(
            job["batch_id"], began["snapshot_id"], "page-1", "null", follow_page(total=0)
        )
        facts = _facts(path, job["job_id"])
    totals = facts["run"]["totals"]
    assert totals["artists_total"] == 0
    assert totals["artists_total_state"] == "recorded"
    assert totals["unknown_artists_reason"] == ""
    assert totals["artists_members"] == 0
    assert facts["artists"] == []


# ---------------------------------------------------------------------------
# work-level relations
# ---------------------------------------------------------------------------


def test_a_filtered_work_counts_once_and_leaves_its_artist_empty(tmp_path):
    path = tmp_path / "library.sqlite3"
    with LibraryRepository(path) as repo:
        job = _submit(repo, request_id="filtered")
        _accept(repo, job, artwork="800", decision="filtered")
        facts = _facts(path, job["job_id"])
    totals = facts["run"]["totals"]
    assert totals["works_seen"] == 1
    assert totals["excluded_by_filter"] == 1
    assert totals["unknown_fields"] == 0
    assert facts["pages"]["expected"] == []
    assert facts["artists"][0]["state"] == "empty"
    assert facts["works_without_expected_pages"] == 0


def test_a_completed_delivery_without_a_link_is_counted_and_named(tmp_path):
    """Older rows may hold a completed delivery whose page link is missing.

    The page is still a save; it is counted and the missing link is named, so a
    report can say "link missing" instead of "never saved".
    """
    path = tmp_path / "library.sqlite3"
    with LibraryRepository(path) as repo:
        job = _submit(repo, request_id="unlinked")
        ids = _accept(repo, job)
        run_page(repo, job, ids[0], executor(repo, tmp_path, Transport(Response())))
        facts_before = _facts(path, job["job_id"])
        assert facts_before["pages"]["links"]["linked_pages"] == 1
        with sqlite3.connect(path) as connection:
            connection.execute("DELETE FROM batch_item_pages")
            connection.commit()
        facts = _facts(path, job["job_id"])
    assert [entry["link"] for entry in facts["pages"]["observed"]] == ["delivery_unlinked"]
    assert facts["pages"]["links"]["unlinked_deliveries"] == 1
    assert facts["pages"]["links"]["linked_pages"] == 0


def test_a_parked_delivery_of_another_job_is_not_this_jobs_save(tmp_path):
    """Two jobs on one work: the other job's parked page proves nothing here.

    Only one batch may be active, so the first job parks its page (stop, settle,
    delivery suspended) before the second job starts - which is exactly the shape
    the audit named: another task still owes that page.  Whether the second job
    links the parked delivery or not, the page must never appear as a save.
    """
    path = tmp_path / "library.sqlite3"
    with LibraryRepository(path) as repo:
        first = _submit(repo, request_id="first")
        ids = _accept(repo, first, artwork="702")
        assert repo.claim_delivery(ids[0], first["batch_id"])
        repo.request_stop(first["job_id"], "pause")
        # The claimed page is retained work, so the batch only settles once the
        # page is released back to the job: it stays owed, it is not abandoned.
        repo.release_delivery(ids[0], first["batch_id"], state="suspended")
        assert repo.settle_stop(first["batch_id"])
        assert repo.get_delivery(ids[0])["state"] in {"suspended", "pending", "retry_wait"}
        # A different output root, because the parked page still reserves its
        # path: the product refuses a second writer for the same file.
        second = _submit(repo, request_id="second", output_root="second-root")
        _accept(repo, second, artwork="702", root="second-root")
        theirs = _facts(path, first["job_id"])
        ours = _facts(path, second["job_id"])
    assert [entry["state"] for entry in theirs["pages"]["observed"]] == ["pending_verify"]
    assert theirs["pages"]["observed"][0]["origin_job"] == "self"
    saving = {"saved_new", "already_verified"}
    assert [entry for entry in ours["pages"]["observed"] if entry["state"] in saving] == []
    counts = coverage_counts(ours)
    assert counts["counts"]["pages_saved_new"] == 0
    assert counts["counts"]["pages_already_verified"] == 0
    assert counts["delivery_evidence"]["state"] != "complete"


def test_a_second_generation_with_other_bytes_is_reported_and_parked(tmp_path):
    """Two generations of one page: both are reported, the engine parks it.

    The synthetic generation is hung on the real delivery/artifact rows (see
    _second_generation); the public resume path carries the same delivery, so
    this shape cannot be produced through the API today.
    """
    path = tmp_path / "library.sqlite3"
    with LibraryRepository(path) as repo:
        job = _submit(repo, request_id="two-generations")
        ids = _accept(repo, job)
        run_page(repo, job, ids[0], executor(repo, tmp_path, Transport(Response())))
        resumed = _resume(repo, job)
        _accept(repo, job, artwork="900", batch_id=resumed["batch_id"])
        _second_generation(
            path, job["job_id"], digest="b" * 64, batch_id=resumed["batch_id"]
        )
        facts = _facts(path, job["job_id"])
    observed = facts["pages"]["observed"]
    assert len({entry["content_sha256"] for entry in observed}) == 2
    counts = coverage_counts(facts)
    assert counts["counts"]["duplicate_pages"] == 1
    assert "page_content_conflict:1" in counts["reasons"]
    coverage = build_coverage(facts, scope={"kind": "follow_job", "job_id": job["job_id"]})
    assert coverage.delivery["state"] != "complete"
    # The page evidence itself: the two generations contradict each other, and
    # no expected content version exists to settle which one is required.
    assert counts["delivery_evidence"]["conflict"] == 1
    # A page carrying two versions is parked, and a parked page is a missing
    # page: "we cannot tell which of the two was required" is not a delivery.
    assert counts["delivery_evidence"]["state"] == "incomplete"
    assert counts["delivery_evidence"]["reason"] == "pages_not_delivered:1"
    assert counts["delivery_evidence"]["missing"] == 1


def test_a_second_generation_with_the_same_bytes_collapses_to_one_observation(tmp_path):
    """Two rows, one save: the same digest must not be counted as a conflict."""
    path = tmp_path / "library.sqlite3"
    with LibraryRepository(path) as repo:
        job = _submit(repo, request_id="same-bytes")
        ids = _accept(repo, job)
        run_page(repo, job, ids[0], executor(repo, tmp_path, Transport(Response())))
        facts_before = _facts(path, job["job_id"])
        digest = facts_before["pages"]["observed"][0]["content_sha256"]
        resumed = _resume(repo, job)
        _accept(repo, job, artwork="900", batch_id=resumed["batch_id"])
        _second_generation(path, job["job_id"], digest=digest, batch_id=resumed["batch_id"])
        facts = _facts(path, job["job_id"])
    assert len(facts["pages"]["observed"]) == 1
    counts = coverage_counts(facts)
    assert counts["counts"]["duplicate_pages"] == 0
    assert not [reason for reason in counts["reasons"] if reason.startswith("page_content_conflict")]


# ---------------------------------------------------------------------------
# query shape and scale
# ---------------------------------------------------------------------------


def test_the_batch_queries_survive_more_batches_than_one_in_list(tmp_path, monkeypatch):
    """The IN(...) lists are chunked; a job with many batches must still read.

    Chunk size is patched to 1 so the boundary is exercised without building
    hundreds of batches: every batch becomes its own query.
    """
    from core import follow_facts_v2 as facts_module

    path = tmp_path / "library.sqlite3"
    with LibraryRepository(path) as repo:
        job = _submit(repo, request_id="chunked")
        _accept(repo, job)
        resumed = _resume(repo, job)
        _accept(repo, job, artwork="900", batch_id=resumed["batch_id"])
        with sqlite3.connect(path) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM batches WHERE job_id = ?", (job["job_id"],)
            ).fetchone()[0] == 2
    monkeypatch.setattr(facts_module, "_SQL_CHUNK", 1)
    facts = _facts(path, job["job_id"])
    assert facts["run"]["totals"]["works_seen"] == 1
    assert facts["run"]["totals"]["works_multi_batch"] == 1
    assert len(facts["pages"]["expected"]) == 1
    assert len(facts["pages"]["observed"]) == 1


def _bulk_works(path, job_id, count):
    """Batch rows for a size test, written straight into the batch_items table.

    The public acceptance path is bounded by the batch budget (max_works), and a
    ten-thousand-work job is a *reading* question here, not an admission one.
    """
    with sqlite3.connect(path) as connection:
        batch_id = connection.execute(
            "SELECT batch_id FROM batches WHERE job_id = ?", (job_id,)
        ).fetchone()[0]
        now = "2026-11-17T00:00:00+00:00"
        connection.executemany(
            "INSERT INTO batch_items(batch_id, illust_id, artist_id, page_count, decision, "
            "counts_work, reason_codes_json, required_pages_json, result_state, bookmarks, "
            "ai_flag, provenance_json, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (batch_id, str(900000 + index), str(100 + index % 50), 1, "download", 1,
                 "[]", "[0]", "completed", None, None, "{}", now, now)
                for index in range(count)
            ],
        )
        connection.commit()


def test_a_thousand_works_read_through_the_public_path(tmp_path):
    path = tmp_path / "library.sqlite3"
    with LibraryRepository(path) as repo:
        job = _submit(repo, request_id="scale-1k", works=1000)
        candidates = tuple(_candidate(str(500000 + i), 1, artist=str(100 + i % 50)) for i in range(1000))
        repo.accept_source_page(
            job["batch_id"],
            SourcePage("follow", "history", "100", "scale-1k", candidates, exhausted=True),
        )
    started = time.perf_counter()
    facts = _facts(path, job["job_id"])
    elapsed = time.perf_counter() - started
    assert facts["run"]["totals"]["works_seen"] == 1000
    assert len(facts["pages"]["expected"]) == 1000
    assert len(facts["artists"]) == 50
    assert elapsed < 30, "reading 1000 works took %.1fs" % elapsed


def test_ten_thousand_works_stay_affordable(tmp_path):
    path = tmp_path / "library.sqlite3"
    with LibraryRepository(path) as repo:
        job = _submit(repo, request_id="scale-10k", works=10)
        _accept(repo, job, artwork="950000")
    _bulk_works(path, job["job_id"], 10000)
    tracemalloc.start()
    started = time.perf_counter()
    try:
        facts = _facts(path, job["job_id"])
    finally:
        elapsed = time.perf_counter() - started
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
    assert facts["run"]["totals"]["works_seen"] == 10001
    assert len(facts["pages"]["expected"]) == 10001
    assert len(facts["artists"]) == 51
    assert elapsed < 60, "reading 10001 works took %.1fs" % elapsed
    assert peak < 400 * 1024 * 1024, "peak memory %.0f MiB" % (peak / 1024 / 1024)


# ---------------------------------------------------------------------------
# the two entry points must agree
# ---------------------------------------------------------------------------


def test_the_desktop_summary_and_the_cli_report_agree(tmp_path):
    """One synthetic library, two entry points, the same numbers and reasons."""
    from types import SimpleNamespace

    from core.collection_desktop import DesktopRuntime

    logs = tmp_path / "download_logs"
    logs.mkdir()
    path = logs / "library.sqlite3"
    with LibraryRepository(path) as repo:
        job = _submit(repo, request_id="agree")
        follow_snapshot(repo, job, 7, 8)
        _accept(repo, job, artwork="710", artist="7")
    runtime = object.__new__(DesktopRuntime)
    runtime.attach_paths(SimpleNamespace(download_logs_dir=logs))
    summary = runtime._coverage_summary(job_id=job["job_id"])
    cli = coverage_report_v2(path, job["job_id"])
    assert summary["provider"] == "v2"
    assert summary["exit_code"] == cli["exit_code"]
    assert summary["report"]["coverage"]["denominators"] == cli["coverage"]["denominators"]
    assert summary["report"]["coverage"]["counts"] == cli["coverage"]["counts"]
    assert summary["report"]["coverage"]["reasons"] == cli["coverage"]["reasons"]
    assert cli["coverage"]["denominators"]["artists_total"] == 2
