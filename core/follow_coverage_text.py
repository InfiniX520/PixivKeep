"""Render a follow-history coverage summary without claiming what it cannot prove.

The numbers come from core.follow_coverage_counts; this module only chooses
words.  Two rules decide most of the text:

* a denominator the caller could not prove is None, and None means the sentence
  that needs it is not printed at all - never "共 N 件" with a guess;
* the words 已查完 / 已穷尽 are reserved for the one case where enumeration,
  decision and delivery are all complete and the counts add up.  Anywhere else
  they would turn a partial run into a finished one.
"""
from __future__ import annotations

from typing import Any, Mapping

REQUIRED = ("enumeration", "decision", "delivery", "counts", "denominators")

STATES = {
    "enumeration": {
        "complete": "来源遍历完成",
        "partial": "部分画师未完成",
        "stopped": "因预算或限流停止",
        "failed": "来源遍历失败",
    },
    "decision": {
        "applied": "筛选判定已应用",
        "unknown_fields": "有字段未知，未判定",
        "skipped": "筛选被跳过",
    },
    "delivery": {
        "complete": "保存与核验已齐",
        "partial": "部分页面未保存或未核验",
        "missing": "尚未保存",
    },
}

STOP_REASONS = {
    "budget": "本批请求预算用完",
    "rate_limited": "来源限流",
    "cancel": "用户取消",
    "error": "错误中止",
}

FORBIDDEN_WHEN_INCOMPLETE = ("已查完", "已穷尽", "全部完成")


def _state(coverage: Mapping, name: str) -> tuple[str, Any]:
    entry = coverage[name]
    if not isinstance(entry, Mapping):
        raise ValueError("%s must be a mapping with state/reason" % name)
    state = entry.get("state")
    if state not in STATES[name]:
        raise ValueError("%s.state is not a known state: %r" % (name, state))
    return str(state), entry.get("reason")


def _count(counts: Mapping, name: str) -> int:
    value = counts.get(name, 0)
    if type(value) is not int or value < 0:
        raise ValueError("counts.%s must be a non-negative integer" % name)
    return value


def _denominator(denominators: Mapping, name: str) -> int | None:
    value = denominators.get(name)
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError("denominators.%s must be null or a non-negative integer" % name)
    return value


def render(coverage: Mapping) -> list[str]:
    """The summary as lines, each sentence backed by a fact in the coverage."""
    if not isinstance(coverage, Mapping):
        raise ValueError("coverage must be a mapping")
    missing = [name for name in REQUIRED if name not in coverage]
    if missing:
        raise ValueError("coverage is missing %s" % ", ".join(missing))
    counts = coverage["counts"]
    denominators = coverage["denominators"]
    if not isinstance(counts, Mapping) or not isinstance(denominators, Mapping):
        raise ValueError("counts and denominators must be mappings")

    enumeration, enumeration_reason = _state(coverage, "enumeration")
    decision, decision_reason = _state(coverage, "decision")
    delivery, delivery_reason = _state(coverage, "delivery")
    complete = (
        enumeration == "complete"
        and decision == "applied"
        and delivery == "complete"
        and bool(coverage.get("consistent", True))
    )

    checked = _count(counts, "artists_checked")
    confirmed = _count(counts, "artists_confirmed")
    pending = _count(counts, "artists_pending")
    failed_pages = _count(counts, "artists_failed_pages")
    unverified = _count(counts, "artists_coverage_unverified")
    artworks = _count(counts, "artworks_seen")
    saved = _count(counts, "pages_saved")
    already = _count(counts, "pages_already_verified")
    excluded = _count(counts, "excluded_by_filter")
    unknown = _count(counts, "unknown_fields")

    lines = ["本轮已检查 %d 位画师。" % checked]
    lines.append(
        "确认当前范围处理完 %d 位；%d 位待继续，%d 位有失败页，%d 位覆盖待核对。"
        % (confirmed, pending, failed_pages, unverified)
    )

    numbers = "新增 %d 个作品、保存 %d 页；已有 %d 页通过完整核验；筛选排除 %d 件，字段未知 %d 件。" % (
        artworks,
        saved,
        already,
        excluded,
        unknown,
    )
    if coverage.get("sample") is True:
        numbers += "（示例）"
    lines.append(numbers)

    artists_total = _denominator(denominators, "artists_total")
    if artists_total is not None:
        lines.append("范围内画师总数 %d 位（有记录可对）。" % artists_total)
    artworks_total = _denominator(denominators, "artworks_total")
    if artworks_total is not None:
        lines.append("范围内作品总数 %d 件（有记录可对）。" % artworks_total)

    for label, name, state, reason in (
        ("查找", "enumeration", enumeration, enumeration_reason),
        ("判定", "decision", decision, decision_reason),
        ("保存", "delivery", delivery, delivery_reason),
    ):
        suffix = "（%s）" % reason if reason else ""
        lines.append("%s: %s%s" % (label, STATES[name][state], suffix))

    stop_reason = coverage.get("stop_reason")
    if stop_reason:
        lines.append(
            "停止原因: %s（%s）"
            % (STOP_REASONS.get(str(stop_reason), str(stop_reason)), stop_reason)
        )

    if not coverage.get("consistent", True):
        reasons = coverage.get("inconsistent_reasons") or []
        detail = "；".join(str(item) for item in reasons) if reasons else "计数之间对不上"
        lines.append("注意: 计数不自洽（%s），本页数字不能当作覆盖结论。" % detail)

    if complete:
        lines.append("结论: 本轮范围已查完，保存已齐。")
    else:
        # The negation must not repeat the forbidden words themselves: a reader
        # grepping for "已穷尽" should only ever find a run that really finished.
        lines.append("结论: 本轮没有查完、也没有保存齐；这些数字不能当作覆盖结论。")
    return lines


def render_text(coverage: Mapping) -> str:
    """One string, for callers that do not want to join lines themselves."""
    return "\n".join(render(coverage))
