"""Offline tests for the versioned, literal ruleset.

Expected values are written out by hand here; nothing is compared against the
implementation's own constants.  The truth table at the bottom is the part a
reviewer should read first: it is the whole contract in six lines.
"""
from __future__ import annotations

import copy

import pytest

from organizer.common import OrganizerError
from organizer.rules import (
    MAX_GROUPS,
    Rule,
    RuleError,
    RuleSet,
    changed_by_version,
    classify,
    ruleset_fingerprint,
)


def ruleset(*rules, version="v1"):
    return RuleSet(version=version, rules=tuple(rules))


def base():
    return ruleset(
        Rule("r-topic", "topic", "cat", ("animal",), 2),
        Rule("r-style", "style", "cute", ("kawaii",), 1),
    )


def test_a_single_strong_hit_is_enough_to_recommend_a_group():
    # EA-07: a weight-2 tag match recommends the group, and that is all it does.
    # It cannot certify that anybody looked at the works, so the answer is not
    # "high, nothing more needed" without a verified review receipt.
    result = classify({"tags": ["cat"]}, base())
    assert result["groups"] == ["animal"]
    assert result["match_strength"] == "high"
    assert result["confidence"] == "low"
    assert result["needs_more"] is True
    assert result["visual_evidence"]["state"] == "unknown"
    assert result["reasons"] == ["visual_evidence_missing", "low_confidence"]
    assert result["anchors"] == [
        {
            "rule_id": "r-topic",
            "dimension": "topic",
            "match": "cat",
            "match_kind": "equals",
            "tags": ["cat"],
            "weight": 2,
        }
    ]


def test_a_weak_hit_is_low_confidence_and_asks_for_more():
    result = classify({"tags": ["cute"]}, base())
    assert result["groups"] == ["kawaii"]
    assert result["confidence"] == "low"
    assert result["needs_more"] is True
    assert result["reasons"] == ["single_weak_hit", "low_confidence"]


def test_no_match_is_unknown_and_not_a_decision_that_no_group_is_needed():
    result = classify({"tags": ["dog"]}, base())
    assert result["groups"] == []
    assert result["confidence"] == "unknown"
    assert result["needs_more"] is True
    assert result["reasons"] == ["no_rule_matched"]


@pytest.mark.parametrize("evidence", [{"tags": []}, {}, {"tags": "cat"}, {"note": "x"}])
def test_missing_or_empty_evidence_is_unknown_not_no_match(evidence):
    result = classify(evidence, base())
    assert result["confidence"] == "unknown"
    assert result["reasons"] == ["evidence_missing"]


def test_equals_is_the_default_and_it_is_only_casefolded():
    assert classify({"tags": ["CAT"]}, base())["groups"] == ["animal"]
    assert classify({"tags": ["concatenate"]}, base())["groups"] == []


def test_contains_has_to_be_asked_for_by_the_rule_author():
    loose = ruleset(Rule("r", "topic", "cat", ("animal",), 2, match_kind="contains"))
    assert classify({"tags": ["concatenate"]}, loose)["groups"] == ["animal"]
    assert classify({"tags": ["CAT"]}, loose)["groups"] == ["animal"]


def test_cjk_containment_also_needs_the_explicit_kind():
    strict = ruleset(Rule("r", "topic", "猫", ("cats",), 2))
    assert classify({"tags": ["猫"]}, strict)["groups"] == ["cats"]
    assert classify({"tags": ["猫咪"]}, strict)["groups"] == []
    loose = ruleset(Rule("r", "topic", "猫", ("cats",), 2, match_kind="contains"))
    assert classify({"tags": ["猫咪"]}, loose)["groups"] == ["cats"]


def test_the_rule_text_is_literal_and_never_a_pattern():
    dotted = ruleset(Rule("r-dot", "topic", "c.t", ("dotted",), 2))
    assert classify({"tags": ["c.t"]}, dotted)["groups"] == ["dotted"]
    assert classify({"tags": ["cat"]}, dotted)["groups"] == []
    star = ruleset(Rule("r-star", "topic", ".*", ("everything",), 2))
    assert classify({"tags": ["cat"]}, star)["groups"] == []


def test_two_hits_are_ranked_by_weight_then_name():
    result = classify({"tags": ["cute", "cat"]}, base())
    assert result["groups"] == ["animal", "kawaii"]
    assert result["match_strength"] == "high"
    assert result["confidence"] == "low"


def test_the_same_group_suggested_twice_appears_once_with_summed_weight():
    both = ruleset(
        Rule("r-a", "topic", "cat", ("animal",), 1),
        Rule("r-b", "style", "cute", ("animal",), 1),
    )
    result = classify({"tags": ["cat", "cute"]}, both)
    assert result["groups"] == ["animal"]
    assert result["match_strength"] == "high"
    assert result["confidence"] == "low"


def test_more_than_five_groups_are_truncated_and_say_so():
    wide = ruleset(
        Rule("r-1", "topic", "cat", ("g1", "g2"), 2),
        Rule("r-2", "style", "cute", ("g3", "g4"), 2),
        Rule("r-3", "usage", "daily", ("g5", "g6"), 2),
    )
    result = classify({"tags": ["cat", "cute", "daily"]}, wide)
    assert len(result["groups"]) == MAX_GROUPS == 5
    assert result["groups"] == ["g1", "g2", "g3", "g4", "g5"]
    assert "groups_truncated" in result["reasons"]


