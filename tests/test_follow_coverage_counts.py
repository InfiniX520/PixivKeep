"""Counters, denominators and the identity that decides 'consistent'.

The rulings pinned here:

* run level numbers come only from facts["run"]["totals"] - with **one**
  exception: works_seen may be the sum of per-artist page counts, but only when
  the artist list is proven to be the whole run;
* a list length is never a denominator: artists_total stays null when the run
  declared no total, and the reason says so;
* an unrecognised state is unverified **and** makes the report inconsistent.
"""
from __future__ import annotations

import pytest

from core.follow_coverage_counts import (
    ARTIST_COUNT_KEYS,
    COUNT_KEYS,
    RUN_COUNT_KEYS,
    coverage_counts,
)


def one(artist_id, state, pages=0):
    return {"artist_id": artist_id, "state": state, "pages": pages, "newest_illust_id": ""}


def facts(artists, *, totals=None, status="running"):
    payload = {
        "run": {"status": status, "stop_reason": ""},
        "artists": list(artists),
        "incomplete": [],
    }
    if totals is not None:
        payload["run"]["totals"] = dict(totals)
    return payload


def complete_totals(count, works=480, saved=173, excluded=307, duplicates=5):
    return {
        "total_artists": count,
        "works_seen": works,
        "pages_saved": saved,
        "pages_already_verified": 420,
        "excluded_by_filter": excluded,
        "unknown_fields": 3,
        "duplicate_pages": duplicates,
        "artworks_total": 500,
    }


def test_every_bucket_comes_from_a_recorded_state():
    report = coverage_counts(
        facts([
            one("a", "completed"), one("b", "empty"),
            one("c", "pending"), one("d", "running"), one("e", "deferred"),
            one("f", "failed"),
        ], totals={"total_artists": 6})
    )
    counts = report["counts"]
    assert counts["artists_confirmed"] == 2
    assert counts["artists_pending"] == 3
    assert counts["artists_failed_pages"] == 1
    assert counts["artists_coverage_unverified"] == 0
    assert counts["artists_checked"] == 6
    assert report["consistent"] is True


def test_an_unrecognised_state_is_unverified_and_inconsistent():
    report = coverage_counts(
        facts([one("a", "completed"), one("b", "brand_new_state")], totals={"total_artists": 2})
    )
    assert report["counts"]["artists_coverage_unverified"] == 1
    assert report["counts"]["artists_pending"] == 0
    assert "unknown_artist_state:brand_new_state" in report["reasons"]
    assert report["consistent"] is False


def test_the_bucket_identity_is_checked():
    report = coverage_counts(
        facts([one("a", "completed"), one("b", "failed")], totals={"total_artists": 2})
    )
    counts = report["counts"]
    total = (
        counts["artists_confirmed"]
        + counts["artists_pending"]
        + counts["artists_failed_pages"]
        + counts["artists_coverage_unverified"]
    )
    assert total == counts["artists_checked"]
    assert report["consistent"] is True


# ---------------------------------------------------------------- works_seen


def test_works_seen_is_summed_only_when_the_list_is_proven_complete():
    report = coverage_counts(
        facts(
            [one("a", "completed", pages=30), one("b", "empty", pages=12)],
            totals={"total_artists": 2},
        )
    )
    assert report["counts"]["artworks_seen"] == 42
    assert "artworks_seen_from_artist_sum" in report["reasons"]
    # The annotation is about provenance, not about trust: it does not, by
    # itself, flip consistency.
    assert report["consistent"] is True


def test_works_seen_stays_unknown_when_the_list_may_be_a_page():
    report = coverage_counts(
        facts([one("a", "completed", pages=30)], totals={"total_artists": 9})
    )
    assert report["counts"]["artworks_seen"] is None
    assert "artworks_seen_unknown:artist_list_not_proven_complete" in report["reasons"]


def test_works_seen_stays_unknown_without_a_declared_total():
    report = coverage_counts(facts([one("a", "completed", pages=30)]))
    assert report["counts"]["artworks_seen"] is None
    assert "artworks_seen_unknown:artist_list_not_proven_complete" in report["reasons"]


def test_works_seen_stays_unknown_when_a_page_count_is_missing():
    report = coverage_counts(
        facts(
            [one("a", "completed", pages=30),
             {"artist_id": "b", "state": "completed", "pages": None}],
            totals={"total_artists": 2},
        )
    )
    assert report["counts"]["artworks_seen"] is None
    assert "artworks_seen_unknown:artist_list_not_proven_complete" in report["reasons"]


def test_a_declared_works_seen_beats_the_sum_even_when_they_differ():
    report = coverage_counts(
        facts([one("a", "completed", pages=30)], totals={"total_artists": 1, "works_seen": 99})
    )
    assert report["counts"]["artworks_seen"] == 99
    assert "artworks_seen_from_artist_sum" not in report["reasons"]


def test_a_negative_declared_works_seen_falls_back_to_the_provable_sum():
    report = coverage_counts(
        facts([one("a", "completed", pages=7)], totals={"total_artists": 1, "works_seen": -5})
    )
    assert report["counts"]["artworks_seen"] == 7
    assert "artworks_seen_from_artist_sum" in report["reasons"]


# ------------------------------------------------------- the other five totals


