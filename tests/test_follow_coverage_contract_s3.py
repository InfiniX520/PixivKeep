"""S3 contract tests: one fact DTO, works separated from image pages.

Covers the five steps of the S3 work package:

1. the DTO names one field per fact and the aliases are normalised at the entry
   (two spellings that disagree are refused, never guessed);
2. work counting and page delivery are reduced separately;
3. a delivery is complete only when the expected page set is known and every
   page in it has a matching content version;
4. the truth table for multi-page, all-existing, partial failure, unknown page
   count, exclusion, duplicate pages, budget pause and permission change;
5. a whole chain over a real crawl-state repository: store -> source ->
   gather_facts -> build_coverage -> render, positive and negative.

Every expectation here is written from the contract, not from the code under
test: the numbers are chosen so that a wrong formula gives a different answer.
"""
from __future__ import annotations

import pytest

from core.follow_coverage import build_coverage
from core.follow_coverage_counts import coverage_counts
from core.follow_coverage_state import coverage_states
from core.follow_crawl_state import FollowArtistSnapshot, FollowCrawlStateRepository
from core.follow_facts import StateSource, gather_facts

SCOPE = {"kind": "follow_run"}


def facts(*, artists=None, totals=None, status="completed", stop="", pages=None):
    run = {"run_id": "r1", "status": status, "stop_reason": stop, "totals": dict(totals or {})}
    payload = {
        "run": run,
        "artists": list(
            artists
            if artists is not None
            else [{"artist_id": "a", "state": "completed", "works_seen": 1}]
        ),
        "incomplete": [],
        "budget": None,
    }
    if pages is not None:
        payload["pages"] = pages
    return payload


def page(identity, state="saved_new", content="sha"):
    return {"page_identity": identity, "state": state, "content_sha256": content}


# --------------------------------------------------------------- 1. the DTO


class _Row:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _Source:
    def __init__(self, run, artists):
        self.run = run
        self._artists = artists

    def artists(self, run_id):
        return self._artists


@pytest.mark.parametrize("spelling", ["total_artists", "artists_total"])
def test_the_run_total_is_accepted_under_either_spelling(spelling):
    source = _Source(
        _Row(run_id="r", status="completed", **{spelling: 2}),
        [
            {"artist_id": "a", "state": "completed", "works_seen": 1},
            {"artist_id": "b", "state": "completed", "works_seen": 1},
        ],
    )
    assert gather_facts(source)["run"]["totals"]["artists_total"] == 2


def test_two_different_run_totals_are_refused_instead_of_guessed():
    source = _Source(
        _Row(run_id="r", status="completed", total_artists=2, artists_total=3),
        [{"artist_id": "a", "state": "completed", "works_seen": 1}],
    )
    with pytest.raises(ValueError) as caught:
        gather_facts(source)
    assert "total_artists" in str(caught.value)


@pytest.mark.parametrize("spelling", ["works_seen", "pages"])
def test_each_artist_work_count_is_accepted_under_either_spelling(spelling):
    source = _Source(
        _Row(run_id="r", status="completed", total_artists=1),
        [{"artist_id": "a", "state": "completed", spelling: 7}],
    )
    artist = gather_facts(source)["artists"][0]
    assert artist["works_seen"] == 7
    assert "pages" not in artist


def test_two_different_artist_work_counts_are_refused():
    source = _Source(
        _Row(run_id="r", status="completed", total_artists=1),
        [{"artist_id": "a", "state": "completed", "works_seen": 3, "pages": 9}],
    )
    with pytest.raises(ValueError) as caught:
        gather_facts(source)
    assert "works_seen" in str(caught.value)


# --------------------------------------------------- 2/3/4 the truth table


