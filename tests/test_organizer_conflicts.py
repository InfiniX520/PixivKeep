"""The website wins: six kinds of "the plan is out of date", and no writes."""
from __future__ import annotations

import copy
import json

import pytest

from organizer import conflicts


def snapshot(groups_by_artist, directory, *, complete=True, snapshot_id="s1"):
    return {
        "id": snapshot_id,
        "memberships_complete": complete,
        "artists": {
            artist_id: {
                "artist_id": artist_id,
                "visibility": "0",
                "groups": list(groups),
            }
            for artist_id, groups in groups_by_artist.items()
        },
        "memberships": {
            "show": {
                tag: {"ids": list(ids), "complete": True} for tag, ids in directory.items()
            },
            "hide": {},
        },
    }


def plan(*rows):
    return {
        "id": "p1",
        "mode": "additive",
        "artists": [
            {"artist_id": artist_id, "groups": list(before), "target": list(target)}
            for artist_id, before, target in rows
        ],
    }


def kinds(decisions):
    return {item["artist_id"]: item["kind"] for item in decisions}


def only(decisions, artist_id):
    matches = [item for item in decisions if item["artist_id"] == artist_id]
    assert len(matches) == 1, matches
    return matches[0]


def test_identical_snapshots_leave_every_planned_artist_unchanged():
    before = snapshot({"a": ["g1"]}, {"g1": ["a"]})
    after = copy.deepcopy(before)
    decisions = conflicts.decide(before, after, plan(("a", ["g1"], ["g1"])))
    item = only(decisions, "a")
    assert item["kind"] == "unchanged"
    assert item["action"] == "keep"
    assert item["needs_human"] is False
    assert item["reason"] == "same_as_planned"


def test_a_user_regroup_is_kept_and_never_overwritten():
    before = snapshot({"a": ["g1"]}, {"g1": ["a"], "g2": []})
    after = snapshot({"a": ["g2"]}, {"g1": [], "g2": ["a"]}, snapshot_id="s2")
    decisions = conflicts.decide(before, after, plan(("a", ["g1"], ["g1"])))
    item = only(decisions, "a")
    assert item["kind"] == "moved"
    assert item["action"] == "needs_human"
    assert item["needs_human"] is True
    assert item["evidence"]["before"] == ["g1"]
    assert item["evidence"]["after"] == ["g2"]


def test_a_renamed_group_is_not_read_as_delete_plus_create():
    before = snapshot({"a": ["old"]}, {"old": ["a"]})
    after = snapshot({"a": ["new"]}, {"new": ["a"]}, snapshot_id="s2")
    assert conflicts.renames(before, after) == {("show", "old"): ("show", "new")}
    item = only(conflicts.decide(before, after, plan(("a", ["old"], ["old"]))), "a")
    assert item["kind"] == "group_renamed"
    assert item["action"] == "needs_human"


def test_a_rename_with_different_members_is_not_a_rename():
    before = snapshot({"a": ["old"], "b": []}, {"old": ["a"]})
    after = snapshot({"a": ["new"], "b": ["new"]}, {"new": ["a", "b"]}, snapshot_id="s2")
    assert conflicts.renames(before, after) == {}
    assert set(conflicts._directory(after)) == {("show", "new")}


def test_a_deleted_group_is_not_recreated():
    before = snapshot({"a": ["gone"]}, {"gone": ["a"]})
    after = snapshot({"a": []}, {}, snapshot_id="s2")
    item = only(conflicts.decide(before, after, plan(("a", [], ["gone"]))), "a")
    assert item["kind"] == "group_deleted"
    assert item["action"] == "needs_human"


def test_an_artist_that_appeared_since_the_snapshot_asks_for_a_replan():
    before = snapshot({"a": ["g1"]}, {"g1": ["a"]})
    after = snapshot({"a": ["g1"], "b": ["g1"]}, {"g1": ["a", "b"]}, snapshot_id="s2")
    decisions = conflicts.decide(before, after, plan(("a", ["g1"], ["g1"])))
    assert kinds(decisions) == {"a": "unchanged", "b": "new_member"}
    assert only(decisions, "b")["action"] == "replan"


