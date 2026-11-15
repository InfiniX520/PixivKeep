"""Two read-only report commands: the coverage summary and the filter explainer.

Both commands exist so the engine work has a product entry point that is
deterministic and assertable without a Tk window.  Neither of them writes
anything: the coverage command reads the crawl-state store through
`core.crawl_state_reader.ReadOnlyCrawlState` (SQLite `mode=ro` plus
`query_only`), and the explainer reads two text files.  No HTTP, no gallery, no
image decoding.

The exit code is a statement about the *conclusion*, not about whether the
command ran.  A report that was produced successfully but says "we could not
prove it" is **not** a success:

    0   the conclusion holds   (coverage: overall=complete; explainer: every
                               candidate has a decided verdict)
    2   coverage only: overall=partial (a dimension is known unfinished)
    5   coverage: overall=unknown; explainer: at least one candidate is unknown
    3   the input could not be read or the arguments were refused

`unknown` never maps to 0.  "Could not decide" and "decided it is fine" are
different answers and the exit code has to keep them apart.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from core.crawl_state_reader import CrawlStateUnreadable, ReadOnlyCrawlState
from core.follow_coverage import CoverageError, build_coverage
from core.follow_facts_v2 import V2FactsError, coverage_facts, read_only_connection
from core.follow_coverage_text import render
from core.follow_facts import StateSource, gather_facts
from core.filter_explain import explain
from core.task_contracts import ContractError, FilterSpec

EXIT_OK = 0
EXIT_ATTENTION = 2
EXIT_REFUSED = 3
EXIT_INCOMPLETE = 5

COVERAGE_EXIT = {"complete": EXIT_OK, "partial": EXIT_ATTENTION, "unknown": EXIT_INCOMPLETE}

#: The only condition keys the explainer understands.
CONDITION_KEYS = ("min_bookmarks", "exclude_ai")


class ReportError(ValueError):
    """The command could not produce a report at all.

    This is an input problem, never a conclusion: it must not be rendered as
    "the run is incomplete" and it must not exit 0.
    """

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = str(code)
        self.detail = str(detail)
        super().__init__(self.code if not detail else "%s: %s" % (self.code, detail))


def error_payload(error: ReportError) -> dict[str, Any]:
    """The same shape the other maintenance commands use for a refusal."""
    return {"status": "error", "code": error.code, "detail": error.detail}


# --------------------------------------------------------------------------
# coverage
# --------------------------------------------------------------------------


def coverage_exit_code(overall: object) -> int:
    """The exit code for one summary verdict (see the table in the module doc)."""
    return COVERAGE_EXIT.get(str(overall), EXIT_INCOMPLETE)


def coverage_report(database: Path, run_id: str) -> dict[str, Any]:
    """Build the coverage summary for one crawl run, read-only.

    Everything the crawl store does not carry (page evidence, filter counters)
    stays absent, which is why a summary built from this source tops out at
    `unknown` - the summariser says so instead of inventing numbers.
    """
    try:
        reader = ReadOnlyCrawlState(database)
    except CrawlStateUnreadable as error:
        raise ReportError(error.code, error.detail) from None
    try:
        facts = gather_facts(StateSource(reader, run_id))
    except CrawlStateUnreadable as error:
        raise ReportError(error.code, error.detail) from None
    finally:
        reader.close()
    scope = {"kind": "follow_run", "run_id": str(run_id), "database": str(database)}
    try:
        coverage = build_coverage(facts, scope=scope)
    except CoverageError as error:
        raise ReportError("facts_rejected", str(error)) from None
    payload = coverage.as_dict()
    return {
        "question": "follow_coverage",
        "scope": payload["scope"],
        "coverage": payload,
        "facts_notes": {"page_evidence": "present" if facts.get("pages") else "missing"},
        "exit_code": coverage_exit_code(payload["overall"]),
    }


def _file_stamp(path: Path) -> tuple[int, int]:
    """(size, mtime) of the database file, for the before/after comparison."""
    info = path.stat()
    return (int(info.st_size), int(info.st_mtime_ns))


def coverage_report_v2(database: Path, job_id: str) -> dict[str, Any]:
    """Build the coverage summary for one V2 job, read-only.

    This is the summary the current downloader feeds: the legacy crawl store has
    no rows for a V2 job, which is why the desktop used to answer
    "database_missing" for work it had just done.  The facts come from one read
    transaction over library.sqlite3 (see core.follow_facts_v2 for what V2 does
    and does not record).

    The result names its provider, so a reader can tell a legacy answer from a
    V2 answer without guessing which database was opened.
    """
    try:
        connection = read_only_connection(database)
    except V2FactsError as error:
        raise ReportError(error.code, error.detail) from None
    # A report about a file another process may be rewriting has to know whether
    # the bytes stayed put while it read them.  A quiescent store is opened
    # immutable (no lock, no index file), so the only defence left is to compare
    # the file before and after: a main file that changed under the read means
    # the numbers describe no single moment.
    before = _file_stamp(database)
    try:
        connection.execute("BEGIN")
        facts = coverage_facts(connection, job_id)
        connection.execute("COMMIT")
    except V2FactsError as error:
        raise ReportError(error.code, error.detail) from None
    except ReportError:
        raise
    finally:
        connection.close()
    if _file_stamp(database) != before:
        raise ReportError(
            "database_changed_during_read",
            "%s 在读取期间被改写；这次摘要不属于任何单一时刻" % database,
        )
    scope = {"kind": "follow_job", "job_id": str(job_id), "provider": "v2"}
    try:
        coverage = build_coverage(facts, scope=scope)
    except CoverageError as error:
        raise ReportError("facts_rejected", str(error)) from None
    payload = coverage.as_dict()
    expected = (facts.get("pages") or {}).get("expected") or []
    notes = {
        "provider": "v2",
        "page_evidence": "present" if expected else "missing",
        "expected_content_version": "missing",
        "note": (
            "V2 保存了应有页集合与落盘摘要，但没有保存作品页的期望内容代次；"
            "因此交付维度只能到 unknown，不能声称本轮页面已齐"
        ),
    }
    if facts.get("page_gaps"):
        notes["pages_expected_missing"] = len(facts["page_gaps"])
    return {
        "question": "follow_coverage",
        "provider": "v2",
        "scope": payload["scope"],
        "coverage": payload,
        "facts_notes": notes,
        "exit_code": coverage_exit_code(payload["overall"]),
    }


_COVERAGE_WORDS = {
    EXIT_OK: "结论成立：本轮范围已查完、保存已齐",
    EXIT_ATTENTION: "结论是部分成立：有维度明确没完成",
    EXIT_INCOMPLETE: "结论不成立：有维度证明不了",
}


def coverage_lines(report: Mapping[str, Any]) -> list[str]:
    """The human text: the engine's own sentences, not a second set of words."""
    lines = list(render(report["coverage"]))
    provider = report["facts_notes"].get("provider", "legacy")
    source = "V2 任务库（只读）" if provider == "v2" else "旧运行状态库（只读，legacy）"
    lines.append(
        "数据来源: %s。页级证据: %s。"
        % (
            source,
            "有"
            if report["facts_notes"]["page_evidence"] == "present"
            else "无——保存是否齐全无法证明",
        )
    )
    lines.append("退出码: %d（%s）" % (report["exit_code"], _COVERAGE_WORDS[report["exit_code"]]))
    return lines


