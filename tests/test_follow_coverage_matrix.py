"""Cross-lane matrix for the U2-A coverage summary.

The five lanes were built against frozen dictionary shapes, not against each
other's code, so this file is where they have to agree: A5's synthetic facts
go through A1's shape, A2's three states, A3's counters and A4's words, and
the assertions below are about the seams as much as about the numbers.
"""
from __future__ import annotations

import copy
import dataclasses
import json

import pytest

import follow_fixtures as F
from core.follow_coverage import (
    COUNT_KEYS,
    DENOMINATOR_KEYS,
    OVERALL_STATES,
    PAGE_END_STATES,
    CoverageError,
    build_coverage,
)
from core.follow_coverage_state import DECISION_STATES, DELIVERY_STATES, ENUMERATION_STATES

SCOPE = {"kind": "follow_run", "run_id": "run-matrix", "account_ref": "acct-sample"}
SCENARIOS = sorted(F.ALL_BUILDERS)
FORBIDDEN = ("已查完", "已穷尽", "全部完成")


def facts_for(name):
    return F.ALL_BUILDERS[name]()


def coverage_for(name):
    return build_coverage(facts_for(name), scope=SCOPE)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_every_scenario_keeps_the_frozen_shape(scenario):
    cov = coverage_for(scenario)
    data = cov.as_dict()
    assert set(data["counts"]) == set(COUNT_KEYS)
    assert set(data["denominators"]) == set(DENOMINATOR_KEYS)
    assert data["overall"] in OVERALL_STATES
    assert data["enumeration"]["state"] in ENUMERATION_STATES
    assert data["decision"]["state"] in DECISION_STATES
    assert data["delivery"]["state"] in DELIVERY_STATES
    assert data["page_end_evidence"]["state"] in PAGE_END_STATES
    json.dumps(data)  # the CLI has to be able to print it


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_the_artist_buckets_always_add_up(scenario):
    counts = coverage_for(scenario).as_dict()["counts"]
    buckets = (
        counts["artists_confirmed"]
        + counts["artists_pending"]
        + counts["artists_failed_pages"]
        + counts["artists_coverage_unverified"]
    )
    assert buckets == counts["artists_checked"]


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_every_synthetic_scenario_is_self_consistent(scenario):
    cov = coverage_for(scenario)
    assert cov.consistent is True
    assert cov.reasons == ()


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_complete_is_never_claimed_without_all_three_dimensions(scenario):
    cov = coverage_for(scenario)
    data = cov.as_dict()
    all_three = (
        data["enumeration"]["state"] == "complete"
        and data["decision"]["state"] == "applied"
        and data["delivery"]["state"] == "complete"
    )
    assert (data["overall"] == "complete") == (all_three and cov.consistent)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_the_words_reserve_completion_for_the_complete_case(scenario):
    cov = coverage_for(scenario)
    text = cov.text()
    if cov.overall == "complete":
        assert "已查完" in text
    else:
        assert not any(word in text for word in FORBIDDEN)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_page_end_is_unknown_without_a_sweep_fact(scenario):
    evidence = coverage_for(scenario).as_dict()["page_end_evidence"]
    assert evidence["state"] == "unknown"
    assert evidence["reason"] in ("no_artists", "no_sweep_evidence")


def test_partial_failed_pages_survive_a_completed_run():
    """A run that says completed still does not win over a failed artist page."""
    cov = coverage_for("partial_failed_pages")
    data = cov.as_dict()
    assert data["enumeration"]["state"] == "partial"
    assert data["enumeration"]["reason"] == "artist_failed:1002"
    assert data["counts"]["artists_failed_pages"] == 1
    assert cov.overall != "complete"


def test_a_budget_stop_says_so_and_stops_the_conclusion():
    cov = coverage_for("budget_stopped")
    data = cov.as_dict()
    assert data["enumeration"]["state"] == "stopped"
    assert cov.stop_reason == "budget"
    assert "停止原因" in cov.text()
    assert cov.overall != "complete"


def test_a_rate_limit_is_a_stop_and_not_a_budget():
    cov = coverage_for("rate_limited_paused")
    assert cov.as_dict()["enumeration"]["state"] == "stopped"
    assert cov.stop_reason == "rate_limited"
    assert "本批请求预算用完" not in cov.text()


