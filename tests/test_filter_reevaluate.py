"""Offline re-evaluation: five buckets, no deletions, unknown never green."""
from __future__ import annotations

import ast
import types
from dataclasses import dataclass

import pytest

from core import filter_reevaluate as reev
from core.filter_reevaluate import BUCKETS, reevaluate

OLD = {"tag": "old"}
NEW = {"tag": "new"}


@dataclass(frozen=True)
class Artwork:
    illust_id: str
    bookmarks: int | None = None


def scripted(table, *, reasons=None, calls=None):
    """A fake explainer driven by {illust_id: (old_verdict, new_verdict)}."""

    def explain(candidate, conditions):
        side = conditions["tag"]
        illust_id = (
            candidate["illust_id"]
            if isinstance(candidate, dict)
            else candidate.illust_id
        )
        if calls is not None:
            calls.append((illust_id, side))
        old_verdict, new_verdict = table[illust_id]
        verdict = old_verdict if side == "old" else new_verdict
        if verdict is _BOOM:
            raise RuntimeError("explainer blew up")
        return {
            "verdict": verdict,
            "reasons": (reasons or {}).get((illust_id, side), []),
        }

    return explain


class _Boom:
    pass


_BOOM = _Boom()


def run(table, ids, **kwargs):
    return reevaluate([Artwork(i) for i in ids], OLD, NEW, explain=scripted(table, **kwargs))


# --- one bucket at a time -------------------------------------------------

def test_a_candidate_that_matched_before_and_now_is_kept():
    assert run({"a": ("match", "match")}, ["a"])["kept"] == ["a"]


def test_narrowing_moves_a_kept_candidate_to_newly_excluded():
    result = run({"a": ("match", "exclude")}, ["a"])
    assert result["newly_excluded"] == ["a"]
    assert result["kept"] == []


def test_widening_re_admits_a_previously_excluded_candidate():
    result = run({"a": ("exclude", "match")}, ["a"])
    assert result["newly_included"] == ["a"]


def test_a_candidate_excluded_on_both_sides_lands_in_still_excluded():
    result = run({"a": ("exclude", "exclude")}, ["a"])
    assert result["still_excluded"] == ["a"]


def test_an_old_unknown_that_matches_now_is_re_admitted():
    """Widening exists to rescue exactly this candidate, not to park it."""
    result = run({"a": ("unknown", "match")}, ["a"])
    assert result["newly_included"] == ["a"]
    assert result["uncovered"] == []
    assert result["uncovered_reasons"] == {}


def test_unknown_on_the_new_side_is_uncovered_not_kept():
    result = run({"a": ("match", "unknown")}, ["a"])
    assert result["uncovered"] == ["a"]
    assert result["kept"] == []


def test_a_new_unknown_outranks_the_old_exclusion():
    result = run({"a": ("exclude", "unknown")}, ["a"])
    assert result["uncovered"] == ["a"]
    assert result["still_excluded"] == []


def test_an_old_unknown_that_is_excluded_now_stays_excluded():
    result = run({"a": ("unknown", "exclude")}, ["a"])
    assert result["still_excluded"] == ["a"]
    assert result["uncovered"] == []


def test_widening_re_admits_every_candidate_the_tight_filter_had_hidden():
    """The nine-fixture shape: tight (min_bookmarks+exclude_ai) -> no filter.

    1001 bookmarks below, 1002 ai excluded, 1005 ai flag missing, 1006 bookmarks
    missing, 1009 merely below the tight threshold: all five are admitted now.
    """
    table = {
        "1001": ("exclude", "match"),
        "1002": ("exclude", "match"),
        "1003": ("exclude", "exclude"),
        "1004": ("unknown", "unknown"),
        "1005": ("unknown", "match"),
        "1006": ("unknown", "match"),
        "1007": ("exclude", "exclude"),
        "1008": ("match", "match"),
        "1009": ("exclude", "match"),
    }
    result = run(table, list(table))
    assert result["newly_included"] == ["1001", "1002", "1005", "1006", "1009"]
    assert result["kept"] == ["1008"]
    assert result["still_excluded"] == ["1003", "1007"]
    assert result["uncovered"] == ["1004"]
    assert result["newly_excluded"] == []
    assert list(result["uncovered_reasons"]) == ["1004"]


def test_a_candidate_still_unknown_under_the_new_conditions_stays_uncovered():
    """kind_unknown stays unanswerable; widening must not invent an answer."""
    result = run({"a": ("unknown", "unknown")}, ["a"])
    assert result["uncovered"] == ["a"]
    assert result["newly_included"] == []