# --------------------------------------------------------------------------
# filter-explain
# --------------------------------------------------------------------------


def _read_conditions(path: Path) -> FilterSpec:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ReportError("conditions_unreadable", "%s (%s)" % (path, error)) from None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ReportError("conditions_not_json", "%s (%s)" % (path, error)) from None
    if not isinstance(data, Mapping):
        raise ReportError("conditions_invalid", "conditions must be a JSON object")
    unknown = [key for key in data if key not in CONDITION_KEYS]
    if unknown:
        raise ReportError("conditions_invalid", "unknown keys: %s" % ", ".join(sorted(unknown)))
    try:
        return FilterSpec(**dict(data))
    except (ContractError, TypeError) as error:
        raise ReportError("conditions_invalid", str(error)) from None


def _read_candidates(path: Path) -> list[dict[str, Any]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ReportError("candidates_unreadable", "%s (%s)" % (path, error)) from None
    candidates: list[dict[str, Any]] = []
    for number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as error:
            raise ReportError("candidate_not_json", "line %d: %s" % (number, error)) from None
        if not isinstance(item, Mapping):
            raise ReportError("candidate_invalid", "line %d: not a JSON object" % number)
        if item.get("illust_id") in (None, ""):
            raise ReportError("candidate_invalid", "line %d: no illust_id" % number)
        candidates.append(dict(item))
    if not candidates:
        # An empty file is far more likely a mistake than a batch with nothing
        # to explain, and "zero candidates, zero unknowns" would report success.
        raise ReportError("candidates_empty", str(path))
    return candidates


def filter_explain_report(candidates: Path, conditions: Path) -> dict[str, Any]:
    """Explain every candidate under one set of conditions, read-only."""
    spec = _read_conditions(conditions)
    rows = []
    for candidate in _read_candidates(candidates):
        try:
            verdict = explain(candidate, spec)
        except ValueError as error:
            raise ReportError(
                "candidate_refused", "%s (%s)" % (candidate.get("illust_id"), error)
            ) from None
        rows.append(
            {
                "illust_id": str(candidate.get("illust_id")),
                "verdict": verdict["verdict"],
                "reasons": [dict(item) for item in verdict["reasons"]],
            }
        )
    verdicts = [row["verdict"] for row in rows]
    decided = all(item in ("match", "exclude") for item in verdicts)
    return {
        "question": "filter_explain",
        "conditions": {"min_bookmarks": spec.min_bookmarks, "exclude_ai": spec.exclude_ai},
        "candidates": rows,
        "summary": {
            "total": len(rows),
            "match": verdicts.count("match"),
            "exclude": verdicts.count("exclude"),
            "unknown": verdicts.count("unknown"),
        },
        "exit_code": EXIT_OK if decided else EXIT_INCOMPLETE,
    }


def filter_explain_lines(report: Mapping[str, Any]) -> list[str]:
    """One line per candidate, then the counts, then the conclusion."""
    conditions = report["conditions"]
    lines = [
        "条件: 收藏数下限=%s  排除 AI=%s"
        % (
            "不限" if conditions["min_bookmarks"] is None else conditions["min_bookmarks"],
            "是" if conditions["exclude_ai"] else "否",
        )
    ]
    for row in report["candidates"]:
        reason_text = "、".join(
            "%s/%s" % (item["code"], item["field"]) for item in row["reasons"]
        ) or "无原因"
        lines.append("候选 %s: %s（%s）" % (row["illust_id"], row["verdict"], reason_text))
    summary = report["summary"]
    lines.append(
        "候选 %d 个: match %d、exclude %d、unknown %d"
        % (summary["total"], summary["match"], summary["exclude"], summary["unknown"])
    )
    if report["exit_code"] == EXIT_OK:
        lines.append("结论: 每个候选都有确定结论。")
    else:
        lines.append(
            "结论: 有 %d 个候选判不了（unknown）——判不了不等于通过。" % summary["unknown"]
        )
    return lines
