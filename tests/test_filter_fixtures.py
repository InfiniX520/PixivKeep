"""Guards for the B5 fixtures: nine scenarios, closed vocabularies, no drift.

These tests do not check the filter logic (that is B2's job and it is built in a
parallel lane); they check that the data handed to the matrix is *complete and
honest*: every contract scenario exists, every verdict and every reason code is
exercised, the vocabularies stay closed, and the objects cannot pollute each
other.
"""
from __future__ import annotations

import ast
import copy
import pathlib

import pytest

import filter_fixtures as fixtures

REQUIRED_KEYS = {
    "name",
    "candidate",
    "conditions",
    "expected_verdict",
    "expected_codes",
    "expected_fields",
}
VERDICTS = {"match", "exclude", "unknown"}
CONDITION_KEYS = {"min_bookmarks", "exclude_ai"}
CANDIDATE_KEYS = {"illust_id", "kind", "bookmarks", "ai_flag", "available"}


def test_the_nine_contract_scenarios_are_all_present():
    assert tuple(fixtures.SCENARIOS) == fixtures.SCENARIO_NAMES
    assert len(fixtures.ALL_SCENARIOS) == 9
    names = [item["name"] for item in fixtures.ALL_SCENARIOS]
    assert len(set(names)) == 9, "scenario names must be unique"
    assert set(names) == set(fixtures.SCENARIO_NAMES)


def test_every_fixture_has_exactly_the_required_keys():
    for item in fixtures.ALL_SCENARIOS:
        assert set(item) == REQUIRED_KEYS, item["name"]
        assert isinstance(item["candidate"], dict), item["name"]
        assert isinstance(item["conditions"], dict), item["name"]
        assert isinstance(item["expected_codes"], tuple), item["name"]
        assert isinstance(item["expected_fields"], tuple), item["name"]


def test_candidate_has_exactly_the_five_fields_the_contract_names():
    for item in fixtures.ALL_SCENARIOS:
        assert set(item["candidate"]) == CANDIDATE_KEYS, item["name"]


def test_conditions_only_use_the_two_frozen_condition_keys():
    for item in fixtures.ALL_SCENARIOS:
        assert set(item["conditions"]) <= CONDITION_KEYS, item["name"]
        if "exclude_ai" in item["conditions"]:
            assert isinstance(item["conditions"]["exclude_ai"], bool), item["name"]
        if "min_bookmarks" in item["conditions"]:
            value = item["conditions"]["min_bookmarks"]
            assert type(value) is int and value >= 0, item["name"]


def test_the_three_verdicts_are_all_exercised():
    seen = {item["expected_verdict"] for item in fixtures.ALL_SCENARIOS}
    assert seen == VERDICTS
    for item in fixtures.ALL_SCENARIOS:
        assert item["expected_verdict"] in VERDICTS, item["name"]


def test_every_reason_code_is_exercised_at_least_once():
    counts = fixtures.sum_codes()
    assert set(counts) == set(fixtures.REASON_CODES), counts
    for code in fixtures.REASON_CODES:
        assert counts[code] >= 1, code


def test_no_expected_code_falls_outside_the_closed_set():
    for item in fixtures.ALL_SCENARIOS:
        for code in item["expected_codes"]:
            assert code in fixtures.REASON_CODES, (item["name"], code)


def test_expected_fields_line_up_with_expected_codes():
    for item in fixtures.ALL_SCENARIOS:
        assert len(item["expected_fields"]) == len(item["expected_codes"]), item["name"]
        for field in item["expected_fields"]:
            assert field in fixtures.FIELDS, (item["name"], field)


def test_kind_unknown_and_missing_bookmarks_are_both_field_unknown_but_on_different_fields():
    kind = fixtures.scenario("kind_unknown")
    bookmarks = fixtures.scenario("bookmarks_missing")
    assert kind["expected_codes"] == ("field_unknown",)
    assert bookmarks["expected_codes"] == ("field_unknown",)
    # Same code, different field: this is exactly the distinction a flattened
    # mapping would lose.
    assert kind["expected_fields"] == ("type",)
    assert bookmarks["expected_fields"] == ("bookmarks",)
    assert kind["expected_verdict"] == bookmarks["expected_verdict"] == "unknown"


def test_ugoira_is_type_unsupported_not_field_unknown():
    item = fixtures.scenario("ugoira")
    assert item["expected_verdict"] == "exclude"
    assert item["expected_codes"] == ("type_unsupported",)
    assert "field_unknown" not in item["expected_codes"]
    assert item["candidate"]["kind"] == "ugoira"


def test_clean_match_carries_no_reasons_and_no_conditions_is_empty():
    clean = fixtures.scenario("clean_match")
    assert clean["expected_verdict"] == "match"
    assert clean["expected_codes"] == ()
    assert clean["expected_fields"] == ()
    assert clean["conditions"], "clean_match must still exercise the conditions"

    bare = fixtures.scenario("no_conditions")
    assert bare["conditions"] == {}
    assert bare["expected_verdict"] == "match"
    assert bare["expected_codes"] == ()
    # The candidate is deliberately unattractive: with no conditions nothing
    # may be filtered, otherwise "no conditions" would silently mean "defaults".
    assert bare["candidate"]["bookmarks"] == 0
    assert bare["candidate"]["ai_flag"] is True


def test_sum_codes_counts_every_expected_code():
    counts = fixtures.sum_codes()
    assert sum(counts.values()) == len(fixtures.codes_of())
    assert fixtures.sum_codes([]) == {}
    subset = [fixtures.scenario("ugoira"), fixtures.scenario("unavailable")]
    assert fixtures.sum_codes(subset) == {"type_unsupported": 1, "access_denied": 1}


def test_every_fixture_is_deterministic_and_deep_copy_safe():
    first = copy.deepcopy(fixtures.ALL_SCENARIOS)
    second = copy.deepcopy(fixtures.ALL_SCENARIOS)
    assert first == second
    # Mutating a copy must not reach the published data.
    first[0]["candidate"]["bookmarks"] = 999
    assert fixtures.ALL_SCENARIOS[0]["candidate"]["bookmarks"] != 999
    assert fixtures.SCENARIOS["bookmarks_below"]["candidate"]["bookmarks"] == 10


def test_no_two_fixtures_share_a_candidate_or_conditions_object():
    candidates = [id(item["candidate"]) for item in fixtures.ALL_SCENARIOS]
    conditions = [id(item["conditions"]) for item in fixtures.ALL_SCENARIOS]
    assert len(set(candidates)) == len(candidates)
    assert len(set(conditions)) == len(conditions)
    illust_ids = [item["candidate"]["illust_id"] for item in fixtures.ALL_SCENARIOS]
    assert len(set(illust_ids)) == len(illust_ids), "each candidate needs its own id"


def test_lookup_by_name_works_and_an_unknown_name_is_an_error():
    assert fixtures.scenario("clean_match") is fixtures.SCENARIOS["clean_match"]
    with pytest.raises(KeyError):
        fixtures.scenario("does_not_exist")


def test_the_fixtures_do_not_import_the_product_code():
    """B5 must stay importable while B1-B4 do not exist yet."""
    forbidden = {"core", "gui", "utils"}
    here = pathlib.Path(__file__).resolve().parent
    for name in ("filter_fixtures.py", "test_filter_fixtures.py"):
        tree = ast.parse((here / name).read_text(encoding="utf-8"), filename=name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in forbidden, (name, alias.name)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert module.split(".")[0] not in forbidden, (name, module)
