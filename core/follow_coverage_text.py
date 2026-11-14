"""Render a follow-history coverage summary without claiming what it cannot prove.

The numbers come from core.follow_coverage_counts, where a count nobody could
measure is None rather than 0.  This module treats the two very differently:

* None means the sentence that would need that number is not printed at all -
  never "0 页", because "we did not measure it" and "it is empty" are different
  facts and only one of them is allowed to become a digit;
* the conclusion names only the dimensions that really are missing.  A blanket
  "也没有保存齐" over a run whose delivery is complete is as false as claiming
  success, even when the intent is to stay conservative;
* the words 已查完 / 已穷尽 / 全部完成 are reserved for the case where all three
  dimensions hold and the counts add up.

A missing count key is still an error: the ten counts are the frozen contract,
so A3 always provides all of them - it may just provide None for some.
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
        # The run carried no page-level evidence, so nothing was proved either
        # way; that is not the same statement as "nothing was saved".
        "unknown": "保存与核验未得证明",
    },
}

STOP_REASONS = {
    "budget": "本批请求预算用完",
    "rate_limited": "来源限流",
    "cancel": "用户取消",
    "error": "错误中止",
}

FORBIDDEN_WHEN_INCOMPLETE = ("已查完", "已穷尽", "全部完成")
NO_SOURCE_SENTENCE = "本轮的作品数、保存页数、排除数、未知字段数都没有来源，无法给出。"
UNKNOWN_ARTISTS_SENTENCE = "本轮检查了多少位画师未知（本轮没有可用的画师计数）。"


def _state(coverage: Mapping, name: str) -> tuple[str, Any]:
    entry = coverage[name]
    if not isinstance(entry, Mapping):
        raise ValueError("%s must be a mapping with state/reason" % name)
    state = entry.get("state")
    if state not in STATES[name]:
        raise ValueError("%s.state is not a known state: %r" % (name, state))
    return str(state), entry.get("reason")


def _count(counts: Mapping, name: str) -> int | None:
    """The recorded count, or None when it was never measured.

    A missing key is an error (the ten counts are frozen), but a key holding
    None is a fact: nobody measured it.  bool is an int subclass, so the type
    check has to be exact or True would pass as 1.
    """
    if name not in counts:
        raise ValueError("counts.%s is missing" % name)
    value = counts[name]
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError("counts.%s must be a non-negative integer or None" % name)
    return value


def _denominator(denominators: Mapping, name: str) -> int | None:
    value = denominators.get(name)
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError("denominators.%s must be null or a non-negative integer" % name)
    return value


def _artists_sentence(confirmed, pending, failed_pages, unverified) -> str | None:
    """One sentence for the four artist buckets, mentioning only measured ones."""
    head = None if confirmed is None else "确认当前范围处理完 %d 位" % confirmed
    rest = [
        text
        for text in (
            None if pending is None else "%d 位待继续" % pending,
            None if failed_pages is None else "%d 位有失败页" % failed_pages,
            None if unverified is None else "%d 位覆盖待核对" % unverified,
        )
        if text is not None
    ]
    if head and rest:
        return head + "；" + "，".join(rest) + "。"
    if head:
        return head + "。"
    if rest:
        return "，".join(rest) + "。"
    return None


def _numbers_sentence(artworks, saved, already, excluded, unknown, duplicates) -> tuple[str, bool]:
    """(sentence, has_a_number); an unmeasured number never becomes a digit.

    The duplicate count is only mentioned when it is a measured, positive
    number: "0 duplicates" and "nobody counted duplicates" are different facts,
    and the second one must not turn into the first.
    """
    groups: list[str] = []
    new_and_saved: list[str] = []
    if artworks is not None:
        new_and_saved.append("新增 %d 个作品" % artworks)
    if saved is not None:
        new_and_saved.append("保存 %d 页" % saved)
    if new_and_saved:
        groups.append("、".join(new_and_saved))
    if already is not None:
        groups.append("已有 %d 页通过完整核验" % already)
    excluded_and_unknown: list[str] = []
    if excluded is not None:
        excluded_and_unknown.append("筛选排除 %d 件" % excluded)
    if unknown is not None:
        excluded_and_unknown.append("字段未知 %d 件" % unknown)
    if excluded_and_unknown:
        groups.append("，".join(excluded_and_unknown))
    if not groups:
        return NO_SOURCE_SENTENCE, False
    if duplicates:
        # Right after the artwork/page numbers it belongs to; "其中" has to have
        # something to refer to, which is why an empty sentence ignores it.
        groups.insert(1, "其中重复分页 %d 件（未重复保存）" % duplicates)
    return "；".join(groups) + "。", True


def _blockers(enumeration: str, decision: str, delivery: str, consistent: bool) -> list[str]:
    """Exactly the dimensions that did not hold, in display order."""
    blockers: list[str] = []
    if enumeration != "complete":
        blockers.append("来源遍历未完成")
    if decision != "applied":
        blockers.append("筛选判定未完成或未应用")
    if delivery == "unknown":
        blockers.append("保存与核验未得证明")
    elif delivery != "complete":
        blockers.append("保存未齐")
    if not consistent:
        blockers.append("计数不自洽")
    return blockers


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
    consistent = bool(coverage.get("consistent", True))
    blockers = _blockers(enumeration, decision, delivery, consistent)

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
    # duplicate_pages is the eleventh count; an older shape without it simply has
    # nothing to say about duplicates, which is not an error.
    duplicates = counts.get("duplicate_pages")
    if duplicates is not None and (type(duplicates) is not int or duplicates < 0):
        raise ValueError("counts.duplicate_pages must be a non-negative integer or None")

    lines = [
        "本轮已检查 %d 位画师。" % checked if checked is not None else UNKNOWN_ARTISTS_SENTENCE
    ]
    artists_line = _artists_sentence(confirmed, pending, failed_pages, unverified)
    if artists_line:
        lines.append(artists_line)
    numbers, has_number = _numbers_sentence(
        artworks, saved, already, excluded, unknown, duplicates
    )
    if has_number and coverage.get("sample") is True:
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

    if not consistent:
        reasons = coverage.get("inconsistent_reasons") or []
        detail = "；".join(str(item) for item in reasons) if reasons else "计数之间对不上"
        lines.append("注意: 计数不自洽（%s），本页数字不能当作覆盖结论。" % detail)

    if not blockers:
        lines.append("结论: 本轮范围已查完，保存已齐。")
    else:
        # The negation must not repeat the forbidden words themselves: a reader
        # grepping for 已穷尽 should only ever find a run that really finished.
        lines.append(
            "结论: 以下未成立——%s；这些数字不能当作覆盖结论。" % "、".join(blockers)
        )
    return lines


def render_text(coverage: Mapping) -> str:
    """One string, for callers that do not want to join lines themselves."""
    return "\n".join(render(coverage))
