"""Compare the three dependency views without touching the environment.

The inputs are the plain dicts produced by dependency_declared,
dependency_env and dependency_imports.  Nothing here reads the disk, sys.path
or the process environment: the comparison is a pure function of its arguments,
so a report can be replayed and reviewed from a saved snapshot.

Verdict policy (spelled out because it decides what "attention" means):

    attention  a runtime dependency is declared but not installed, or the
               installed version does not satisfy EVERY applicable constraint.
               A known gap outranks an unknown input: an unknown must never
               hide a conflict that is already proven.
    unknown    nothing is known to be broken, but something cannot be judged:
               one of the three views is ill shaped, a declaration carries a
               specifier this interpreter cannot parse, a marker cannot be
               evaluated, or the declaration view itself reported parse errors
               (declared["errors"] is not empty - an incomplete declaration
               list is not evidence of a complete environment).
    ok         no such gap and nothing unjudged.

dev/build declarations are compared too - they show up in missing and
version_mismatch - but they never raise the verdict: a machine that ships the
app without black or isort is not an unhealthy runtime.  unexpected and
unused_declared are informational for the same reason: a shared interpreter
always carries packages nobody declared (pip, setuptools, transitive
dependencies), and this comparison has no dependency metadata that could tell
a transitive package from a stray one.

Constraint aggregation
----------------------

A name may be declared more than once.  Every constraint from the effective
group is kept and ALL of them must hold: the older "first row wins" rule
silently dropped the second specifier of the same package, so ">=1" together
with "<2" against an installed 2.5 came out ok.  The effective group is still
the first group that declares the name (runtime before dev before build), so a
dev declaration can never downgrade a runtime one; a constraint from another
group is recorded in also_declared for the reader instead of being thrown away.

Rendered rows keep their frozen shape: missing carries {name, specifier} and
version_mismatch carries {name, declared, installed}.  When a name carries
several applicable specifiers they are joined with "," - which is how PEP 508
spells a conjunction anyway - so no information is lost and no key changes
meaning.

Name handling follows PEP 503: case-insensitive, and runs of "-", "_" and "."
are one separator.  Extras (requests[socks]) are recorded but do not take part
in matching.  A small alias table maps the few import names that differ from
their distribution name (yaml -> PyYAML, PIL -> Pillow, ...); anything else is
compared by its normalised name, which may report a declared package as unused
when its import name is unrelated.

Environment markers are honoured the way the contract asks: only a marker that
is proven not to apply is excluded from the judgement.  A marker that cannot be
evaluated makes the constraint unjudged instead of quietly passing.
"""
from __future__ import annotations

import re

try:  # optional: exact specifier and marker evaluation
    from packaging.markers import Marker
    from packaging.specifiers import SpecifierSet
    from packaging.version import Version
except Exception:  # pragma: no cover - an environment without packaging
    Marker = None  # type: ignore[assignment]
    SpecifierSet = None  # type: ignore[assignment]
    Version = None  # type: ignore[assignment]

#: Groups in precedence order: the first group that declares a name decides the
#: group it is judged in, so a runtime declaration is never downgraded by a dev
#: one.  Constraints from the other groups are kept as also_declared.
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


def _constraint(raw: object) -> dict[str, object]:
    """One declaration line, split into extras / specifier / marker.

    applies is True when the constraint has to hold, False when a marker
    proved it does not apply on this interpreter, and None when nobody can
    judge it.  None never turns into a pass.
    """
    text = "" if raw is None else str(raw).strip()
    extras = ""
    if text.startswith("["):
        close = text.find("]")
        if close > 0:
            extras = text[1:close].strip()
            text = text[close + 1:].strip()
    marker = ""
    if ";" in text:
        text, marker = text.split(";", 1)
        text, marker = text.strip(), marker.strip()
    record: dict[str, object] = {
        "raw": None if raw is None else str(raw),
        "extras": extras,
        "specifier": text,
        "marker": marker,
        "applies": True,
        "reason": "",
    }
    if marker:
        if Marker is None:
            record["applies"] = None
            record["reason"] = "marker_unjudged: packaging absent"
            return record
        try:
            record["applies"] = bool(Marker(marker).evaluate())
        except Exception:  # noqa: BLE001 - an unparsable marker is data, not a crash
            record["applies"] = None
            record["reason"] = "marker_unjudged: %s" % marker
            return record
        if record["applies"] is False:
            record["reason"] = "marker_not_applicable: %s" % marker
            return record
    if text:
        if SpecifierSet is None:
            record["applies"] = None
            record["reason"] = "specifier_unjudged: packaging absent"
        else:
            try:
                SpecifierSet(text)
            except Exception:  # noqa: BLE001 - a bad specifier is data
                record["applies"] = None
                record["reason"] = "specifier_unjudged: %s" % text
    return record


def _declared(declared: object) -> dict[str, dict[str, object]] | None:
    """One entry per declared name, keeping every constraint; None when ill shaped.

    The effective group is still the first one that declares the name, but all
    of that group's rows are kept: dropping the second specifier of a package
    turned a provable conflict into an ok.
    """
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
            entry = entries.get(key)
            if entry is None:
                entry = entries[key] = {
                    "name": name,
                    "group": group,
                    "constraints": [],
                    "also_declared": [],
                }
            constraint = _constraint(item.get("specifier"))
            if group == entry["group"]:
                entry["constraints"].append(constraint)  # type: ignore[union-attr]
            else:
                entry["also_declared"].append(  # type: ignore[union-attr]
                    {"group": group, "specifier": item.get("specifier")}
                )
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