# --- exhaustiveness -------------------------------------------------------

@pytest.mark.parametrize(
    "table",
    [
        {"a": ("match", "match"), "b": ("match", "exclude")},
        {"a": ("exclude", "match"), "b": ("exclude", "exclude")},
        {"a": ("unknown", "match"), "b": ("match", "match"), "c": ("exclude", "exclude")},
        {"a": ("match", "unknown"), "b": ("exclude", "match"), "c": ("match", "match")},
        {"a": ("exclude", "exclude"), "b": ("exclude", "exclude")},
    ],
)
def test_the_five_buckets_are_exhaustive_and_disjoint(table):
    ids = list(table)
    result = run(table, ids)
    listed = [item for name in BUCKETS for item in result[name]]
    assert len(listed) == len(ids)
    assert sorted(listed) == sorted(ids)
    assert len(set(listed)) == len(listed)


def test_an_empty_candidate_list_returns_empty_buckets():
    result = reevaluate([], OLD, NEW, explain=scripted({}))
    assert {name: result[name] for name in BUCKETS} == {name: [] for name in BUCKETS}
    assert result["uncovered_reasons"] == {}


# --- input discipline -----------------------------------------------------

def test_an_identical_repeat_is_deduped_and_counted():
    explain = scripted({"a": ("match", "match")})
    result = reevaluate([Artwork("a"), Artwork("a")], OLD, NEW, explain=explain)
    assert result["kept"] == ["a"]
    assert result["duplicates"] == {"a": 2}


def test_three_identical_repeats_count_three_occurrences():
    explain = scripted({"a": ("match", "match")})
    result = reevaluate([Artwork("a"), Artwork("a"), Artwork("a")], OLD, NEW, explain=explain)
    assert result["duplicates"] == {"a": 3}
    assert result["kept"] == ["a"]


def test_repeats_that_disagree_are_refused_and_name_the_field():
    explain = scripted({"a": ("match", "match")})
    with pytest.raises(ValueError, match="different content") as caught:
        reevaluate(
            [Artwork("a", bookmarks=5), Artwork("a", bookmarks=500)], OLD, NEW, explain=explain
        )
    message = str(caught.value)
    assert "bookmarks" in message
    assert "positions 1 and 2" in message


def test_a_conflict_outside_bookmarks_is_refused_too():
    """All fields are compared, not just the one the filter happens to read."""
    explain = scripted({"a": ("match", "match")})
    first = {"illust_id": "a", "title": "snow", "bookmarks": 5}
    second = {"illust_id": "a", "title": "rain", "bookmarks": 5}
    with pytest.raises(ValueError, match="title"):
        reevaluate([first, second], OLD, NEW, explain=explain)


def test_a_list_without_repeats_reports_no_duplicates():
    explain = scripted({"a": ("match", "match"), "b": ("match", "match")})
    assert reevaluate([Artwork("a"), Artwork("b")], OLD, NEW, explain=explain)["duplicates"] == {}


def test_deduping_does_not_change_the_buckets():
    table = {"a": ("exclude", "match"), "b": ("match", "exclude")}
    plain = run(table, ["a", "b"])
    repeated = run(table, ["a", "b", "a", "b"])
    assert {name: plain[name] for name in BUCKETS} == {name: repeated[name] for name in BUCKETS}
    assert repeated["duplicates"] == {"a": 2, "b": 2}


def test_the_bucket_sum_counts_unique_candidates_not_raw_rows():
    table = {"a": ("match", "match"), "b": ("exclude", "exclude")}
    result = run(table, ["a", "b", "b"])
    assert sum(len(result[name]) for name in BUCKETS) == 2
    assert result["duplicates"] == {"b": 2}


@pytest.mark.parametrize("bad", [None, "", "   ", 123])
def test_a_candidate_without_a_usable_illust_id_is_refused(bad):
    with pytest.raises(ValueError, match="illust_id"):
        reevaluate([Artwork(bad)], OLD, NEW, explain=scripted({}))


def test_a_mapping_candidate_is_accepted_too():
    explain = scripted({"a": ("match", "match")})
    result = reevaluate([{"illust_id": "a", "bookmarks": 10}], OLD, NEW, explain=explain)
    assert result["kept"] == ["a"]


def test_the_conditions_are_handed_over_untouched():
    seen = []

    def explain(candidate, conditions):
        seen.append(conditions)
        return {"verdict": "match", "reasons": []}

    old, new = {"min_bookmarks": 10}, {"min_bookmarks": 5}
    reevaluate([Artwork("a")], old, new, explain=explain)
    assert seen[0] is old and seen[1] is new


