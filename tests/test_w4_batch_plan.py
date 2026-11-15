"""W4: batch set conservation, the resume truth table, and immutable exports.

Audit EA-05 (a batch cut short by its budget dropped the artists it did not
take, and applied artists were dropped by a resume instead of being verified)
and EA-06 (a batch member list could be edited through its own export).
Every expectation is written out here instead of being read back from the
constant it checks.
"""
from __future__ import annotations

import json

import pytest

from organizer.batches import (
    AWAITING_VERIFY_STATES,
    EVENT_TARGET,
    FINAL_STATES,
    OPEN_STATES,
    PLAN_STOP_REASONS,
    RESUMABLE_STATES,
    STATES,
    Batch,
    BatchError,
    BatchPlan,
    advance,
    next_action,
    plan_batch,
    plan_batches,
    rebuild,
    resume,
)


def pool(count):
    return ["a%02d" % index for index in range(count)]


def code_of(call):
    with pytest.raises(BatchError) as caught:
        call()
    return caught.value.code


def walked(batch, artist_id, *events):
    for event in events:
        batch = advance(batch, artist_id, event)
    return batch


def to_state(batch, artist_id, state):
    """Reach one state through persisted progress (forced, for the matrix)."""
    reached = rebuild(batch, {artist_id: state})
    assert reached.state_of(artist_id) == state
    return reached


