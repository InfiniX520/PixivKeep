"""The dependency matrix is a pure comparison: every case here is a dict."""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tools" / "maintenance")]

import dependency_matrix as matrix  # noqa: E402


def declared(runtime=(), dev=(), build=()):
    return {
        "files": [{"path": "requirements.txt", "kind": "requirements"}],
        "runtime": list(runtime),
        "dev": list(dev),
        "build": list(build),
        "errors": [],
        "generated_at": "2026-11-14T00:00:00+00:00",
    }


def installed(*pairs):
    return {
        "python": {"version": "3.12.14"},
        "installed": [{"name": name, "version": version} for name, version in pairs],
        "generated_at": "2026-11-14T00:00:00+00:00",
    }


def imported(*names):
    return {
        "modules": [
            {
                "top_level": name,
                "files": ["core/x.py"],
                "count": 1,
                "stdlib": False,
                "resolved": {"origin": "/site-packages/%s/__init__.py" % name},
                "unresolved_reason": None,
            }
            for name in names
        ],
        "generated_at": "2026-11-14T00:00:00+00:00",
    }


def test_missing_runtime_dependency_is_attention():
    result = matrix.compare(
        declared(runtime=[{"name": "requests", "specifier": ">=2.28.0"}]),
        installed(),
        imported("requests"),
    )
    assert result["missing"] == [{"name": "requests", "specifier": ">=2.28.0"}]
    assert result["verdict"] == "attention"


def test_unexpected_package_is_listed_but_does_not_raise_the_verdict():
    result = matrix.compare(
        declared(runtime=[{"name": "requests", "specifier": ">=2.28.0"}]),
        installed(("requests", "2.32.0"), ("pip", "24.2")),
        imported("requests"),
    )
    assert result["unexpected"] == [{"name": "pip", "version": "24.2"}]
    assert result["verdict"] == "ok"


def test_unused_declared_package_is_listed():
    result = matrix.compare(
        declared(runtime=[{"name": "requests", "specifier": None}, {"name": "PyYAML", "specifier": ">=6.0.2"}]),
        installed(("requests", "2.32.0"), ("pyyaml", "6.0.2")),
        imported("requests"),
    )
    assert result["unused_declared"] == [{"name": "PyYAML"}]
    assert result["verdict"] == "ok"


def test_version_mismatch_is_attention():
    result = matrix.compare(
        declared(runtime=[{"name": "requests", "specifier": ">=3.0"}]),
        installed(("requests", "2.32.0")),
        imported("requests"),
    )
    assert result["version_mismatch"] == [
        {"name": "requests", "declared": ">=3.0", "installed": "2.32.0"}
    ]
    assert result["verdict"] == "attention"


def test_case_and_separators_are_normalised():
    result = matrix.compare(
        declared(runtime=[{"name": "Requests", "specifier": None}, {"name": "typing-extensions", "specifier": None}]),
        installed(("requests", "2.32.0"), ("typing_extensions", "4.12.2")),
        imported("requests", "typing_extensions"),
    )
    assert result["missing"] == []
    assert result["unused_declared"] == []
    assert result["version_mismatch"] == []
    assert result["verdict"] == "ok"


def test_extras_are_ignored_when_matching():
    result = matrix.compare(
        declared(runtime=[{"name": "requests[socks]", "specifier": None}]),
        installed(("requests", "2.32.0")),
        imported("requests"),
    )
    assert result["missing"] == []
    assert result["verdict"] == "ok"


@pytest.mark.parametrize(
    "declared_view, installed_view, imported_view",
    [
        (None, installed(), imported()),
        (declared(), None, imported()),
        (declared(), installed(), None),
        ({"files": []}, installed(), imported()),
        (declared(), {"installed": "nope"}, imported()),
        (declared(), installed(), {"modules": [{"files": []}]}),
    ],
)
def test_a_missing_or_ill_shaped_view_is_unknown(declared_view, installed_view, imported_view):
    result = matrix.compare(declared_view, installed_view, imported_view)
    assert result == {
        "missing": [],
        "unexpected": [],
        "unused_declared": [],
        "version_mismatch": [],
        "verdict": "unknown",
    }