def test_a_group_name_over_twenty_utf16_units_is_refused_when_the_rule_is_built():
    with pytest.raises(RuleError) as caught:
        Rule("r-long", "topic", "cat", ("x" * 21,), 1)
    assert caught.value.rule_id == "r-long"
    assert caught.value.code in {"invalid_text", "group_name_too_long"}


def test_a_rule_needs_a_known_dimension_and_a_unique_id():
    with pytest.raises(RuleError) as caught:
        Rule("r-dim", "mood", "cat", ("animal",), 1)
    assert caught.value.code == "rule_dimension_invalid"
    with pytest.raises(RuleError) as caught:
        ruleset(Rule("r-1", "topic", "cat", ("a",), 1), Rule("r-1", "style", "cute", ("b",), 1))
    assert caught.value.code == "rule_id_duplicate"


def test_the_version_is_part_of_the_identity_and_rule_order_is_not():
    first = Rule("r-1", "topic", "cat", ("animal",), 1)
    second = Rule("r-2", "style", "cute", ("kawaii",), 1)
    a = ruleset(first, second)
    b = ruleset(second, first)
    c = ruleset(first, second, version="v2")
    loose = ruleset(Rule("r-1", "topic", "cat", ("animal",), 1, match_kind="contains"), second)
    assert ruleset_fingerprint(a) == ruleset_fingerprint(b)
    assert ruleset_fingerprint(a) != ruleset_fingerprint(c)
    assert ruleset_fingerprint(a) != ruleset_fingerprint(loose)
    assert len(ruleset_fingerprint(a)) == 64


def test_an_unknown_match_kind_is_refused_with_the_rule_named():
    with pytest.raises(RuleError) as caught:
        Rule("r-kind", "topic", "cat", ("animal",), 1, match_kind="prefix")
    assert caught.value.code == "rule_match_kind_invalid"
    assert caught.value.rule_id == "r-kind"


def test_classification_is_deterministic_and_does_not_touch_its_input():
    evidence = {"tags": ["cat", "cute"]}
    before = copy.deepcopy(evidence)
    assert classify(evidence, base()) == classify(evidence, base())
    assert evidence == before


def test_a_version_move_lists_who_would_change_without_writing_anything():
    old = base()
    new = ruleset(
        Rule("r-topic", "topic", "cat", ("animal",), 2),
        Rule("r-style", "style", "cute", ("kawaii",), 1),
        Rule("r-new", "usage", "cat", ("felines",), 1),
        version="v2",
    )
    per_artist = {"1": {"tags": ["cat"]}, "2": {"tags": ["dog"]}}
    moved = changed_by_version(per_artist, old, new)
    assert list(moved) == ["1"]
    assert moved["1"]["before"]["groups"] == ["animal"]
    assert moved["1"]["after"]["groups"] == ["animal", "felines"]
    same = changed_by_version(per_artist, old, RuleSet(version="v9", rules=old.rules))
    assert same == {}


# The truth table: evidence -> suggestion, written by hand.  Every row is also
# a statement about what must NOT happen (a group invented from nothing).
# A verified review receipt is what moves a row to high; that path needs a store
# and is proven in tests/test_w5_visual_evidence.py.  No row here can be high,
# because a tag list is a claim about text, not a record of looking (EA-07).
TRUTH_TABLE = [
    ({"tags": ["cat"]}, ["animal"], "low", True),
    ({"tags": ["cute"]}, ["kawaii"], "low", True),
    ({"tags": ["dog"]}, [], "unknown", True),
    ({"tags": []}, [], "unknown", True),
    ({"tags": ["CAT"]}, ["animal"], "low", True),
    ({"tags": ["cat", "cute"]}, ["animal", "kawaii"], "low", True),
]


@pytest.mark.parametrize("evidence,groups,confidence,needs_more", TRUTH_TABLE)
def test_the_truth_table(evidence, groups, confidence, needs_more):
    result = classify(evidence, base())
    assert result["groups"] == groups
    assert result["confidence"] == confidence
    assert result["needs_more"] is needs_more


# The same evidence under the two match kinds: the whole point of the default.
MATCH_KIND_TABLE = [
    ("equals", {"tags": ["concatenate"]}, []),
    ("equals", {"tags": ["cat"]}, ["animal"]),
    ("contains", {"tags": ["concatenate"]}, ["animal"]),
    ("contains", {"tags": ["cat"]}, ["animal"]),
    ("equals", {"tags": ["猫咪"]}, []),
    ("contains", {"tags": ["猫咪"]}, ["cats"]),
]


@pytest.mark.parametrize("kind,evidence,groups", MATCH_KIND_TABLE)
def test_the_truth_table_for_match_kinds(kind, evidence, groups):
    text = "猫" if "猫咪" in evidence.get("tags", []) else "cat"
    name = ("cats",) if text == "猫" else ("animal",)
    rule = Rule("r-kind", "topic", text, name, 2, match_kind=kind)
    assert classify(evidence, ruleset(rule))["groups"] == groups


def test_the_module_reads_nothing_and_calls_no_model():
    import ast
    import pathlib

    source = pathlib.Path(__file__).resolve().parents[1] / "organizer" / "rules.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "dataclasses", "hashlib", "json", "typing", "organizer"}
    text = source.read_text(encoding="utf-8")
    for forbidden in ("open(", "sqlite3", "requests", "urllib", "socket", "subprocess"):
        assert forbidden not in text.split('"""')[2]
