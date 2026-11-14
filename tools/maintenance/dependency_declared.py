"""Read the project's declared dependencies without touching the environment.

This module answers one question: *what does this repository say it needs?*
It never installs, upgrades, removes or resolves anything; it only reads files
and reports what they declare.

Public contract (frozen by docs/optimization/2026-11-14/U1B_FIVE_WAY.md):

    declared_requirements(root) -> {
        "files":   [{"path": str, "kind": str}],
        "runtime": [{"name": str, "specifier": str}],
        "dev":     [...],
        "build":   [...],
        "errors":  [{"file": str, "reason": str}],
        "generated_at": iso8601 str,
    }

Kinds are `runtime` (requirements.txt), `dev` (requirements-dev.txt),
`build` (requirements-build.txt), `pyproject` (pyproject.toml) and
`include` for a file that is only reachable through `-r`.

Rules that the tests pin:

* comments and blank lines are ignored; a trailing backslash continues a line;
* `-r other.txt` / `--requirement other.txt` is expanded in place, relative to
  the including file, with cycle and depth detection;
* anything this parser cannot represent becomes one entry in `errors` instead of
  an exception: bad option lines, unparsable requirement lines, broken TOML,
  unreadable or missing include targets;
* a specifier is kept verbatim (`">=3.7.0"`, `">=1.0,<2"`), and the empty
  string means "no version constraint"; extras are kept in the specifier so no
  information is lost;
* `pyproject.toml` contributes `[project].dependencies` to runtime and
  `[build-system].requires` to build.  `[project.optional-dependencies]` is a
  known gap: it is not represented in this contract and is therefore ignored.

The module is deliberately read-only: its imports are the standard library only
and it never runs a subprocess or mutates the environment.
"""
from __future__ import annotations

import datetime
import re
import tomllib
from pathlib import Path

#: (file name, kind) in the order the report lists them.
TOP_LEVEL_FILES = (
    ("requirements.txt", "runtime"),
    ("requirements-dev.txt", "dev"),
    ("requirements-build.txt", "build"),
    ("pyproject.toml", "pyproject"),
)

MAX_INCLUDE_DEPTH = 8

#: A requirement starts with a distribution name; everything after it is kept
#: verbatim as the specifier (including extras and environment markers).
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _strip_comment(line: str) -> str:
    """Drop a `#` comment: at the start, or preceded by whitespace (pip's rule)."""
    if line.lstrip().startswith("#"):
        return ""
    match = re.search(r"\s#", line)
    if match is not None:
        return line[: match.start()]
    return line


def _join_continuations(text: str) -> list[str]:
    """Split into logical lines, joining a trailing backslash continuation."""
    raw = text.splitlines()
    logical: list[str] = []
    pending = ""
    for line in raw:
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            pending += stripped[:-1] + " "
            continue
        logical.append(pending + line)
        pending = ""
    if pending:
        logical.append(pending)
    return logical


def _parse_requirement(text: str) -> tuple[str, str] | None:
    """(name, specifier) for one requirement line, or None when unparsable."""
    body = text.strip()
    if not body:
        return None
    match = _NAME_RE.match(body)
    if match is None:
        return None
    name = body[: match.end()]
    return name, body[match.end():].strip()


def _include_target(text: str) -> str | None:
    """The path named by `-r x` / `--requirement x`, or None for other lines."""
    body = text.strip()
    for prefix in ("-r", "--requirement"):
        if body == prefix:
            return ""
        if body.startswith(prefix + " ") or body.startswith(prefix + "="):
            return body[len(prefix) + 1:].strip()
    return None


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _parse_requirements_file(
    path: Path,
    *,
    kind: str,
    chain: tuple[Path, ...],
    files: list[dict[str, str]],
    buckets: dict[str, list[dict[str, str]]],
    seen: dict[str, set[tuple[str, str]]],
    errors: list[dict[str, str]],
    display: str,
) -> None:
    """Expand one requirements file into its bucket, in place."""
    try:
        text = _read_text(path)
    except FileNotFoundError:
        errors.append({"file": display, "reason": "include_missing: %s" % path.name})
        return
    except (OSError, UnicodeDecodeError) as error:
        errors.append(
            {"file": display, "reason": "unreadable: %s: %s" % (path.name, error)}
        )
        return

    target_bucket = buckets[kind]
    for line_number, raw_line in enumerate(_join_continuations(text), start=1):
        line = _strip_comment(raw_line)
        if not line.strip():
            continue
        include = _include_target(line)
        if include is not None:
            if not include:
                errors.append(
                    {
                        "file": display,
                        "reason": "bad_include: line %d has no target" % line_number,
                    }
                )
                continue
            child = (path.parent / include).resolve()
            child_display = _display_for(child, as_written=include)
            if not child.is_file():
                errors.append(
                    {"file": display, "reason": "include_missing: %s" % include}
                )
                continue
            if child in (path, *chain):
                errors.append(
                    {"file": display, "reason": "include_cycle: %s" % include}
                )
                continue
            if len(chain) >= MAX_INCLUDE_DEPTH:
                errors.append(
                    {"file": display, "reason": "include_depth: %s" % include}
                )
                continue
            if not any(item["path"] == child_display for item in files):
                files.append({"path": child_display, "kind": "include"})
            _parse_requirements_file(
                child,
                kind=kind,
                chain=chain + (path,),
                files=files,
                buckets=buckets,
                seen=seen,
                errors=errors,
                display=child_display,
            )
            continue
        if line.lstrip().startswith("-"):
            errors.append(
                {
                    "file": display,
                    "reason": "unsupported_option: line %d: %s"
                    % (line_number, line.strip()[:80]),
                }
            )
            continue
        parsed = _parse_requirement(line)
        if parsed is None or not parsed[0]:
            errors.append(
                {
                    "file": display,
                    "reason": "bad_requirement: line %d: %s"
                    % (line_number, line.strip()[:80]),
                }
            )
            continue
        name, specifier = parsed
        key = (name.casefold(), specifier)
        if key in seen[kind]:
            continue
        seen[kind].add(key)
        target_bucket.append({"name": name, "specifier": specifier})


