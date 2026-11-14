"""S4: the dependency report has to notice when its own evidence is missing.

Every case here is synthetic: the declaration and import views are real code
running on tmp_path, the environment view is a dict, and nothing is installed,
uninstalled or upgraded.  Expected values are written out by hand so a change
in the implementation cannot make these pass by changing the expectation.
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tools" / "maintenance")]

from tools.maintenance import dependency_matrix as matrix  # noqa: E402
from tools.maintenance import dependency_report as report  # noqa: E402


def clean_env(installed=(), pip_exit=0, metadata_errors=()):
    return {
        "installed": [{"name": name, "version": version} for name, version in installed],
        "pip_check": {"exit": pip_exit, "output": "synthetic"},
        "metadata_errors": list(metadata_errors),
    }


def run(tmp_path, monkeypatch, capsys, env, *extra):
    monkeypatch.setattr(report.dependency_env, "environment_facts", lambda: env)
    code = report.main(["--json", "--root", str(tmp_path), *extra])
    return code, json.loads(capsys.readouterr().out)


# --- evidence completeness, through the public CLI -------------------------


def test_a_missing_include_is_an_incomplete_input_and_not_a_success(tmp_path, monkeypatch, capsys):
    (tmp_path / "requirements.txt").write_text("-r missing.txt\n", "utf-8")
    code, payload = run(tmp_path, monkeypatch, capsys, clean_env())
    assert code == 3
    assert payload["exit"] == 3
    assert payload["verdict"] == "unknown"
    assert payload["inputs"]["declared"]["ok"] is False
    assert payload["inputs"]["declared"]["errors"][0]["file"] == "requirements.txt"
    assert payload["inputs"]["declared"]["errors"][0]["reason"].startswith("include_missing:")
    assert payload["ingest_incomplete"] == ["declared"]
    assert any("声明解析" in note for note in payload["notes"])


def test_an_unparsable_source_file_is_an_incomplete_input(tmp_path, monkeypatch, capsys):
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "broken.py").write_text("def :\n", "utf-8")
    code, payload = run(tmp_path, monkeypatch, capsys, clean_env())
    assert code == 3
    assert payload["inputs"]["imports"]["ok"] is False
    assert payload["imports"]["unparsed"][0]["files"] == ["core/broken.py"]
    assert payload["imports"]["unparsed"][0]["unresolved_reason"].startswith("syntax_error:")
    assert payload["ingest_incomplete"] == ["imports"]


def test_a_failing_pip_check_is_an_incomplete_input(tmp_path, monkeypatch, capsys):
    (tmp_path / "requirements.txt").write_text("requests>=2.0\n", "utf-8")
    code, payload = run(tmp_path, monkeypatch, capsys, clean_env([("requests", "3.0")], pip_exit=1))
    assert code == 3
    assert payload["inputs"]["environment"]["ok"] is False
    assert payload["inputs"]["environment"]["pip_check_exit"] == 1
    assert payload["ingest_incomplete"] == ["environment"]
    assert any("pip check" in note for note in payload["notes"])


def test_unreadable_metadata_is_an_incomplete_input(tmp_path, monkeypatch, capsys):
    broken = [{"name": "damaged", "reason": "KeyError: Name"}]
    code, payload = run(tmp_path, monkeypatch, capsys, clean_env(metadata_errors=broken))
    assert code == 3
    assert payload["inputs"]["environment"]["ok"] is False
    assert payload["inputs"]["environment"]["metadata_errors"] == broken
    assert any("元数据" in note for note in payload["notes"])


def test_complete_evidence_and_satisfied_constraints_exit_zero(tmp_path, monkeypatch, capsys):
    (tmp_path / "requirements.txt").write_text("requests>=2.0\n", "utf-8")
    code, payload = run(tmp_path, monkeypatch, capsys, clean_env([("requests", "3.0")]))
    assert code == 0
    assert payload["verdict"] == "ok"
    assert payload["inputs"]["declared"]["ok"] is True
    assert payload["inputs"]["imports"]["ok"] is True
    assert payload["inputs"]["environment"]["ok"] is True
    assert payload["ingest_incomplete"] == []


def test_a_proven_conflict_is_not_hidden_by_an_incomplete_input(tmp_path, monkeypatch, capsys):
    (tmp_path / "requirements.txt").write_text(
        "requests>=9.0\n-r missing.txt\n", "utf-8"
    )
    code, payload = run(tmp_path, monkeypatch, capsys, clean_env([("requests", "3.0")]))
    assert code == 2
    assert payload["verdict"] == "attention"
    assert payload["matrix"]["verdict"] == "attention"
    assert payload["inputs"]["declared"]["ok"] is False
    assert payload["ingest_incomplete"] == ["declared"]


def test_the_report_never_reaches_for_the_environment(tmp_path, monkeypatch, capsys):
    source = Path(report.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    assert "subprocess" not in roots
    assert not hasattr(report, "install")


# --- all applicable constraints, not the first one -------------------------


def declared_view(runtime=(), dev=(), build=(), errors=(), sources=()):
    return {
        "files": [],
        "runtime": list(runtime),
        "dev": list(dev),
        "build": list(build),
        "errors": list(errors),
        "sources": list(sources),
    }


def installed_view(*pairs):
    return {"installed": [{"name": name, "version": version} for name, version in pairs]}


def test_every_runtime_constraint_of_a_name_is_enforced():
    result = matrix.compare(
        declared_view(runtime=[{"name": "example", "specifier": ">=1"}, {"name": "example", "specifier": "<2"}]),
        installed_view(("example", "2.5")),
        {"modules": []},
    )
    assert result["verdict"] == "attention"
    assert result["version_mismatch"] == [
        {"name": "example", "declared": ">=1,<2", "installed": "2.5"}
    ]


def test_satisfied_constraints_still_pass_when_a_name_is_declared_twice():
    result = matrix.compare(
        declared_view(runtime=[{"name": "example", "specifier": ">=1"}, {"name": "example", "specifier": "<3"}]),
        installed_view(("example", "2.5")),
        {"modules": []},
    )
    assert result["verdict"] == "ok"
    assert result["version_mismatch"] == []


def test_a_repeated_identical_constraint_is_not_repeated_in_the_row():
    result = matrix.compare(
        declared_view(runtime=[{"name": "example", "specifier": ">=1"}, {"name": "example", "specifier": ">=1"}]),
        installed_view(("example", "2.5")),
        {"modules": []},
    )
    rows = matrix.constraints(declared_view(runtime=[
        {"name": "example", "specifier": ">=1"},
        {"name": "example", "specifier": ">=1"},
    ]))
    assert rows[0]["applies"] == [">=1"]
    assert result["verdict"] == "ok"


def test_a_runtime_constraint_is_not_downgraded_by_a_dev_one():
    view = declared_view(
        runtime=[{"name": "requests", "specifier": ">=3.0"}],
        dev=[{"name": "requests", "specifier": ">=2.0"}],
    )
    result = matrix.compare(view, installed_view(("requests", "2.32.0")), {"modules": []})
    assert result["version_mismatch"] == [
        {"name": "requests", "declared": ">=3.0", "installed": "2.32.0"}
    ]
    rows = matrix.constraints(view)
    assert rows[0]["group"] == "runtime"
    assert rows[0]["also_declared"] == [{"group": "dev", "specifier": ">=2.0"}]


def test_a_marker_that_does_not_apply_is_excluded_not_ignored():
    view = declared_view(runtime=[{"name": "example", "specifier": ">=99 ; python_version < '3.0'"}])
    result = matrix.compare(view, installed_view(("example", "1.0")), {"modules": []})
    assert result["verdict"] == "ok"
    rows = matrix.constraints(view)
    assert rows[0]["applies"] == []
    assert rows[0]["excluded"][0]["reason"].startswith("marker_not_applicable:")


def test_a_marker_that_applies_is_enforced():
    view = declared_view(runtime=[{"name": "example", "specifier": ">=99 ; python_version >= '3.0'"}])
    result = matrix.compare(view, installed_view(("example", "1.0")), {"modules": []})
    assert result["verdict"] == "attention"
    assert result["version_mismatch"][0]["declared"] == ">=99"


def test_an_unparsable_marker_is_unjudged_and_shown():
    view = declared_view(runtime=[{"name": "example", "specifier": ">=1 ; python_version is not a thing"}])
    result = matrix.compare(view, installed_view(("example", "2.5")), {"modules": []})
    assert result["verdict"] == "unknown"
    assert result["version_mismatch"] == []
    rows = matrix.constraints(view)
    assert rows[0]["unjudged"][0]["reason"].startswith("marker_unjudged:")


def test_extras_are_recorded_separately_from_the_specifier():
    view = declared_view(runtime=[{"name": "requests", "specifier": "[socks]>=2.0"}])
    result = matrix.compare(view, installed_view(("requests", "2.32.0")), {"modules": []})
    assert result["verdict"] == "ok"
    rows = matrix.constraints(view)
    assert rows[0]["applies"] == [">=2.0"]
    detail = matrix._constraint("[socks]>=2.0")
    assert detail["extras"] == "socks" and detail["specifier"] == ">=2.0"


def test_sources_are_attached_when_the_declared_view_carries_them():
    view = declared_view(
        runtime=[{"name": "example", "specifier": ">=1"}],
        sources=[{"name": "example", "specifier": ">=1", "file": "requirements.txt", "line": 3, "group": "runtime"}],
    )
    rows = matrix.constraints(view)
    assert rows[0]["sources"] == [
        {"file": "requirements.txt", "line": 3, "group": "runtime", "specifier": ">=1"}
    ]


def test_a_declaration_view_with_parse_errors_cannot_be_ok():
    view = declared_view(
        runtime=[{"name": "example", "specifier": ">=1"}],
        errors=[{"file": "requirements.txt", "reason": "bad_requirement: line 2"}],
    )
    result = matrix.compare(view, installed_view(("example", "2.5")), {"modules": []})
    assert result["verdict"] == "unknown"


def test_the_declaration_sources_come_from_the_parser_with_file_and_line(tmp_path):
    from tools.maintenance import dependency_declared

    (tmp_path / "requirements.txt").write_text("# note\nrequests>=2.0\n", "utf-8")
    (tmp_path / "requirements-dev.txt").write_text("pytest>=7\n", "utf-8")
    view = dependency_declared.declared_requirements(tmp_path)
    assert view["sources"] == [
        {"name": "requests", "specifier": ">=2.0", "file": "requirements.txt", "line": 2, "group": "runtime"},
        {"name": "pytest", "specifier": ">=7", "file": "requirements-dev.txt", "line": 1, "group": "dev"},
    ]
    rows = {row["name"]: row for row in matrix.constraints(view)}
    assert rows["requests"]["sources"][0]["line"] == 2
    assert rows["requests"]["sources"][0]["file"] == "requirements.txt"
    assert rows["pytest"]["sources"][0]["line"] == 1


def test_the_matrix_result_keeps_the_frozen_five_keys():
    result = matrix.compare(declared_view(), installed_view(), {"modules": []})
    assert set(result) == {"missing", "unexpected", "unused_declared", "version_mismatch", "verdict"}

