"""Offline tests for the organizer batch planner (E1-3 / E1-4, lane L1).

Every expectation is written out here; nothing is read back from the module
constant it is supposed to check.
"""
from __future__ import annotations

import copy
import dataclasses
import json

import pytest

from organizer.batches import (
    FINAL_STATES,
    OPEN_STATES,
    STATES,
    STOP_REASONS,
    ArtistCost,
    Batch,
    BatchError,
    advance,
    plan_batch,
    plan_batches,
    rebuild,
    resume,
)

POOL = ["a3", "a1", "a2", "a4", "a5"]


def make(**overrides):
    settings = {"account_id": "acct-1", "artists": 5, "requests": 100}
    settings.update(overrides)
    return plan_batch(settings.pop("pool", POOL), **settings)


def code_of(call):
    with pytest.raises(BatchError) as caught:
        call()
    return caught.value.code


def test_the_pool_order_never_changes_the_batch():
    one = plan_batch(POOL, account_id="acct-1", artists=5, requests=100)
    two = plan_batch(list(reversed(POOL)), account_id="acct-1", artists=5, requests=100)
    assert one.artist_ids == ("a1", "a2", "a3", "a4", "a5")
    assert one.fingerprint() == two.fingerprint()
    assert one.steps == two.steps


def test_finished_artists_are_not_planned_again():
    batch = plan_batch(POOL, account_id="acct-1", artists=5, requests=100, done_ids=["a2", "a4"])
    assert batch.artist_ids == ("a1", "a3", "a5")
    assert batch.stop_reason == "none"


def test_a_pool_that_is_all_finished_is_complete_with_no_work():
    batch = plan_batch(POOL, account_id="acct-1", artists=5, requests=100, done_ids=POOL)
    assert batch.artist_ids == ()
    assert batch.stop_reason == "pool_exhausted"
    assert batch.status == "complete"


def test_the_artist_cap_pauses_the_batch_and_leaves_the_rest_for_later():
    batch = make(artists=2)
    assert batch.artist_ids == ("a1", "a2")
    assert batch.stop_reason == "artists_limit"
    assert batch.status == "paused"


def test_a_request_budget_of_zero_does_no_work_and_says_so():
    batch = make(requests=0)
    assert batch.artist_ids == ()
    assert batch.stop_reason == "requests_limit"
    assert batch.status == "paused"


def test_the_request_budget_is_measured_with_the_cost_estimate():
    batch = make(requests=5, cost=ArtistCost(requests=2))
    assert batch.artist_ids == ("a1", "a2")
    assert batch.stop_reason == "requests_limit"


def test_a_byte_budget_and_a_time_budget_each_stop_the_batch():
    by_bytes = make(bytes_budget=25, cost=ArtistCost(requests=1, bytes_per_artist=10))
    assert by_bytes.artist_ids == ("a1", "a2")
    assert by_bytes.stop_reason == "bytes_limit"
    by_time = make(seconds=2.5, cost=ArtistCost(requests=1, seconds_per_artist=1.0))
    assert by_time.artist_ids == ("a1", "a2")
    assert by_time.stop_reason == "seconds_limit"


@pytest.mark.parametrize("reason", ["artists_limit", "requests_limit", "bytes_limit",
                                    "seconds_limit", "pool_exhausted", "none"])
def test_the_stop_reason_vocabulary_is_the_frozen_six(reason):
    assert reason in STOP_REASONS


def test_every_stop_reason_is_actually_reachable():
    seen = {
        make(artists=2).stop_reason,
        make(requests=0).stop_reason,
        make(bytes_budget=1, cost=ArtistCost(bytes_per_artist=10)).stop_reason,
        make(seconds=1, cost=ArtistCost(seconds_per_artist=5.0)).stop_reason,
        make(pool=[]).stop_reason,
        make().stop_reason,
    }
    assert seen == set(STOP_REASONS)


def test_the_fingerprint_moves_when_the_content_moves():
    base = make()
    assert base.fingerprint() != make(group="g1").fingerprint()
    assert base.fingerprint() != make(requests=99).fingerprint()
    assert base.fingerprint() == make().fingerprint()


def test_every_legal_edge_of_the_state_machine():
    edges = {
        ("pending", "sample"): "sampled",
        ("pending", "skip"): "skipped",
        ("pending", "fail"): "failed",
        ("sampled", "review"): "reviewed",
        ("reviewed", "plan"): "planned",
        ("planned", "apply"): "applied",
        ("applied", "verify"): "verified",
        ("failed", "retry"): "pending",
        ("failed", "skip"): "skipped",
    }
    for (start, event), target in edges.items():
        batch = make()
        batch = _force(batch, "a1", start)
        assert advance(batch, "a1", event).state_of("a1") == target


def _force(batch, artist_id, state):
    steps = dict(batch.steps)
    steps[artist_id] = state
    return Batch(
        batch_id=batch.batch_id,
        account_id=batch.account_id,
        created_at=batch.created_at,
        scope=batch.scope,
        policy=batch.policy,
        steps=steps,
        status=batch.status,
        stop_reason=batch.stop_reason,
    )


@pytest.mark.parametrize(
    "start,event",
    [("applied", "sample"), ("verified", "apply"), ("skipped", "review"), ("applied", "retry")],
)
def test_illegal_moves_are_refused_with_a_code(start, event):
    batch = _force(make(), "a1", start)
    assert code_of(lambda: advance(batch, "a1", event)) == "transition_not_allowed"


