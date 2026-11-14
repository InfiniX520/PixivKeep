"""Tests for the rollback material (lane R of the U1-B five-way split)."""
from __future__ import annotations

import ast
import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.maintenance.dependency_rollback import (  # noqa: E402
    DEFAULT_STATES,
    rollback_material,
)

MODULE = ROOT / "tools" / "maintenance" / "dependency_rollback.py"

ENV = {
    "python": {
        "version": "3.12.14",
        "implementation": "CPython",
        "executable": "D:/Miniconda3/envs/JuiLiu/python.exe",
        "env_prefix": "D:/Miniconda3/envs/JuiLiu",
        "is_conda": True,
    },
    "platform": {"system": "Windows", "release": "11", "machine": "AMD64"},
    "pip_check": {"exit": 0, "output": "No broken requirements found."},
    "installed": [
        {"name": "requests", "version": "2.32.3"},
        {"name": "PyYAML", "version": "6.0.2"},
        {"name": "pixivpy3", "version": "3.7.4"},
    ],
    "generated_at": "2026-11-14T00:00:00+00:00",
}


def test_default_states_describe_a_preparation_only_batch():
    states = rollback_material(ENV)["three_states"]
    assert states == DEFAULT_STATES
    assert states["advisory_matched"] == "unknown"
    assert states["upgrade_done"] is False
    assert states["compat_checked"] == "not_run"


def test_states_can_be_overridden_for_the_batch_that_really_upgrades():
    material = rollback_material(
        ENV,
        at={"advisory_matched": "checked", "upgrade_done": True, "compat_checked": "passed"},
    )
    assert material["three_states"] == {
        "advisory_matched": "checked",
        "upgrade_done": True,
        "compat_checked": "passed",
    }


def test_a_partial_override_keeps_the_other_defaults():
    states = rollback_material(ENV, at={"upgrade_done": True})["three_states"]
    assert states["upgrade_done"] is True
    assert states["advisory_matched"] == "unknown"
    assert states["compat_checked"] == "not_run"


def test_unknown_state_keys_are_refused_instead_of_ignored():
    with pytest.raises(ValueError):
        rollback_material(ENV, at={"upgraded": True})


def test_upgrade_done_must_be_a_bool_and_strings_must_be_non_empty():
    with pytest.raises(ValueError):
        rollback_material(ENV, at={"upgrade_done": "yes"})
    with pytest.raises(ValueError):
        rollback_material(ENV, at={"compat_checked": "  "})


def test_at_must_be_a_mapping():
    with pytest.raises(TypeError):
        rollback_material(ENV, at=[])


def test_packages_are_sorted_and_carry_name_and_version_only():
    material = rollback_material(ENV)
    assert [item["name"] for item in material["packages"]] == ["PyYAML", "pixivpy3", "requests"]
    assert all(set(item) == {"name", "version"} for item in material["packages"])


def test_a_duplicate_package_keeps_one_entry():
    env = copy.deepcopy(ENV)
    env["installed"].append({"name": "requests", "version": "2.31.0"})
    packages = rollback_material(env)["packages"]
    assert sum(1 for item in packages if item["name"] == "requests") == 1


def test_interpreter_carries_the_executable_and_version():
    interpreter = rollback_material(ENV)["interpreter"]
    assert interpreter == {"executable": "D:/Miniconda3/envs/JuiLiu/python.exe", "version": "3.12.14"}


def test_restore_steps_name_the_interpreter_and_the_frozen_list():
    material = rollback_material(ENV)
    steps = material["restore_steps"]
    assert steps
    assert all(isinstance(step, str) and step.strip() for step in steps)
    joined = "\n".join(steps)
    assert "D:/Miniconda3/envs/JuiLiu/python.exe" in joined
    assert "requirements-frozen-" in joined
    assert "3 个包" in joined
    assert "--no-deps" in joined
    assert "pip install --user" in joined


def test_window_conditions_say_the_batch_changed_nothing():
    conditions = rollback_material(ENV)["window_conditions"]
    assert conditions
    assert all(isinstance(item, str) and item.strip() for item in conditions)
    joined = "\n".join(conditions)
    assert "不做任何安装" in joined
    assert "用户明确同意" in joined
    assert "pip check 非零时不要升级" in joined


def test_an_empty_environment_does_not_crash_and_reports_nulls():
    material = rollback_material({})
    assert material["packages"] == []
    assert material["interpreter"] == {"executable": None, "version": None}
    assert material["restore_steps"]
    assert "先确认是哪一个解释器" in "\n".join(material["restore_steps"])
    assert material["three_states"] == DEFAULT_STATES


def test_a_non_mapping_environment_is_treated_as_no_facts():
    material = rollback_material(None)
    assert material["packages"] == []
    assert material["interpreter"]["executable"] is None


def test_the_input_environment_is_not_modified():
    env = copy.deepcopy(ENV)
    before = copy.deepcopy(env)
    rollback_material(env)
    assert env == before


def test_the_module_has_no_code_path_that_changes_the_environment():
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split('.')[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split('.')[0])
    assert not (imported & {"subprocess", "os", "shutil", "pip"})
    banned = {"install", "uninstall", "upgrade", "run", "call", "check_call",
              "check_output", "Popen", "system"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = None
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            assert name not in banned, "unexpected call: %s" % name