def test_an_artist_the_plan_knows_but_the_snapshot_does_not_is_artist_gone():
    """A known absence is not an unknown one: the snapshot read fine."""
    before = snapshot({"a": ["g1"], "b": ["g1"]}, {"g1": ["a", "b"]})
    after = snapshot({"a": ["g1"]}, {"g1": ["a"]}, snapshot_id="s2")
    item = only(conflicts.decide(before, after, plan(("b", ["g1"], ["g1"]))), "b")
    assert item["kind"] == "artist_gone"
    assert item["reason"] == "artist_not_followed_any_more"
    assert item["action"] == "needs_human"
    assert conflicts.writes_for([item]) == []


def test_a_gone_artist_in_an_incomplete_snapshot_stays_unknown():
    """The same absence, but the snapshot cannot be trusted: unknown."""
    before = snapshot({"a": ["g1"], "b": ["g1"]}, {"g1": ["a", "b"]})
    after = snapshot({"a": ["g1"]}, {"g1": ["a"]}, complete=False, snapshot_id="s2")
    item = only(conflicts.decide(before, after, plan(("b", ["g1"], ["g1"]))), "b")
    assert item["kind"] == "unknown"
    assert item["reason"] == "snapshot_incomplete"


def cross_side_snapshot():
    """Same group name on both sides, with different members."""
    before = {
        "id": "s1",
        "memberships_complete": True,
        "artists": {
            "a": {"artist_id": "a", "visibility": "0", "groups": ["same"]},
            "b": {"artist_id": "b", "visibility": "1", "groups": ["same"]},
        },
        "memberships": {
            "show": {"same": {"ids": ["a"], "complete": True}},
            "hide": {"same": {"ids": ["b"], "complete": True}},
        },
    }
    return before, copy.deepcopy(before)


def test_a_bare_group_name_that_exists_on_both_sides_is_ambiguous():
    before, after = cross_side_snapshot()
    item = only(conflicts.decide(before, after, plan(("a", ["same"], ["same"]))), "a")
    assert item["kind"] == "unknown"
    assert item["reason"] == "group_ambiguous"
    assert item["action"] == "needs_human"


def test_an_explicit_visibility_pair_is_judged_on_its_own_side_only():
    before, after = cross_side_snapshot()
    # The private side gains a group; the public side did not change at all.
    after["memberships"]["hide"]["g2"] = {"ids": ["b"], "complete": True}
    after["artists"]["b"]["groups"] = ["same", "g2"]
    explicit = plan(("a", ["same"], [("show", "same")]))
    assert only(conflicts.decide(before, after, explicit), "a")["kind"] == "unchanged"
    private = plan(("b", ["same"], [("hide", "same")]))
    assert only(conflicts.decide(before, after, private), "b")["kind"] == "moved"


@pytest.mark.parametrize("broken", ["before", "after"])
def test_an_incomplete_snapshot_turns_every_decision_into_unknown(broken):
    before = snapshot({"a": ["g1"]}, {"g1": ["a"]}, complete=broken != "before")
    after = snapshot({"a": ["g1"]}, {"g1": ["a"]}, complete=broken != "after", snapshot_id="s2")
    decisions = conflicts.decide(before, after, plan(("a", ["g1"], ["g1"])))
    assert [item["kind"] for item in decisions] == ["unknown"]
    assert decisions[0]["reason"] == "snapshot_incomplete"
    # And it is not silently "nothing changed", which is the whole point.
    assert decisions[0]["kind"] != "unchanged"


def test_a_missing_membership_partition_is_not_a_shrinking_group():
    before = snapshot({"a": ["g1"]}, {"g1": ["a"]})
    after = copy.deepcopy(before)
    after["memberships"] = {"show": {"g1": {"ids": None}}, "hide": {}}
    decisions = conflicts.decide(before, after, plan(("a", ["g1"], ["g1"])))
    assert conflicts._directory(after) is None
    assert only(decisions, "a")["kind"] in ("unchanged", "moved", "unknown")


def test_the_decision_table_is_total_and_the_actions_are_a_closed_set():
    assert set(conflicts.ACTION_FOR) == set(conflicts.KINDS)
    assert set(conflicts.ACTION_FOR.values()) <= set(conflicts.ACTIONS)
    assert "write" not in conflicts.ACTIONS
    for kind, action in conflicts.ACTION_FOR.items():
        assert action in conflicts.ACTIONS, kind


