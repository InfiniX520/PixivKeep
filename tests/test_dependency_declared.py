"""Declaration parsing for U1-B lane D: read-only, typed errors, no exceptions."""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "maintenance"))
import dependency_declared as declared  # noqa: E402


def write(root: Path, name: str, text: str) -> Path:
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def names(entries):
    return [entry["name"] for entry in entries]


def specifier_of(entries, name):
    for entry in entries:
        if entry["name"] == name:
            return entry["specifier"]
    raise AssertionError("missing entry: " + name)


# --------------------------------------------------------------------------
# The real repository
# --------------------------------------------------------------------------


def test_the_real_repository_declares_its_runtime_dependencies():
    report = declared.declared_requirements(ROOT)

    assert [item["path"] for item in report["files"]] == [
        "requirements.txt",
        "requirements-dev.txt",
        "requirements-build.txt",
        "pyproject.toml",
    ]
    assert [item["kind"] for item in report["files"]] == [
        "runtime",
        "dev",
        "build",
        "pyproject",
    ]
    assert "pixivpy3" in names(report["runtime"])
    assert specifier_of(report["runtime"], "pixivpy3") == ">=3.7.0"
    assert specifier_of(report["runtime"], "requests") == ">=2.28.0"
    assert report["errors"] == []
    assert report["generated_at"].endswith("+00:00")


def test_dev_and_build_expand_the_runtime_include():
    report = declared.declared_requirements(ROOT)

    for bucket in ("dev", "build"):
        assert "pixivpy3" in names(report[bucket]), bucket
        assert "requests" in names(report[bucket]), bucket
    assert "pytest" in names(report["dev"])
    assert "pyinstaller" in names(report["build"])
    # the include is expanded, not duplicated
    assert names(report["dev"]).count("pixivpy3") == 1
    # requirements.txt is a top-level file, so it keeps its own kind
    kinds = {item["path"]: item["kind"] for item in report["files"]}
    assert kinds["requirements.txt"] == "runtime"


# --------------------------------------------------------------------------
# Synthetic trees
# --------------------------------------------------------------------------


def test_comments_and_blank_lines_are_ignored(tmp_path):
    write(tmp_path, "requirements.txt", "# a comment\n\npackage>=1.0  # trailing\n\n")

    report = declared.declared_requirements(tmp_path)

    assert report["runtime"] == [{"name": "package", "specifier": ">=1.0"}]
    assert report["errors"] == []


def test_a_trailing_backslash_continues_one_requirement(tmp_path):
    write(tmp_path, "requirements.txt", "package\\\n>=1.0\n")

    report = declared.declared_requirements(tmp_path)

    assert report["runtime"] == [{"name": "package", "specifier": ">=1.0"}]


def test_a_missing_include_is_reported_and_the_rest_is_still_read(tmp_path):
    write(tmp_path, "requirements.txt", "-r absent.txt\nkept>=2\n")

    report = declared.declared_requirements(tmp_path)

    assert names(report["runtime"]) == ["kept"]
    assert report["errors"][0]["file"] == "requirements.txt"
    assert report["errors"][0]["reason"].startswith("include_missing:")


def test_an_include_cycle_is_reported_once_and_stops(tmp_path):
    write(tmp_path, "requirements.txt", "-r other.txt\nfirst>=1\n")
    write(tmp_path, "other.txt", "-r requirements.txt\nsecond>=2\n")

    report = declared.declared_requirements(tmp_path)

    # -r is expanded in place, exactly where the include line sits
    assert names(report["runtime"]) == ["second", "first"]
    reasons = [item["reason"] for item in report["errors"]]
    assert reasons == ["include_cycle: requirements.txt"]
    assert report["errors"][0]["file"] == "other.txt"


def test_an_include_target_that_is_not_a_top_level_file_is_listed_as_include(tmp_path):
    write(tmp_path, "requirements.txt", "-r extra/constraints.txt\nkept>=1\n")
    write(tmp_path, "extra/constraints.txt", "pinned==1.2.3\n")

    report = declared.declared_requirements(tmp_path)

    assert names(report["runtime"]) == ["pinned", "kept"]
    kinds = {item["path"]: item["kind"] for item in report["files"]}
    assert kinds["extra/constraints.txt"] == "include"
    assert all(not Path(item["path"]).is_absolute() for item in report["files"])


