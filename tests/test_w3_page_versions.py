"""EA-03: a page proves delivery only against the *required* content version.

Independent tests for the W3 fix (audit finding EA-03, contract S0 §3).  Every
case is synthetic: plain fact dicts, no database, no network, no real library.

The vocabulary under test (core.follow_coverage_counts):

    expected   page_identity -> required content version | None
    observed   page_identity -> {state, versions}
    delivered  state proves a save AND exactly one observed version, equal to
               the required one; anything else is a named gap, never a guess.

The point of the whole file is that "a page exists with some version" and
"the page that was required exists" are different statements, and that only
the second one may end as delivery=complete.
"""
from __future__ import annotations

import pytest

from core.follow_coverage import build_coverage
from core.follow_coverage_counts import coverage_counts
from core.follow_coverage_state import coverage_states

NEW = "b" * 64
OLD = "a" * 64
SCOPE = {"kind": "follow_run", "run_id": "run-synthetic"}


def facts(*, pages, artists=None, totals=None, status="completed"):
    """One run's facts.  The page evidence is the only varying part."""
    return {
        "run": {
            "run_id": "run-synthetic",
            "status": status,
            "stop_reason": "",
            "totals": dict(
                totals
                or {"artists_total": 1, "works_seen": 1, "excluded_by_filter": 0}
            ),
        },
        "artists": artists or [{"artist_id": "a-1", "state": "completed", "works_seen": 1}],
        "incomplete": [],
        "budget": None,
        "pages": pages,
    }


def page(identity, *, state="saved_new", version=NEW):
    row = {"page_identity": identity, "state": state}
    if version is not None:
        row["content_sha256"] = version
    return row


def required(identity, version):
    row = {"page_identity": identity}
    if version is not None:
        row["content_sha256"] = version
    return row


def judge(pages, **kwargs):
    payload = facts(pages=pages, **kwargs)
    report = coverage_counts(payload)
    return payload, report, coverage_states(payload, counts=report)


# --------------------------------------------------------------- the positive case
def test_the_required_version_that_was_saved_is_a_complete_delivery():
    payload, report, states = judge(
        {"expected": [required("w#0", NEW)], "observed": [page("w#0", version=NEW)]}
    )
    assert states["delivery"] == {"state": "complete", "reason": None}
    evidence = report["delivery_evidence"]
    assert (evidence["expected"], evidence["missing"], evidence["delivered"]) == (1, 0, 1)
    assert report["counts"]["pages_expected"] == 1
    assert report["counts"]["pages_saved_new"] == 1


def test_a_stored_page_count_is_not_what_proves_delivery():
    """The legacy total is displayed, never used as the proof."""
    payload, report, states = judge(
        {"expected": [required("w#0", NEW)], "observed": [page("w#0", version=NEW)]},
        totals={"artists_total": 1, "works_seen": 1, "excluded_by_filter": 0, "pages_saved": 9},
    )
    assert states["delivery"]["state"] == "complete"
    assert report["counts"]["pages_saved"] == 9


# ------------------------------------------------------------- the version mismatch
def test_only_the_old_version_is_not_a_delivery():
    _, report, states = judge(
        {"expected": [required("w#0", NEW)], "observed": [page("w#0", version=OLD)]}
    )
    # "the required page is not there" is still true, but the reason must say
    # why: something *was* saved, with another version.
    assert states["delivery"] == {"state": "missing", "reason": "pages_content_mismatch:1"}
    evidence = report["delivery_evidence"]
    assert evidence["stale"] == 1 and evidence["delivered"] == 0
    assert "page_content_mismatch:1" in report["reasons"]


def test_an_already_verified_page_must_still_carry_the_required_version():
    for state in ("saved_new", "already_verified"):
        _, report, states = judge(
            {
                "expected": [required("w#0", NEW)],
                "observed": [page("w#0", state=state, version=OLD)],
            }
        )
        assert states["delivery"]["state"] != "complete", state
        assert report["delivery_evidence"]["stale"] == 1, state


def test_one_matching_and_one_stale_page_is_never_complete():
    _, report, states = judge(
        {
            "expected": [required("w#0", NEW), required("w#1", NEW)],
            "observed": [page("w#0", version=NEW), page("w#1", version=OLD)],
        }
    )
    assert states["delivery"]["state"] == "partial"
    assert states["delivery"]["reason"] == "pages_content_mismatch:1"
    assert report["delivery_evidence"]["delivered"] == 1
    assert report["delivery_evidence"]["stale"] == 1


