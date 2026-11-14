"""The fixtures are data, so they must be checked like data."""
from __future__ import annotations

import inspect

import pytest

import follow_fixtures as fixtures

RUN_KEYS = {"run_id", "account_ref", "config_key", "status", "stop_reason", "totals"}
ARTIST_KEYS = {"artist_id", "state", "pages", "newest_illust_id"}
INCOMPLETE_KEYS = {"artist_id", "reason"}


def _all():
    return {name: builder() for name, builder in fixtures.ALL_BUILDERS.items()}


def test_every_builder_returns_the_frozen_shape():
    for name, facts in _all().items():
        assert set(facts) == {"run", "artists", "incomplete", "budget"}, name
        run = facts["run"]
        assert set(run) == RUN_KEYS, name
        assert isinstance(run["run_id"], str) and run["run_id"], name
        assert isinstance(run["status"], str), name
        assert isinstance(run["totals"], dict), name
        for artist in facts["artists"]:
            assert set(artist) == ARTIST_KEYS, (name, artist)
            assert isinstance(artist["artist_id"], str) and artist["artist_id"], name
            assert isinstance(artist["pages"], int) and artist["pages"] >= 0, name
        for item in facts["incomplete"]:
            assert set(item) == INCOMPLETE_KEYS, (name, item)
        budget = facts["budget"]
        if budget is not None:
            assert set(budget) == {"state", "remaining"}, name
            assert isinstance(budget["remaining"], int), name


def test_artists_are_sorted_and_unique():
    for name, facts in _all().items():
        ids = [artist["artist_id"] for artist in facts["artists"]]
        assert ids == sorted(ids), name
        assert len(ids) == len(set(ids)), name


def test_the_run_totals_agree_with_the_page_counts():
    for name, facts in _all().items():
        totals = facts["run"]["totals"]
        assert totals["works_seen"] == totals["pages_saved"] + totals["excluded_by_filter"], name
        assert totals["pages_saved"] == sum(artist["pages"] for artist in facts["artists"]), name


def test_all_complete_has_nothing_pending():
    facts = fixtures.all_complete()
    assert facts["incomplete"] == []
    assert {artist["state"] for artist in facts["artists"]} == {"completed"}
    assert facts["run"]["status"] == "completed"


def test_partial_failed_pages_names_the_failed_artist():
    facts = fixtures.partial_failed_pages()
    failed = [artist for artist in facts["artists"] if artist["state"] == "failed"]
    assert failed, "the fixture must contain a failed artist"
    assert [item["artist_id"] for item in facts["incomplete"]] == [failed[0]["artist_id"]]
    assert facts["run"]["status"] == "completed", "a completed run with a failed artist is the trap"


def test_budget_stopped_is_paused_on_the_budget_with_nothing_left():
    facts = fixtures.budget_stopped()
    assert facts["run"]["status"] == "paused"
    assert facts["run"]["stop_reason"] == "budget"
    assert facts["budget"]["state"] == "exhausted"
    assert facts["budget"]["remaining"] == 0
    assert [artist["state"] for artist in facts["artists"]].count("pending") == 2


def test_rate_limited_paused_still_has_budget_left():
    facts = fixtures.rate_limited_paused()
    assert facts["run"]["status"] == "paused"
    assert facts["run"]["stop_reason"] == "rate_limited"
    assert facts["budget"]["remaining"] > 0, "a cooldown is not a budget stop"
    assert any(artist["state"] == "running" for artist in facts["artists"])


def test_duplicate_pages_are_counted_and_bounded():
    facts = fixtures.duplicate_pages()
    totals = facts["run"]["totals"]
    assert totals["duplicate_pages"] > 0
    assert totals["duplicate_pages"] <= totals["pages_saved"]


def test_all_filtered_saves_nothing_and_fails_nothing():
    facts = fixtures.all_filtered()
    totals = facts["run"]["totals"]
    assert totals["pages_saved"] == 0
    assert totals["excluded_by_filter"] == totals["works_seen"]
    assert all(artist["state"] == "completed" for artist in facts["artists"])
    assert facts["incomplete"] == []


def test_private_scope_missing_has_no_account_reference():
    facts = fixtures.private_scope_missing()
    assert facts["run"]["account_ref"] == ""
    assert facts["run"]["totals"]["unknown_fields"] == 1


def test_empty_following_is_a_real_zero_not_a_null():
    facts = fixtures.empty_following()
    assert facts["artists"] == []
    assert facts["run"]["totals"]["total_artists"] == 0
    assert facts["incomplete"] == []


def test_builders_are_deterministic():
    for name, builder in fixtures.ALL_BUILDERS.items():
        assert builder() == builder(), name


def test_the_fixture_module_does_not_import_the_lanes_under_construction():
    source = inspect.getsource(fixtures)
    for forbidden in ("import core", "from core", "follow_coverage", "follow_facts"):
        assert forbidden not in source, forbidden


@pytest.mark.parametrize("name", sorted(fixtures.ALL_BUILDERS))
def test_every_builder_is_reachable_by_name(name):
    facts = fixtures.ALL_BUILDERS[name]()
    assert facts["run"]["totals"]["total_artists"] == len(facts["artists"])