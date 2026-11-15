"""The batch report: totals that add up, and unknowns that stay unknown."""
from __future__ import annotations

import copy
import json

import pytest

from organizer.batches import ArtistCost, STATES, plan_batch, rebuild
from organizer.conflicts import KINDS
from organizer.group_scope import build_scope
from organizer.report import (
    MEASURED_KEYS,
    REPORT_VERSION,
    ReportError,
    build_report,
    counts_from,
    report_json,
)


def snapshot(artists, *, complete=True, memberships=None):
    return {
        "id": "snap-1",
        "artists": artists,
        "memberships": (
            memberships
            if memberships is not None
            else {"show": {"fav": {"ids": sorted(artists)}}, "hide": {}}
        ),
        "memberships_complete": complete,
    }


ARTISTS = {
    "1001": {"visibility": "0", "groups": ["fav"], "is_following": True},
    "1002": {"visibility": "0", "groups": ["fav"], "is_following": True},
    "1003": {"visibility": "0", "groups": ["fav"], "is_following": True},
}


def make_batch(progress=None, pool=None):
    batch = plan_batch(
        pool if pool is not None else sorted(ARTISTS),
        account_id="acct-1",
        artists=10,
        requests=20,
        bytes_budget=10_000,
        seconds=60.0,
        snapshot_id="snap-1",
        group="fav",
        rules_version="pilot-v1",
        cost=ArtistCost(requests=2, bytes_per_artist=500, seconds_per_artist=1.5),
    )
    return batch if progress is None else rebuild(batch, progress)


def make_scope(artists=None, *, complete=True):
    return build_scope(snapshot(artists or ARTISTS, complete=complete), groups=["fav"])


def decision(artist_id, kind, reason="same_as_planned"):
    actions = {
        "unchanged": "keep",
        "moved": "needs_human",
        "group_renamed": "needs_human",
        "group_deleted": "needs_human",
        "artist_gone": "needs_human",
        "new_member": "replan",
        "unknown": "needs_human",
    }
    return {
        "artist_id": artist_id,
        "kind": kind,
        "action": actions[kind],
        "reason": reason,
        "needs_human": actions[kind] == "needs_human",
        "evidence": {"before": [], "after": [], "observed_at": "2026-11-15"},
    }


def sample(progress=None, decisions=None, *, measured=None, complete=True, pool=None):
    return build_report(
        make_batch(progress, pool),
        decisions if decisions is not None else [decision("1001", "unchanged")],
        make_scope(complete=complete),
        measured=measured,
        generated_at="2026-11-15T03:00:00Z",
    )


def test_every_state_is_counted_and_the_total_is_the_artist_count():
    report = sample({"1001": "applied", "1002": "failed", "1003": "pending"})
    counts = report["counts"]
    assert counts["total"] == 3
    assert counts["total"] == len(report["per_artist"])
    assert sum(counts["states"].values()) == counts["total"]
    assert counts["states"]["applied"] == 1
    assert counts["states"]["failed"] == 1
    assert counts["states"]["pending"] == 1


def test_states_that_did_not_happen_are_zero_not_absent():
    counts = sample()["counts"]
    assert set(counts["states"]) == set(STATES)
    assert counts["states"]["verified"] == 0


def test_every_kind_is_present_and_absent_kinds_are_zero():
    counts = sample({"1001": "applied"})["counts"]
    assert set(counts["decisions"]) == set(KINDS)
    assert counts["decisions"]["unknown"] == 0


def test_the_report_can_be_recounted_from_its_own_rows():
    report = sample({"1001": "applied", "1002": "failed"})
    assert counts_from(report["per_artist"]) == report["counts"]


def test_unknown_needs_human_and_failed_are_all_listed_with_reasons():
    report = sample(
        {"1001": "failed", "1002": "pending", "1003": "applied"},
        [
            decision("1001", "unchanged"),
            decision("1002", "unknown", "snapshot_incomplete"),
            decision("1003", "moved", "website_groups_changed"),
        ],
    )
    listed = {row["artist_id"]: row for row in report["unknowns"]}
    assert set(listed) == {"1001", "1002", "1003"}
    assert listed["1001"]["state"] == "failed"
    assert listed["1002"]["kind"] == "unknown"
    assert listed["1002"]["reason"] == "snapshot_incomplete"
    assert listed["1003"]["reason"] == "website_groups_changed"
    assert report["counts"]["needs_human"] == 2


def test_no_measurement_stays_unknown_and_never_becomes_zero():
    section = sample()["planned_vs_measured"]
    assert section["state"] == "unknown"
    assert section["reason"] == "no_measurement"
    assert section["measured"] is None
    assert section["delta"] is None
    assert section["planned"]["artists"] == 3


def test_a_measurement_and_a_plan_are_reported_side_by_side():
    section = sample(measured={"requests": 7, "bytes": 1500.0})["planned_vs_measured"]
    assert section["state"] == "measured"
    assert section["measured"] == {"requests": 7.0, "bytes": 1500.0}
    assert section["planned"]["estimate"]["requests"] == 6.0
    assert section["delta"]["requests"] == 1.0
    assert section["delta"]["bytes"] == 0.0
    assert "seconds" not in section["delta"]


def test_the_planned_section_keeps_the_budget_and_the_estimate_apart():
    planned = sample()["planned_vs_measured"]["planned"]
    assert planned["artists"] == 3
    assert planned["budget"]["requests"] == 20
    assert planned["estimate"]["requests"] == 6.0
    assert planned["count_per_artist"] == 16


