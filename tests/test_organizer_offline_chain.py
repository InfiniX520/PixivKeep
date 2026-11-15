"""One end-to-end pass over the five offline E1 modules.

Each module has its own unit tests; this file is the seam.  A snapshot feeds a
scope, the scope feeds a batch, evidence feeds the rules, a second snapshot
feeds the conflict table, and the report has to add all of it up without
inventing a measurement it never took.

Every expectation below is counted by hand from the synthetic data above it,
never read back out of the module under test.
"""
from __future__ import annotations

import copy
import json

from organizer.batches import advance, plan_batch, resume
from organizer.conflicts import decide, writes_for
from organizer.group_scope import build_scope
from organizer.report import build_report, report_json
import pytest

from organizer.common import OrganizerError
from organizer.rules import Rule, RuleSet, classify

# artist id -> (visibility flag, groups, is_following)
# flag "0" is the public side, "1" the private one; the group names are chosen
# so that "fav" exists only on the public side and "rare" only beside it.
ARTISTS = {
    "1001": ("0", ["fav"], True),
    "1002": ("0", ["fav"], True),
    "1003": ("0", ["fav", "rare"], True),
    "1004": ("0", ["other"], True),
    "1005": ("1", ["secret"], True),
    "1006": ("0", ["fav"], False),
}


def snapshot(artists=None, *, complete=True):
    rows = ARTISTS if artists is None else artists
    partitions = {"show": {}, "hide": {}}
    for artist_id, (flag, groups, _following) in rows.items():
        side = "show" if flag == "0" else "hide"
        for name in groups:
            partitions[side].setdefault(name, {"ids": []})["ids"].append(artist_id)
    return {
        "snapshot_id": "snap-1",
        "account_ref": "acct-1",
        "memberships": partitions,
        "memberships_complete": complete,
        "artists": {
            artist_id: {
                "groups": list(groups),
                "visibility": flag,
                "is_following": following,
            }
            for artist_id, (flag, groups, following) in rows.items()
        },
    }


def regrouped(artist_id, extra):
    """The same list after the user added one group to one artist by hand."""
    rows = dict(ARTISTS)
    flag, groups, following = rows[artist_id]
    rows[artist_id] = (flag, list(groups) + [extra], following)
    return snapshot(rows)


def test_step_one_the_scope_is_the_members_of_the_named_group():
    """Independently counted: 1001/1002/1003 are followed and list fav on show.

    1005 only belongs to a private-side group (so the bare name fav is unambiguous),
    1006 is not followed any more, 1004 never listed it.
    """
    scope = build_scope(snapshot(), groups=["fav"])
    assert scope.artist_ids == ("1001", "1002", "1003")
    assert scope.completeness == "complete"
    assert ("show", "fav") in tuple(scope.group_pairs)
    assert scope.fingerprint


def test_a_bare_group_name_that_exists_on_both_sides_is_refused():
    """The public and private lists are separate namespaces.

    Merging them would silently pull a private-side group into a download
    scope, so the engine has to refuse the bare name and accept the pair.
    """
    rows = dict(ARTISTS)
    rows["1005"] = ("1", ["fav"], True)
    both = snapshot(rows)
    with pytest.raises(OrganizerError) as caught:
        build_scope(both, groups=["fav"])
    assert caught.value.code.startswith("group_ambiguous")
    private = build_scope(both, groups=[("hide", "fav")])
    assert private.artist_ids == ("1005",)
    public = build_scope(both, groups=[("show", "fav")])
    assert public.artist_ids == ("1001", "1002", "1003")
    assert private.fingerprint != public.fingerprint


def test_step_two_the_batch_takes_members_in_order_within_the_budget():
    scope = build_scope(snapshot(), groups=["fav"])
    batch = plan_batch(
        scope.artist_ids,
        account_id="acct-1",
        artists=2,
        requests=3,
        snapshot_id="snap-1",
        group="fav",
    )
    assert len(batch.artist_ids) == 2
    assert set(batch.artist_ids) <= set(scope.artist_ids)
    assert batch.artist_ids == tuple(sorted(batch.artist_ids)[:2])
    # A bigger ceiling has to change the batch identity, or the fingerprint is
    # not describing what was planned.
    wider = plan_batch(
        scope.artist_ids,
        account_id="acct-1",
        artists=3,
        requests=3,
        snapshot_id="snap-1",
        group="fav",
    )
    assert len(wider.artist_ids) == 3
    assert wider.fingerprint() != batch.fingerprint()


def test_step_three_the_rules_suggest_from_literal_evidence_only():
    ruleset = RuleSet(
        version="pilot-v1",
        rules=(
            Rule(
                rule_id="r-cat",
                dimension="topic",
                match="猫",
                suggest_groups=("猫科",),
                weight=2,
            ),
            Rule(
                rule_id="r-sky",
                dimension="style",
                match="sky",
                suggest_groups=("风景",),
                weight=1,
            ),
        ),
    )
    hit = classify({"tags": ["猫", "Sky"]}, ruleset)
    # weight 2 for the cat rule beats weight 1 for the sky rule
    assert hit["groups"] == ["猫科", "风景"]
    assert hit["confidence"] == "high"
    assert hit["needs_more"] is False
    assert [anchor["rule_id"] for anchor in hit["anchors"]] == ["r-cat", "r-sky"]
    # the anchor keeps the tag as it was written, not the folded form
    assert hit["anchors"][1]["tags"] == ["Sky"]

    missing = classify({"tags": []}, ruleset)
    assert missing["groups"] == []
    assert missing["confidence"] == "unknown"
    assert missing["needs_more"] is True
    assert missing["reasons"] == ["evidence_missing"]