# ----------------------------------------------------------- two versions, one page
@pytest.mark.parametrize("order", [(OLD, NEW), (NEW, OLD)])
def test_two_versions_of_one_page_conflict_and_the_list_order_decides_nothing(order):
    observed = [page("w#0", version=version) for version in order]
    _, report, states = judge(
        {"expected": [required("w#0", NEW)], "observed": observed}
    )
    assert states["delivery"]["state"] != "complete"
    evidence = report["delivery_evidence"]
    # The contradiction is a fact about the observations, and the page itself is
    # parked, so it is also one missing page: not delivered, not "unverified".
    assert (evidence["conflict"], evidence["missing"], evidence["delivered"]) == (1, 1, 0)
    assert "page_content_conflict:1" in report["reasons"]
    # The page is parked, not counted as a save: "one of them matches" is not
    # a proof, and picking the newer one by list position would be a guess.
    assert report["counts"]["pages_pending_verify"] == 1
    assert report["counts"]["pages_saved_new"] == 0


def test_two_versions_and_a_different_state_are_both_reported():
    _, report, states = judge(
        {
            "expected": [required("w#0", NEW)],
            "observed": [page("w#0", version=OLD), page("w#0", state="failed", version=NEW)],
        }
    )
    assert states["delivery"]["state"] != "complete"
    assert "page_state_conflict:1" in report["reasons"]
    assert "page_content_conflict:1" in report["reasons"]


def test_many_versions_of_one_page_are_one_conflict_not_four():
    observed = [page("w#0", version=version) for version in ("v1", "v2", "v3", "v4")]
    _, report, states = judge(
        {"expected": [required("w#0", "v4")], "observed": observed}
    )
    assert states["delivery"]["state"] != "complete"
    assert "page_content_conflict:1" in report["reasons"]
    assert report["counts"]["duplicate_pages"] == 3


# ------------------------------------------------------------ the missing requirement
def test_a_required_page_without_a_version_cannot_be_called_complete():
    _, report, states = judge(
        {"expected": [required("w#0", None)], "observed": [page("w#0", version=NEW)]}
    )
    assert states["delivery"] == {
        "state": "unknown",
        "reason": "expected_content_version_missing:1",
    }
    assert report["delivery_evidence"]["unpinned"] == 1
    # The page is not parked as "awaiting verification": nothing about it is
    # pending, the *requirement* is the missing half, so it stays a saved page.
    assert report["counts"]["pages_pending_verify"] == 0
    assert report["counts"]["pages_saved_new"] == 1


def test_a_bare_identity_in_the_expected_set_is_an_unpinned_requirement():
    """The old shape is still read; it just cannot prove a version."""
    _, report, states = judge(
        {"expected": ["w#0"], "observed": [page("w#0", version=NEW)]}
    )
    assert states["delivery"]["state"] == "unknown"
    assert report["delivery_evidence"]["unpinned"] == 1


def test_an_observation_without_a_version_cannot_prove_the_requirement():
    _, report, states = judge(
        {"expected": [required("w#0", NEW)], "observed": [page("w#0", version=None)]}
    )
    assert states["delivery"] == {
        "state": "unknown",
        "reason": "observed_content_version_missing:1",
    }
    assert report["delivery_evidence"]["unversioned"] == 1


def test_two_expected_rows_that_disagree_leave_the_page_unpinned():
    _, report, states = judge(
        {
            "expected": [required("w#0", NEW), required("w#0", OLD)],
            "observed": [page("w#0", version=NEW)],
        }
    )
    assert states["delivery"]["state"] == "unknown"
    assert "expected_content_conflict:1" in report["reasons"]
    assert report["counts"]["pages_expected"] == 1


def test_two_expected_rows_with_the_same_version_are_one_page():
    _, report, states = judge(
        {
            "expected": [required("w#0", NEW), required("w#0", NEW)],
            "observed": [page("w#0", version=NEW)],
        }
    )
    assert report["counts"]["pages_expected"] == 1
    assert states["delivery"]["state"] == "complete"


