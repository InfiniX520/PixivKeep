"""Read-only dependency report: one JSON, one text view, no environment change.

Five small modules do the work (environment facts, declared requirements, import
scan, comparison, rollback material); this file only composes them.  It never
installs, upgrades or uninstalls anything: the report is a statement about the
interpreter as it is, plus what a future maintenance window would have to carry
away in order to be reversible.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):  # run as a script: python tools/maintenance/dependency_report.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.maintenance import dependency_declared  # noqa: E402
from tools.maintenance import dependency_env  # noqa: E402
from tools.maintenance import dependency_imports  # noqa: E402
from tools.maintenance import dependency_matrix  # noqa: E402
from tools.maintenance import dependency_rollback  # noqa: E402

SCHEMA = "dependency-report/1"


def _normalise(name: object) -> str:
    text = str(name or "").strip().lower()
    for mark in ("-", "_", "."):
        text = text.replace(mark, "-")
    return text


def _declared_groups(declared: dict) -> dict[str, str]:
    """PEP 503-normalised name -> the bucket it was declared in."""
    groups: dict[str, str] = {}
    # First bucket wins: a name that arrives through "-r requirements.txt" inside
    # the dev or build file is still a runtime dependency, and calling it "build"
    # would hide the one group a reader can act on.
    for bucket in ("runtime", "dev", "build"):
        for item in declared.get(bucket) or []:
            if isinstance(item, dict):
                groups.setdefault(_normalise(item.get("name")), bucket)
    return groups


def _unparsed(imported: dict) -> list[dict]:
    return [item for item in imported.get("modules") or [] if not item.get("top_level")]


def build_report(root: Path | None = None) -> dict:
    """Compose the five modules into one report.  Reads, never writes."""
    root = Path(root or Path(__file__).resolve().parents[2]).resolve()
    env = dependency_env.environment_facts()
    declared = dependency_declared.declared_requirements(root)
    imported = dependency_imports.imported_modules(root)
    # The matrix adapters take the *reports*, not the bare rows: installed is
    # read through env["installed"] and the import view needs every row to carry
    # a real top_level, so parse failures are filtered out here (they are
    # reported separately as unparsed).
    matrix = dependency_matrix.compare(declared, env, {"modules": [item for item in imported.get("modules") or [] if item.get("top_level")]})
    rollback = dependency_rollback.rollback_material(env)

    groups = _declared_groups(declared)
    unused = []
    for item in matrix.get("unused_declared") or []:
        if not isinstance(item, dict):
            continue
        unused.append(dict(item, group=groups.get(_normalise(item.get("name")), "unknown")))

    notes: list[str] = []
    if matrix.get("verdict") == "unknown":
        notes.append(
            "verdict=unknown：输入不足或版本规格无法判定；四张清单为空不代表没有差异。"
        )
    if env.get("metadata_errors"):
        notes.append(
            "%d 个分发包的元数据读不出来，已跳过并计数。" % len(env["metadata_errors"])
        )
    if unused:
        runtime_unused = [item for item in unused if item.get("group") == "runtime"]
        notes.append(
            "unused_declared 含 %d 项，其中 runtime 组 %d 项；dev/build 的「未使用」多半只是在 "
            "tests/ 或构建脚本里用，import 扫描只覆盖 core/gui/utils。"
            % (len(unused), len(runtime_unused))
        )
    notes.append(
        "unexpected 是「装了但既没声明也没直接 import」的清单，天然包含传递依赖，不是缺陷列表。"
    )
    notes.append("本报告只读：没有安装、升级、卸载，也没有修改任何环境变量。")

    return {
        "schema": SCHEMA,
        "root": str(root),
        "generated_at": env.get("generated_at"),
        "environment": {key: value for key, value in env.items() if key != "installed"},
        "installed_count": len(env.get("installed") or []),
        "declared": declared,
        "imports": {
            "generated_at": imported.get("generated_at"),
            "modules": [item for item in imported.get("modules") or [] if item.get("top_level")],
            "unparsed": _unparsed(imported),
        },
        "matrix": dict(matrix, unused_declared=unused),
        "rollback": rollback,
        "notes": notes,
    }


def _lines(report: dict) -> list[str]:
    env = report["environment"]
    python = env.get("python") or {}
    platform = env.get("platform") or {}
    matrix = report["matrix"]
    lines = [
        "环境: %s %s（%s）" % (python.get("implementation"), python.get("version"), python.get("executable")),
        "平台: %s %s" % (platform.get("system"), platform.get("release")),
        "pip check: exit=%s %s" % ((env.get("pip_check") or {}).get("exit"), (env.get("pip_check") or {}).get("output", "").strip()[:60]),
        "已装分发包: %d" % report["installed_count"],
        "声明: runtime %d / dev %d / build %d（文件 %d）"
        % (
            len(report["declared"].get("runtime") or []),
            len(report["declared"].get("dev") or []),
            len(report["declared"].get("build") or []),
            len(report["declared"].get("files") or []),
        ),
        "import: %d 个顶层模块，未解析文件 %d" % (len(report["imports"]["modules"]), len(report["imports"]["unparsed"])),
        "差异: missing %d / unexpected %d / unused %d / mismatch %d → verdict=%s"
        % (
            len(matrix.get("missing") or []),
            len(matrix.get("unexpected") or []),
            len(matrix.get("unused_declared") or []),
            len(matrix.get("version_mismatch") or []),
            matrix.get("verdict"),
        ),
    ]
    for item in (matrix.get("missing") or [])[:10]:
        lines.append("  缺: %s %s" % (item.get("name"), item.get("specifier") or ""))
    for item in (matrix.get("version_mismatch") or [])[:10]:
        lines.append("  不符: %s 声明 %s 实装 %s" % (item.get("name"), item.get("declared"), item.get("installed")))
    for item in (matrix.get("unused_declared") or [])[:10]:
        lines.append("  未用: %s（%s）" % (item.get("name"), item.get("group", "unknown")))
    states = report["rollback"]["three_states"]
    lines.append(
        "三态: advisory=%s upgrade_done=%s compat=%s"
        % (states.get("advisory_matched"), states.get("upgrade_done"), states.get("compat_checked"))
    )
    for note in report["notes"]:
        lines.append("注: " + note)
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dependency_report.py", description="只读依赖报告（不安装、不升级）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--root", default=None, help="仓库根目录（默认本文件上两级）")
    arguments = parser.parse_args(argv)
    report = build_report(Path(arguments.root) if arguments.root else None)
    if arguments.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for line in _lines(report):
            print(line)
    verdict = report["matrix"].get("verdict")
    return 0 if verdict == "ok" else 3 if verdict == "unknown" else 2


if __name__ == "__main__":
    raise SystemExit(main())