def test_a_partial_scope_is_named_in_the_proof_limits():
    report = sample(complete=False)
    assert report["proof_limits"]["coverage"] == "partial"
    assert report["proof_limits"]["coverage_note"].startswith("scope_not_complete")


def test_a_complete_scope_carries_no_coverage_note():
    report = sample()
    assert report["proof_limits"]["coverage"] == "complete"
    assert report["proof_limits"]["coverage_note"] == ""


def test_the_report_says_it_does_not_prove_the_website_was_written():
    assert sample()["proof_limits"]["website_write_back"] == "not_proven_here"


def test_json_is_stable_and_sorted():
    report = sample({"1001": "applied"})
    text = report_json(report)
    assert text == report_json(sample({"1001": "applied"}))
    assert json.loads(text) == report
    assert text.startswith("{\"batch\":")


def test_the_same_inputs_give_the_same_report_twice():
    first = sample({"1001": "applied", "1002": "failed"})
    second = sample({"1001": "applied", "1002": "failed"})
    assert first == second


def test_the_batch_and_the_scope_are_not_modified():
    batch = make_batch({"1001": "applied"})
    scope = make_scope()
    batch_before = copy.deepcopy(batch.as_dict())
    scope_before = copy.deepcopy(scope.as_dict())
    build_report(batch, [decision("1001", "unchanged")], scope)
    assert batch.as_dict() == batch_before
    assert scope.as_dict() == scope_before


def test_a_decision_about_an_artist_the_batch_never_covered_is_refused():
    with pytest.raises(ReportError) as caught:
        build_report(
            make_batch(),
            [decision("1001", "unchanged"), decision("9999", "unchanged")],
            make_scope(),
        )
    assert caught.value.code == "decision_outside_batch"
    assert caught.value.detail == ["9999"]


def test_two_decisions_for_one_artist_are_refused():
    with pytest.raises(ReportError) as caught:
        build_report(
            make_batch(),
            [decision("1001", "unchanged"), decision("1001", "moved")],
            make_scope(),
        )
    assert caught.value.code == "decision_duplicate"


def test_a_batch_artist_without_a_decision_is_reported_not_dropped():
    report = build_report(make_batch(), [decision("1001", "unchanged")], make_scope())
    assert report["per_artist"]["1002"]["kind"] is None
    assert report["per_artist"]["1002"]["action"] is None
    assert report["counts"]["undecided"] == 2
    dropped = [row for row in report["unknowns"] if row["reason"] == "no_decision_for_artist"]
    assert {row["artist_id"] for row in dropped} == {"1002", "1003"}


def test_an_unknown_measurement_key_is_refused():
    with pytest.raises(ReportError) as caught:
        sample(measured={"requests": 1, "pages": 3})
    assert caught.value.code == "measurement_unknown_key"


@pytest.mark.parametrize("bad", [-1, True, "3", None])
def test_a_bad_measurement_value_is_refused(bad):
    with pytest.raises(ReportError):
        sample(measured={"bytes": bad})


def test_a_scope_that_does_not_cover_the_batch_is_refused():
    # EA-09: this used to be a field in a successful report (covers_batch=false,
    # exit 0).  An artist the scope never selected cannot be reported on, so the
    # combination is refused instead of annotated.
    with pytest.raises(ReportError) as caught:
        build_report(
            make_batch(pool=["1001", "1002", "1003", "9999"]),
            [decision("1001", "unchanged")],
            make_scope(),
        )
    assert caught.value.code == "scope_excludes_batch"
    assert caught.value.detail == ["9999"]


def test_a_wider_scope_is_refused_by_default_and_recorded_when_declared():
    # One group planned in several batches makes the scope wider than the batch;
    # that is legitimate, but it changes what the totals mean, so it is opt-in.
    narrow_batch = make_batch(pool=["1001"])
    with pytest.raises(ReportError) as caught:
        build_report(narrow_batch, [decision("1001", "unchanged")], make_scope())
    assert caught.value.code == "scope_broader_than_batch"
    assert caught.value.detail == ["1002", "1003"]

    report = build_report(
        narrow_batch, [decision("1001", "unchanged")], make_scope(), superset_ok=True
    )
    assert report["scope"]["covers_batch"] is True
    assert report["scope"]["artist_count"] == 3
    assert report["scope"]["superset"] == {
        "allowed": True,
        "extra_artists": ["1002", "1003"],
    }


def test_an_empty_batch_reports_zero_and_no_unknowns():
    empty = plan_batch([], account_id="acct-1", artists=5, requests=5)
    # An empty batch covers no artist, so the scope is wider than it; the caller
    # has to say so (EA-09) even though there is nobody to report about.
    report = build_report(empty, [], make_scope(), superset_ok=True)
    assert report["counts"]["total"] == 0
    assert report["unknowns"] == []
    assert report["planned_vs_measured"]["state"] == "unknown"


def test_the_provenance_carries_the_two_fingerprints_and_the_version():
    report = sample()
    provenance = report["provenance"]
    assert provenance["batch_fingerprint"] == make_batch().fingerprint()
    assert provenance["scope_fingerprint"] == make_scope().fingerprint
    assert provenance["rules_version"] == "pilot-v1"
    assert provenance["generated_at"] == "2026-11-15T03:00:00Z"
    assert report["report_version"] == REPORT_VERSION


def test_the_measured_keys_are_a_closed_set():
    assert MEASURED_KEYS == ("requests", "bytes", "seconds")
