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


def numbers_line(value):
    """The line that carries the run-level counts, whatever else changed.

    It is identified by how it starts rather than by a substring: the artist
    sentence also contains 页 ("有失败页"), which made a looser match useless.
    """
    prefixes = ("新增", "保存", "已有", "筛选排除", "字段未知", "本轮的作品数")
    for line in render(value):
        if ": " in line or line.startswith(("结论", "注意", "范围内", "本轮已检查", "本轮检查了")):
            continue
        if "都没有来源" in line or line.startswith(prefixes):
            return line
    raise AssertionError("no numbers line was printed")


def test_the_all_known_numbers_line_is_unchanged():
    assert numbers_line(coverage()) == (
        "新增 100 个作品、保存 173 页；已有 420 页通过完整核验；筛选排除 81 件，字段未知 3 件。"
    )


def test_five_unmeasured_counts_print_no_digits_at_all():
    counts = dict(coverage()["counts"])
    for name in ("artworks_seen", "pages_saved", "pages_already_verified", "excluded_by_filter", "unknown_fields"):
        counts[name] = None
    line = numbers_line(coverage(counts=counts))
    assert line == "本轮的作品数、保存页数、排除数、未知字段数都没有来源，无法给出。"
    assert not any(character.isdigit() for character in line)
    assert "0" not in line


def test_an_unmeasured_count_is_never_printed_as_zero():
    counts = dict(coverage()["counts"])
    for name in ("artworks_seen", "pages_already_verified", "excluded_by_filter", "unknown_fields"):
        counts[name] = None
    counts["pages_saved"] = 5
    line = numbers_line(coverage(counts=counts))
    assert "保存 5 页" in line
    assert "新增 0" not in line
    assert "排除 0" not in line
    assert "已有 0" not in line


def test_an_unknown_artist_count_changes_the_first_sentence():
    counts = dict(coverage()["counts"])
    counts["artists_checked"] = None
    printed = text(coverage(counts=counts))
    assert "本轮检查了多少位画师未知（本轮没有可用的画师计数）。" in printed
    assert "本轮已检查 30 位画师。" not in printed


def test_four_unknown_artist_buckets_drop_the_second_sentence():
    counts = dict(coverage()["counts"])
    for name in ("artists_confirmed", "artists_pending", "artists_failed_pages", "artists_coverage_unverified"):
        counts[name] = None
    printed = text(coverage(counts=counts))
    assert "确认当前范围处理完" not in printed
    assert "待继续" not in printed


def test_one_known_bucket_still_says_what_it_knows():
    counts = dict(coverage()["counts"])
    for name in ("artists_confirmed", "artists_failed_pages", "artists_coverage_unverified"):
        counts[name] = None
    printed = text(coverage(counts=counts))
    assert "5 位待继续。" in printed


def test_duplicate_pages_is_mentioned_only_when_it_is_a_real_number():
    counts = dict(coverage()["counts"])
    counts["duplicate_pages"] = 4
    assert "其中重复分页 4 件（未重复保存）" in numbers_line(coverage(counts=counts))
    counts["duplicate_pages"] = 0
    assert "重复分页" not in numbers_line(coverage(counts=counts))
    counts["duplicate_pages"] = None
    assert "重复分页" not in numbers_line(coverage(counts=counts))


def test_an_empty_numbers_sentence_ignores_duplicates_too():
    counts = dict(coverage()["counts"])
    for name in ("artworks_seen", "pages_saved", "pages_already_verified", "excluded_by_filter", "unknown_fields"):
        counts[name] = None
    counts["duplicate_pages"] = 3
    line = numbers_line(coverage(counts=counts))
    assert "重复分页" not in line
    assert not any(character.isdigit() for character in line)


def test_sample_marking_needs_a_number_to_belong_to():
    counts = dict(coverage()["counts"])
    for name in ("artworks_seen", "pages_saved", "pages_already_verified", "excluded_by_filter", "unknown_fields"):
        counts[name] = None
    assert "（示例）" not in numbers_line(coverage(counts=counts, sample=True))
    assert "（示例）" in numbers_line(coverage(sample=True))


def test_a_bool_is_not_a_count():
    counts = dict(coverage()["counts"])
    counts["pages_saved"] = True
    with pytest.raises(ValueError):
        render(coverage(counts=counts))


def test_a_missing_count_key_is_refused():
    counts = dict(coverage()["counts"])
    del counts["unknown_fields"]
    with pytest.raises(ValueError):
        render(coverage(counts=counts))


def test_no_none_scenario_ever_claims_the_run_finished():
    counts = dict(coverage()["counts"])
    for name in list(counts):
        counts[name] = None
    counts["duplicate_pages"] = 2
    printed = text(coverage(counts=counts, consistent=False))
    for word in FORBIDDEN_WHEN_INCOMPLETE:
        assert word not in printed, word
