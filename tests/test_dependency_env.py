"""Lane E: the environment facts must be complete, honest and read-only.

The module under test answers "what interpreter is this, and what is installed
in it".  These tests pin three things: every frozen key is present and shaped
as agreed, a broken distribution is skipped instead of crashing the report, and
the module contains no code path that could change the environment.
"""
from __future__ import annotations

import ast
import re
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT)]

from tools.maintenance import dependency_env as env  # noqa: E402

FROZEN_KEYS = {"python", "platform", "pip_check", "installed", "generated_at"}


def test_environment_facts_reports_every_contract_key():
    facts = env.environment_facts()

    assert FROZEN_KEYS <= set(facts)
    # The only documented extension is the skipped-metadata list.
    assert set(facts) - FROZEN_KEYS == {"metadata_errors"}

    python = facts["python"]
    assert set(python) == {"version", "implementation", "executable", "env_prefix", "is_conda"}
    assert re.fullmatch(r"\d+\.\d+\.\d+", python["version"]), python["version"]
    assert isinstance(python["implementation"], str) and python["implementation"]
    assert Path(python["executable"]).name.startswith("python")
    assert Path(python["env_prefix"]).is_absolute()
    assert isinstance(python["is_conda"], bool)

    platform_facts = facts["platform"]
    assert set(platform_facts) == {"system", "release", "machine"}
    assert all(isinstance(value, str) for value in platform_facts.values())

    check = facts["pip_check"]
    assert set(check) == {"exit", "output"}
    assert isinstance(check["exit"], int)
    assert isinstance(check["output"], str)

    assert isinstance(facts["installed"], list)
    assert isinstance(facts["metadata_errors"], list)
    datetime.fromisoformat(facts["generated_at"])


def test_installed_list_is_sorted_by_name_then_version():
    facts = env.environment_facts()

    keys = [(item["name"].lower(), item["version"]) for item in facts["installed"]]
    assert keys == sorted(keys)
    assert all(set(item) == {"name", "version"} for item in facts["installed"])


def test_is_conda_matches_the_prefix_directory():
    facts = env.environment_facts()

    expected = (Path(sys.prefix) / "conda-meta").is_dir()
    assert facts["python"]["is_conda"] is expected


def test_a_nonzero_pip_check_is_recorded_not_raised(monkeypatch):
    def fake_run(argv, **kwargs):
        assert argv[-3:] == ["-m", "pip", "check"]
        return SimpleNamespace(returncode=1, stdout="pkg 1.0 needs thing>=2", stderr="")

    monkeypatch.setattr(env.subprocess, "run", fake_run)

    check = env.environment_facts()["pip_check"]

    assert check["exit"] == 1
    assert "pkg 1.0 needs thing>=2" in check["output"]


def test_a_pip_check_timeout_is_recorded_not_raised(monkeypatch):
    def fake_run(argv, **kwargs):
        raise env.subprocess.TimeoutExpired(cmd="pip check", timeout=1)

    monkeypatch.setattr(env.subprocess, "run", fake_run)

    check = env.environment_facts()["pip_check"]

    assert check["exit"] == -1
    assert "timed out" in check["output"]


def test_a_pip_check_that_cannot_start_is_recorded(monkeypatch):
    def fake_run(argv, **kwargs):
        raise FileNotFoundError("no pip here")

    monkeypatch.setattr(env.subprocess, "run", fake_run)

    check = env.environment_facts()["pip_check"]

    assert check["exit"] == -1
    assert "could not start" in check["output"]


def test_a_broken_distribution_is_skipped_and_counted(monkeypatch):
    class Good:
        metadata = {"Name": "alpha"}
        version = "1.0"

    class Broken:
        @property
        def metadata(self):
            raise ValueError("corrupted METADATA")

        @property
        def version(self):
            raise ValueError("corrupted RECORD")

    monkeypatch.setattr(env.metadata, "distributions", lambda: [Good(), Broken()])

    facts = env.environment_facts()

    assert facts["installed"] == [{"name": "alpha", "version": "1.0"}]
    assert len(facts["metadata_errors"]) == 1
    assert facts["metadata_errors"][0]["name"] == "?"
    assert "corrupted METADATA" in facts["metadata_errors"][0]["reason"]


def test_distributions_are_sorted_case_insensitively(monkeypatch):
    class Distribution:
        def __init__(self, name, version):
            self.metadata = {"Name": name}
            self.version = version

    monkeypatch.setattr(
        env.metadata,
        "distributions",
        lambda: [Distribution("Zeta", "1"), Distribution("alpha", "2"), Distribution("Alpha", "1")],
    )

    facts = env.environment_facts()

    # Sorted by lowercased name, then version: Alpha/1 before alpha/2.
    assert [item["name"] for item in facts["installed"]] == ["Alpha", "alpha", "Zeta"]


def _module_source() -> str:
    path = ROOT / "tools" / "maintenance" / "dependency_env.py"
    return path.read_text(encoding="utf-8")


def test_the_module_contains_no_environment_mutating_call():
    tree = ast.parse(_module_source())

    forbidden = {"install", "uninstall", "upgrade", "freeze", "download"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = ""
            if isinstance(node.func, ast.Attribute):
                name = node.func.attr
            elif isinstance(node.func, ast.Name):
                name = node.func.id
            assert name.lower() not in forbidden, "environment-mutating call: %s" % name

    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported |= {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert "os" not in imported
    assignments = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)]
    assert not any("environ" in ast.dump(node) for node in assignments)


def test_the_only_pip_command_is_check():
    tree = ast.parse(_module_source())

    pip_lists = []
    for node in ast.walk(tree):
        if isinstance(node, ast.List) and node.elts:
            constants = [
                element.value
                for element in node.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            ]
            if "pip" in constants:
                pip_lists.append(constants)

    assert len(pip_lists) == 1, pip_lists
    assert set(pip_lists[0]) == {"-m", "pip", "check"}
