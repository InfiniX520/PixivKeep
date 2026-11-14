"""A saved plan is a run snapshot: frozen, copyable, fingerprintable."""
from __future__ import annotations

import re

import pytest

from core.filter_plan import FilterPlan
from core.task_contracts import ContractError, FilterSpec, OutputSpec, QualitySpec


def test_defaults_describe_an_empty_filter_with_the_standard_output():
    plan = FilterPlan()
    payload = plan.as_dict()
    assert payload["conditions"] == {"min_bookmarks": None, "exclude_ai": False}
    assert payload["output_policy"]["folder_by_artist"] is True
    assert payload["integrity_policy"] == {
        "original": True,
        "all_pages": True,
        "validation_policy_version": 1,
    }
    assert payload["resource_start"] == {}


def test_the_four_parts_are_the_frozen_contract_types():
    plan = FilterPlan(
        conditions=FilterSpec(min_bookmarks=100),
        output_policy=OutputSpec(folder_by_artist=False),
        integrity_policy=QualitySpec(all_pages=False),
        resource_start={"start_page": 3},
    )
    assert isinstance(plan.conditions, FilterSpec)
    assert isinstance(plan.output_policy, OutputSpec)
    assert isinstance(plan.integrity_policy, QualitySpec)
    assert plan.resource_start["start_page"] == 3


@pytest.mark.parametrize("field_name", ["conditions", "output_policy", "integrity_policy"])
def test_a_part_of_the_wrong_type_is_refused(field_name):
    with pytest.raises(ContractError):
        FilterPlan(**{field_name: "not-a-spec"})


def test_resource_start_is_read_only():
    plan = FilterPlan(resource_start={"a": 1})
    with pytest.raises(TypeError):
        plan.resource_start["a"] = 2


def test_resource_start_is_copied_so_the_caller_cannot_move_the_snapshot():
    source = {"a": 1, "b": "two"}
    plan = FilterPlan(resource_start=source)
    source["a"] = 99
    source["c"] = True
    assert dict(plan.resource_start) == {"a": 1, "b": "two"}


@pytest.mark.parametrize(
    "value",
    [
        {"a": {"nested": 1}},
        {"a": [1, 2]},
        {"a": 1.5},
        {"a": object()},
    ],
)
def test_resource_start_accepts_only_json_scalars(value):
    with pytest.raises(ContractError):
        FilterPlan(resource_start=value)


def test_resource_start_accepts_the_four_scalar_types():
    plan = FilterPlan(resource_start={"text": "x", "number": 7, "flag": True, "none": None})
    assert dict(plan.resource_start) == {
        "text": "x",
        "number": 7,
        "flag": True,
        "none": None,
    }


@pytest.mark.parametrize("key", ["", "a" * 65, "bad\x00key", 7, None])
def test_resource_start_keys_are_checked(key):
    with pytest.raises(ContractError):
        FilterPlan(resource_start={key: 1})


def test_resource_start_must_be_a_mapping():
    with pytest.raises(ContractError):
        FilterPlan(resource_start=["a", "b"])


def test_the_fingerprint_is_a_sha256_hex_digest():
    fingerprint = FilterPlan().fingerprint()
    assert re.fullmatch(r"[0-9a-f]{64}", fingerprint)


def test_equal_plans_share_a_fingerprint():
    one = FilterPlan(conditions=FilterSpec(min_bookmarks=5), resource_start={"page": 1})
    two = FilterPlan(conditions=FilterSpec(min_bookmarks=5), resource_start={"page": 1})
    assert one.fingerprint() == two.fingerprint()


def test_the_fingerprint_ignores_dictionary_insertion_order():
    one = FilterPlan(resource_start={"a": 1, "b": 2, "c": 3})
    two = FilterPlan(resource_start={"c": 3, "a": 1, "b": 2})
    assert one.fingerprint() == two.fingerprint()