def test_every_reason_the_module_uses_is_in_its_vocabulary():
    before = snapshot({"a": ["g1"]}, {"g1": ["a"]})
    after = snapshot({"a": ["g2"], "b": []}, {"g1": [], "g2": ["a"]}, snapshot_id="s2")
    decisions = conflicts.decide(before, after, plan(("a", ["g1"], ["g1"])))
    for item in decisions:
        assert item["reason"] in conflicts.REASONS, item
        assert item["kind"] in conflicts.KINDS
        assert item["action"] == conflicts.ACTION_FOR[item["kind"]]


def test_the_module_never_offers_a_write():
    before = snapshot({"a": ["g1"]}, {"g1": ["a"]})
    after = snapshot({"a": ["g2"]}, {"g1": [], "g2": ["a"]}, snapshot_id="s2")
    decisions = conflicts.decide(before, after, plan(("a", ["g1"], ["g1"])))
    assert conflicts.writes_for(decisions) == []
    for item in decisions:
        assert item["action"] in ("keep", "replan", "needs_human")
        assert item["action"] != "write"
    source = json.dumps(decisions, ensure_ascii=False)
    assert "tag_add" not in source and "tag_delete" not in source


def test_the_same_input_twice_gives_the_same_answer_and_does_not_mutate_it():
    before = snapshot({"a": ["g1"], "b": ["g1"]}, {"g1": ["a", "b"]})
    after = snapshot({"a": ["g2"], "b": ["g1"]}, {"g1": ["b"], "g2": ["a"]}, snapshot_id="s2")
    the_plan = plan(("a", ["g1"], ["g1"]), ("b", ["g1"], ["g1"]))
    frozen = copy.deepcopy((before, after, the_plan))
    first = conflicts.decide(before, after, the_plan, observed_at="2026-11-15T00:00:00Z")
    second = conflicts.decide(before, after, the_plan, observed_at="2026-11-15T00:00:00Z")
    assert first == second
    assert (before, after, the_plan) == frozen
    assert [item["artist_id"] for item in first] == sorted(item["artist_id"] for item in first)


def test_insertion_order_of_the_snapshots_does_not_change_the_answer():
    artists = {"a": ["g1"], "b": ["g2"], "c": []}
    directory = {"g1": ["a"], "g2": ["b"]}
    before = snapshot(artists, directory)
    after = snapshot(artists, directory, snapshot_id="s2")
    shuffled_before = snapshot(dict(reversed(list(artists.items()))),
                               dict(reversed(list(directory.items()))))
    shuffled_after = snapshot(dict(reversed(list(artists.items()))),
                              dict(reversed(list(directory.items()))), snapshot_id="s2")
    the_plan = plan(("a", ["g1"], ["g1"]), ("b", ["g2"], ["g2"]))
    assert conflicts.decide(before, after, the_plan) == conflicts.decide(
        shuffled_before, shuffled_after, the_plan
    )


def test_the_summary_names_every_kind_even_when_it_did_not_happen():
    before = snapshot({"a": ["g1"]}, {"g1": ["a"]})
    same = copy.deepcopy(before)
    counts = conflicts.summary(conflicts.decide(before, same, plan(("a", ["g1"], ["g1"]))))
    assert set(counts) == set(conflicts.KINDS)
    assert counts["unchanged"] == 1
    assert counts["moved"] == 0


def test_a_broken_plan_row_is_skipped_instead_of_crashing():
    before = snapshot({"a": ["g1"]}, {"g1": ["a"]})
    broken = {"id": "p", "artists": [{"groups": [], "target": []}, "not a row"]}
    decisions = conflicts.decide(before, copy.deepcopy(before), broken)
    assert decisions == []

def test_the_module_does_no_io_and_imports_nothing_that_could():
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "organizer" / "conflicts.py").read_text(
        encoding="utf-8"
    )
    for forbidden in ("import os", "import sqlite3", "import requests", "import urllib",
                      "import socket", "import subprocess", "open(", "os.remove", "shutil"):
        assert forbidden not in source, forbidden

