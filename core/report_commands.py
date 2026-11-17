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
from core.follow_facts_v2 import (
    READ_ONLY_IMMUTABLE,
    V2FactsError,
    coverage_facts,
    read_only_connection,
)
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


def _read_note(read: Mapping[str, Any]) -> str:
    """One sentence about what this read promised and how it kept it.

    The coverage command answers about a file it did not write, so the report
    has to say which of the two promises it made: a direct locking read of a
    live database (where SQLite updates its own -shm index) or a read of a
    private copy (where the source directory is not touched at all).
    """
    plan = read.get("plan") or {}
    mode = str(plan.get("mode") or "")
    if mode == "copy":
        members = ", ".join(sorted((plan.get("copy") or {}).get("members") or {}))
        return (
            "读取方式: 私有副本（把 %s 复制到临时目录后读取；源目录前后名字与属性未变，"
            "副本在关闭时删除）" % (members or "主文件")
        )
    if mode == "ro+immutable":
        return "读取方式: 调用方声明的冻结副本（immutable，不参与锁；读取前后主文件属性一致）"
    if plan.get("promise") == "names_only":
        return "读取方式: 直接只读（加入 SQLite 锁协议；源库名字未变，-shm 锁索引由 SQLite 更新）"
    return "读取方式: 直接只读（rollback 库；未新建或删除任何文件）"


