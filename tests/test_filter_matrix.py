"""Cross-lane matrix for the U2-B filter engine.

The five lanes were built against a frozen vocabulary, not against each
other: B2 keeps its own copy of the reason table and its own legacy map so
the two could be written in parallel.  This file is where the copies have to
agree, and where B5's fixtures finally meet a real explain().
"""
from __future__ import annotations

import copy
import json

import pytest

import filter_fixtures as F
from core.filter_explain import (
    FIELDS as EXPLAIN_FIELDS,
    LEGACY_CODE_MAP as EXPLAIN_LEGACY,
    REASON_CODES as EXPLAIN_CODES,
    explain,
)
from core.filter_plan import FilterPlan
from core.filter_reasons import (
    FIELDS as TABLE_FIELDS,
    LEGACY_CODE_MAP as TABLE_LEGACY,
    REASON_CODES as TABLE_CODES,
    validate_reason,
    verdict_of,
)
from core.filter_reevaluate import BUCKETS, reevaluate

STRICT = {"min_bookmarks": 100, "exclude_ai": True}
LOOSE = {}


def candidates():
    return [item["candidate"] for item in F.ALL_SCENARIOS]


def ids(name):
    return [item["candidate"]["illust_id"] for item in F.ALL_SCENARIOS if item["name"] == name]


def buckets_with(result):
    return {name: set(result[name]) for name in BUCKETS}


def test_the_two_lanes_share_one_vocabulary():
    assert EXPLAIN_CODES == TABLE_CODES
    assert EXPLAIN_FIELDS == TABLE_FIELDS


def test_the_two_legacy_maps_are_the_same_table():
    assert EXPLAIN_LEGACY == TABLE_LEGACY
    assert len(TABLE_LEGACY) == 8
    for legacy, (code, field, unknown) in TABLE_LEGACY.items():
        assert isinstance(legacy, str) and legacy
        assert code in TABLE_CODES
        assert field in TABLE_FIELDS
        assert isinstance(unknown, bool)


@pytest.mark.parametrize("name", F.SCENARIO_NAMES)
def test_every_fixture_explains_as_the_fixture_says(name):
    fixture = F.SCENARIOS[name]
    result = explain(fixture["candidate"], fixture["conditions"])
    assert result["verdict"] == fixture["expected_verdict"]
    assert [item["code"] for item in result["reasons"]] == list(fixture["expected_codes"])
    assert [item["field"] for item in result["reasons"]] == list(fixture["expected_fields"])


@pytest.mark.parametrize("name", F.SCENARIO_NAMES)
def test_every_reason_passes_the_shared_validator(name):
    fixture = F.SCENARIOS[name]
    for item in explain(fixture["candidate"], fixture["conditions"])["reasons"]:
        validate_reason(dict(item))
        assert item["code"] in TABLE_CODES
        assert item["field"] in TABLE_FIELDS
        assert isinstance(item["unknown"], bool)


@pytest.mark.parametrize("name", F.SCENARIO_NAMES)
def test_the_verdict_rule_is_the_same_in_both_lanes(name):
    fixture = F.SCENARIOS[name]
    result = explain(fixture["candidate"], fixture["conditions"])
    assert verdict_of(result["reasons"]) == result["verdict"]


def test_the_fixtures_exercise_every_code_and_every_verdict():
    counts = F.sum_codes()
    assert set(counts) == set(TABLE_CODES)
    verdicts = {item["expected_verdict"] for item in F.ALL_SCENARIOS}
    assert verdicts == {"match", "exclude", "unknown"}


def test_a_tighter_plan_buckets_every_candidate_exactly_once():
    result = reevaluate(candidates(), LOOSE, STRICT, explain=explain)
    got = buckets_with(result)
    assert sum(len(bucket) for bucket in got.values()) == len(candidates())
    assert got["kept"] == set(ids("clean_match"))
    assert got["newly_excluded"] == set(ids("no_conditions")) | set(ids("bookmarks_below")) | set(
        ids("ai_excluded")
    )
    assert got["still_excluded"] == set(ids("ugoira")) | set(ids("unavailable"))
    assert got["uncovered"] == set(ids("kind_unknown")) | set(ids("bookmarks_missing")) | set(
        ids("ai_flag_missing")
    )
    assert got["newly_included"] == set()


def test_a_looser_plan_re_admits_without_deleting_anything():
    before = copy.deepcopy(candidates())
    result = reevaluate(candidates(), STRICT, LOOSE, explain=explain)
    got = buckets_with(result)
    assert got["newly_included"] == set(ids("bookmarks_below")) | set(ids("ai_excluded")) | set(
        ids("no_conditions")) | set(ids("bookmarks_missing")) | set(ids("ai_flag_missing")
    )
    assert got["still_excluded"] == set(ids("ugoira")) | set(ids("unavailable"))
    assert got["uncovered"] == set(ids("kind_unknown"))
    assert got["kept"] == set(ids("clean_match"))
    assert got["newly_excluded"] == set()
    assert candidates() == before


def test_only_uncovered_candidates_get_reasons():
    result = reevaluate(candidates(), LOOSE, STRICT, explain=explain)
    assert set(result["uncovered_reasons"]) == set(result["uncovered"])
    for reasons in result["uncovered_reasons"].values():
        assert reasons
        for item in reasons:
            validate_reason(dict(item))


def test_the_plan_is_the_snapshot_the_reevaluation_uses():
    plan = FilterPlan()
    tightened = plan.with_conditions(STRICT)
    result = reevaluate(candidates(), plan.conditions, tightened.conditions, explain=explain)
    literal = reevaluate(candidates(), LOOSE, STRICT, explain=explain)
    assert buckets_with(result) == buckets_with(literal)
    assert plan.conditions.min_bookmarks is None
    assert tightened.conditions.min_bookmarks == 100


def test_a_plan_round_trip_keeps_the_fingerprint():
    plan = FilterPlan().with_conditions(STRICT)
    again = FilterPlan.from_dict(plan.as_dict())
    assert again.fingerprint() == plan.fingerprint()
    assert again.conditions == plan.conditions


def test_changing_the_conditions_moves_the_fingerprint():
    plan = FilterPlan()
    assert plan.fingerprint() != plan.with_conditions(STRICT).fingerprint()


@pytest.mark.parametrize("name", F.SCENARIO_NAMES)
def test_the_fixture_candidates_are_plain_json(name):
    fixture = F.SCENARIOS[name]
    json.dumps(fixture["candidate"])
    json.dumps(fixture["conditions"])
