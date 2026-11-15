"""The three coverage states, one rule at a time."""
from __future__ import annotations

import pytest

from core.follow_coverage_state import (
    DECISION_STATES,
    DELIVERY_STATES,
    ENUMERATION_STATES,
    CoverageStateError,
    coverage_states,
)


def facts(*, status="completed", stop="", artists=(), totals=None, **extra):
    run = {
        "run_id": "run-1",
        "account_ref": "account-1",
        "config_key": "cfg",
        "status": status,
        "stop_reason": stop,
        "totals": dict(totals or {}),
    }
    payload = {"run": run, "artists": list(artists), "incomplete": [], "budget": None}
    payload.update(extra)
    return payload


def artist(artist_id="a-1", state="completed", pages=3):
    """One artist row; the number is works seen (the DTO word is works_seen)."""
    return {"artist_id": artist_id, "state": state, "works_seen": pages, "newest_illust_id": ""}


def page(identity="w-1#0", state="saved_new", content="sha-1"):
    return {"page_identity": identity, "state": state, "content_sha256": content}


def delivered(*identities):
    """Page evidence where every given identity was required *and* delivered.

    此用例原先按旧语义（身份集合）写成，2026-11-15 审计 EA-03 后按契约 §3 改为期望携带内容代次：
    expected 只给身份时无法比对内容，只能算未知，所以这里连要求一起给出。
    """
    return {
        "expected": [
            {"page_identity": item, "content_sha256": "sha-1"} for item in identities
        ],
        "observed": [page(item) for item in identities],
    }


def test_a_finished_walk_is_complete_on_all_three_dimensions():
    states = coverage_states(
        facts(
            artists=[artist("a-1"), artist("a-2")],
            totals={"works_seen": 13, "excluded_by_filter": 4, "unknown_fields": 0},
            pages=delivered("w-1#0", "w-2#0"),
        )
    )
    assert states["enumeration"] == {"state": "complete", "reason": None}
    assert states["decision"] == {"state": "applied", "reason": None}
    assert states["delivery"] == {"state": "complete", "reason": None}


def test_one_failed_artist_makes_the_enumeration_partial_and_names_it():
    states = coverage_states(facts(artists=[artist("a-1"), artist("a-2", state="failed")]))
    assert states["enumeration"]["state"] == "partial"
    assert states["enumeration"]["reason"] == "artist_failed:a-2"


def test_a_paused_run_is_stopped_not_partial():
    states = coverage_states(facts(status="paused", stop="budget", artists=[artist("a-1")]))
    assert states["enumeration"] == {"state": "stopped", "reason": "budget"}


def test_a_failed_run_is_failed_and_keeps_the_reason():
    states = coverage_states(facts(status="failed", stop="network", artists=[artist("a-1")]))
    assert states["enumeration"] == {"state": "failed", "reason": "network"}


def test_a_partial_run_status_is_partial():
    states = coverage_states(facts(status="partial", stop="画师任务异常", artists=[artist("a-1")]))
    assert states["enumeration"]["state"] == "partial"
    assert "画师任务异常" in states["enumeration"]["reason"]


def test_a_running_walk_is_partial_and_says_why():
    states = coverage_states(facts(status="running", artists=[artist("a-1", state="running")]))
    assert states["enumeration"] == {"state": "partial", "reason": "artist_running:a-1"}


def test_unfollowed_artists_never_block_completion():
    states = coverage_states(
        facts(
            artists=[artist("a-1"), artist("a-2", state="unfollowed")],
            totals={"works_seen": 3, "excluded_by_filter": 0},
            pages=delivered("w-1#0", "w-2#0", "w-3#0"),
        )
    )
    assert states["enumeration"]["state"] == "complete"
    assert states["delivery"]["state"] == "complete"


