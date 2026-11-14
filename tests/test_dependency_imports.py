"""Lane I: the import scanner, on synthetic trees and on the real one.

Synthetic trees live under pytest's own temporary directories; nothing here
reads the real library, gallery or backups.
"""
from __future__ import annotations

import ast
import datetime
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
TOOLS = REPO / "tools" / "maintenance"
sys.path.insert(0, str(TOOLS))
import dependency_imports as scan  # noqa: E402


def _tree(root: Path, files: dict) -> Path:
    for relative, text in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            target.write_bytes(text)
        else:
            target.write_text(text, encoding="utf-8")
    return root


def _by_name(result: dict) -> dict:
    return {item["top_level"]: item for item in result["modules"] if item["top_level"]}


def _broken(result: dict) -> list:
    return [item for item in result["modules"] if not item["top_level"]]


def test_stdlib_is_marked_and_has_no_origin(tmp_path):
    root = _tree(tmp_path, {"core/a.py": "import os\nfrom json import dumps\n"})
    names = _by_name(scan.imported_modules(root))
    assert set(names) == {"os", "json"}
    for item in names.values():
        assert item["stdlib"] is True
        assert item["resolved"] is None
        assert item["unresolved_reason"] is None
        assert item["count"] == 1
        assert item["files"] == ["core/a.py"]


def test_third_party_is_resolved_to_a_file(tmp_path):
    root = _tree(tmp_path, {"gui/b.py": "import requests\n"})
    item = _by_name(scan.imported_modules(root))["requests"]
    assert item["stdlib"] is False
    assert isinstance(item["resolved"], dict)
    assert item["resolved"]["origin"].replace("\\", "/").endswith("requests/__init__.py")
    assert item["unresolved_reason"] is None


def test_local_packages_are_not_dependencies(tmp_path):
    root = _tree(tmp_path, {"gui/c.py": "from core.library_paths import PathResolver\n"})
    item = _by_name(scan.imported_modules(root))["core"]
    assert item["stdlib"] is False
    assert item["resolved"] is None
    assert item["unresolved_reason"] == "local_package"


def test_relative_imports_do_not_invent_modules(tmp_path):
    root = _tree(tmp_path, {
        "core/pkg/mod.py": "from . import sibling\nfrom ..other import thing\n",
        "core/pkg/__init__.py": "",
    })
    result = scan.imported_modules(root)
    assert result["modules"] == []


def test_the_same_module_in_two_files_aggregates(tmp_path):
    root = _tree(tmp_path, {"core/a.py": "import os\n", "gui/b.py": "import os\n"})
    item = _by_name(scan.imported_modules(root))["os"]
    assert item["count"] == 2
    assert item["files"] == ["core/a.py", "gui/b.py"]


def test_a_missing_third_party_is_reported_not_installed(tmp_path):
    root = _tree(tmp_path, {"core/d.py": "import totally_missing_module_xyz\n"})
    item = _by_name(scan.imported_modules(root))["totally_missing_module_xyz"]
    assert item["resolved"] is None
    assert item["unresolved_reason"] == "not_installed"


def test_a_syntax_error_is_reported_and_does_not_hide_other_files(tmp_path):
    root = _tree(tmp_path, {
        "core/good.py": "import os\n",
        "gui/broken.py": "def (\n",
    })
    result = scan.imported_modules(root)
    assert "os" in _by_name(result)
    broken = _broken(result)
    assert len(broken) == 1
    assert broken[0]["files"] == ["gui/broken.py"]
    assert broken[0]["unresolved_reason"].startswith("syntax_error:")
    assert broken[0]["resolved"] is None and broken[0]["stdlib"] is False


def test_an_unreadable_file_is_reported_as_such(tmp_path):
    root = _tree(tmp_path, {"utils/binary.py": b"\xff\xfe not utf-8\n"})
    broken = _broken(scan.imported_modules(root))
    assert len(broken) == 1
    assert broken[0]["unresolved_reason"] == "unreadable: UnicodeDecodeError"


def test_generated_at_is_an_iso_timestamp(tmp_path):
    root = _tree(tmp_path, {"core/a.py": "import os\n"})
    stamp = scan.imported_modules(root)["generated_at"]
    datetime.datetime.fromisoformat(stamp)


def test_the_scanner_cannot_change_the_environment():
    """Read-only proof: no process spawning and no installer calls."""
    source = (TOOLS / "dependency_imports.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden_calls = {"install", "uninstall", "upgrade", "run", "check_call", "Popen"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in {"subprocess", "os"}, alias.name
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in {"subprocess", "os"}
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in forbidden_calls, node.func.attr
        if isinstance(node, ast.Attribute) and node.attr == "environ":
            raise AssertionError("the scanner must not touch the environment")


def test_the_real_tree_finds_requests_as_a_third_party():
    result = scan.imported_modules(REPO)
    names = _by_name(result)
    assert "requests" in names, "the application imports requests, so the scan must see it"
    item = names["requests"]
    assert item["stdlib"] is False
    assert item["count"] >= 1
    assert item["resolved"] is not None
    assert item["resolved"]["origin"].replace("\\", "/").endswith("requests/__init__.py")
    assert all(path.startswith(("core/", "gui/", "utils/")) for path in item["files"])
    for local in ("core", "gui", "utils"):
        if local in names:
            assert names[local]["unresolved_reason"] == "local_package"
    assert _broken(result) == [], "the shipped tree must parse cleanly"
