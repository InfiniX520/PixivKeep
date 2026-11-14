"""Read-only dependency report: one JSON, one text view, no environment change.

Five small modules do the work (environment facts, declared requirements, import
scan, comparison, rollback material); this file only composes them.  It never
installs, upgrades or uninstalls anything: the report is a statement about the
interpreter as it is, plus what a future maintenance window would have to carry
away in order to be reversible.

Evidence completeness is part of the answer, not a footnote.  Three collectors
feed the judgement and each of them can come back incomplete: a requirements
file that includes a file that is not there, a source file that cannot be
parsed, a pip check that fails or a distribution whose metadata is unreadable.
Every one of those makes the corresponding inputs.*.ok False, and an incomplete
input can never produce exit 0.  Exit codes:

    0  inputs complete, every applicable constraint satisfied, pip check clean
    2  a conflict or a missing dependency is proven (attention)
    3  an input is incomplete or was not executed (unknown)

A proven conflict outranks an unknown input: if a runtime constraint is already
known to fail, the report says attention/2 even when another collector was
incomplete - the reader has something to act on.  The incompleteness is still
listed in inputs and in the notes.
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

SCHEMA = "dependency-report/2"

#: verdict -> process exit code.  Spelled out instead of implied.
EXIT_CODES = {"ok": 0, "attention": 2, "unknown": 3}


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


def _rows(value: object) -> list[dict]:
    return [dict(item) for item in value or () if isinstance(item, dict)]


def collect_inputs(declared: dict, imported: dict, env: dict) -> dict:
    """The three evidence collectors, each with its own ok flag.

    ok False means "this view is incomplete", which is a statement about the
    evidence, not about the environment: it can never end in exit 0.
    """
    errors = _rows(declared.get("errors"))
    unparsed = _unparsed(imported)
    pip = env.get("pip_check") if isinstance(env.get("pip_check"), dict) else {}
    pip_exit = pip.get("exit")
    metadata_errors = _rows(env.get("metadata_errors"))
    return {
        "declared": {"ok": not errors, "errors": errors},
        "imports": {"ok": not unparsed, "unparsed": unparsed},
        "environment": {
            "ok": pip_exit == 0 and not metadata_errors,
            "pip_check_exit": pip_exit,
            "metadata_errors": metadata_errors,
        },
    }


def _incomplete(inputs: dict) -> list[str]:
    return [name for name in ("declared", "imports", "environment") if not inputs[name]["ok"]]


def overall_verdict(matrix_verdict: object, inputs: dict) -> str:
    """attention > unknown > ok, with incompleteness folded into unknown."""
    if matrix_verdict == "attention":
        return "attention"
    if matrix_verdict != "ok" or _incomplete(inputs):
        return "unknown"
    return "ok"


def build_report(root: Path | None = None) -> dict:
    """Compose the five modules into one report.  Reads, never writes."""
    root = Path(root or Path(__file__).resolve().parents[2]).resolve()
    env = dependency_env.environment_facts()
    declared = dependency_declared.declared_requirements(root)
    imported = dependency_imports.imported_modules(root)
    # The matrix adapters take the *reports*, not the bare rows: installed is
    # read through env["installed"] and the import view needs every row to carry
    # a real top_level, so parse failures are filtered out here (they are
    # reported separately as unparsed, and they make inputs.imports.ok False).
    matrix = dependency_matrix.compare(declared, env, {"modules": [item for item in imported.get("modules") or [] if item.get("top_level")]})
    rollback = dependency_rollback.rollback_material(env)
    inputs = collect_inputs(declared, imported, env)
    aggregated = dependency_matrix.constraints(declared)
    verdict = overall_verdict(matrix.get("verdict"), inputs)
    exit_code = EXIT_CODES[verdict]

    groups = _declared_groups(declared)
    unused = []
    for item in matrix.get("unused_declared") or []:
        if not isinstance(item, dict):
            continue
        unused.append(dict(item, group=groups.get(_normalise(item.get("name")), "unknown")))

    notes: list[str] = []
    if inputs["declared"]["ok"] is False:
        notes.append(
            "声明解析有 %d 条错误（include 缺失/坏行/坏 TOML），声明视图不完整，verdict 不能是 ok。"
            % len(inputs["declared"]["errors"])
        )
    if inputs["imports"]["ok"] is False:
        notes.append(
            "%d 个源文件无法解析，import 视图不完整，unused_declared 可能多报。"
            % len(inputs["imports"]["unparsed"])
        )
    if inputs["environment"]["ok"] is False:
        pip_exit = inputs["environment"]["pip_check_exit"]
        if pip_exit != 0:
            notes.append("pip check 未通过或未执行（exit=%s），环境视图不完整。" % (pip_exit,))
        if inputs["environment"]["metadata_errors"]:
            notes.append(
                "%d 个分发包的元数据读不出来，已跳过并计数。"
                % len(inputs["environment"]["metadata_errors"])
            )
    unjudged = [
        item["name"] for item in aggregated if item.get("unjudged")
    ]
    if unjudged:
        notes.append(
            "%d 个依赖有无法判定的约束（marker 或 specifier 解析不了）: %s"
            % (len(unjudged), ", ".join(unjudged[:5]))
        )
    if matrix.get("verdict") == "unknown" and verdict == "unknown":
        notes.append(
            "verdict=unknown：输入不足或版本规格无法判定；四张清单为空不代表没有差异。"
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
        "verdict": verdict,
        "exit": exit_code,
        "inputs": inputs,
        "ingest_incomplete": _incomplete(inputs),
        "environment": {key: value for key, value in env.items() if key != "installed"},
        "installed_count": len(env.get("installed") or []),
        "declared": declared,
        "constraints": aggregated,
        "imports": {
            "generated_at": imported.get("generated_at"),
            "modules": [item for item in imported.get("modules") or [] if item.get("top_level")],
            "unparsed": _unparsed(imported),
        },
        "matrix": dict(matrix, unused_declared=unused),
        "rollback": rollback,
        "notes": notes,
    }


def _flag(ok: bool) -> str:
    return "ok" if ok else "不完整"


def _lines(report: dict) -> list[str]:
    env = report["environment"]
    python = env.get("python") or {}
    platform = env.get("platform") or {}
    matrix = report["matrix"]
    inputs = report["inputs"]
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
        "输入完整性: declared=%s (%d 错误) / imports=%s (%d 未解析) / environment=%s (pip exit=%s, %d 元数据错误)"
        % (
            _flag(inputs["declared"]["ok"]), len(inputs["declared"]["errors"]),
            _flag(inputs["imports"]["ok"]), len(inputs["imports"]["unparsed"]),
            _flag(inputs["environment"]["ok"]), inputs["environment"]["pip_check_exit"],
            len(inputs["environment"]["metadata_errors"]),
        ),
        "约束: %d 个依赖名，其中 %d 个有无法判定的约束"
        % (len(report["constraints"]), len([row for row in report["constraints"] if row.get("unjudged")])),
        "差异: missing %d / unexpected %d / unused %d / mismatch %d → matrix=%s"
        % (
            len(matrix.get("missing") or []),
            len(matrix.get("unexpected") or []),
            len(matrix.get("unused_declared") or []),
            len(matrix.get("version_mismatch") or []),
            matrix.get("verdict"),
        ),
        "判定: %s（exit=%s）%s"
        % (
            report["verdict"], report["exit"],
            "" if not report["ingest_incomplete"] else "；不完整输入: " + ", ".join(report["ingest_incomplete"]),
        ),
    ]
    for item in (matrix.get("missing") or [])[:10]:
        lines.append("  缺: %s %s" % (item.get("name"), item.get("specifier") or ""))
    for item in (matrix.get("version_mismatch") or [])[:10]:
        lines.append("  不符: %s 声明 %s 实装 %s" % (item.get("name"), item.get("declared"), item.get("installed")))
    for item in (matrix.get("unused_declared") or [])[:10]:
        lines.append("  未用: %s（%s）" % (item.get("name"), item.get("group", "unknown")))
    for row in report["constraints"]:
        if not row.get("unjudged"):
            continue
        reasons = "; ".join(str(item.get("reason")) for item in row["unjudged"])
        lines.append("  未判定: %s（%s）" % (row.get("name"), reasons))
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
    return int(report["exit"])


if __name__ == "__main__":
    raise SystemExit(main())