def test_a_real_change_moves_the_fingerprint():
    base = FilterPlan()
    assert base.fingerprint() != base.with_conditions({"min_bookmarks": 10}).fingerprint()
    assert (
        base.fingerprint()
        != FilterPlan(integrity_policy=QualitySpec(all_pages=False)).fingerprint()
    )
    assert base.fingerprint() != FilterPlan(resource_start={"page": 2}).fingerprint()


def test_with_conditions_returns_a_new_plan_and_leaves_the_old_one_alone():
    original = FilterPlan(conditions=FilterSpec(min_bookmarks=1), resource_start={"page": 1})
    before = original.as_dict()
    before_fingerprint = original.fingerprint()
    changed = original.with_conditions(FilterSpec(min_bookmarks=50))
    assert changed is not original
    assert changed.conditions.min_bookmarks == 50
    assert original.as_dict() == before
    assert original.fingerprint() == before_fingerprint
    assert original.conditions.min_bookmarks == 1


def test_with_conditions_accepts_a_mapping():
    plan = FilterPlan().with_conditions({"min_bookmarks": 3, "exclude_ai": True})
    assert plan.conditions == FilterSpec(min_bookmarks=3, exclude_ai=True)


def test_with_conditions_refuses_an_unknown_key():
    with pytest.raises(ContractError):
        FilterPlan().with_conditions({"min_bookmarks": 3, "min_pages": 2})


def test_as_dict_is_a_copy_that_cannot_move_the_plan():
    plan = FilterPlan(resource_start={"page": 1})
    payload = plan.as_dict()
    payload["resource_start"]["page"] = 99
    payload["conditions"]["exclude_ai"] = True
    payload["output_policy"]["name_template"] = "changed"
    assert plan.resource_start["page"] == 1
    assert plan.conditions.exclude_ai is False
    assert plan.output_policy.name_template == "{illust_id}_p{page_index}"


def test_from_dict_round_trips_and_keeps_the_fingerprint():
    plan = FilterPlan(
        conditions=FilterSpec(min_bookmarks=42, exclude_ai=True),
        output_policy=OutputSpec(folder_by_artist=False, name_template="{illust_id}_p{page_index}"),
        integrity_policy=QualitySpec(original=True, all_pages=False),
        resource_start={"page": 2, "note": "x"},
    )
    restored = FilterPlan.from_dict(plan.as_dict())
    assert restored.as_dict() == plan.as_dict()
    assert restored.fingerprint() == plan.fingerprint()


def test_from_dict_rebuilds_the_contract_types_and_freezes_the_mapping():
    restored = FilterPlan.from_dict(
        {
            "conditions": {"min_bookmarks": 1, "exclude_ai": False},
            "output_policy": {"folder_by_artist": True},
            "integrity_policy": {"all_pages": True},
            "resource_start": {"page": 1},
        }
    )
    assert isinstance(restored.conditions, FilterSpec)
    assert isinstance(restored.output_policy, OutputSpec)
    assert isinstance(restored.integrity_policy, QualitySpec)
    with pytest.raises(TypeError):
        restored.resource_start["page"] = 2


def test_from_dict_refuses_an_unknown_top_level_field():
    with pytest.raises(ContractError):
        FilterPlan.from_dict({"conditions": {}, "unknown": 1})


def test_from_dict_refuses_a_non_mapping():
    with pytest.raises(ContractError):
        FilterPlan.from_dict(["conditions"])


def test_from_dict_refuses_an_unknown_nested_field():
    with pytest.raises(ContractError):
        FilterPlan.from_dict({"conditions": {"min_bookmarks": 1, "extra": True}})


def test_from_dict_enforces_the_contract_rules_of_each_part():
    with pytest.raises(ContractError):
        FilterPlan.from_dict({"conditions": {"min_bookmarks": -1}})
    with pytest.raises(ContractError):
        FilterPlan.from_dict({"integrity_policy": {"validation_policy_version": 2}})
    with pytest.raises(ContractError):
        FilterPlan.from_dict({"output_policy": {"folder_by_artist": "yes"}})


def test_the_docstring_states_the_run_snapshot_semantics():
    documentation = FilterPlan.__doc__ or ""
    assert "next" in documentation and "run" in documentation