# --- explainer contract ---------------------------------------------------

def test_the_explainer_is_called_exactly_twice_per_candidate():
    calls = []
    run({"a": ("match", "match"), "b": ("exclude", "exclude")}, ["a", "b"], calls=calls)
    assert len(calls) == 4
    assert sorted(calls) == [("a", "new"), ("a", "old"), ("b", "new"), ("b", "old")]


def test_an_unknown_verdict_word_is_refused():
    def explain(candidate, conditions):
        return {"verdict": "maybe", "reasons": []}

    with pytest.raises(ValueError, match="unknown verdict"):
        reevaluate([Artwork("a")], OLD, NEW, explain=explain)


def test_a_non_mapping_result_is_refused():
    def explain(candidate, conditions):
        return ["match"]

    with pytest.raises(ValueError, match="must return a mapping"):
        reevaluate([Artwork("a")], OLD, NEW, explain=explain)


def test_reasons_that_are_not_a_list_are_refused():
    def explain(candidate, conditions):
        return {"verdict": "match", "reasons": "because"}

    with pytest.raises(ValueError, match="non-list reasons"):
        reevaluate([Artwork("a")], OLD, NEW, explain=explain)


def test_a_reason_that_is_not_a_mapping_is_refused():
    def explain(candidate, conditions):
        return {"verdict": "match", "reasons": ["because"]}

    with pytest.raises(ValueError, match="non-mapping reason"):
        reevaluate([Artwork("a")], OLD, NEW, explain=explain)


def test_a_non_callable_explainer_is_refused():
    with pytest.raises(ValueError, match="callable"):
        reevaluate([Artwork("a")], OLD, NEW, explain="nope")


# --- ordering and reasons -------------------------------------------------

def test_every_bucket_is_sorted_even_when_the_input_is_not():
    table = {
        "c": ("match", "match"),
        "a": ("match", "match"),
        "b": ("match", "match"),
    }
    assert run(table, ["c", "a", "b"])["kept"] == ["a", "b", "c"]


def test_uncovered_reasons_merge_both_sides_and_drop_exact_duplicates():
    shared = {"code": "field_unknown", "field": "bookmarks", "evidence": None, "unknown": True}
    other = {"code": "field_unknown", "field": "ai_flag", "evidence": None, "unknown": True}
    reasons = {("a", "old"): [shared], ("a", "new"): [shared, other]}
    result = run({"a": ("unknown", "unknown")}, ["a"], reasons=reasons)
    assert result["uncovered_reasons"]["a"] == [shared, other]


def test_uncovered_reasons_only_names_candidates_the_new_run_could_not_judge():
    reasons = {("a", "new"): [{"code": "field_unknown"}], ("b", "old"): [{"code": "field_unknown"}]}
    result = run({"a": ("match", "unknown"), "b": ("match", "match")}, ["a", "b"], reasons=reasons)
    assert list(result["uncovered_reasons"]) == ["a"]


# --- the module itself ----------------------------------------------------

def _module_source() -> str:
    return (
        __import__("pathlib").Path(reev.__file__).read_text(encoding="utf-8")
    )


def test_the_module_cannot_delete_move_or_rewrite_anything():
    tree = ast.parse(_module_source())
    forbidden_calls = {"unlink", "remove", "rmtree", "move", "rename", "replace", "rmdir"}
    forbidden_imports = {"os", "shutil", "pathlib", "subprocess"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr not in forbidden_calls, "forbidden call: %s" % node.attr
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in forbidden_imports, alias.name
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in forbidden_imports, node.module
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"open", "eval", "exec"}, node.func.id


def test_the_explainer_is_imported_lazily_so_this_module_stands_alone(monkeypatch):
    tree = ast.parse(_module_source())
    top_level = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    for node in top_level:
        if isinstance(node, ast.ImportFrom):
            assert node.module != "core.filter_explain", "top-level import defeats the seam"
        else:
            assert all(alias.name != "core.filter_explain" for alias in node.names)
    nested = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "core.filter_explain"
    ]
    assert nested, "the lazy import must still exist somewhere inside a function"

    calls = []

    def fake_explain(candidate, conditions):
        calls.append((candidate.illust_id, conditions["tag"]))
        return {"verdict": "match", "reasons": []}

    # The fake explainer is only used through the lazy import path.

    monkeypatch.setitem(
        __import__("sys").modules,
        "core.filter_explain",
        types.SimpleNamespace(explain=fake_explain),
    )
    result = reevaluate([Artwork("a")], OLD, NEW)
    assert result["kept"] == ["a"]
    assert len(calls) == 2
