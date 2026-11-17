"""The coverage summary has to carry the relations it was built from (RA-03).

The facts module proves the numbers; this file proves that the numbers a reader
sees in facts_notes say which cohort they describe, and that an unknown cohort is
named as unknown rather than printed as zero.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.library_repository import LibraryRepository
from core.report_commands import coverage_lines, coverage_report_v2


def test_a_job_without_a_recorded_snapshot_names_the_unknown_cohort(tmp_path):
    from delivery_fixtures import make_job

    path = tmp_path / "library.sqlite3"
    with LibraryRepository(path) as repo:
        job, _ids, _ = make_job(repo)
    report = coverage_report_v2(path, job["job_id"])
    notes = report["facts_notes"]
    assert notes["artists_total_unknown"] == "snapshot_selection_not_recorded"
    assert "画师总体: 未知" in notes["artist_cohort"]
    assert "0 位" not in notes["artist_cohort"].split("；")[0]
    assert notes["artists_visited"] == 1
    # The report still refuses to call this complete: an unknown denominator is
    # not a pass, whatever the page numbers look like.
    assert report["exit_code"] != 0
    text = "\n".join(coverage_lines(report))
    assert "未知" in text


def test_a_complete_snapshot_gives_the_cohort_its_real_size(tmp_path):
    from test_follow_repository import snapshot, start

    path = tmp_path / "library.sqlite3"
    with LibraryRepository(path) as repo:
        job = start(repo)
        snapshot(repo, job, 7, 8)
        job_id = job["job_id"]
    report = coverage_report_v2(path, job_id)
    notes = report["facts_notes"]
    assert notes["snapshot_scope"] == "snapshot"
    assert notes["snapshot_members"] == 2
    assert notes["artist_cohort"] == (
        "画师总体: 快照成员 2 位（活跃 %s），本任务到达 %s 位"
        % (notes["snapshot_members_active"], notes["artists_visited"])
    )
    assert "artists_total_unknown" not in notes


def test_the_page_links_travel_with_the_summary(tmp_path):
    from delivery_fixtures import make_job

    path = tmp_path / "library.sqlite3"
    with LibraryRepository(path) as repo:
        job, _ids, _ = make_job(repo)
    report = coverage_report_v2(path, job["job_id"])
    links = report["facts_notes"]["page_links"]
    assert set(links) == {
        "linked_pages",
        "artifact_only",
        "from_another_job",
        "unlinked_deliveries",
    }
    assert all(value is None or type(value) is int for value in links.values())