def test_a_bad_requirement_line_becomes_an_error_and_does_not_stop_the_file(tmp_path):
    write(tmp_path, "requirements.txt", "!!! nope\ngood>=1\n")

    report = declared.declared_requirements(tmp_path)

    assert names(report["runtime"]) == ["good"]
    assert report["errors"][0]["reason"].startswith("bad_requirement:")


def test_an_option_line_is_reported_instead_of_silently_dropped(tmp_path):
    write(tmp_path, "requirements.txt", "--index-url https://example.invalid/simple\ngood>=1\n")

    report = declared.declared_requirements(tmp_path)

    assert names(report["runtime"]) == ["good"]
    assert report["errors"][0]["reason"].startswith("unsupported_option:")


def test_a_specifier_is_kept_verbatim_including_the_marker(tmp_path):
    write(
        tmp_path,
        "requirements.txt",
        "package>=1.0,<2 ; python_version < '3.11'\nother\n",
    )

    report = declared.declared_requirements(tmp_path)

    assert specifier_of(report["runtime"], "package") == ">=1.0,<2 ; python_version < '3.11'"
    assert specifier_of(report["runtime"], "other") == ""


def test_a_broken_pyproject_is_one_error_and_the_file_is_still_listed(tmp_path):
    write(tmp_path, "requirements.txt", "kept>=1\n")
    write(tmp_path, "pyproject.toml", "[tool.isort\nprofile = 'black'\n")

    report = declared.declared_requirements(tmp_path)

    assert names(report["runtime"]) == ["kept"]
    assert {"path": "pyproject.toml", "kind": "pyproject"} in report["files"]
    assert report["errors"][0]["file"] == "pyproject.toml"
    assert report["errors"][0]["reason"].startswith("bad_toml:")


def test_pyproject_contributes_runtime_and_build_requirements(tmp_path):
    write(
        tmp_path,
        "pyproject.toml",
        "[project]\n"
        'name = "demo"\n'
        'dependencies = ["requests>=2.28", "packaging"]\n'
        "[build-system]\n"
        'requires = ["setuptools>=61"]\n',
    )

    report = declared.declared_requirements(tmp_path)

    assert names(report["runtime"]) == ["requests", "packaging"]
    assert names(report["build"]) == ["setuptools"]
    assert report["errors"] == []


def test_a_duplicate_declaration_appears_once_per_bucket(tmp_path):
    write(tmp_path, "requirements.txt", "same>=1\n")
    write(tmp_path, "requirements-dev.txt", "-r requirements.txt\nsame>=1\npytest\n")

    report = declared.declared_requirements(tmp_path)

    assert names(report["dev"]) == ["same", "pytest"]
    assert names(report["runtime"]) == ["same"]


def test_an_empty_project_reports_no_files_and_no_errors(tmp_path):
    report = declared.declared_requirements(tmp_path)

    assert report["files"] == []
    assert report["runtime"] == report["dev"] == report["build"] == []
    assert report["errors"] == []


# --------------------------------------------------------------------------
# Read-only proof
# --------------------------------------------------------------------------


def test_the_module_is_read_only_by_construction():
    source = Path(declared.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    # the manual __main__ block may import json/sys for printing; the library
    # part must stay inside the allowed standard-library set
    tree.body = [
        node
        for node in tree.body
        if not (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "__name__"
        )
    ]

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert imported <= {"__future__", "datetime", "re", "tomllib", "pathlib"}, imported

    mutating = {
        "write_text",
        "write_bytes",
        "mkdir",
        "unlink",
        "remove",
        "rename",
        "replace",
        "chmod",
        "rmdir",
        "system",
        "call",
        "run",
        "check_call",
        "check_output",
        "Popen",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            lowered = node.func.id.lower()
            assert not any(word in lowered for word in ("install", "uninstall", "upgrade"))
        if isinstance(node, ast.Attribute):
            assert node.attr not in mutating, node.attr
