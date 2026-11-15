"""W4: immutable scopes and the two-source membership check (EA-06, EA-08).

EA-06: a GroupScope answered to plain attribute assignment, so a checkpoint
could be widened behind the fingerprint that was supposed to describe it.
EA-08: build_scope repeated the top-level "complete" flag instead of checking
the memberships block against the artist rows, so a snapshot contradicting
itself still produced a scope marked complete.
"""
from __future__ import annotations

import copy

import pytest

from organizer.common import OrganizerError
from organizer.group_scope import GroupScope, build_scope, ensure_current

ATTRIBUTES = (
    "kind", "snapshot_id", "group_pairs", "groups", "artist_ids", "overlap",
    "source", "completeness", "unknown_reason", "sides", "blocks", "fingerprint",
)


def artist(artist_id, groups, *, visibility="0", is_following=None):
    row = {"artist_id": artist_id, "groups": list(groups), "visibility": visibility}
    if is_following is not None:
        row["is_following"] = is_following
    return row


def snapshot(artists, *, memberships=None, status="complete", complete=True):
    data = {"id": "snap-1", "status": status, "artists": dict(artists),
            "memberships_complete": complete}
    if memberships is not None:
        data["memberships"] = memberships
    return data


def code_of(call):
    with pytest.raises(OrganizerError) as caught:
        call()
    return caught.value.code


def one_member(**kwargs):
    rows = {"1": artist("1", ["g"])}
    return build_scope(snapshot(rows, memberships={"show": {"g": {"ids": ["1"]}}}, **kwargs),
                       groups=["g"])


@pytest.mark.parametrize("name", ATTRIBUTES)
def test_no_attribute_of_a_scope_can_be_assigned_or_deleted(name):
    scope = one_member()
    with pytest.raises(AttributeError):
        setattr(scope, name, "replaced")
    with pytest.raises(AttributeError):
        delattr(scope, name)
    assert scope.artist_ids == ("1",)


def test_the_export_is_a_fresh_copy_and_never_the_live_state():
    rows = {"1": artist("1", ["g", "h"])}
    scope = build_scope(
        snapshot(rows, memberships={"show": {"g": {"ids": ["1"]}, "h": {"ids": ["1"]}}}),
        groups=["g", "h"],
    )
    dump = scope.as_dict()
    dump["artist_ids"].append("999")
    dump["blocks"][0][1].append("999")
    dump["overlap"]["1"].append("zzz")
    dump["source"]["visibility"] = "hide"
    assert scope.artist_ids == ("1",)
    assert scope.blocks == (("show", ("1",)),)
    assert scope.overlap == {"1": ("g", "h")}
    assert scope.source["visibility"] == "show"
    assert scope.fingerprint == GroupScope.from_dict(scope.as_dict()).fingerprint


def test_the_constructor_does_not_share_the_callers_containers():
    members = ["1"]
    blocks = [["show", ["1"]]]
    overlap = {"1": ["g"]}
    source = {"visibility": "show"}
    scope = GroupScope(kind="group_download", snapshot_id="s", group_pairs=[("show", "g")],
                       artist_ids=members, sides=["show"], blocks=blocks,
                       overlap=overlap, source=source, completeness="complete",
                       unknown_reason="")
    members.append("999")
    blocks[0][1].append("999")
    overlap["1"].append("h")
    source["visibility"] = "hide"
    assert scope.artist_ids == ("1",)
    assert scope.blocks == (("show", ("1",)),)
    assert scope.overlap == {"1": ("g",)}
    assert scope.source["visibility"] == "show"


def test_a_scope_can_still_be_copied():
    scope = one_member()
    assert copy.copy(scope) == scope
    assert copy.deepcopy(scope) == scope
    assert copy.deepcopy(scope).fingerprint == scope.fingerprint


def test_a_complete_flag_does_not_override_a_membership_contradiction():
    # The memberships block lists 1 and 2; only artist 1 exists as a row.
    data = snapshot({"1": artist("1", ["g"])},
                    memberships={"show": {"g": {"ids": ["1", "2"]}}})
    scope = build_scope(data, groups=["g"])
    assert scope.completeness == "partial"
    assert scope.complete is False
    assert scope.unknown_reason == "scope_membership_mismatch:show:g"
    assert scope.source["membership_mismatches"] == ["show:g"]
    assert scope.source["catalog_complete"] is False
    # The member that is visible is still reported; it is just not provably all.
    assert scope.artist_ids == ("1",)


def test_an_artist_row_the_block_does_not_list_is_a_mismatch_too():
    data = snapshot({"1": artist("1", ["g"]), "2": artist("2", ["g"])},
                    memberships={"show": {"g": {"ids": ["1"]}}})
    scope = build_scope(data, groups=["g"])
    assert scope.completeness == "partial"
    assert scope.source["membership_mismatches"] == ["show:g"]


def test_a_group_without_recorded_ids_is_not_reported_as_cross_checked():
    data = snapshot({"1": artist("1", ["g"])}, memberships={"show": {"g": {}}})
    scope = build_scope(data, groups=["g"])
    # Nothing contradicts anything, so the top-level claim stands...
    assert scope.completeness == "complete"
    # ...but the scope records that the block never enumerated the group, so a
    # reader can tell this apart from a cross-checked scope.
    assert scope.source["memberships"] == "partially_recorded"
    assert scope.source["membership_mismatches"] == []


def test_a_snapshot_without_a_memberships_block_says_so():
    scope = build_scope(snapshot({"1": artist("1", ["g"])}), groups=["g"])
    assert scope.completeness == "complete"
    assert scope.source["memberships"] == "not_recorded"


