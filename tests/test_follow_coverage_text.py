"""The summary must not print a number it cannot prove, nor a word that ends a run."""
from __future__ import annotations

import pytest

from core.follow_coverage_text import FORBIDDEN_WHEN_INCOMPLETE, render, render_text


def coverage(**overrides):
    base = {
        "enumeration": {"state": "complete", "reason": None},
        "decision": {"state": "applied", "reason": None},
        "delivery": {"state": "complete", "reason": None},
        "counts": {
            "artists_checked": 30,
            "artists_confirmed": 22,
            "artists_pending": 5,
            "artists_failed_pages": 2,
            "artists_coverage_unverified": 1,
            "artworks_seen": 100,
            "pages_saved": 173,
            "pages_already_verified": 420,
            "excluded_by_filter": 81,
            "unknown_fields": 3,
        },
        "denominators": {"artists_total": 30, "artworks_total": None},
        "stop_reason": None,
        "consistent": True,
    }
    base.update(overrides)
    return base


def text(value):
    return "\n".join(render(value))


def test_a_complete_run_may_say_it_finished():
    printed = text(coverage())
    assert "本轮已检查 30 位画师。" in printed
    assert "确认当前范围处理完 22 位；5 位待继续，2 位有失败页，1 位覆盖待核对。" in printed
    assert "结论: 本轮范围已查完，保存已齐。" in printed


def test_a_partial_run_never_uses_the_words_that_end_it():
    printed = text(coverage(
        enumeration={"state": "partial", "reason": "artist_failed_pages"},
        delivery={"state": "partial", "reason": "pending_pages"},
    ))
    for word in FORBIDDEN_WHEN_INCOMPLETE:
        assert word not in printed, word
    assert "查找: 部分画师未完成（artist_failed_pages）" in printed
    assert "保存: 部分页面未保存或未核验（pending_pages）" in printed
    assert "这些数字不能当作覆盖结论" in printed


def test_a_budget_stop_says_why_and_never_claims_completion():
    printed = text(coverage(
        enumeration={"state": "stopped", "reason": "budget"},
        stop_reason="budget",
    ))
    for word in FORBIDDEN_WHEN_INCOMPLETE:
        assert word not in printed, word
    assert "停止原因: 本批请求预算用完（budget）" in printed
    assert "查找: 因预算或限流停止（budget）" in printed


def test_an_unproven_denominator_is_not_printed_at_all():
    printed = text(coverage(denominators={"artists_total": None, "artworks_total": None}))
    assert "范围内画师总数" not in printed
    assert "范围内作品总数" not in printed
    assert "共 " not in printed


def test_a_known_denominator_is_printed_with_its_source():
    printed = text(coverage(denominators={"artists_total": 30, "artworks_total": 100}))
    assert "范围内画师总数 30 位（有记录可对）。" in printed
    assert "范围内作品总数 100 件（有记录可对）。" in printed


def test_sample_numbers_are_marked_as_sample():
    printed = text(coverage(sample=True))
    assert "（示例）" in printed


def test_the_three_dimensions_are_three_separate_sentences():
    printed = text(coverage(
        decision={"state": "unknown_fields", "reason": "favorite_count_unknown"},
        delivery={"state": "partial", "reason": "pending_pages"},
    ))
    assert "判定: 有字段未知，未判定（favorite_count_unknown）" in printed
    assert "保存: 部分页面未保存或未核验（pending_pages）" in printed
    assert "查找: 来源遍历完成" in printed


def test_inconsistent_counts_are_called_out_and_block_the_conclusion():
    printed = text(coverage(consistent=False, inconsistent_reasons=["confirmed+pending != checked"]))
    assert "注意: 计数不自洽（confirmed+pending != checked）" in printed
    assert "已查完" not in printed


def test_a_missing_key_is_refused_instead_of_defaulted():
    broken = coverage()
    del broken["delivery"]
    with pytest.raises(ValueError):
        render(broken)


def test_an_unknown_state_is_refused():
    with pytest.raises(ValueError):
        render(coverage(enumeration={"state": "probably_fine", "reason": None}))


def test_a_negative_count_is_refused():
    counts = dict(coverage()["counts"])
    counts["pages_saved"] = -1
    with pytest.raises(ValueError):
        render(coverage(counts=counts))


def test_a_denominator_that_is_not_a_number_is_refused():
    with pytest.raises(ValueError):
        render(coverage(denominators={"artists_total": "thirty", "artworks_total": None}))


def test_render_text_joins_the_same_lines():
    value = coverage()
    assert render_text(value) == "\n".join(render(value))
