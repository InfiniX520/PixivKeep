"""Read-only facts about the interpreter this project is running in.

Nothing in this module changes the environment: it never installs, upgrades or
uninstalls a package, never writes an environment variable and never touches a
file.  The single subprocess is ``pip check``, which only reads installed
metadata and reports what is inconsistent.

The result is deliberately plain data (JSON-safe dicts and lists) so the
composer can print it, diff it, or attach it to a report without importing
anything else from this lane.

Contract (frozen in docs/optimization/2026-11-14/U1B_FIVE_WAY.md section 0)::

    environment_facts() -> {
      "python": {version, implementation, executable, env_prefix, is_conda},
      "platform": {system, release, machine},
      "pip_check": {exit, output},
      "installed": [{name, version}],          # sorted by name, then version
      "generated_at": iso8601
    }

One additive key is reported beyond the frozen five: ``metadata_errors`` lists
the distributions whose metadata could not be read (a broken ``METADATA``,
a corrupted ``RECORD``).  Such a distribution is skipped instead of crashing
the report, and the list is what makes the skip visible; an empty list means
every distribution was read.  It is additive on purpose: no frozen key changes
shape, so a composer written against section 0 keeps working.
"""
from __future__ import annotations

import importlib.metadata as metadata
import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: Reading metadata is fast; a hang here means a broken pip, not a slow check.
PIP_CHECK_TIMEOUT_SECONDS = 60.0


def _generated_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _python_facts() -> dict[str, Any]:
    prefix = Path(sys.prefix)
    try:
        is_conda = (prefix / "conda-meta").is_dir()
    except OSError:
        # An unreadable prefix is not evidence of a conda environment.
        is_conda = False
    return {
        "version": platform.python_version(),
        "implementation": platform.python_implementation(),
        "executable": sys.executable,
        "env_prefix": str(prefix),
        "is_conda": bool(is_conda),
    }


def _platform_facts() -> dict[str, str]:
    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
    }


def _pip_check() -> dict[str, Any]:
    """Run only ``pip check`` and record what it said, including failure.

    A non-zero exit is a fact about the environment, not an exception: the
    caller has to be able to report "pip check said these packages are broken"
    without the report itself dying.
    """
    argv = [sys.executable, "-m", "pip", "check"]
    try:
        finished = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=PIP_CHECK_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return {
            "exit": -1,
            "output": "pip check timed out after %.0f s"
            % PIP_CHECK_TIMEOUT_SECONDS,
        }
    except OSError as error:
        return {"exit": -1, "output": "pip check could not start: %s" % error}
    output = ((finished.stdout or "") + (finished.stderr or "")).strip()
    return {"exit": int(finished.returncode), "output": output}


def _distribution_name(distribution: object) -> str:
    """A name for the error list, even when the metadata itself is broken."""
    for attribute in ("name", "version"):
        try:
            value = getattr(distribution, attribute, None)
        except Exception:  # noqa: BLE001 - a broken distribution can raise anything
            continue
        if isinstance(value, str) and value:
            return value
    return "?"


def _installed() -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    rows: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    for distribution in metadata.distributions():
        try:
            name = distribution.metadata["Name"]
            version = distribution.version
            if not name or not version:
                raise ValueError("distribution without a name or version")
        except Exception as error:  # noqa: BLE001 - one bad package must not stop the list
            errors.append(
                {
                    "name": _distribution_name(distribution),
                    "reason": "%s: %s" % (type(error).__name__, error),
                }
            )
            continue
        rows.append({"name": str(name), "version": str(version)})
    rows.sort(key=lambda item: (item["name"].lower(), item["version"]))
    errors.sort(key=lambda item: item["name"].lower())
    return rows, errors


def environment_facts() -> dict[str, Any]:
    """Everything this module can observe about the running interpreter."""
    installed, metadata_errors = _installed()
    return {
        "python": _python_facts(),
        "platform": _platform_facts(),
        "pip_check": _pip_check(),
        "installed": installed,
        "metadata_errors": metadata_errors,
        "generated_at": _generated_at(),
    }


def main() -> int:
    print(json.dumps(environment_facts(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover - thin script guard
    raise SystemExit(main())
