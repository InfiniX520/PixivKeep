"""Compare the three dependency views without touching the environment.

The inputs are the plain dicts produced by dependency_declared,
dependency_env and dependency_imports.  Nothing here reads the disk, sys.path
or the process environment: the comparison is a pure function of its arguments,
so a report can be replayed and reviewed from a saved snapshot.

Verdict policy (spelled out because it decides what "attention" means):

    unknown    one of the three views is missing or ill shaped, or a runtime
               declaration carries a specifier this interpreter cannot evaluate
               (packaging absent, or the specifier is not valid).  No guessing.
    attention  a runtime dependency is declared but not installed, or the
               installed version does not satisfy its declaration.
    ok         no such gap.

dev/build declarations are compared too - they show up in missing and
version_mismatch - but they never raise the verdict: a machine that ships the
app without black or isort is not an unhealthy runtime.  unexpected and
unused_declared are informational for the same reason: a shared interpreter
always carries packages nobody declared (pip, setuptools, transitive
dependencies), and this comparison has no dependency metadata that could tell
a transitive package from a stray one.

Name handling follows PEP 503: case-insensitive, and runs of "-", "_" and "."
are one separator.  Extras (requests[socks]) are ignored.  A small alias table
maps the few import names that differ from their distribution name (yaml ->
PyYAML, PIL -> Pillow, ...); anything else is compared by its normalised name,
which may report a declared package as unused when its import name is
unrelated.
"""
from __future__ import annotations

import re

try:  # optional: exact specifier evaluation
    from packaging.specifiers import SpecifierSet
    from packaging.version import Version
except Exception:  # pragma: no cover - an environment without packaging
    SpecifierSet = None  # type: ignore[assignment]
    Version = None  # type: ignore[assignment]

#: Groups in precedence order: the first group that declares a name wins, so
#: a runtime declaration is never downgraded by a dev one.
_GROUPS = ("runtime", "dev", "build")

_SEPARATORS = re.compile(r"[-_.]+")

#: import name -> distribution name, for the cases where they differ.
_IMPORT_ALIASES = {
    "yaml": "pyyaml",
    "pil": "pillow",
    "cv2": "opencv-python",
    "skimage": "scikit-image",
    "bs4": "beautifulsoup4",
    "dateutil": "python-dateutil",
    "dotenv": "python-dotenv",
    "jwt": "pyjwt",
    "serial": "pyserial",
    "attr": "attrs",
    "pkg-resources": "setuptools",
}


def _key(name: object) -> str:
    """Normalised distribution key, or "" when the name carries nothing."""
    text = str(name).split("[", 1)[0].strip()
    if not text:
        return ""
    return _SEPARATORS.sub("-", text).lower()


def _result() -> dict[str, object]:
    return {
        "missing": [],
        "unexpected": [],
        "unused_declared": [],
        "version_mismatch": [],
        "verdict": "unknown",
    }


def _satisfies(specifier: object, version: object) -> bool | None:
    """True/False when this interpreter can judge, None when it cannot.

    None is not "fine": it means the declaration exists but nobody can say
    whether the installed version meets it, so the caller must not call it ok.
    """
    if specifier is None or (isinstance(specifier, str) and not specifier.strip()):
        return True
    if SpecifierSet is None or Version is None:
        return None
    if version is None:
        return None
    try:
        return Version(str(version)) in SpecifierSet(str(specifier))
    except (ValueError, TypeError):
        return None


def _declared(declared: object) -> dict[str, dict[str, object]] | None:
    """One entry per declared name, first group winning; None when ill shaped."""
    if not isinstance(declared, dict):
        return None
    groups = [group for group in _GROUPS if isinstance(declared.get(group), list)]
    if not groups:
        return None
    entries: dict[str, dict[str, object]] = {}
    for group in groups:
        for item in declared[group]:
            if not isinstance(item, dict):
                return None
            name = item.get("name")
            if not isinstance(name, str):
                return None
            key = _key(name)
            if not key:
                return None
            if key not in entries:
                entries[key] = {
                    "name": name,
                    "specifier": item.get("specifier"),
                    "group": group,
                }
    return entries


def _installed_view(installed: object) -> dict[str, dict[str, object]] | None:
    if not isinstance(installed, dict):
        return None
    rows = installed.get("installed")
    if not isinstance(rows, list):
        return None
    view: dict[str, dict[str, object]] = {}
    for row in rows:
        if not isinstance(row, dict):
            return None
        key = _key(row.get("name"))
        if not key:
            return None
        view[key] = {"name": row.get("name"), "version": row.get("version")}
    return view


def _imported_view(imported: object) -> set[str] | None:
    if not isinstance(imported, dict):
        return None
    rows = imported.get("modules")
    if not isinstance(rows, list):
        return None
    keys: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            return None
        top_level = row.get("top_level")
        if not isinstance(top_level, str):
            return None
        key = _key(top_level)
        if not key:
            return None
        keys.add(key)
    return keys


def compare(declared: object, installed: object, imported: object) -> dict[str, object]:
    """Compare declared, installed and imported dependencies.

    Returns the five keys frozen by the U1-B contract: missing, unexpected,
    unused_declared, version_mismatch and verdict.  When an input cannot be read
    the four lists are empty and the verdict is "unknown" - an empty list here
    means "not compared", never "nothing found".
    """
    entries = _declared(declared)
    installed_view = _installed_view(installed)
    imported_keys = _imported_view(imported)
    if entries is None or installed_view is None or imported_keys is None:
        return _result()

    imported_effective = set(imported_keys)
    imported_effective.update(
        _IMPORT_ALIASES.get(key, key) for key in imported_keys
    )

    result = _result()
    missing: list[dict[str, object]] = result["missing"]  # type: ignore[assignment]
    unexpected: list[dict[str, object]] = result["unexpected"]  # type: ignore[assignment]
    unused_declared: list[dict[str, object]] = result["unused_declared"]  # type: ignore[assignment]
    mismatch: list[dict[str, object]] = result["version_mismatch"]  # type: ignore[assignment]

    unjudged = False
    runtime_gap = False
    for key in sorted(entries):
        entry = entries[key]
        name = str(entry["name"])
        group = str(entry["group"])
        specifier = entry["specifier"]
        found = installed_view.get(key)
        if found is None:
            missing.append({"name": name, "specifier": specifier})
            if group == "runtime":
                runtime_gap = True
            continue
        verdict = _satisfies(specifier, found.get("version"))
        if verdict is False:
            mismatch.append(
                {
                    "name": name,
                    "declared": specifier,
                    "installed": found.get("version"),
                }
            )
            if group == "runtime":
                runtime_gap = True
        elif verdict is None and group == "runtime":
            unjudged = True
        if key not in imported_effective:
            unused_declared.append({"name": name})

    for key in sorted(installed_view):
        if key in entries or key in imported_effective:
            continue
        row = installed_view[key]
        unexpected.append({"name": row.get("name"), "version": row.get("version")})

    if unjudged:
        result["verdict"] = "unknown"
    elif runtime_gap:
        result["verdict"] = "attention"
    else:
        result["verdict"] = "ok"
    return result