@pytest.mark.parametrize("count", [0, 1, 5, 21])
def test_every_pool_size_is_split_without_a_hole(count):
    artists = pool(count)
    plan = plan_batches(artists, account_id="acct-1", artists=4, requests=1000)
    assert list(plan.planned) == artists
    assert plan.remaining == ()
    assert plan.stop_reason == ("pool_exhausted" if count == 0 else "none")
    assert len(plan) == (0 if count == 0 else (count + 3) // 4)
    # Iterating the plan still yields the batches, and nothing else.
    assert [batch.artist_ids for batch in plan] == [
        tuple(batch.artist_ids) for batch in plan.batches
    ]


def test_a_budget_smaller_than_the_batch_size_does_not_drop_an_artist():
    artists = ["1", "2", "3", "4", "5"]
    plan = plan_batches(artists, account_id="synthetic", artists=3, requests=1)
    taken = [artist for batch in plan for artist in batch.artist_ids]
    assert sorted(taken) == artists
    assert plan.remaining == ()
    assert [len(batch.artist_ids) for batch in plan] == [1, 1, 1, 1, 1]


def test_the_cursor_advances_by_what_was_taken_not_by_the_cap():
    artists = pool(7)
    plan = plan_batches(artists, account_id="acct-1", artists=3, requests=2)
    assert [len(batch.artist_ids) for batch in plan] == [2, 2, 2, 1]
    assert list(plan.planned) == artists
    assert plan.remaining == ()


def test_a_budget_that_fits_nothing_stops_and_names_the_whole_queue():
    artists = pool(5)
    plan = plan_batches(artists, account_id="acct-1", artists=3, requests=0)
    assert plan.batches == ()
    assert plan.remaining == tuple(artists)
    assert plan.stop_reason == "capacity_zero"
    assert plan.stop_reason in PLAN_STOP_REASONS
    # Asking again answers the same thing: no empty batch, no swallowed name.
    again = plan_batches(artists, account_id="acct-1", artists=3, requests=0)
    assert again == plan


def test_done_ids_may_repeat_and_arrive_out_of_order():
    artists = pool(5)
    plan = plan_batches(
        artists,
        account_id="acct-1",
        artists=2,
        requests=100,
        done_ids=["a02", "a02", "a00"],
    )
    assert list(plan.planned) == ["a01", "a03", "a04"]
    assert plan.remaining == ()
    done = {"a00", "a02"}
    assert done | set(plan.planned) | set(plan.remaining) == set(artists)
    assert not (set(plan.planned) & set(plan.remaining))


def test_a_plan_round_trips_through_its_own_dump():
    plan = plan_batches(pool(5), account_id="acct-1", artists=2, requests=100)
    dump = json.loads(json.dumps(plan.as_dict()))
    assert dump["counts"] == {"batches": 3, "planned": 5, "remaining": 0}
    restored = BatchPlan.from_dict(dump)
    assert restored == plan
    assert [batch.fingerprint() for batch in restored] == [
        batch.fingerprint() for batch in plan
    ]


@pytest.mark.parametrize(
    "payload,code",
    [
        ({}, "plan_field_missing"),
        ("not a plan", "plan_not_a_mapping"),
        ({"batches": "x", "remaining": [], "stop_reason": "none"}, "plan_batches_invalid"),
        ({"batches": [], "remaining": "x", "stop_reason": "none"}, "plan_remaining_invalid"),
        ({"batches": [], "remaining": [], "stop_reason": "later"}, "invalid_stop_reason"),
    ],
)
def test_a_plan_dump_that_cannot_answer_is_refused(payload, code):
    assert code_of(lambda: BatchPlan.from_dict(payload)) == code


def test_a_batch_export_cannot_be_edited_into_a_different_member_set():
    batch = plan_batch(pool(2), account_id="acct-1", artists=2, requests=5)
    before = batch.as_dict()["fingerprint"]
    dump = batch.as_dict()
    dump["scope"]["artist_ids"].append("999")
    dump["policy"]["budget"]["requests"] = 999
    dump["steps"]["a00"] = "verified"
    assert batch.artist_ids == ("a00", "a01")
    assert batch.policy["budget"]["requests"] == 5
    assert batch.steps["a00"] == "pending"
    assert batch.fingerprint() == before


def test_the_constructor_does_not_share_the_callers_containers():
    scope = {"artist_ids": ["a00"], "snapshot_id": "s", "group": "", "rules_version": ""}
    policy = {"budget": {"requests": 1}, "cost": {"requests": 1}}
    steps = {"a00": "pending"}
    batch = Batch(batch_id="b", account_id="a", created_at="", scope=scope,
                  policy=policy, steps=steps, status="active", stop_reason="none")
    scope["artist_ids"].append("a01")
    policy["budget"]["requests"] = 99
    steps["a00"] = "verified"
    assert batch.artist_ids == ("a00",)
    assert batch.policy["budget"]["requests"] == 1
    assert batch.steps["a00"] == "pending"
    with pytest.raises(AttributeError):
        batch.scope["artist_ids"].append("a02")
    with pytest.raises(TypeError):
        batch.policy["budget"]["requests"] = 2


def test_applied_is_awaiting_verification_not_finished():
    batch = to_state(plan_batch(["1"], account_id="a", artists=1, requests=1),
                     "1", "applied")
    assert "1" in resume(batch, {})
    assert next_action(batch, "1") == "verify"
    done = advance(batch, "1", "verify")
    assert done.state_of("1") == "verified"
    assert resume(done, {}) == ()
    assert next_action(done, "1") == ""
    assert set(FINAL_STATES) == {"verified", "skipped"}
    assert set(AWAITING_VERIFY_STATES) == {"applied"}
    assert set(RESUMABLE_STATES) == set(OPEN_STATES) | {"applied"}


def test_next_action_never_names_an_event_that_re_sends_a_write():
    batch = to_state(plan_batch(["1"], account_id="a", artists=1, requests=1),
                     "1", "failed")
    assert next_action(batch, "1") == "reconcile"
    # reconcile is not an advance event, so a caller cannot replay it blindly.
    assert "reconcile" not in EVENT_TARGET
    assert code_of(lambda: advance(batch, "1", "reconcile")) == "event_unknown"


@pytest.mark.parametrize("state", STATES)
def test_every_state_survives_a_round_trip_and_keeps_resuming(state):
    # requests=2 so both artists fit: the point is the state of "1", and "2"
    # has to stay in the batch as the untouched control.
    batch = to_state(plan_batch(["1", "2"], account_id="a", artists=2, requests=2),
                     "1", state)
    dump = json.loads(json.dumps(batch.as_dict()))
    restored = Batch.from_dict(dump)
    assert restored == batch
    assert restored.state_of("1") == state
    assert ("1" in resume(restored, {})) is (state in RESUMABLE_STATES)
    assert ("2" in resume(restored, {})) is True


def test_the_status_is_computed_from_the_steps_not_carried_forward():
    batch = plan_batch(["1"], account_id="a", artists=1, requests=1)
    paused = Batch(batch_id=batch.batch_id, account_id=batch.account_id,
                   created_at=batch.created_at, scope=batch.scope,
                   policy=batch.policy, steps={"1": "verified"},
                   status="paused", stop_reason="artists_limit")
    assert paused.status == "complete"
    assert pause_before_finishing().status == "paused"


def pause_before_finishing():
    batch = plan_batch(["1"], account_id="a", artists=1, requests=1)
    return Batch(batch_id=batch.batch_id, account_id=batch.account_id,
                 created_at=batch.created_at, scope=batch.scope,
                 policy=batch.policy, steps={"1": "planned"},
                 status="active", stop_reason="artists_limit")


def test_a_dump_whose_status_contradicts_its_steps_is_refused():
    batch = plan_batch(["1"], account_id="a", artists=1, requests=1)
    dump = batch.as_dict()
    dump["status"] = "complete"
    assert code_of(lambda: Batch.from_dict(dump)) == "batch_status_inconsistent"


def test_a_batch_whose_steps_do_not_match_its_members_is_refused():
    batch = plan_batch(["1"], account_id="a", artists=1, requests=1)
    extra = dict(batch.steps)
    extra["2"] = "pending"
    assert code_of(lambda: Batch(
        batch_id="b", account_id="a", created_at="", scope=batch.scope,
        policy=batch.policy, steps=extra, status="active", stop_reason="none")) == (
        "steps_do_not_match_scope")
    missing = {}
    assert code_of(lambda: Batch(
        batch_id="b", account_id="a", created_at="", scope=batch.scope,
        policy=batch.policy, steps=missing, status="active", stop_reason="none")) == (
        "steps_do_not_match_scope")


def test_finishing_the_last_artist_completes_an_active_batch_too():
    batch = plan_batch(["1"], account_id="a", artists=1, requests=1)
    assert batch.status == "active"
    done = walked(batch, "1", "sample", "review", "plan", "apply", "verify")
    assert done.status == "complete"
    reloaded = Batch.from_dict(json.loads(json.dumps(done.as_dict())))
    assert reloaded.status == "complete"
    assert resume(reloaded, {}) == ()