# ---------------------------------------------------------------- repeats and states
def test_the_same_version_seen_twice_is_one_page_and_still_complete():
    _, report, states = judge(
        {
            "expected": [required("w#0", NEW)],
            "observed": [page("w#0", version=NEW), page("w#0", version=NEW)],
        }
    )
    assert states["delivery"] == {"state": "complete", "reason": None}
    assert report["counts"]["duplicate_pages"] == 1
    assert report["counts"]["pages_expected"] == 1


@pytest.mark.parametrize("state", ["failed", "pending_verify"])
def test_a_page_that_did_not_finish_saving_is_a_gap_not_a_delivery(state):
    _, report, states = judge(
        {"expected": [required("w#0", NEW)], "observed": [page("w#0", state=state, version=NEW)]}
    )
    assert states["delivery"]["state"] == "missing"
    assert report["delivery_evidence"]["delivered"] == 0


def test_an_expected_page_nobody_observed_is_missing():
    _, report, states = judge({"expected": [required("w#0", NEW)], "observed": []})
    assert states["delivery"] == {"state": "missing", "reason": "pages_not_delivered:1"}
    assert report["delivery_evidence"]["missing"] == 1


# ------------------------------------------------------------------- the invariants
def test_the_delivery_buckets_partition_the_expected_pages():
    _, report, _states = judge(
        {
            "expected": [
                required("w#0", NEW),
                required("w#1", NEW),
                required("w#2", None),
                required("w#3", NEW),
            ],
            "observed": [
                page("w#0", version=NEW),
                page("w#1", version=OLD),
                page("w#2", version=OLD),
                page("w#3", version=None),
            ],
        }
    )
    evidence = report["delivery_evidence"]
    parts = (
        evidence["delivered"],
        evidence["missing"],
        evidence["unpinned"],
        evidence["unversioned"],
    )
    assert sum(parts) == evidence["expected"] == 4
    assert (evidence["stale"], evidence["conflict"]) == (1, 0)


def test_no_page_evidence_at_all_is_still_unknown():
    _, report, states = judge(None)
    assert states["delivery"] == {
        "state": "unknown",
        "reason": "page_evidence_missing:pages_expected",
    }
    assert report["delivery_evidence"]["expected"] is None


def test_an_empty_requirement_is_still_vacuously_complete():
    _, _report, states = judge({"expected": [], "observed": []})
    assert states["delivery"] == {"state": "complete", "reason": None}


def test_the_new_reasons_stay_sorted_and_unique():
    _, report, _states = judge(
        {
            "expected": [required("w#0", NEW), required("w#0", OLD), required("w#1", None)],
            "observed": [page("w#0", version=OLD), page("w#1", version=NEW)],
        }
    )
    assert report["reasons"] == sorted(set(report["reasons"]))
    assert "expected_content_conflict:1" in report["reasons"]


# ------------------------------------------------------------- through the whole chain
def test_the_whole_chain_reaches_complete_only_with_a_matching_version():
    good = facts(pages={"expected": [required("w#0", NEW)], "observed": [page("w#0", version=NEW)]})
    assert build_coverage(good, scope=SCOPE).overall == "complete"

    stale = facts(
        pages={"expected": [required("w#0", NEW)], "observed": [page("w#0", version=OLD)]}
    )
    coverage = build_coverage(stale, scope=SCOPE)
    assert coverage.delivery["state"] != "complete"
    assert coverage.overall != "complete"
    assert "page_content_mismatch:1" in coverage.reasons

    unpinned = facts(
        pages={"expected": [required("w#0", None)], "observed": [page("w#0", version=NEW)]}
    )
    coverage = build_coverage(unpinned, scope=SCOPE)
    assert coverage.delivery["state"] == "unknown"
    assert coverage.overall != "complete"


def test_the_summary_names_the_content_mismatch_instead_of_only_counting_pages():
    payload = facts(
        pages={
            "expected": [required("w#0", NEW)],
            "observed": [page("w#0", version=OLD)],
        }
    )
    coverage = build_coverage(payload, scope=SCOPE)
    assert "page_content_mismatch:1" in coverage.reasons
    # The human line keeps the delivery state ("yet to be saved": the required
    # page is not there) but now says which page and why.
    assert "pages_content_mismatch:1" in coverage.text()