@pytest.mark.parametrize(
    "case, pages, works, expected_state, expected_reason, expected_pages",
    [
        (
            "one work, three pages, all delivered with a version",
            {
                "expected": ["w#0", "w#1", "w#2"],
                "observed": [page("w#0"), page("w#1"), page("w#2")],
            },
            1,
            "complete",
            None,
            {"pages_expected": 3, "pages_saved_new": 3},
        ),
        (
            "one work, three pages, one delivered",
            {"expected": ["w#0", "w#1", "w#2"], "observed": [page("w#0")]},
            1,
            "partial",
            "pages_not_delivered:2",
            {"pages_expected": 3, "pages_saved_new": 1},
        ),
        (
            "two new pages plus one already verified",
            {
                "expected": ["w#0", "w#1", "w#2"],
                "observed": [
                    page("w#0"),
                    page("w#1"),
                    page("w#2", state="already_verified"),
                ],
            },
            1,
            "complete",
            None,
            {"pages_saved_new": 2, "pages_already_verified": 1},
        ),
        (
            "everything was already verified, nothing new",
            {"expected": ["w#0"], "observed": [page("w#0", state="already_verified")]},
            1,
            "complete",
            None,
            {"pages_saved_new": 0, "pages_already_verified": 1},
        ),
        (
            "expected pages but nothing delivered at all",
            {"expected": ["w#0", "w#1"], "observed": []},
            1,
            "missing",
            "pages_not_delivered:2",
            {"pages_expected": 2},
        ),
        (
            "no expected page set: unknown, not complete",
            None,
            1,
            "unknown",
            "page_evidence_missing:pages_expected",
            {"pages_expected": None},
        ),
        (
            "one page observed twice with the same answer counts once",
            {"expected": ["w#0"], "observed": [page("w#0"), page("w#0")]},
            1,
            "complete",
            None,
            {"pages_saved_new": 1, "duplicate_pages": 1},
        ),
        (
            "a failed page is not a delivered page",
            {"expected": ["w#0", "w#1"], "observed": [page("w#0", state="failed")]},
            1,
            "missing",
            "pages_not_delivered:2",
            {"pages_failed": 1},
        ),
        (
            "a page waiting for another look is not a delivered page",
            {"expected": ["w#0"], "observed": [page("w#0", state="pending_verify")]},
            1,
            "missing",
            "pages_not_delivered:1",
            {"pages_pending_verify": 1},
        ),
    ],
)
def test_delivery_truth_table(
    case, pages, works, expected_state, expected_reason, expected_pages
):
    payload = facts(
        artists=[{"artist_id": "a", "state": "completed", "works_seen": works}],
        totals={"artists_total": 1, "works_seen": works, "excluded_by_filter": 0},
        pages=pages,
    )
    report = coverage_counts(payload)
    assert coverage_states(payload, counts=report)["delivery"] == {
        "state": expected_state,
        "reason": expected_reason,
    }, case
    for key, value in expected_pages.items():
        assert report["counts"][key] == value, (case, key)


def test_excluding_works_creates_no_pages():
    payload = facts(
        artists=[{"artist_id": "a", "state": "completed", "works_seen": 10}],
        totals={"artists_total": 1, "works_seen": 10, "excluded_by_filter": 7},
        pages={"expected": [], "observed": []},
    )
    report = coverage_counts(payload)
    assert report["counts"]["works_deliverable"] == 3
    assert report["counts"]["pages_expected"] == 0
    assert coverage_states(payload, counts=report)["delivery"]["state"] == "complete"


def test_a_budget_pause_is_a_stopped_walk_and_an_incomplete_delivery():
    payload = facts(
        # One artist finished and one is still pending: the pause is a known gap.
        artists=[
            {"artist_id": "a", "state": "completed", "works_seen": 2},
            {"artist_id": "b", "state": "pending", "works_seen": 0},
        ],
        totals={"artists_total": 2},
        status="paused",
        stop="budget",
        pages={"expected": ["w#0"], "observed": [page("w#0")]},
    )
    states = coverage_states(payload)
    assert states["enumeration"] == {"state": "stopped", "reason": "budget"}
    assert states["delivery"]["state"] == "partial"


def test_a_permission_change_that_unfollows_an_artist_does_not_block():
    payload = facts(
        artists=[
            {"artist_id": "a", "state": "completed", "works_seen": 1},
            {"artist_id": "b", "state": "unfollowed", "works_seen": 0},
        ],
        totals={"artists_total": 2, "works_seen": 1, "excluded_by_filter": 0},
        pages={"expected": ["w#0"], "observed": [page("w#0")]},
    )
    states = coverage_states(payload)
    assert states["enumeration"]["state"] == "complete"
    assert states["delivery"]["state"] == "complete"