def test_both_visibility_sides_are_checked_separately():
    rows = {"1": artist("1", ["g"], visibility="0"),
            "2": artist("2", ["g"], visibility="1")}
    good = snapshot(rows, memberships={"show": {"g": {"ids": ["1"]}},
                                       "hide": {"g": {"ids": ["2"]}}})
    scope = build_scope(good, groups=[("show", "g"), ("hide", "g")])
    assert scope.completeness == "complete"
    bad = snapshot(rows, memberships={"show": {"g": {"ids": ["1"]}},
                                      "hide": {"g": {"ids": ["2", "3"]}}})
    broken = build_scope(bad, groups=[("show", "g"), ("hide", "g")])
    assert broken.completeness == "partial"
    assert broken.source["membership_mismatches"] == ["hide:g"]


@pytest.mark.parametrize(
    "memberships,code",
    [
        ("not a mapping", "snapshot_membership_invalid"),
        ({"show": "not a mapping"}, "snapshot_membership_invalid"),
        ({"show": {"g": "not a mapping"}}, "snapshot_membership_invalid"),
        ({"show": {"g": {"ids": "12"}}}, "snapshot_membership_invalid"),
        ({"show": {"g": {"ids": [1, {"x": 1}]}}}, "snapshot_membership_invalid"),
    ],
)
def test_a_broken_membership_block_is_refused(memberships, code):
    data = snapshot({"1": artist("1", ["g"])}, memberships=memberships)
    assert code_of(lambda: build_scope(data, groups=["g"])) == code


def test_a_complete_snapshot_round_trips():
    rows = {"1": artist("1", ["g"]), "2": artist("2", ["g", "h"])}
    data = snapshot(rows, memberships={"show": {"g": {"ids": ["1", "2"]},
                                                "h": {"ids": ["2"]}}})
    scope = build_scope(data, groups=["g", "h"])
    restored = GroupScope.from_dict(scope.as_dict())
    assert restored == scope
    assert restored.fingerprint == scope.fingerprint
    assert restored.completeness == "complete"
    assert restored.source["memberships"] == "cross_checked"


def test_a_dump_with_an_edited_member_list_is_refused():
    scope = one_member()
    dump = scope.as_dict()
    dump["artist_ids"].append("999")
    assert code_of(lambda: GroupScope.from_dict(dump)) == "scope_members_inconsistent"
    other = scope.as_dict()
    other["blocks"][0][1].append("999")
    assert code_of(lambda: GroupScope.from_dict(other)) == "scope_members_inconsistent"


def test_a_recomputed_fingerprint_is_outside_what_a_digest_can_prove():
    """The fingerprint detects drift; it is not a signature. Say so, test it.

    Anyone who edits the members can also recompute the digest, and from_dict
    then accepts the result.  The documentation calls it a consistency digest
    for exactly this reason; this test keeps that sentence honest.
    """
    scope = one_member()
    forged = GroupScope(kind="group_download", snapshot_id="snap-1",
                        group_pairs=[("show", "g")], artist_ids=["999"],
                        sides=["show"], blocks=[("show", ["999"])], overlap={},
                        source={"visibility": "show"}, completeness="complete",
                        unknown_reason="")
    dump = scope.as_dict()
    dump["artist_ids"] = ["999"]
    dump["blocks"] = [["show", ["999"]]]
    dump["fingerprint"] = forged.fingerprint
    accepted = GroupScope.from_dict(dump)
    assert accepted.artist_ids == ("999",)
    assert accepted.fingerprint == forged.fingerprint
    assert forged.artist_ids == ("999",)


def test_ensure_current_refuses_weaker_evidence_for_the_same_members():
    rows = {"1": artist("1", ["g"])}
    block = {"show": {"g": {"ids": ["1"]}}}
    before = build_scope(snapshot(rows, memberships=block), groups=["g"])
    assert before.completeness == "complete"
    weaker = snapshot(rows, memberships=block, complete=False)
    with pytest.raises(OrganizerError) as caught:
        ensure_current(before, weaker)
    assert caught.value.code == "scope_evidence_degraded"


def test_ensure_current_returns_better_evidence_for_the_same_members():
    rows = {"1": artist("1", ["g"])}
    block = {"show": {"g": {"ids": ["1"]}}}
    before = build_scope(snapshot(rows, memberships=block, complete=False), groups=["g"])
    assert before.completeness == "partial"
    fresh = ensure_current(before, snapshot(rows, memberships=block))
    assert fresh.fingerprint == before.fingerprint
    assert fresh.completeness == "complete"


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("blocks", "not a list", "scope_field_invalid:blocks"),
        ("blocks", [["show"]], "scope_field_invalid:blocks"),
        ("group_pairs", [["show"]], "scope_field_invalid:group_pairs"),
        ("artist_ids", "1", "scope_field_invalid:artist_ids"),
        ("sides", "show", "scope_field_invalid:sides"),
        ("overlap", [], "scope_field_invalid:overlap"),
        ("source", [], "scope_field_invalid:source"),
        ("sides", ["public"], "scope_group_invalid"),
        ("completeness", "mostly", "scope_completeness_invalid"),
    ],
)
def test_bad_shapes_in_a_dump_are_named_not_guessed(field, value, code):
    dump = one_member().as_dict()
    dump[field] = value
    assert code_of(lambda: GroupScope.from_dict(dump)) == code


def test_a_dump_missing_a_field_names_it():
    dump = one_member().as_dict()
    dump.pop("sides")
    assert code_of(lambda: GroupScope.from_dict(dump)) == "scope_field_missing:sides"