def coverage_report_v2(
    database: Path, job_id: str, *, immutable_source: str = ""
) -> dict[str, Any]:
    """Build the coverage summary for one V2 job, read-only.

    This is the summary the current downloader feeds: the legacy crawl store has
    no rows for a V2 job, which is why the desktop used to answer
    "database_missing" for work it had just done.  The facts come from one read
    transaction over library.sqlite3 (see core.follow_facts_v2 for what V2 does
    and does not record).

    The result names its provider, so a reader can tell a legacy answer from a
    V2 answer without guessing which database was opened, and it carries the
    read plan: which mode was used, what it promised about the source file
    family, and what was measured afterwards (RA-02).  The two promises are not
    the same thing: a live WAL database is read through the locking protocol and
    SQLite updates its own -shm index, while a quiescent WAL database is read
    from a private copy so the library directory gains no file at all.

    immutable_source is only for a snapshot the *caller* owns and has frozen;
    it is the one mode that skips locking, so it may never be inferred here.
    """
    try:
        connection = read_only_connection(database, immutable_source=immutable_source)
    except V2FactsError as error:
        raise ReportError(error.code, error.detail) from None
    plan = connection.plan
    stamp_before = _file_stamp(database) if plan.mode == READ_ONLY_IMMUTABLE else None
    read: dict[str, Any] = {}
    try:
        connection.execute("BEGIN")
        facts = coverage_facts(connection, job_id)
        connection.execute("COMMIT")
        # Measured while the connection is still open: a live database may
        # checkpoint when the last connection closes, and that is not this
        # run's side effect.
        read = connection.read_evidence()
    except V2FactsError as error:
        raise ReportError(error.code, error.detail) from None
    except ReportError:
        raise
    finally:
        connection.close()
    source = read.get("source") or {}
    appeared = list(source.get("created") or []) + list(source.get("removed") or [])
    if appeared:
        # The plan promised the source file family keeps its names.  A name that
        # appeared or vanished while we read means the promise did not hold,
        # whoever caused it; a summary built over that window is not a claim
        # about one directory.
        raise ReportError(
            "source_side_effects_observed",
            "读取期间源文件族的名字发生变化: %s" % ", ".join(sorted(appeared)),
        )
    if plan.mode == READ_ONLY_IMMUTABLE and _file_stamp(database) != stamp_before:
        # Only an immutable read needs this: it holds no lock, so the main file
        # moving means the numbers describe no single moment.  A locking read
        # (mode=ro) already sees one consistent snapshot and a concurrent commit
        # there is not a reason to refuse the report.
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
    # Whether a required version exists is read from the facts, not assumed: when
    # V2 starts recording one, this line must stop saying "missing" by itself.
    versions = [item.get("content_sha256") for item in expected]
    notes = {
        "provider": "v2",
        "page_evidence": "present" if expected else "missing",
        "expected_content_version": (
            "present" if any(versions) else "missing"
        ),
        "read_mode": plan.mode,
        "read_promise": plan.promise,
        "read_note": _read_note(read),
        "note": (
            "V2 保存了应有页集合与落盘摘要，并记录了作品页的期望内容代次；"
            "交付维度按代次比对"
            if any(versions)
            else (
                "V2 保存了应有页集合与落盘摘要，但没有保存作品页的期望内容代次；"
                "因此交付维度只能到 unknown，不能声称本轮页面已齐"
            )
        ),
    }
    if facts.get("page_gaps"):
        notes["pages_expected_missing"] = len(facts["page_gaps"])
    if facts.get("works_without_expected_pages"):
        # "可交付作品数" counts every work minus the filtered ones; the expected
        # page set only covers works whose decision was download.  Saying both
        # numbers without this note invites dividing one by the other.
        notes["works_without_expected_pages"] = facts["works_without_expected_pages"]
        notes["unit_note"] = (
            "可交付作品数按 works_seen 减被过滤计算，与实际应有页数（只统计 decision=download 的作品）"
            "口径不同，两者不能相除"
        )
    if facts.get("unmapped_delivery_states"):
        notes["unmapped_delivery_states"] = list(facts["unmapped_delivery_states"])
    # RA-03: the denominators and the page links are relations, not counters that
    # can be guessed from the batch rows.  A reader has to be able to tell "the
    # snapshot says 40 artists and this task reached 12" from "nobody recorded how
    # many there are", so both travel as words and as numbers.
    snapshot = facts.get("snapshot") or {}
    totals = (facts.get("run") or {}).get("totals") or {}
    visited = totals.get("artists_visited")
    members = snapshot.get("members")
    if members is not None:
        notes["artist_cohort"] = (
            "画师总体: 快照成员 %d 位（活跃 %s），本任务到达 %s 位"
            % (
                int(members),
                "未知" if snapshot.get("members_active") is None
                else int(snapshot["members_active"]),
                "未知" if visited is None else int(visited),
            )
        )
        notes["snapshot_scope"] = str(snapshot.get("scope") or "")
        notes["snapshot_members"] = int(members)
        notes["snapshot_members_active"] = snapshot.get("members_active")
        notes["artists_visited"] = visited
    elif totals.get("artists_total") is None:
        reason = str(totals.get("unknown_artists_reason") or "unknown")
        notes["artist_cohort"] = (
            "画师总体: 未知（%s）；本任务到达 %s 位"
            % (reason, "未知" if visited is None else int(visited))
        )
        notes["artists_total_unknown"] = reason
        notes["artists_visited"] = visited
    links = (facts.get("pages") or {}).get("links") or {}
    if links:
        notes["page_links"] = {
            "linked_pages": links.get("linked_pages"),
            "artifact_only": links.get("artifact_only"),
            "from_another_job": links.get("from_another_job"),
            "unlinked_deliveries": links.get("unlinked_deliveries"),
        }
        if links.get("from_another_job"):
            notes["cross_job_note"] = (
                "%d 页来自其他任务的已存文件（artifact 链接）：计入已有页，"
                "但不代表本任务重新保存过" % int(links["from_another_job"])
            )
    for name in ("works_multi_batch", "works_conflict", "works_superseded"):
        if facts.get(name):
            notes[name] = int(facts[name])
    return {
        "question": "follow_coverage",
        "provider": "v2",
        "scope": payload["scope"],
        "coverage": payload,
        "facts_notes": notes,
        "read": read,
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
    read = report.get("read") or {}
    if read:
        lines.append(_read_note(read))
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