def test_duplicates_are_counted_but_never_added_to_what_was_saved():
    counts = coverage_for("duplicate_pages").as_dict()["counts"]
    assert counts["duplicate_pages"] == 3
    assert counts["duplicate_pages"] <= counts["pages_saved"]
    assert "重复分页 3" in coverage_for("duplicate_pages").text()


def test_a_fully_filtered_scenario_saves_nothing_and_is_still_complete():
    """Everything that was seen was excluded on purpose: nothing is missing."""
    cov = coverage_for("all_filtered")
    data = cov.as_dict()
    assert data["counts"]["pages_saved"] == 0
    assert data["counts"]["excluded_by_filter"] == data["counts"]["artworks_seen"] == 30
    assert data["delivery"]["state"] == "complete"
    assert cov.overall == "complete"


def test_an_empty_following_list_has_a_real_zero_denominator():
    cov = coverage_for("empty_following")
    data = cov.as_dict()
    assert data["counts"]["artists_checked"] == 0
    assert data["denominators"]["artists_total"] == 0
    assert "范围内画师总数 0 位" in cov.text()
    assert "范围内作品总数" not in cov.text()


def test_missing_private_fields_block_the_decision_and_not_the_saving():
    cov = coverage_for("private_scope_missing")
    data = cov.as_dict()
    assert data["decision"]["state"] == "unknown_fields"
    assert data["delivery"]["state"] == "complete"
    assert cov.overall != "complete"
    text = cov.text()
    assert "筛选判定未完成或未应用" in text
    assert "保存未齐" not in text
    assert "没有保存齐" not in text


def test_only_the_clean_scenario_reaches_a_complete_overall():
    overalls = {name: coverage_for(name).overall for name in SCENARIOS}
    assert overalls["all_complete"] == "complete"
    assert overalls["partial_failed_pages"] != "complete"


def test_the_scope_has_to_say_what_it_covers():
    facts = facts_for("all_complete")
    for bad in ({}, {"kind": ""}, {"run_id": "r"}, [("kind", "x")], "follow_run"):
        with pytest.raises(CoverageError) as caught:
            build_coverage(facts, scope=bad)
        assert caught.value.code == "scope_invalid"


def test_the_scope_is_frozen_and_copied():
    scope = {"kind": "follow_run", "run_id": "r1"}
    cov = build_coverage(facts_for("all_complete"), scope=scope)
    scope["run_id"] = "r2"
    assert cov.scope["run_id"] == "r1"
    with pytest.raises(TypeError):
        cov.scope["run_id"] = "r3"


def test_the_facts_have_to_carry_the_four_keys():
    facts = facts_for("all_complete")
    for key in ("run", "artists", "incomplete", "budget"):
        broken = dict(facts)
        broken.pop(key)
        with pytest.raises(CoverageError) as caught:
            build_coverage(broken, scope=SCOPE)
        assert caught.value.code == "facts_shape_invalid"


def test_an_artist_row_that_is_not_a_mapping_is_refused():
    facts = facts_for("all_complete")
    facts["artists"] = ["1001"]
    with pytest.raises(CoverageError) as caught:
        build_coverage(facts, scope=SCOPE)
    assert caught.value.code == "facts_shape_invalid"


def test_a_counter_report_that_lost_a_key_is_refused(monkeypatch):
    import core.follow_coverage as module

    original = module.coverage_counts

    def broken(facts):
        report = original(facts)
        counts = dict(report["counts"])
        counts.pop("pages_saved")
        return dict(report, counts=counts)

    monkeypatch.setattr(module, "coverage_counts", broken)
    with pytest.raises(CoverageError) as caught:
        module.build_coverage(facts_for("all_complete"), scope=SCOPE)
    assert caught.value.code == "counts_shape_invalid"


def test_a_bad_dimension_state_is_refused(monkeypatch):
    import core.follow_coverage as module

    original = module.coverage_states

    def broken(facts):
        states = original(facts)
        states["delivery"] = {"state": "probably", "reason": ""}
        return states

    monkeypatch.setattr(module, "coverage_states", broken)
    with pytest.raises(CoverageError) as caught:
        module.build_coverage(facts_for("all_complete"), scope=SCOPE)
    assert caught.value.code == "dimension_invalid"