def test_an_invalid_specifier_is_unjudged_not_a_mismatch():
    result = matrix.compare(
        declared(runtime=[{"name": "requests", "specifier": "not a specifier"}]),
        installed(("requests", "2.32.0")),
        imported("requests"),
    )
    assert result["version_mismatch"] == []
    assert result["verdict"] == "unknown"


def test_without_packaging_a_runtime_specifier_is_unknown(monkeypatch):
    monkeypatch.setattr(matrix, "SpecifierSet", None)
    monkeypatch.setattr(matrix, "Version", None)
    result = matrix.compare(
        declared(runtime=[{"name": "requests", "specifier": ">=2.28.0"}]),
        installed(("requests", "2.32.0")),
        imported("requests"),
    )
    assert result["version_mismatch"] == []
    assert result["verdict"] == "unknown"


def test_a_dev_only_gap_is_listed_but_not_attention():
    result = matrix.compare(
        declared(runtime=[{"name": "requests", "specifier": None}], dev=[{"name": "black", "specifier": ">=24.0"}]),
        installed(("requests", "2.32.0")),
        imported("requests"),
    )
    assert result["missing"] == [{"name": "black", "specifier": ">=24.0"}]
    assert result["verdict"] == "ok"


def test_the_runtime_declaration_wins_over_the_dev_one():
    result = matrix.compare(
        declared(
            runtime=[{"name": "requests", "specifier": ">=3.0"}],
            dev=[{"name": "requests", "specifier": ">=2.0"}],
        ),
        installed(("requests", "2.32.0")),
        imported("requests"),
    )
    assert result["version_mismatch"] == [
        {"name": "requests", "declared": ">=3.0", "installed": "2.32.0"}
    ]


def test_an_alias_maps_the_import_name_to_the_distribution():
    result = matrix.compare(
        declared(runtime=[{"name": "PyYAML", "specifier": ">=6.0.2"}]),
        installed(("pyyaml", "6.0.2")),
        imported("yaml"),
    )
    assert result["unused_declared"] == []
    assert result["verdict"] == "ok"


def test_empty_but_valid_views_are_ok():
    result = matrix.compare(declared(), installed(), imported())
    assert result["verdict"] == "ok"
    assert result["missing"] == [] and result["unexpected"] == []


def test_output_lists_are_sorted():
    result = matrix.compare(
        declared(),
        installed(("zeta", "1"), ("alpha", "1"), ("mu", "1")),
        imported(),
    )
    assert [row["name"] for row in result["unexpected"]] == ["alpha", "mu", "zeta"]


def test_the_module_is_pure():
    source = Path(matrix.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden = {"os", "sys", "subprocess", "pathlib", "importlib", "shutil", "socket", "urllib", "io", "tempfile"}
    imported_roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open":
            raise AssertionError("the matrix must not open files")
    assert not (imported_roots & forbidden), sorted(imported_roots & forbidden)
    assert imported_roots <= {"__future__", "re", "packaging"}

def test_an_installed_entry_without_a_version_is_unjudged():
    result = matrix.compare(
        declared(runtime=[{"name": "requests", "specifier": ">=2.28.0"}]),
        {"installed": [{"name": "requests"}]},
        imported("requests"),
    )
    assert result["version_mismatch"] == []
    assert result["verdict"] == "unknown"


def test_a_declared_entry_without_a_name_is_unknown():
    result = matrix.compare(
        declared(runtime=[{"specifier": ">=1.0"}]),
        installed(('requests', '2.32.0')),
        imported("requests"),
    )
    assert result == {
        "missing": [],
        "unexpected": [],
        "unused_declared": [],
        "version_mismatch": [],
        "verdict": "unknown",
    }
