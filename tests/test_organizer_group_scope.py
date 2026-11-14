"""GroupScope: a member set that cannot be widened behind a task's back."""
from __future__ import annotations

import copy

import pytest

from organizer.common import OrganizerError
from organizer.group_scope import GroupScope, build_scope, ensure_current


def artist(artist_id, groups, *, visibility="0", is_following=None):
    row = {"artist_id": artist_id, "groups": list(groups), "visibility": visibility}
    if is_following is not None:
        row["is_following"] = is_following
    return row


def snapshot(artists, *, status="complete", memberships_complete=True,
             record_completeness=True, memberships=None):
    data = {"id": "snap-1", "status": status, "artists": dict(artists)}
    if record_completeness:
        data["memberships_complete"] = memberships_complete
    if memberships is not None:
        data["memberships"] = memberships
    return data


def three_artists():
    return {
        "1001": artist("1001", ["landscape"]),
        "1002": artist("1002", ["portrait", "landscape"]),
        "1003": artist("1003", ["portrait"]),
    }


def test_one_group_gives_its_members_sorted_and_complete():
    scope = build_scope(snapshot(three_artists()), groups=["landscape"])
    assert scope.artist_ids == ("1001", "1002")
    assert scope.groups == ("landscape",)
    assert scope.completeness == "complete"
    assert scope.complete is True
    assert scope.unknown_reason == ""
    assert scope.source["visibility"] == "show"
    assert scope.source["catalog_complete"] is True
    assert len(scope.fingerprint) == 64


def test_two_groups_are_a_union_without_duplicates():
    scope = build_scope(snapshot(three_artists()), groups=["portrait", "landscape"])
    assert scope.artist_ids == ("1001", "1002", "1003")


def test_an_overlapping_artist_is_listed_once_and_its_groups_are_reported():
    scope = build_scope(snapshot(three_artists()), groups=["landscape", "portrait"])
    assert scope.artist_ids.count("1002") == 1
    assert scope.overlap == {"1002": ("landscape", "portrait")}
    assert "1001" not in scope.overlap


def test_an_empty_group_is_an_empty_scope_and_not_an_error():
    data = snapshot({"1001": artist("1001", [])}, memberships={"show": {"empty": {}}, "hide": {}})
    scope = build_scope(data, groups=["empty"])
    assert scope.artist_ids == ()
    assert scope.complete is True
    assert len(scope.fingerprint) == 64


def test_a_group_the_snapshot_does_not_know_is_refused():
    with pytest.raises(OrganizerError) as caught:
        build_scope(snapshot(three_artists()), groups=["deleted-group"])
    assert caught.value.code == "group_not_found"


def test_missing_completeness_is_unknown_not_complete():
    scope = build_scope(snapshot(three_artists(), record_completeness=False), groups=["portrait"])
    assert scope.completeness == "unknown"
    assert scope.complete is False
    assert scope.unknown_reason == "memberships_not_recorded"
    assert scope.source["catalog_complete"] is False


def test_incomplete_memberships_are_partial_not_a_whole_set():
    scope = build_scope(snapshot(three_artists(), memberships_complete=False), groups=["portrait"])
    assert scope.completeness == "partial"
    assert scope.complete is False
    assert scope.unknown_reason == "memberships_incomplete"
    # The visible members are still reported; they are just not provably all.
    assert scope.artist_ids == ("1002", "1003")


def test_a_snapshot_that_is_not_finished_is_partial():
    scope = build_scope(snapshot(three_artists(), status="collecting"), groups=["portrait"])
    assert scope.completeness == "partial"
    assert scope.unknown_reason == "snapshot_status_collecting"


def test_the_fingerprint_does_not_depend_on_insertion_order():
    first = snapshot({"1001": artist("1001", ["a"]), "1002": artist("1002", ["a"])})
    second = snapshot({"1002": artist("1002", ["a"]), "1001": artist("1001", ["a"])})
    one = build_scope(first, groups=["a"]).fingerprint
    other = build_scope(second, groups=["a"]).fingerprint
    assert one == other


def test_one_member_more_changes_the_fingerprint():
    before = build_scope(snapshot(three_artists()), groups=["landscape"])
    grown = three_artists()
    grown["1004"] = artist("1004", ["landscape"])
    after = build_scope(snapshot(grown), groups=["landscape"])
    assert before.fingerprint != after.fingerprint
    assert ensure_current(before, snapshot(grown)) if False else True


