"""B2 explain(): the vocabulary, the three states and the refusals.

Every case is synthetic.  The module under test decides nothing about filtering
itself - it reuses `evaluate_filters` - so these tests check the *translation*
and the two facts that module does not read (artwork kind, availability).
"""
from __future__ import annotations

import ast
import inspect
from types import SimpleNamespace

import pytest

from core.candidate_filter import ArtworkMetadata
from core.filter_explain import (
    FIELDS,
    REASON_CODES,
    explain,
)
from core.task_contracts import FilterSpec


def candidate(**overrides):
    base = {
        "illust_id": "100",
        "kind": "illust",
        "bookmarks": 500,
        "ai_flag": False,
        "available": True,
    }
    base.update(overrides)
    return base


def codes(result):
    return [item["code"] for item in result["reasons"]]


def test_an_ordinary_work_with_no_conditions_is_a_match():
    result = explain(candidate(), {})
    assert result == {"verdict": "match", "reasons": []}


def test_satisfied_conditions_stay_a_match():
    conditions = {"min_bookmarks": 10, "exclude_ai": True}
    result = explain(candidate(bookmarks=10, ai_flag=False), conditions)
    assert result["verdict"] == "match"
    assert result["reasons"] == []


def test_bookmarks_below_the_minimum_exclude_with_the_number_as_evidence():
    result = explain(candidate(bookmarks=9), {"min_bookmarks": 10})
    assert result["verdict"] == "exclude"
    assert result["reasons"] == [
        {"code": "favorites_below", "field": "bookmarks", "evidence": 9, "unknown": False}
    ]


def test_an_ai_work_is_excluded_when_the_switch_is_on():
    result = explain(candidate(ai_flag=True), {"exclude_ai": True})
    assert result["verdict"] == "exclude"
    assert codes(result) == ["ai_excluded"]
    assert result["reasons"][0]["evidence"] is True


def test_missing_bookmarks_are_unknown_and_never_zero():
    result = explain(candidate(bookmarks=None), {"min_bookmarks": 10})
    assert result["verdict"] == "unknown"
    assert result["reasons"] == [
        {"code": "field_unknown", "field": "bookmarks", "evidence": None, "unknown": True}
    ]


def test_a_missing_ai_flag_is_unknown_too():
    result = explain(candidate(ai_flag=None), {"exclude_ai": True})
    assert result["verdict"] == "unknown"
    assert result["reasons"][0]["field"] == "ai_flag"


def test_a_known_rejection_wins_over_an_unknown_field():
    # bookmarks unreadable, but the work is visibly AI: excluded, not unknown.
    conditions = {"min_bookmarks": 10, "exclude_ai": True}
    result = explain(candidate(bookmarks=None, ai_flag=True), conditions)
    assert result["verdict"] == "exclude"
    assert codes(result) == ["ai_excluded"]


def test_an_ugoira_work_is_an_unsupported_type_not_an_unknown_field():
    result = explain(candidate(kind="ugoira"), {})
    assert result["verdict"] == "exclude"
    assert result["reasons"] == [
        {"code": "type_unsupported", "field": "type", "evidence": "ugoira", "unknown": False}
    ]


def test_an_ugoira_with_bad_bookmarks_reports_both_reasons_in_a_stable_order():
    result = explain(candidate(kind="ugoira", bookmarks=1), {"min_bookmarks": 10})
    assert codes(result) == ["favorites_below", "type_unsupported"]
    assert result["verdict"] == "exclude"


def test_an_unreadable_kind_is_unknown_not_unsupported():
    result = explain(candidate(kind="unknown"), {})
    assert result["verdict"] == "unknown"
    assert result["reasons"] == [
        {"code": "field_unknown", "field": "type", "evidence": None, "unknown": True}
    ]


def test_a_nonsense_kind_from_a_raw_mapping_counts_as_unknown():
    result = explain(candidate(kind="storyboard"), {})
    assert result["verdict"] == "unknown"
    assert result["reasons"][0]["code"] == "field_unknown"


def test_an_invisible_work_is_denied_access():
    result = explain(candidate(available=False), {})
    assert result["verdict"] == "exclude"
    assert result["reasons"] == [
        {"code": "access_denied", "field": "availability", "evidence": False, "unknown": False}
    ]


def test_unknown_availability_produces_no_reason_on_purpose():
    # "Nobody told us" is not "blocked": make_candidate treats None the same way.
    assert explain(candidate(available=None), {})["verdict"] == "match"


def test_visible_availability_produces_no_reason():
    assert explain(candidate(available=True), {})["reasons"] == []


def test_a_conditions_mapping_behaves_like_a_filter_spec():
    spec = FilterSpec(min_bookmarks=10, exclude_ai=True)
    mapping = {"min_bookmarks": 10, "exclude_ai": True}
    for work in (candidate(bookmarks=9), candidate(bookmarks=10), candidate(ai_flag=True)):
        assert explain(work, spec) == explain(work, mapping)


def test_an_unknown_condition_key_is_refused():
    with pytest.raises(ValueError) as caught:
        explain(candidate(), {"min_bookmarks": 1, "tags": ["x"]})
    assert "unknown keys" in str(caught.value)