def test_the_other_run_level_numbers_are_never_derived():
    report = coverage_counts(
        facts([one("a", "completed", pages=99)], totals={"total_artists": 1})
    )
    derived_from_nothing = (
        "pages_saved",
        "pages_already_verified",
        "excluded_by_filter",
        "unknown_fields",
        "duplicate_pages",
    )
    for key in derived_from_nothing:
        assert report["counts"][key] is None, key
        assert "not_measured:" + key in report["reasons"], key


def test_the_six_run_level_keys_are_passed_through_untouched():
    totals = complete_totals(1)
    report = coverage_counts(facts([one("a", "completed")], totals=totals))
    # The report word and the storage word differ for exactly one key.
    source = {"artworks_seen": "works_seen"}
    for key in RUN_COUNT_KEYS:
        assert report["counts"][key] == totals[source.get(key, key)], key


# ------------------------------------------------------------- denominators


def test_a_missing_total_artists_leaves_the_denominator_unknown():
    report = coverage_counts(facts([one("a", "completed"), one("b", "completed")]))
    assert report["denominators"]["artists_total"] is None
    assert "denominator_unknown:artists_total" in report["reasons"]


def test_a_list_length_is_never_promoted_to_a_denominator():
    report = coverage_counts(facts([one("a", "completed"), one("b", "completed")]))
    assert report["denominators"]["artists_total"] is None


def test_a_mismatched_total_artists_keeps_the_declared_number_and_flags_it():
    report = coverage_counts(facts([one("a", "completed")], totals={"total_artists": 9}))
    assert report["denominators"]["artists_total"] == 9
    assert "total_artists_mismatch:9!=1" in report["reasons"]
    assert report["consistent"] is False


def test_artworks_total_is_null_without_a_run_total():
    report = coverage_counts(facts([one("a", "completed")], totals={"total_artists": 1}))
    assert report["denominators"]["artworks_total"] is None


def test_works_total_is_accepted_as_the_same_declaration():
    report = coverage_counts(
        facts([one("a", "completed")], totals={"total_artists": 1, "works_total": 42})
    )
    assert report["denominators"]["artworks_total"] == 42


def test_a_summed_works_seen_never_reaches_the_denominator():
    report = coverage_counts(
        facts([one("a", "completed", pages=30)], totals={"total_artists": 1})
    )
    assert report["counts"]["artworks_seen"] == 30
    assert report["denominators"]["artworks_total"] is None


# ------------------------------------------------------------- consistency


def test_works_seen_must_split_into_saved_and_excluded():
    bad = complete_totals(1)
    bad["works_seen"] = 481
    report = coverage_counts(facts([one("a", "completed")], totals=bad))
    assert report["consistent"] is False
    assert "works_seen_does_not_split:481!=173+307" in report["reasons"]


def test_the_split_is_checked_only_when_all_three_are_known():
    partial = {"total_artists": 1, "works_seen": 10, "pages_saved": 4}
    report = coverage_counts(facts([one("a", "completed")], totals=partial))
    assert "works_seen_does_not_split" not in " ".join(report["reasons"])


def test_duplicate_pages_cannot_exceed_pages_saved():
    bad = complete_totals(1)
    bad["duplicate_pages"] = 174
    report = coverage_counts(facts([one("a", "completed")], totals=bad))
    assert report["consistent"] is False
    assert "duplicate_pages_exceed_pages_saved" in report["reasons"]


# ------------------------------------------------------------- input hygiene


def test_a_duplicate_artist_is_counted_once_and_named():
    report = coverage_counts(
        facts([one("a", "completed"), one("a", "completed")], totals={"total_artists": 1})
    )
    assert report["counts"]["artists_checked"] == 1
    assert "duplicate_artist:1" in report["reasons"]


def test_a_duplicate_with_two_states_is_a_conflict():
    report = coverage_counts(
        facts([one("a", "completed"), one("a", "failed")], totals={"total_artists": 1})
    )
    assert report["counts"]["artists_checked"] == 1
    assert report["counts"]["artists_failed_pages"] == 0
    assert "duplicate_artist_state_conflict:a" in report["reasons"]


def test_the_report_carries_exactly_the_frozen_keys():
    report = coverage_counts(facts([one("a", "completed")], totals=complete_totals(1)))
    assert set(report) == {"counts", "denominators", "consistent", "reasons"}
    assert set(report["counts"]) == set(COUNT_KEYS)
    assert set(ARTIST_COUNT_KEYS) | set(RUN_COUNT_KEYS) == set(COUNT_KEYS)
    assert set(report["denominators"]) == {"artists_total", "artworks_total"}


def test_reasons_are_sorted_and_unique():
    report = coverage_counts(
        facts([one("a", "x"), one("b", "x")], totals={"total_artists": 2, "pages_saved": -1})
    )
    assert report["reasons"] == sorted(set(report["reasons"]))


def test_an_empty_list_with_a_declared_zero_is_consistent():
    totals = complete_totals(0, works=0, saved=0, excluded=0, duplicates=0)
    report = coverage_counts(facts([], totals=totals))
    assert report["counts"]["artists_checked"] == 0
    assert report["denominators"]["artists_total"] == 0
    assert report["consistent"] is True


def test_an_empty_list_without_totals_stays_unknown():
    report = coverage_counts(facts([]))
    assert report["denominators"]["artists_total"] is None
    assert "denominator_unknown:artists_total" in report["reasons"]


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