def test_an_unknown_artist_state_is_not_hidden_in_a_bucket():
    report = coverage_counts(
        facts(
            artists=[{"artist_id": "a", "state": "quarantined", "works_seen": 1}],
            totals={"artists_total": 1},
        )
    )
    assert report["counts"]["artists_coverage_unverified"] == 1
    assert report["consistent"] is False
    assert "unknown_artist_state:quarantined" in report["reasons"]


# ------------------------------------------------------------- 5. whole chain


def _repository(tmp_path, artists):
    repository = FollowCrawlStateRepository(tmp_path / "crawl.sqlite3")
    run = repository.create_run(
        account_id="acct",
        config_key="cfg",
        artists=[
            FollowArtistSnapshot(artist_id=item, artist_name="n-" + item) for item in artists
        ],
        start_mode="history",
    )
    return repository, run


def _finish(repository, run_id, works):
    for artist_id, seen in works.items():
        repository.mark_artist_completed(run_id, artist_id, works_seen=seen, queued_items=0)
    repository.mark_run(run_id, "completed")


def test_chain_keeps_a_one_artist_denominator_and_counts_its_works(tmp_path):
    """UX-05: the denominator must survive gather_facts -> build_coverage."""
    repository, run = _repository(tmp_path, ["a1"])
    try:
        _finish(repository, run.run_id, {"a1": 3})
        source = StateSource(repository, run.run_id)
        gathered = gather_facts(source)
        assert gathered["run"]["totals"]["artists_total"] == 1
        assert gathered["artists"][0]["works_seen"] == 3
        coverage = build_coverage(
            gathered, scope={"kind": "follow_run", "run_id": run.run_id}
        )
        data = coverage.as_dict()
        assert data["denominators"]["artists_total"] == 1
        assert data["counts"]["artworks_seen"] == 3
        assert data["counts"]["artists_checked"] == 1
        # Crawl facts carry no page evidence, so the saving side is unknown.
        assert data["delivery"]["state"] == "unknown"
        assert coverage.overall == "unknown"
        text = coverage.text()
        assert "已查完" not in text
        assert "范围内画师总数 1 位" in text
    finally:
        repository.close()


def test_chain_reaches_complete_only_with_page_evidence(tmp_path):
    """UX-06: one saved page never proves that one work was delivered."""
    repository, run = _repository(tmp_path, ["a1"])
    try:
        _finish(repository, run.run_id, {"a1": 1})
        pages = {"expected": ["w#0", "w#1", "w#2"], "observed": [page("w#0")]}
        source = StateSource(repository, run.run_id, pages=lambda run_id: pages)
        coverage = build_coverage(
            gather_facts(source), scope={"kind": "follow_run", "run_id": run.run_id}
        )
        data = coverage.as_dict()
        assert data["delivery"]["state"] == "partial"
        assert data["delivery"]["reason"] == "pages_not_delivered:2"
        assert coverage.overall != "complete"
        assert "已查完" not in coverage.text()
    finally:
        repository.close()


def test_chain_is_complete_when_every_expected_page_has_a_version(tmp_path):
    repository, run = _repository(tmp_path, ["a1"])
    try:
        _finish(repository, run.run_id, {"a1": 1})
        pages = {"expected": ["w#0", "w#1"], "observed": [page("w#0"), page("w#1")]}
        source = StateSource(repository, run.run_id, pages=lambda run_id: pages)
        coverage = build_coverage(
            gather_facts(source), scope={"kind": "follow_run", "run_id": run.run_id}
        )
        data = coverage.as_dict()
        assert data["enumeration"]["state"] == "complete"
        assert data["delivery"]["state"] == "complete"
        assert data["counts"]["pages_expected"] == 2
        assert data["counts"]["pages_saved_new"] == 2
        # The crawl store persists no filter counters, so the decision is
        # "skipped" and the overall stays unknown: complete delivery is proven,
        # complete coverage is not.  The text must not claim otherwise.
        assert data["decision"]["state"] == "skipped"
        assert coverage.overall == "unknown"
        assert "已查完" not in coverage.text()
    finally:
        repository.close()