@pytest.mark.parametrize("bad", ["10", 10.5, True, -1])
def test_a_bad_minimum_is_refused(bad):
    with pytest.raises(ValueError):
        explain(candidate(), {"min_bookmarks": bad})


def test_a_bad_exclude_switch_is_refused():
    with pytest.raises(ValueError):
        explain(candidate(), {"exclude_ai": "yes"})


def test_conditions_must_be_a_spec_or_a_mapping():
    with pytest.raises(ValueError):
        explain(candidate(), [("min_bookmarks", 1)])


def test_a_mapping_candidate_and_an_artwork_metadata_agree():
    mapping = candidate(bookmarks=9, ai_flag=None, available=False)
    metadata = ArtworkMetadata(
        illust_id="100", kind="illust", bookmarks=9, ai_flag=None, available=False
    )
    for conditions in ({}, {"min_bookmarks": 10}, {"exclude_ai": True}):
        assert explain(mapping, conditions) == explain(metadata, conditions)


def test_the_candidate_is_not_modified():
    mapping = candidate(bookmarks=None)
    before = dict(mapping)
    explain(mapping, {"min_bookmarks": 10, "exclude_ai": True})
    assert mapping == before


def test_every_reason_uses_the_closed_sets_and_a_scalar_evidence():
    result = explain(candidate(bookmarks=1, ai_flag=True, kind="ugoira", available=False),
                     {"min_bookmarks": 10, "exclude_ai": True})
    assert result["verdict"] == "exclude"
    assert codes(result) == ["favorites_below", "ai_excluded", "type_unsupported", "access_denied"]
    for item in result["reasons"]:
        assert set(item) == {"code", "field", "evidence", "unknown"}
        assert item["code"] in REASON_CODES
        assert item["field"] in FIELDS
        assert item["evidence"] is None or isinstance(item["evidence"], (str, int, bool))


def test_the_filtering_state_and_the_reasons_must_agree(monkeypatch):
    # A state that disagrees with its own reasons would mean the table and the
    # behaviour have drifted; explain() refuses instead of guessing.
    from core import filter_explain

    monkeypatch.setattr(
        filter_explain,
        "evaluate_filters",
        lambda *args, **kwargs: SimpleNamespace(
            state="allow", reasons=("bookmarks_below_minimum",)
        ),
    )
    with pytest.raises(ValueError) as caught:
        explain(candidate(bookmarks=1), {"min_bookmarks": 10})
    assert "filter_state_mismatch" in str(caught.value)


def test_a_filtering_state_with_only_unknown_reasons_is_unknown(monkeypatch):
    from core import filter_explain

    monkeypatch.setattr(
        filter_explain,
        "evaluate_filters",
        lambda *args, **kwargs: SimpleNamespace(
            state="metadata_unknown", reasons=("bookmarks_unknown",)
        ),
    )
    result = explain(candidate(bookmarks=None), {"min_bookmarks": 10})
    assert result["verdict"] == "unknown"


def test_an_unknown_filtering_state_is_refused(monkeypatch):
    from core import filter_explain

    monkeypatch.setattr(
        filter_explain,
        "evaluate_filters",
        lambda *args, **kwargs: SimpleNamespace(state="exploded", reasons=()),
    )
    with pytest.raises(ValueError) as caught:
        explain(candidate(), {})
    assert "unknown_filter_state" in str(caught.value)

def test_each_filter_rejection_becomes_the_documented_reason():
    # Expected triples are written out here, from the documented table, not read
    # back from the module: a copy of the implementation would only detect edits.
    cases = [
        (candidate(bookmarks=1), {"min_bookmarks": 10}, ("favorites_below", "bookmarks", False)),
        (candidate(bookmarks=None), {"min_bookmarks": 10}, ("field_unknown", "bookmarks", True)),
        (candidate(ai_flag=True), {"exclude_ai": True}, ("ai_excluded", "ai_flag", False)),
        (candidate(ai_flag=None), {"exclude_ai": True}, ("field_unknown", "ai_flag", True)),
    ]
    for work, conditions, expected in cases:
        result = explain(work, conditions)
        assert len(result["reasons"]) == 1, (work, expected)
        reason = result["reasons"][0]
        assert (reason["code"], reason["field"], reason["unknown"]) == expected

def test_an_unmapped_legacy_code_is_a_contract_break(monkeypatch):
    from core import filter_explain

    monkeypatch.setattr(
        filter_explain,
        "evaluate_filters",
        lambda *args, **kwargs: SimpleNamespace(state="filtered", reasons=("mystery_code",)),
    )
    with pytest.raises(ValueError) as caught:
        explain(candidate(), {})
    assert "unmapped_reason_code" in str(caught.value)


@pytest.mark.parametrize("bad", ["false", 0, 1, "yes"])
def test_a_non_boolean_availability_is_refused(bad):
    with pytest.raises(ValueError) as caught:
        explain(candidate(available=bad), {})
    assert "availability" in str(caught.value)


def test_the_module_reads_nothing():
    from core import filter_explain

    tree = ast.parse(inspect.getsource(filter_explain))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "collections", "core"}, imported
    source = inspect.getsource(filter_explain)
    for forbidden in ("open(", "requests", "sqlite3", "urllib", "socket", "subprocess"):
        assert forbidden not in source
