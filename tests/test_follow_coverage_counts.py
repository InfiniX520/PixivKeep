"""Counters, denominators and the identity that decides 'consistent'."""
from __future__ import annotations

import pytest

from core.follow_coverage_counts import COUNT_KEYS, coverage_counts


def facts(artists, *, totals=None, status="running", incomplete=None):
    payload = {"run": {"status": status, "stop_reason": ""}, "artists": list(artists)}
    if totals is not None:
        payload["run"]["totals"] = dict(totals)
    payload["incomplete"] = list(incomplete or [])
    return payload


def one(artist_id, state):
    return {"artist_id": artist_id, "state": state, "pages": 0, "newest_illust_id": ""}


def test_every_bucket_comes_from_a_recorded_state():
    report = coverage_counts(
        facts([
            one("a", "completed"), one("b", "empty"),
            one("c", "pending"), one("d", "running"), one("e", "deferred"),
            one("f", "failed"),
        ])
    )
    counts = report["counts"]
    assert counts["artists_confirmed"] == 2
    assert counts["artists_pending"] == 3
    assert counts["artists_failed_pages"] == 1
    assert counts["artists_coverage_unverified"] == 0
    assert counts["artists_checked"] == 6
    assert report["consistent"] is True


def test_an_unrecognised_state_is_unverified_not_pending():
    report = coverage_counts(facts([one("a", "completed"), one("b", "brand_new_state")]))
    assert report["counts"]["artists_coverage_unverified"] == 1
    assert report["counts"]["artists_pending"] == 0
    assert "unknown_artist_state:brand_new_state" in report["reasons"]


def test_the_bucket_identity_is_the_consistent_flag():
    report = coverage_counts(facts([one("a", "completed"), one("b", "failed")]))
    counts = report["counts"]
    total = (
        counts["artists_confirmed"]
        + counts["artists_pending"]
        + counts["artists_failed_pages"]
        + counts["artists_coverage_unverified"]
    )
    assert total == counts["artists_checked"]
    assert report["consistent"] is True


def test_a_duplicate_artist_is_counted_once_and_named():
    report = coverage_counts(facts([one("a", "completed"), one("a", "completed")]))
    assert report["counts"]["artists_checked"] == 1
    assert "duplicate_artist:1" in report["reasons"]


def test_a_duplicate_with_two_states_is_flagged_as_a_conflict():
    report = coverage_counts(facts([one("a", "completed"), one("a", "failed")]))
    assert report["counts"]["artists_checked"] == 1
    assert "duplicate_artist_state_conflict:a" in report["reasons"]
    assert report["counts"]["artists_failed_pages"] == 0


def test_totals_are_passed_through_and_never_invented():
    report = coverage_counts(
        facts([one("a", "completed")], totals={"artworks_seen": 480, "pages_saved": 173})
    )
    assert report["counts"]["artworks_seen"] == 480
    assert report["counts"]["pages_saved"] == 173
    # Nothing in the facts measured these, so they stay null instead of zero.
    assert report["counts"]["excluded_by_filter"] is None
    assert report["counts"]["unknown_fields"] is None
    assert "not_measured:excluded_by_filter" in report["reasons"]


def test_a_denominator_is_null_unless_a_run_total_provides_it():
    report = coverage_counts(facts([one("a", "completed"), one("b", "completed")]))
    assert report["denominators"] == {"artists_total": None, "artworks_total": None}
    assert "denominator_unknown:artists_total" in report["reasons"]


def test_a_list_length_is_not_a_denominator():
    # Two artists in the list, no run total: the count of the list must not be
    # promoted into "there are two artists in the world".
    report = coverage_counts(facts([one("a", "completed"), one("b", "completed")]))
    assert report["denominators"]["artists_total"] is None


def test_pages_saved_cannot_exceed_artworks_seen():
    report = coverage_counts(
        facts([one("a", "completed")], totals={"artworks_seen": 10, "pages_saved": 11})
    )
    assert report["consistent"] is False
    assert "pages_saved_exceeds_artworks_seen" in report["reasons"]


def test_a_denominator_below_the_seen_artists_is_inconsistent():
    report = coverage_counts(
        facts([one("a", "completed"), one("b", "completed")], totals={"artists_total": 1})
    )
    assert report["consistent"] is False
    assert "denominator_below_seen_artists" in report["reasons"]


def test_a_negative_total_is_not_used_as_a_number():
    report = coverage_counts(
        facts([one("a", "completed")], totals={"artworks_seen": -5, "pages_saved": 3})
    )
    assert report["counts"]["artworks_seen"] is None
    assert "not_measured:artworks_seen" in report["reasons"]


def test_counts_are_never_negative():
    report = coverage_counts(facts([one("a", "completed")], totals={"pages_saved": 0}))
    for key in COUNT_KEYS:
        value = report["counts"][key]
        assert value is None or value >= 0, key


def test_the_report_carries_exactly_the_frozen_keys():
    report = coverage_counts(facts([one("a", "completed")]))
    assert set(report) == {"counts", "denominators", "consistent", "reasons"}
    assert set(report["counts"]) == set(COUNT_KEYS)
    assert set(report["denominators"]) == {"artists_total", "artworks_total"}


def test_reasons_are_sorted_and_unique():
    report = coverage_counts(
        facts([one("a", "x"), one("b", "x")], totals={"pages_saved": -1, "artworks_seen": -1})
    )
    assert report["reasons"] == sorted(set(report["reasons"]))


def test_an_empty_following_list_is_consistent_and_zeroed():
    report = coverage_counts(facts([]))
    assert report["counts"]["artists_checked"] == 0
    assert report["counts"]["artists_confirmed"] == 0
    assert report["consistent"] is True


def test_a_missing_artists_key_is_refused():
    with pytest.raises(ValueError):
        coverage_counts({"run": {}})


def test_non_mapping_facts_are_refused():
    with pytest.raises(TypeError):
        coverage_counts([])


def test_an_artist_without_an_id_or_state_is_refused():
    with pytest.raises(ValueError):
        coverage_counts(facts([{"state": "completed"}]))
    with pytest.raises(ValueError):
        coverage_counts(facts([{"artist_id": "a"}]))