def test_the_three_dimensions_stay_independent():
    states = coverage_states(
        facts(
            # Nothing was saved, yet the walk itself finished and the filter
            # still met unknown fields: three different answers at once.
            artists=[artist("a-1", pages=0)],
            totals={"unknown_fields": 2, "works_seen": 5, "excluded_by_filter": 2},
            # Two pages were expected and neither was delivered, while the walk
            # itself finished and the filter met unknown fields: three answers.
            pages={"expected": ["w-1#0", "w-1#1"], "observed": []},
        )
    )
    assert states["decision"] == {"state": "unknown_fields", "reason": "unknown_fields:2"}
    assert states["enumeration"]["state"] == "complete"
    assert states["delivery"]["state"] == "missing"


def test_a_supplied_dimension_is_used_verbatim():
    states = coverage_states(
        facts(
            artists=[artist("a-1")],
            decision={"state": "skipped", "reason": "user_disabled_filter"},
            delivery={"state": "partial", "reason": "one_page_unverified"},
        )
    )
    assert states["decision"] == {"state": "skipped", "reason": "user_disabled_filter"}
    assert states["delivery"] == {"state": "partial", "reason": "one_page_unverified"}


def test_with_no_filter_facts_the_decision_is_skipped_not_applied():
    states = coverage_states(facts(artists=[artist("a-1")]))
    assert states["decision"] == {"state": "skipped", "reason": "no_filter_facts"}


def test_an_empty_expected_page_set_with_evidence_is_complete_delivery():
    states = coverage_states(
        facts(
            artists=[artist("a-1", pages=0)],
            totals={"works_seen": 4, "excluded_by_filter": 4},
            pages={"expected": [], "observed": []},
        )
    )
    # Nothing should have been saved, and the evidence says so: complete.
    assert states["delivery"] == {"state": "complete", "reason": None}


def test_no_page_evidence_is_unknown_not_complete():
    states = coverage_states(facts(artists=[artist("a-1")], totals={}))
    assert states["delivery"] == {
        "state": "unknown",
        "reason": "page_evidence_missing:pages_expected",
    }


def test_expected_pages_that_were_never_delivered_are_missing():
    states = coverage_states(
        facts(
            artists=[artist("a-1")],
            totals={"works_seen": 30, "excluded_by_filter": 10},
            pages={"expected": ["w-1#0", "w-2#0", "w-3#0"], "observed": []},
        )
    )
    assert states["delivery"] == {"state": "missing", "reason": "pages_not_delivered:3"}


def test_a_partial_delivery_is_partial_and_names_the_count():
    states = coverage_states(
        facts(
            artists=[artist("a-1")],
            totals={"works_seen": 30, "excluded_by_filter": 10},
            pages={
                "expected": ["w-1#0", "w-2#0", "w-3#0"],
                "observed": [page("w-1#0")],
            },
        )
    )
    assert states["delivery"] == {"state": "partial", "reason": "pages_not_delivered:2"}


def test_a_known_gap_outranks_the_unmeasured_saving_side():
    states = coverage_states(facts(artists=[artist("a-1", state="pending", pages=0)], totals={}))
    assert states["delivery"]["state"] == "partial"
    assert states["delivery"]["reason"] == "artist_pending:a-1"


def test_works_and_pages_alone_cannot_prove_a_delivery():
    """One work, one saved page, no page set: that is unproven, not complete."""
    states = coverage_states(
        facts(artists=[artist("a-1")], totals={"works_seen": 1, "excluded_by_filter": 0, "pages_saved": 1})
    )
    assert states["delivery"]["state"] == "unknown"


def test_one_work_saved_as_three_pages_can_be_complete():
    """Three pages of one work: the page set proves it, the work count does not."""
    states = coverage_states(
        facts(
            artists=[artist("a-1", pages=1)],
            totals={"works_seen": 1, "excluded_by_filter": 0, "pages_saved": 3},
            pages=delivered("w-1#0", "w-1#1", "w-1#2"),
        )
    )
    assert states["delivery"] == {"state": "complete", "reason": None}


def test_a_legacy_pages_saved_total_is_irrelevant_to_completeness():
    # The crawl total is missing here; the page evidence is what decides.
    states = coverage_states(
        facts(artists=[artist("a-1")], totals={"works_seen": 1}, pages=delivered("w-1#0"))
    )
    assert states["delivery"] == {"state": "complete", "reason": None}


