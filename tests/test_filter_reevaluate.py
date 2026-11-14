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


def test_unknown_on_the_old_side_is_uncovered_not_excluded():
    result = run({"a": ("unknown", "match")}, ["a"])
    assert result["uncovered"] == ["a"]
    assert result["newly_included"] == []


def test_unknown_on_the_new_side_is_uncovered_not_kept():
    result = run({"a": ("match", "unknown")}, ["a"])
    assert result["uncovered"] == ["a"]
    assert result["kept"] == []


def test_unknown_outranks_exclude_on_the_other_side():
    result = run({"a": ("exclude", "unknown")}, ["a"])
    assert result["uncovered"] == ["a"]
    assert result["still_excluded"] == []


def test_unknown_outranks_match_on_the_other_side():
    result = run({"a": ("unknown", "exclude")}, ["a"])
    assert result["uncovered"] == ["a"]
    assert result["newly_excluded"] == []


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

def test_a_duplicate_illust_id_is_refused():
    explain = scripted({"a": ("match", "match")})
    with pytest.raises(ValueError, match="duplicate illust_id"):
        reevaluate([Artwork("a"), Artwork("a")], OLD, NEW, explain=explain)


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


def test_uncovered_reasons_only_names_uncovered_candidates():
    reasons = {("a", "old"): [{"code": "field_unknown"}], ("b", "old"): [{"code": "field_unknown"}]}
    result = run({"a": ("unknown", "match"), "b": ("match", "match")}, ["a", "b"], reasons=reasons)
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