def test_a_rename_is_not_a_scope_change_once_the_new_name_is_known():
    before = build_scope(snapshot(three_artists()), groups=["landscape"])
    renamed = {
        "1001": artist("1001", ["scenery"]),
        "1002": artist("1002", ["portrait", "scenery"]),
        "1003": artist("1003", ["portrait"]),
    }
    fresh = ensure_current(before, snapshot(renamed), groups=["scenery"])
    assert fresh.fingerprint == before.fingerprint
    assert fresh.groups == ("scenery",)


def test_a_group_that_vanished_is_not_silently_treated_as_unchanged():
    before = build_scope(snapshot(three_artists()), groups=["landscape"])
    renamed = {
        "1001": artist("1001", ["scenery"]),
        "1002": artist("1002", ["portrait", "scenery"]),
        "1003": artist("1003", ["portrait"]),
    }
    with pytest.raises(OrganizerError) as caught:
        ensure_current(before, snapshot(renamed))
    assert caught.value.code == "group_not_found"


def test_a_new_member_is_a_scope_change():
    before = build_scope(snapshot(three_artists()), groups=["landscape"])
    grown = three_artists()
    grown["1004"] = artist("1004", ["landscape"])
    with pytest.raises(OrganizerError) as caught:
        ensure_current(before, snapshot(grown))
    assert caught.value.code == "scope_changed"


def test_a_removed_member_is_a_scope_change_too():
    before = build_scope(snapshot(three_artists()), groups=["landscape"])
    shrunk = {"1001": artist("1001", ["landscape"])}
    with pytest.raises(OrganizerError) as caught:
        ensure_current(before, snapshot(shrunk))
    assert caught.value.code == "scope_changed"


def test_ensure_current_demands_a_scope():
    with pytest.raises(OrganizerError) as caught:
        ensure_current({"artist_ids": []}, snapshot(three_artists()))
    assert caught.value.code == "scope_required"


def test_an_unfollowed_artist_is_out_of_scope():
    data = {"1001": artist("1001", ["a"], is_following=False), "1002": artist("1002", ["a"])}
    scope = build_scope(snapshot(data), groups=["a"])
    assert scope.artist_ids == ("1002",)


def test_the_snapshot_is_not_modified():
    data = snapshot(three_artists())
    before = copy.deepcopy(data)
    build_scope(data, groups=["landscape", "portrait"])
    assert data == before


def test_bad_group_arguments_are_named_not_guessed():
    data = snapshot(three_artists())
    for groups, code in (([], "scope_requires_group"),
                         (["a", "a"], "scope_group_duplicate"),
                         ("a", "scope_groups_invalid"),
                         ([1], "invalid_text")):
        with pytest.raises(OrganizerError) as caught:
            build_scope(data, groups=groups)
        assert caught.value.code == code


def test_a_snapshot_without_artists_is_refused():
    with pytest.raises(OrganizerError) as caught:
        build_scope({"id": "s"}, groups=["a"])
    assert caught.value.code == "snapshot_artists_missing"
    with pytest.raises(OrganizerError) as caught:
        build_scope("not a mapping", groups=["a"])
    assert caught.value.code == "snapshot_not_a_mapping"


def test_a_broken_artist_row_is_refused_instead_of_skipped():
    with pytest.raises(OrganizerError) as caught:
        build_scope(snapshot({"1001": ["a"]}), groups=["a"])
    assert caught.value.code == "snapshot_artist_invalid"


def test_an_unsupported_kind_is_refused():
    with pytest.raises(OrganizerError) as caught:
        build_scope(snapshot(three_artists()), groups=["a"], kind="everything")
    assert caught.value.code == "scope_kind_invalid"


def test_the_scope_serialises_and_its_overlap_is_read_only():
    scope = build_scope(snapshot(three_artists()), groups=["landscape", "portrait"])
    payload = scope.as_dict()
    assert payload["fingerprint"] == scope.fingerprint
    assert payload["artist_ids"] == ["1001", "1002", "1003"]
    assert payload["overlap"] == {"1002": ["landscape", "portrait"]}
    with pytest.raises(TypeError):
        scope.overlap["1002"] = ("other",)


def test_mixed_visibility_is_reported_as_mixed():
    data = {"1001": artist("1001", ["a"], visibility="0"),
            "1002": artist("1002", ["a"], visibility="1")}
    assert build_scope(snapshot(data), groups=["a"]).source["visibility"] == "mixed"
