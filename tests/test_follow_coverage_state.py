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
    return {"artist_id": artist_id, "state": state, "pages": pages, "newest_illust_id": ""}


def test_a_finished_walk_is_complete_on_all_three_dimensions():
    states = coverage_states(
        facts(artists=[artist("a-1"), artist("a-2")], totals={"excluded_by_filter": 4, "unknown_fields": 0, "pages_saved": 9})
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
        facts(artists=[artist("a-1"), artist("a-2", state="unfollowed")], totals={"pages_saved": 3})
    )
    assert states["enumeration"]["state"] == "complete"
    assert states["delivery"]["state"] == "complete"


def test_the_three_dimensions_stay_independent():
    states = coverage_states(
        facts(
            # Nothing was saved, yet the walk itself finished and the filter
            # still met unknown fields: three different answers at once.
            artists=[artist("a-1", pages=0)],
            totals={"unknown_fields": 2, "pages_saved": 0},
        )
    )
    # The filter met unknown fields while the walk and the delivery finished.
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


def test_nothing_saved_is_missing_delivery():
    states = coverage_states(facts(artists=[artist("a-1", pages=0)], totals={"pages_saved": 0}))
    assert states["delivery"] == {"state": "missing", "reason": "nothing_saved"}


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
    states = coverage_states(facts(artists=[artist("a-1")], totals={"excluded_by_filter": 1, "pages_saved": 1}))
    assert states["enumeration"]["state"] in ENUMERATION_STATES
    assert states["decision"]["state"] in DECISION_STATES
    assert states["delivery"]["state"] in DELIVERY_STATES
    assert set(states) == {"enumeration", "decision", "delivery"}
    for entry in states.values():
        assert set(entry) == {"state", "reason"}
