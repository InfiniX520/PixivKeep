"""Which top-level modules the application actually imports (read-only).

Contract: docs/optimization/2026-11-14/U1B_FIVE_WAY.md, lane I.

The scan answers one question only: what does this source tree name in an
import statement, and can that name be resolved right now.  It never imports a
module to answer it (importlib.util.find_spec resolves a top-level name without
executing the module itself), never writes anything, and never touches the
environment.

Key semantics (frozen, the report composer depends on them):

    modules[i].top_level          top-level name of the import ("requests")
    modules[i].files              distinct scanned files that name it, posix
                                  paths relative to the given root, sorted
    modules[i].count              number of import statements naming it
    modules[i].stdlib             True when the interpreter calls it stdlib
    modules[i].resolved           {"origin": str} when find_spec found a file,
                                  otherwise null
    modules[i].unresolved_reason  null when it resolved or is stdlib;
                                  "local_package" for core/gui/utils;
                                  "not_installed" when find_spec returned None;
                                  "resolution_error: <Type>" when find_spec
                                  raised; "no_origin" for namespace packages

A file that cannot be read or parsed has no import statement to attribute, so
it is reported as its own entry with an empty top_level and a reason starting
with "unreadable: " or "syntax_error: ".  That keeps "we could not look" apart
from "we looked and found nothing": a broken file must not silently disappear
from the report.
"""
from __future__ import annotations

import ast
import datetime
import importlib.util
import sys
from pathlib import Path

#: The packages this project ships; an import of one of them is local by
#: construction, not a third-party dependency.
SCANNED_PACKAGES = ("core", "gui", "utils")
LOCAL_PACKAGES = frozenset(SCANNED_PACKAGES)


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _python_files(root: Path):
    """Every .py file under the scanned packages, in a stable order."""
    for package in SCANNED_PACKAGES:
        base = root / package
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            yield path


def _imported_names(tree: ast.AST) -> list[str]:
    """Top-level names named by one module's import statements.

    Relative imports are skipped on purpose: "from . import x" cannot name a
    third-party module, and reading its module part as a distribution name
    would invent a dependency that does not exist.
    """
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                head = alias.name.split(".")[0]
                if head:
                    names.append(head)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            if node.module:
                head = node.module.split(".")[0]
                if head:
                    names.append(head)
    return names


def _resolve(name: str) -> tuple[bool, dict[str, str] | None, str | None]:
    """(stdlib, resolved, unresolved_reason) for one top-level name."""
    if name in sys.stdlib_module_names or name in sys.builtin_module_names:
        return True, None, None
    if name in LOCAL_PACKAGES:
        return False, None, "local_package"
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, AttributeError, ValueError) as error:
        return False, None, "resolution_error: %s" % type(error).__name__
    if spec is None:
        return False, None, "not_installed"
    origin = getattr(spec, "origin", None)
    if not origin:
        # A namespace package has no file of its own; that is a fact about the
        # package, not evidence that it comes from an installed distribution.
        return False, None, "no_origin"
    return False, {"origin": str(origin)}, None


def imported_modules(root) -> dict:
    """Scan core/, gui/ and utils/ under *root* for import statements."""
    root_path = Path(root)
    files: dict[str, set[str]] = {}
    counts: dict[str, int] = {}
    broken: list[dict] = []
    for path in _python_files(root_path):
        relative = path.relative_to(root_path).as_posix()
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            broken.append({
                "top_level": "",
                "files": [relative],
                "count": 0,
                "stdlib": False,
                "resolved": None,
                "unresolved_reason": "unreadable: %s" % type(error).__name__,
            })
            continue
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as error:
            broken.append({
                "top_level": "",
                "files": [relative],
                "count": 0,
                "stdlib": False,
                "resolved": None,
                "unresolved_reason": "syntax_error: line %s: %s"
                % (error.lineno, error.msg),
            })
            continue
        for name in _imported_names(tree):
            files.setdefault(name, set()).add(relative)
            counts[name] = counts.get(name, 0) + 1

    modules: list[dict] = []
    for name in sorted(files):
        stdlib, resolved, reason = _resolve(name)
        modules.append({
            "top_level": name,
            "files": sorted(files[name]),
            "count": counts[name],
            "stdlib": stdlib,
            "resolved": resolved,
            "unresolved_reason": reason,
        })
    modules.extend(sorted(broken, key=lambda item: item["files"][0]))
    return {"modules": modules, "generated_at": _utc_now()}