def test_a_page_without_a_content_version_is_not_delivered():
    # 此用例原先按旧语义（身份集合）写成，2026-11-15 审计 EA-03 后按契约 §3 改为期望携带内容代次；
    # 判据同时收紧：页面确实存了、只是证明不了版本，所以是 unknown，不是 missing。
    states = coverage_states(
        facts(
            artists=[artist("a-1")],
            pages={
                "expected": [{"page_identity": "w-1#0", "content_sha256": "sha-1"}],
                "observed": [page("w-1#0", content="")],
            },
        )
    )
    assert states["delivery"] == {
        "state": "unknown",
        "reason": "observed_content_version_missing:1",
    }


def test_a_page_without_any_content_version_on_either_side_is_unknown():
    """Neither side names a version: still unknown - never missing, never complete.

    missing would claim nothing was saved (something was), and complete would
    claim the required bytes are there (nobody said which bytes were required).
    """
    states = coverage_states(
        facts(
            artists=[artist("a-1")],
            pages={"expected": ["w-1#0"], "observed": [page("w-1#0", content="")]},
        )
    )
    assert states["delivery"] == {
        "state": "unknown",
        "reason": "expected_content_version_missing:1",
    }
    assert states["delivery"]["state"] != "missing"
    assert states["delivery"]["state"] != "complete"


def test_two_conflicting_observations_of_one_page_are_not_a_delivery():
    states = coverage_states(
        facts(
            artists=[artist("a-1")],
            pages={
                "expected": ["w-1#0"],
                "observed": [page("w-1#0", "saved_new"), page("w-1#0", "failed")],
            },
        )
    )
    assert states["delivery"]["state"] == "missing"

def test_an_unfinished_run_cannot_claim_complete_delivery():
    states = coverage_states(facts(status="running", artists=[artist("a-1", state="completed")]))
    assert states["delivery"]["state"] == "partial"
    assert states["delivery"]["reason"] == "run_not_completed"


def test_an_empty_follow_list_is_complete_enumeration_and_nothing_to_deliver():
    states = coverage_states(facts(artists=[]))
    assert states["enumeration"]["state"] == "complete"
    assert states["delivery"] == {"state": "complete", "reason": "no_artists"}
    assert states["decision"]["state"] == "skipped"


@pytest.mark.parametrize("bad", [None, 3, "facts", []])
def test_non_mapping_facts_are_refused(bad):
    with pytest.raises(CoverageStateError) as caught:
        coverage_states(bad)
    assert caught.value.code == "facts_shape_invalid"


def test_missing_run_is_refused():
    with pytest.raises(CoverageStateError) as caught:
        coverage_states({"artists": []})
    assert caught.value.code == "facts_shape_invalid"


def test_missing_artists_is_refused():
    with pytest.raises(CoverageStateError) as caught:
        coverage_states({"run": {"status": "completed"}})
    assert caught.value.code == "facts_missing_artists"


def test_an_artist_without_a_state_is_refused():
    with pytest.raises(CoverageStateError) as caught:
        coverage_states(facts(artists=[{"artist_id": "a-1"}]))
    assert caught.value.code == "artist_state_missing"


def test_a_supplied_dimension_outside_the_vocabulary_is_refused():
    with pytest.raises(CoverageStateError) as caught:
        coverage_states(facts(artists=[], decision={"state": "maybe"}))
    assert caught.value.code == "dimension_state_invalid"


def test_every_returned_state_belongs_to_its_own_vocabulary():
    states = coverage_states(facts(artists=[artist("a-1")], totals={"works_seen": 1, "excluded_by_filter": 0, "pages_saved": 1}))
    assert states["enumeration"]["state"] in ENUMERATION_STATES
    assert states["decision"]["state"] in DECISION_STATES
    assert states["delivery"]["state"] in DELIVERY_STATES
    assert set(states) == {"enumeration", "decision", "delivery"}
    for entry in states.values():
        assert set(entry) == {"state", "reason"}