def test_a_swept_artist_list_proves_the_page_end():
    facts = facts_for("all_complete")
    for row in facts["artists"]:
        row["swept_at"] = "2026-11-14T10:00:00"
    assert build_coverage(facts, scope=SCOPE).as_dict()["page_end_evidence"]["state"] == "complete"
    facts["artists"][0]["swept_at"] = ""
    assert build_coverage(facts, scope=SCOPE).as_dict()["page_end_evidence"]["state"] == "partial"


def test_a_declared_page_end_wins_over_the_derivation():
    facts = facts_for("all_complete")
    facts["page_end_evidence"] = {"state": "partial", "reason": "declared"}
    evidence = build_coverage(facts, scope=SCOPE).as_dict()["page_end_evidence"]
    assert evidence == {"state": "partial", "reason": "declared"}


def test_an_unmeasured_save_side_is_unknown_and_not_finished():
    facts = facts_for("all_complete")
    facts["run"]["totals"].pop("pages_saved")
    cov = build_coverage(facts, scope=SCOPE)
    data = cov.as_dict()
    assert data["delivery"]["state"] == "unknown"
    assert data["counts"]["pages_saved"] is None
    assert cov.overall == "unknown"
    assert cov.overall_reason == "delivery_not_measured"
    assert "保存与核验未得证明" in cov.text()


def test_a_saving_shortfall_is_caught_even_when_every_artist_finished():
    facts = facts_for("all_complete")
    facts["run"]["totals"]["pages_saved"] = 9
    cov = build_coverage(facts, scope=SCOPE)
    assert cov.as_dict()["delivery"]["state"] in ("partial", "missing")
    assert cov.as_dict()["delivery"]["reason"].startswith("pages_not_saved:")
    assert cov.overall != "complete"


def test_the_works_count_is_summed_only_when_the_list_is_complete():
    facts = facts_for("all_complete")
    facts["run"]["totals"].pop("works_seen")
    cov = build_coverage(facts, scope=SCOPE)
    assert cov.as_dict()["counts"]["artworks_seen"] == 34
    assert "artworks_seen_from_artist_sum" in cov.reasons
    assert cov.consistent is True


def test_an_incomplete_artist_list_is_not_turned_into_a_denominator():
    facts = facts_for("all_complete")
    facts["run"]["totals"]["total_artists"] = 5
    cov = build_coverage(facts, scope=SCOPE)
    data = cov.as_dict()
    # The declared total stays the denominator; what changes is that the runs
    # own numbers can no longer be trusted together.
    assert data["denominators"]["artists_total"] == 5
    assert cov.consistent is False
    assert any("total_artists_mismatch" in reason for reason in cov.reasons)
    assert cov.overall == "unknown"


def test_without_any_totals_the_run_level_numbers_stay_absent():
    facts = facts_for("all_complete")
    facts["run"]["totals"] = {}
    cov = build_coverage(facts, scope=SCOPE)
    counts = cov.as_dict()["counts"]
    for key in ("artworks_seen", "pages_saved", "excluded_by_filter", "unknown_fields", "duplicate_pages"):
        assert counts[key] is None
    assert counts["artists_checked"] == 3
    assert any("没有来源" in line for line in cov.lines())


def test_as_dict_hands_out_copies():
    cov = coverage_for("all_complete")
    data = cov.as_dict()
    data["counts"]["pages_saved"] = 999
    assert cov.counts["pages_saved"] == 19
    again = cov.as_dict()
    assert again["counts"]["pages_saved"] == 19


def test_the_summary_is_frozen():
    cov = coverage_for("all_complete")
    with pytest.raises(dataclasses.FrozenInstanceError):
        cov.overall = "complete"
    assert isinstance(copy.deepcopy(cov.as_dict()), dict)


def test_building_a_summary_never_marks_the_numbers_as_samples():
    for name in SCENARIOS:
        cov = coverage_for(name)
        assert "sample" not in cov.as_dict()