def _declaration_errors(declared: object) -> tuple[object, ...]:
    if not isinstance(declared, dict):
        return ()
    errors = declared.get("errors")
    return tuple(errors) if isinstance(errors, list) else ()


def _display(constraints: list) -> object:
    """The specifier text a rendered row shows: every applicable one, joined."""
    applicable = [item for item in constraints if item["applies"] is not False]
    pieces = [item["specifier"] for item in applicable if item["specifier"]]
    if pieces:
        return ",".join(dict.fromkeys(pieces))
    if applicable:
        return applicable[0]["raw"]
    return None


def _judgeable(constraints: list) -> str | None:
    pieces = [item["specifier"] for item in constraints
              if item["applies"] is True and item["specifier"]]
    return ",".join(dict.fromkeys(pieces)) if pieces else None


def compare(declared: object, installed: object, imported: object) -> dict[str, object]:
    """Compare declared, installed and imported dependencies.

    Returns the five keys frozen by the U1-B contract: missing, unexpected,
    unused_declared, version_mismatch and verdict.  When an input cannot be read
    the four lists are empty and the verdict is "unknown" - an empty list here
    means "not compared", never "nothing found".

    A declaration view that reports parse errors cannot produce "ok": an
    incomplete list of declarations is not evidence of a healthy environment.
    """
    entries = _declared(declared)
    installed_view = _installed_view(installed)
    imported_keys = _imported_view(imported)
    if entries is None or installed_view is None or imported_keys is None:
        return _result()

    imported_effective = set(imported_keys)
    imported_effective.update(_IMPORT_ALIASES.get(key, key) for key in imported_keys)

    result = _result()
    missing: list[dict[str, object]] = result["missing"]  # type: ignore[assignment]
    unexpected: list[dict[str, object]] = result["unexpected"]  # type: ignore[assignment]
    unused_declared: list[dict[str, object]] = result["unused_declared"]  # type: ignore[assignment]
    mismatch: list[dict[str, object]] = result["version_mismatch"]  # type: ignore[assignment]

    unjudged = bool(_declaration_errors(declared))
    runtime_gap = False
    for key in sorted(entries):
        entry = entries[key]
        name = str(entry["name"])
        group = str(entry["group"])
        constraints = entry["constraints"]  # type: ignore[assignment]
        applicable = [item for item in constraints if item["applies"] is not False]
        if not applicable:
            # Every declaration for this name proved inapplicable on this
            # interpreter (sys_platform == "linux" on Windows, python_version
            # guards, ...).  It is not a missing package, it cannot mismatch, it
            # cannot move the verdict and it is not an unused declaration
            # either: it belongs to another platform, and hiding it from
            # missing while calling it "declared but unused" would send the
            # reader off to delete a dependency that platform needs.  The
            # constraint stays visible in constraints[].excluded with the marker
            # reason.
            continue
        display = _display(constraints)
        judgeable = _judgeable(constraints)
        unjudgeable = [item for item in applicable if item["applies"] is None]
        found = installed_view.get(key)
        if found is None:
            missing.append({"name": name, "specifier": display})
            if group == "runtime":
                runtime_gap = True
            continue
        if unjudgeable and group == "runtime":
            unjudged = True
        if judgeable is not None:
            verdict = _satisfies(judgeable, found.get("version"))
            if verdict is False:
                mismatch.append({
                    "name": name,
                    "declared": display,
                    "installed": found.get("version"),
                })
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

    # A proven gap outranks an unknown input: the reader has to act on it, and
    # "we could not check everything" must not swallow it.
    if runtime_gap:
        result["verdict"] = "attention"
    elif unjudged:
        result["verdict"] = "unknown"
    else:
        result["verdict"] = "ok"
    return result


def constraints(declared: object) -> list[dict[str, object]]:
    """The aggregated declaration view: one row per normalised name.

    Unlike compare() this keeps every constraint - applicable, excluded by a
    marker, or unjudged - plus the group it is judged in and the other groups
    that mentioned it.  Sources are attached when the declared view carries
    them (dependency_declared reports one entry per parsed line); a row without
    a source is still a row, it is not an error.
    """
    entries = _declared(declared)
    if entries is None:
        return []
    sources: dict[str, list[dict[str, object]]] = {}
    if isinstance(declared, dict):
        for item in declared.get("sources") or ():
            if not isinstance(item, dict):
                continue
            key = _key(item.get("name"))
            if not key:
                continue
            sources.setdefault(key, []).append({
                "file": item.get("file"),
                "line": item.get("line"),
                "group": item.get("group"),
                "specifier": item.get("specifier"),
            })
    rows: list[dict[str, object]] = []
    for key in sorted(entries):
        entry = entries[key]
        items = entry["constraints"]  # type: ignore[assignment]
        rows.append({
            "name": entry["name"],
            "key": key,
            "group": entry["group"],
            "applies": list(dict.fromkeys(
                item["specifier"] for item in items
                if item["applies"] is True and item["specifier"]
            )),
            "excluded": [{"specifier": item["specifier"], "reason": item["reason"]}
                         for item in items if item["applies"] is False],
            "unjudged": [{"specifier": item["specifier"], "reason": item["reason"]}
                         for item in items if item["applies"] is None],
            "also_declared": list(entry["also_declared"]),  # type: ignore[arg-type]
            "sources": sources.get(key, []),
        })
    return rows