def test_advancing_twice_with_the_same_event_is_idempotent():
    once = advance(make(), "a1", "sample")
    twice = advance(once, "a1", "sample")
    assert twice.state_of("a1") == "sampled"
    assert twice.steps == once.steps


def test_advancing_an_unknown_artist_or_event_is_refused():
    batch = make()
    assert code_of(lambda: advance(batch, "zz", "sample")) == "artist_not_in_batch"
    assert code_of(lambda: advance(batch, "a1", "teleport")) == "event_unknown"


def test_rebuilding_from_progress_keeps_untouched_artists_pending():
    batch = make()
    rebuilt = rebuild(batch, {"a1": "sampled", "a3": "applied"})
    assert rebuilt.state_of("a1") == "sampled"
    assert rebuilt.state_of("a3") == "applied"
    assert rebuilt.state_of("a2") == "pending"
    assert rebuilt.fingerprint() == batch.fingerprint()


def test_resume_returns_only_open_artists_and_never_a_finished_one():
    batch = make()
    pending = resume(batch, {"a1": "applied", "a2": "skipped", "a3": "sampled"})
    assert pending == ("a3", "a4", "a5")
    assert not set(pending) & set(FINAL_STATES)
    assert set(OPEN_STATES) >= {"pending", "sampled"}


def test_progress_from_another_batch_is_refused():
    assert code_of(lambda: rebuild(make(), {"zz": "sampled"})) == (
        "progress_artist_outside_batch"
    )


def test_progress_with_an_unknown_state_is_refused():
    assert code_of(lambda: rebuild(make(), {"a1": "mostly"})) == "progress_state_invalid"


def test_progress_may_not_reopen_a_finished_artist():
    batch = rebuild(make(), {"a1": "applied"})
    assert code_of(lambda: rebuild(batch, {"a1": "sampled"})) == (
        "progress_would_reopen_finished"
    )


def test_a_paused_batch_becomes_complete_when_its_last_open_artist_finishes():
    batch = plan_batch(["a1"], account_id="acct-1", artists=1, requests=100)
    assert batch.status == "active"
    paused = _force(batch, "a1", "planned")
    paused = Batch(
        batch_id=paused.batch_id,
        account_id=paused.account_id,
        created_at=paused.created_at,
        scope=paused.scope,
        policy=paused.policy,
        steps=paused.steps,
        status="paused",
        stop_reason="artists_limit",
    )
    done = advance(advance(paused, "a1", "apply"), "a1", "verify")
    assert done.state_of("a1") == "verified"
    assert done.status == "complete"


def test_splitting_a_pool_covers_it_once_with_distinct_fingerprints():
    pool = ["a%02d" % index for index in range(27)]
    batches = plan_batches(pool, account_id="acct-1", artists=10, requests=100)
    assert [len(batch.artist_ids) for batch in batches] == [10, 10, 7]
    union = [artist for batch in batches for artist in batch.artist_ids]
    assert union == sorted(pool)
    assert len({batch.fingerprint() for batch in batches}) == 3
    reversed_batches = plan_batches(
        list(reversed(pool)), account_id="acct-1", artists=10, requests=100
    )
    assert [b.artist_ids for b in reversed_batches] == [b.artist_ids for b in batches]


def test_splitting_with_a_zero_size_is_refused():
    assert code_of(lambda: plan_batches(POOL, account_id="a", artists=0, requests=1)) == (
        "invalid_artists"
    )


def test_the_inputs_are_not_modified():
    pool = copy.deepcopy(POOL)
    done = ["a2"]
    done_copy = list(done)
    plan_batch(pool, account_id="acct-1", artists=5, requests=100, done_ids=done)
    assert pool == POOL
    assert done == done_copy


def test_a_batch_is_immutable_and_its_mappings_are_read_only():
    batch = make()
    with pytest.raises(dataclasses.FrozenInstanceError):
        batch.status = "complete"
    with pytest.raises(TypeError):
        batch.steps["a1"] = "applied"
    with pytest.raises(TypeError):
        batch.scope["artist_ids"] = ()


def test_the_batch_serialises_and_records_what_it_planned_with():
    payload = make(artists=2, requests=7).as_dict()
    json.dumps(payload)
    assert payload["policy"]["budget"] == {
        "artists": 2,
        "requests": 7,
        "bytes": 0,
        "seconds": 0.0,
    }
    assert payload["policy"]["cost"]["requests"] == 1
    assert payload["scope"]["artist_ids"] == ["a1", "a2"]
    assert set(payload["steps"].values()) == {"pending"}
    assert len(payload["fingerprint"]) == 64


def test_a_batch_needs_an_account_and_a_positive_id():
    assert code_of(lambda: plan_batch(POOL, account_id="", artists=1, requests=1)) == (
        "invalid_account_id"
    )
    assert code_of(
        lambda: plan_batch(POOL, account_id="a", artists=1, requests=1, batch_id="  ")
    ) == "invalid_batch_id"


def test_states_come_from_the_frozen_vocabulary():
    batch = make()
    assert set(batch.steps.values()) == {"pending"}
    assert "failed" in STATES
    assert code_of(lambda: rebuild(batch, {"a1": ""})) == "progress_state_invalid"
