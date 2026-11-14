"""Re-evaluate existing candidates under new filter conditions, touching no file.

Why this exists
---------------
Widening a filter must *re-admit* artwork that an earlier, stricter run had
excluded -- without downloading anything again.  Narrowing must mark artwork as
newly excluded without deleting a single byte.  Both questions are answerable
offline, from the metadata already on disk, as long as the answer is honest
about the candidates nobody can judge.

The rules, in one place
-----------------------
* Five buckets, exhaustive and mutually exclusive, keyed by `illust_id`:
  `kept` (matched before and now), `newly_excluded` (matched, now excluded),
  `newly_included` (not matched before, matches now), `still_excluded` (not
  matched before and still not) and `uncovered` (still unknown **now**).
* **The new verdict decides.**  The old verdict only refines the bucket.  An
  artwork whose metadata was unknown under the old conditions and that matches
  under the new ones is the textbook "widen and re-admit" case: it belongs in
  `newly_included`, not in `uncovered`.  Filing it under `uncovered` would make
  the widening silently fail for exactly the candidates it was meant to rescue.
* `uncovered` therefore means one thing only: this candidate cannot be judged
  **under the new conditions**.  Its keys are exactly the candidates whose new
  verdict is "unknown", and their reasons (both sides, de-duplicated) go to
  `uncovered_reasons`.
* Widening and narrowing are the same walk in opposite directions; there is no
  branch that removes, moves or rewrites anything.
* A duplicated `illust_id` is refused instead of silently collapsed: the
  invariant `sum(len(bucket)) == len(candidates)` is the point of this module,
  and a duplicate makes that sentence ambiguous.

The decision itself is not re-implemented here: `explain` is injected (or
imported lazily from `core.filter_explain`), so this module only classifies.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Iterable, Mapping

#: The verdict vocabulary the explainer may answer with.
VERDICTS = ("match", "exclude", "unknown")

#: The five buckets, in display order.
BUCKETS = ("kept", "newly_excluded", "newly_included", "still_excluded", "uncovered")

#: (new verdict, old verdict) -> bucket.  The new side leads: a candidate that
#: matches now is admitted whatever the old side could not decide, and one that
#: still fits the filter stays excluded for the same reason.
TRANSITIONS = {
    ("match", "match"): "kept",
    ("match", "exclude"): "newly_included",
    ("match", "unknown"): "newly_included",
    ("exclude", "match"): "newly_excluded",
    ("exclude", "exclude"): "still_excluded",
    ("exclude", "unknown"): "still_excluded",
}

Explain = Callable[[Any, Any], Mapping[str, Any]]


def _default_explain() -> Explain:
    """Return the real explainer, imported *inside* the call.

    The explainer lives in a module that may not exist yet (it is developed in
    parallel), so importing it at this module's top level would make every
    caller -- including tests that inject their own -- depend on it.
    """
    from core.filter_explain import explain as filter_explain

    return filter_explain


def _illust_id(candidate: object) -> str:
    """The key this module buckets by; a candidate without one is a caller bug."""
    value = (
        candidate.get("illust_id")
        if isinstance(candidate, Mapping)
        else getattr(candidate, "illust_id", None)
    )
    if not isinstance(value, str) or not value.strip():
        raise ValueError("candidate must carry a non-empty illust_id: %r" % (value,))
    return value


def _reasons(value: object, *, side: str) -> list[dict[str, Any]]:
    """Validate the reason list we forward into `uncovered_reasons`."""
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError("%s explain() returned non-list reasons: %r" % (side, value))
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("%s explain() returned a non-mapping reason: %r" % (side, item))
        result.append(dict(item))
    return result


def _judge(explain: Explain, candidate: object, conditions: object, *, side: str):
    """One explainer call, with the shape rules enforced here rather than later."""
    result = explain(candidate, conditions)
    if not isinstance(result, Mapping):
        raise ValueError("%s explain() must return a mapping, got %r" % (side, type(result)))
    verdict = result.get("verdict")
    if verdict not in VERDICTS:
        raise ValueError("%s explain() returned an unknown verdict: %r" % (side, verdict))
    return str(verdict), _reasons(result.get("reasons"), side=side)


def _reason_key(reason: Mapping[str, Any]) -> str:
    """A stable key so the same reason from both sides is not listed twice."""
    return json.dumps(reason, sort_keys=True, ensure_ascii=False, default=str)


def reevaluate(
    candidates: Iterable[object],
    old_conditions: object,
    new_conditions: object,
    *,
    explain: Explain | None = None,
) -> dict[str, Any]:
    """Bucket every candidate by (old verdict, new verdict).

    `candidates` are `ArtworkMetadata` objects or same-shaped mappings; they are
    only read.  `old_conditions` / `new_conditions` are `FilterSpec` objects or
    same-shaped mappings and are handed to the explainer untouched.

    Returns the five buckets plus `uncovered_reasons` (one entry per uncovered
    `illust_id`, holding the de-duplicated reasons from both sides).  Every
    bucket is sorted ascending by `illust_id`; a candidate appears in exactly
    one bucket, so the five lengths add up to the number of candidates.
    """
    judge = _default_explain() if explain is None else explain
    if not callable(judge):
        raise ValueError("explain must be callable")

    buckets: dict[str, list[str]] = {name: [] for name in BUCKETS}
    uncovered_reasons: dict[str, list[dict[str, Any]]] = {}
    seen: dict[str, int] = {}
    total = 0

    for candidate in candidates:
        total += 1
        illust_id = _illust_id(candidate)
        if illust_id in seen:
            raise ValueError(
                "duplicate illust_id %r at position %d (first at %d)"
                % (illust_id, total - 1, seen[illust_id])
            )
        seen[illust_id] = total - 1

        old_verdict, old_reasons = _judge(judge, candidate, old_conditions, side="old")
        new_verdict, new_reasons = _judge(judge, candidate, new_conditions, side="new")

        if new_verdict == "unknown":
            # Only an unresolved verdict *now* is "uncovered": the candidate
            # still cannot be judged, whatever the old side managed to say.
            buckets["uncovered"].append(illust_id)
            merged: list[dict[str, Any]] = []
            keys: set[str] = set()
            for reason in old_reasons + new_reasons:
                key = _reason_key(reason)
                if key not in keys:
                    keys.add(key)
                    merged.append(reason)
            uncovered_reasons[illust_id] = merged
            continue

        bucket = TRANSITIONS[(new_verdict, old_verdict)]
        buckets[bucket].append(illust_id)

    for name in BUCKETS:
        buckets[name].sort()

    result: dict[str, Any] = {name: buckets[name] for name in BUCKETS}
    result["uncovered_reasons"] = {
        key: uncovered_reasons[key] for key in sorted(uncovered_reasons)
    }
    return result