def test_step_four_a_manual_regrouping_is_kept_and_never_written_back():
    before = snapshot()
    after = regrouped("1001", "new")
    plan = {"artists": [{"artist_id": "1001", "target": ["fav"], "groups": ["fav"]}]}
    decisions = decide(before, after, plan)
    row = next(item for item in decisions if item["artist_id"] == "1001")
    assert row["kind"] == "moved"
    # A manual regrouping is not ours to finish: it goes to a human, and it
    # still produces no write.
    assert row["action"] == "needs_human"
    assert row["needs_human"] is True
    assert row["evidence"]["after"] == ["fav", "new"]
    # The whole point: a conflict table can never produce a write.
    assert writes_for(decisions) == []


def test_step_five_the_report_adds_up_and_keeps_measurement_out_of_it():
    before = snapshot()
    scope = build_scope(before, groups=["fav"])
    batch = plan_batch(
        scope.artist_ids,
        account_id="acct-1",
        artists=2,
        requests=3,
        snapshot_id="snap-1",
        group="fav",
    )
    after = regrouped(sorted(batch.artist_ids)[-1], "new")
    plan = {
        "artists": [
            {"artist_id": artist_id, "target": ["fav"], "groups": ["fav"]}
            for artist_id in batch.artist_ids
        ]
    }
    decisions = decide(before, after, plan)
    report = build_report(batch, decisions, scope, generated_at="2026-11-15T00:00:00Z")

    assert report["counts"]["total"] == len(batch.artist_ids)
    assert sum(report["counts"]["states"].values()) == len(batch.artist_ids)
    # Recount from the rows, which is the only way to know the summary is not
    # describing a different set than the detail.
    tallied = {}
    for entry in report["per_artist"].values():
        tallied[entry["state"]] = tallied.get(entry["state"], 0) + 1
    assert tallied == {name: count for name, count in report["counts"]["states"].items() if count}

    named = {item["artist_id"] for item in report["unknowns"]}
    assert sorted(batch.artist_ids)[-1] in named

    # No measurement was taken, so none may be invented.
    assert report["planned_vs_measured"]["state"] == "unknown"
    assert report["planned_vs_measured"]["measured"] is None
    assert report["planned_vs_measured"]["delta"] is None
    assert report["proof_limits"]["website_write_back"] == "not_proven_here"
    assert report["proof_limits"]["coverage"] == "complete"
    assert report["scope"]["covers_batch"] is True
    json.loads(report_json(report))


def test_step_six_an_incomplete_snapshot_is_named_and_never_passes_as_whole():
    scope = build_scope(snapshot(complete=False), groups=["fav"])
    assert scope.completeness in ("partial", "unknown")
    batch = plan_batch(
        scope.artist_ids,
        account_id="acct-1",
        artists=2,
        requests=3,
        snapshot_id="snap-1",
        group="fav",
    )
    report = build_report(batch, [], scope)
    assert report["proof_limits"]["coverage"] != "complete"
    assert report["proof_limits"]["coverage_note"].startswith("scope_not_complete:")
    # With no decisions every artist is undecided, and that has to be visible
    # rather than silently counted as zero problems.
    assert len(report["unknowns"]) == len(batch.artist_ids)
    assert all(item["reason"] == "no_decision_for_artist" for item in report["unknowns"])


def test_the_chain_does_not_mutate_what_it_was_given():
    before = snapshot()
    scope = build_scope(before, groups=["fav"])
    batch = plan_batch(scope.artist_ids, account_id="acct-1", artists=2, requests=3)
    after = regrouped("1001", "new")
    plan = {"artists": [{"artist_id": "1001", "target": ["fav"], "groups": ["fav"]}]}
    guard_before = copy.deepcopy(before)
    guard_plan = copy.deepcopy(plan)
    assert decide(before, after, plan)
    build_report(batch, [], scope)
    assert before == guard_before
    assert plan == guard_plan


def test_resume_after_a_pause_returns_only_unfinished_members():
    scope = build_scope(snapshot(), groups=["fav"])
    batch = plan_batch(scope.artist_ids, account_id="acct-1", artists=2, requests=2)
    first = batch.artist_ids[0]
    # applied still owes its verification, so the resumed run keeps it; only a
    # read-back (verified) or an explicit skip takes an artist out of the set.
    # Contract change, audit EA-05.
    assert resume(batch, {first: "applied"}) == (first, batch.artist_ids[1])
    assert resume(batch, {first: "verified"}) == (batch.artist_ids[1],)
    assert resume(batch, {first: "skipped"}) == (batch.artist_ids[1],)
    assert resume(batch, {}) == batch.artist_ids

    # "still resumable" must not be read as "resumable forever": the artist
    # leaves the set as soon as the read-back lands, which is the other half of
    # the same rule.  advance() is used here so the sequence is the real one,
    # not a hand-written state.
    walked = batch
    for event in ("sample", "review", "plan", "apply"):
        walked = advance(walked, first, event)
    assert walked.state_of(first) == "applied"
    assert first in resume(walked, {})
    read_back = advance(walked, first, "verify")
    assert read_back.state_of(first) == "verified"
    assert resume(read_back, {}) == (batch.artist_ids[1],)