def _parse_pyproject(
    path: Path,
    *,
    files: list[dict[str, str]],
    buckets: dict[str, list[dict[str, str]]],
    seen: dict[str, set[tuple[str, str]]],
    errors: list[dict[str, str]],
    display: str,
) -> None:
    """Read [project].dependencies and [build-system].requires from pyproject."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        errors.append({"file": display, "reason": "include_missing: pyproject.toml"})
        return
    except OSError as error:
        errors.append(
            {"file": display, "reason": "unreadable: pyproject.toml: %s" % error}
        )
        return
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        errors.append({"file": display, "reason": "bad_toml: %s" % error})
        return

    project = document.get("project")
    if isinstance(project, dict):
        _add_pep508_list(
            project.get("dependencies"),
            kind="runtime",
            buckets=buckets,
            seen=seen,
            errors=errors,
            display=display,
            field="project.dependencies",
        )
    build_system = document.get("build-system")
    if isinstance(build_system, dict):
        _add_pep508_list(
            build_system.get("requires"),
            kind="build",
            buckets=buckets,
            seen=seen,
            errors=errors,
            display=display,
            field="build-system.requires",
        )


def _add_pep508_list(
    values: object,
    *,
    kind: str,
    buckets: dict[str, list[dict[str, str]]],
    seen: dict[str, set[tuple[str, str]]],
    errors: list[dict[str, str]],
    display: str,
    field: str,
) -> None:
    if values is None:
        return
    if not isinstance(values, list):
        errors.append(
            {"file": display, "reason": "bad_field: %s is not a list" % field}
        )
        return
    for item in values:
        if not isinstance(item, str):
            errors.append(
                {"file": display, "reason": "bad_field: %s has a non-string" % field}
            )
            continue
        parsed = _parse_requirement(item)
        if parsed is None or not parsed[0]:
            errors.append(
                {
                    "file": display,
                    "reason": "bad_requirement: %s: %s" % (field, item[:80]),
                }
            )
            continue
        name, specifier = parsed
        key = (name.casefold(), specifier)
        if key in seen[kind]:
            continue
        seen[kind].add(key)
        buckets[kind].append({"name": name, "specifier": specifier})


def _display_for(path: Path, *, as_written: str) -> str:
    """The name a file is referenced by, so no absolute path leaks into the report."""
    candidate = Path(as_written)
    if candidate.is_absolute():
        return candidate.name
    return candidate.as_posix()


def declared_requirements(root) -> dict[str, object]:
    """Everything the repository declares, or a typed error entry per problem."""
    base = Path(root).expanduser().resolve()
    files: list[dict[str, str]] = []
    buckets: dict[str, list[dict[str, str]]] = {
        "runtime": [],
        "dev": [],
        "build": [],
    }
    seen: dict[str, set[tuple[str, str]]] = {
        "runtime": set(),
        "dev": set(),
        "build": set(),
    }
    errors: list[dict[str, str]] = []

    for name, kind in TOP_LEVEL_FILES:
        path = base / name
        if not path.is_file():
            continue
        files.append({"path": name, "kind": kind})
        if kind == "pyproject":
            _parse_pyproject(
                path,
                files=files,
                buckets=buckets,
                seen=seen,
                errors=errors,
                display=name,
            )
        else:
            _parse_requirements_file(
                path,
                kind=kind,
                chain=(),
                files=files,
                buckets=buckets,
                seen=seen,
                errors=errors,
                display=name,
            )

    return {
        "files": files,
        "runtime": buckets["runtime"],
        "dev": buckets["dev"],
        "build": buckets["build"],
        "errors": errors,
        "generated_at": _now(),
    }


if __name__ == "__main__":  # pragma: no cover - thin manual entry
    import json
    import sys

    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[2]
    print(json.dumps(declared_requirements(target), ensure_ascii=False, indent=2))
