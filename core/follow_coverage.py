"""One coverage summary for a follow-crawl run: what range, how far, what is missing.

Three dimensions stay separate on purpose.  "Did we look at everything",
"could the filter be applied" and "did the pages get saved" fail independently,
and collapsing them into one boolean is how a partial run ends up described as
a finished one.  This module only assembles what the three lower modules
already decided:

    core.follow_facts           the facts, copied read-only out of a run
    core.follow_coverage_state  enumeration / decision / delivery
    core.follow_coverage_counts the counters, the denominators, the identity check

Two rules are enforced here rather than left to the renderer:

* a counter with no source stays None and is printed as nothing at all - never
  as zero, because "we did not measure that" and "we measured zero" are
  different answers;
* overall=complete requires all three dimensions complete *and* counts that add
  up.  Anything else is partial (a dimension says it is unfinished) or unknown
  (no dimension is unfinished, but one cannot be proven either).

build_coverage never sets "sample": example numbers belong to docs and demos,
not to a report about a real run.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from core.follow_coverage_counts import coverage_counts
from core.follow_coverage_state import (
    DECISION_STATES,
    DELIVERY_STATES,
    ENUMERATION_STATES,
    coverage_states,
)
from core.follow_coverage_text import render, render_text

COUNT_KEYS = (
    "artists_checked",
    "artists_confirmed",
    "artists_pending",
    "artists_failed_pages",
    "artists_coverage_unverified",
    "artworks_seen",
    "pages_saved",
    "pages_already_verified",
    "excluded_by_filter",
    "unknown_fields",
    "duplicate_pages",
)
DENOMINATOR_KEYS = ("artists_total", "artworks_total")
DIMENSIONS = ("enumeration", "decision", "delivery")
PAGE_END_STATES = ("complete", "partial", "unknown")
OVERALL_STATES = ("complete", "partial", "unknown")

_STATE_TABLES = {
    "enumeration": ENUMERATION_STATES,
    "decision": DECISION_STATES,
    "delivery": DELIVERY_STATES,
}

_FACT_KEYS = ("run", "artists", "incomplete", "budget")
_SCOPE_TYPES = (str, int, bool, type(None))


class CoverageError(ValueError):
    """A caller bug in the shape of the facts, the scope or the counters.

    These are not coverage conclusions: bad input must never be rendered as
    "the run stopped" or "the run failed".
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__("%s: %s" % (code, message))
        self.code = code
        self.message = message


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _counter(value: object, name: str):
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise CoverageError("count_invalid", "%s must be null or a non-negative integer" % name)
    return int(value)


def _frozen(mapping: Mapping) -> Mapping:
    return MappingProxyType(dict(mapping))


def _facts(facts: object) -> Mapping:
    if not isinstance(facts, Mapping):
        raise CoverageError("facts_shape_invalid", "facts must be a mapping")
    missing = [name for name in _FACT_KEYS if name not in facts]
    if missing:
        raise CoverageError("facts_shape_invalid", "facts is missing %s" % ", ".join(missing))
    artists = facts["artists"]
    if not isinstance(artists, Sequence) or isinstance(artists, (str, bytes)):
        raise CoverageError("facts_shape_invalid", "facts['artists'] must be a list")
    for row in artists:
        if not isinstance(row, Mapping):
            raise CoverageError("facts_shape_invalid", "every artist row must be a mapping")
    return facts


def _scope(scope: object) -> Mapping:
    if not isinstance(scope, Mapping):
        raise CoverageError("scope_invalid", "scope must be a mapping")
    if not scope:
        raise CoverageError("scope_invalid", "scope must say what range this summary covers")
    clean = {}
    for key, value in scope.items():
        if not isinstance(key, str) or not key:
            raise CoverageError("scope_invalid", "scope keys must be non-empty strings")
        if not isinstance(value, _SCOPE_TYPES):
            raise CoverageError("scope_invalid", "scope[%r] must be a scalar" % key)
        clean[key] = value
    kind = clean.get("kind")
    if not isinstance(kind, str) or not kind:
        raise CoverageError("scope_invalid", "scope must carry a non-empty 'kind'")
    return _frozen(clean)


def _dimension(states: Mapping, name: str) -> Mapping:
    entry = states.get(name) if isinstance(states, Mapping) else None
    if not isinstance(entry, Mapping):
        raise CoverageError("dimension_invalid", "%s must be a mapping" % name)
    extra = [key for key in entry if key not in ("state", "reason")]
    if extra:
        raise CoverageError("dimension_invalid", "%s has unexpected keys: %s" % (name, ", ".join(extra)))
    state = entry.get("state")
    if not isinstance(state, str) or not state:
        raise CoverageError("dimension_invalid", "%s.state is missing" % name)
    if state not in _STATE_TABLES[name]:
        raise CoverageError(
            "dimension_invalid",
            "%s.state %r is not one of %s" % (name, state, _STATE_TABLES[name]),
        )
    return _frozen({"state": state, "reason": _text(entry.get("reason"))})


def _counters(report: object):
    if not isinstance(report, Mapping):
        raise CoverageError("counts_shape_invalid", "coverage_counts must return a mapping")
    missing = [key for key in ("counts", "denominators", "consistent") if key not in report]
    if missing:
        raise CoverageError("counts_shape_invalid", "counts report is missing %s" % ", ".join(missing))
    counts = report["counts"]
    if not isinstance(counts, Mapping):
        raise CoverageError("counts_shape_invalid", "counts must be a mapping")
    if set(counts) != set(COUNT_KEYS):
        raise CoverageError(
            "counts_shape_invalid",
            "counts must carry exactly the frozen keys (missing %s, unexpected %s)"
            % (sorted(set(COUNT_KEYS) - set(counts)) or "-", sorted(set(counts) - set(COUNT_KEYS)) or "-"),
        )
    clean_counts = {key: _counter(counts[key], key) for key in COUNT_KEYS}
    denominators = report["denominators"]
    if not isinstance(denominators, Mapping):
        raise CoverageError("counts_shape_invalid", "denominators must be a mapping")
    if set(denominators) != set(DENOMINATOR_KEYS):
        raise CoverageError("counts_shape_invalid", "denominators must carry exactly %s" % (DENOMINATOR_KEYS,))
    clean_dividers = {key: _counter(denominators[key], key) for key in DENOMINATOR_KEYS}
    consistent = report["consistent"]
    if not isinstance(consistent, bool):
        raise CoverageError("counts_shape_invalid", "consistent must be a bool")
    reasons = report.get("reasons") or ()
    if isinstance(reasons, str) or not isinstance(reasons, Sequence):
        raise CoverageError("counts_shape_invalid", "reasons must be a sequence of strings")
    return _frozen(clean_counts), _frozen(clean_dividers), consistent, tuple(str(item) for item in reasons)


def _page_end_evidence(facts: Mapping) -> Mapping:
    declared = facts.get("page_end_evidence")
    if declared is not None:
        if not isinstance(declared, Mapping):
            raise CoverageError("page_end_invalid", "facts['page_end_evidence'] must be a mapping")
        state = declared.get("state")
        if state not in PAGE_END_STATES:
            raise CoverageError(
                "page_end_invalid", "page_end_evidence.state must be one of %s" % (PAGE_END_STATES,)
            )
        return _frozen({"state": str(state), "reason": _text(declared.get("reason"))})
    artists = facts["artists"]
    if not artists:
        return _frozen({"state": "unknown", "reason": "no_artists"})
    swept = sum(1 for row in artists if _text(row.get("swept_at")))
    if swept == 0:
        return _frozen({"state": "unknown", "reason": "no_sweep_evidence"})
    if swept == len(artists):
        return _frozen({"state": "complete", "reason": "every_artist_swept"})
    return _frozen({"state": "partial", "reason": "some_artists_not_swept"})


def _overall(enumeration: str, decision: str, delivery: str, consistent: bool):
    if not consistent:
        return "unknown", "counts_inconsistent"
    if enumeration == "complete" and decision == "applied" and delivery == "complete":
        return "complete", ""
    unfinished = (
        enumeration in ("partial", "stopped", "failed")
        or delivery in ("partial", "missing")
        or decision == "unknown_fields"
    )
    if unfinished:
        return "partial", "a_dimension_is_not_complete"
    # Nothing is known to be unfinished, but one dimension is not proven either:
    # an unmeasured save side is not the same answer as a finished one.
    if delivery == "unknown":
        return "unknown", "delivery_not_measured"
    if decision == "skipped":
        return "unknown", "decision_not_applied"
    return "unknown", "a_dimension_is_unknown"


def _notes(facts: Mapping, counts: Mapping, denominators: Mapping, page_end: Mapping):
    notes = []
    unknown = [key for key in COUNT_KEYS if counts[key] is None]
    if unknown:
        notes.append("以下计数没有来源，按未知处理（不会打印成 0）: %s" % ", ".join(unknown))
    if denominators["artists_total"] is None:
        notes.append("范围内画师总数未知，没有可对的分母")
    if denominators["artworks_total"] is None:
        notes.append("范围内作品总数未知，没有可对的分母")
    artists = facts["artists"]
    if artists:
        swept = sum(1 for row in artists if _text(row.get("swept_at")))
        notes.append("page_end: %d/%d 位画师有本次清扫完成证据" % (swept, len(artists)))
    incomplete = facts["incomplete"] or ()
    if incomplete:
        shown = ", ".join(
            "%s=%s" % (_text(row.get("artist_id")), _text(row.get("reason")) or "unknown")
            for row in list(incomplete)[:5]
        )
        more = "" if len(incomplete) <= 5 else "（共 %d 位）" % len(incomplete)
        notes.append("未完成画师: %s%s" % (shown, more))
    budget = facts["budget"]
    if budget is not None:
        remaining = budget.get("remaining")
        notes.append(
            "来源预算: state=%s, remaining=%s"
            % (_text(budget.get("state")) or "unknown", "unknown" if remaining is None else remaining)
        )
    return tuple(notes)


@dataclass(frozen=True)
class FollowCoverage:
    """One immutable coverage summary.  Build it with build_coverage()."""

    scope: Mapping
    overall: str
    overall_reason: str
    enumeration: Mapping
    decision: Mapping
    delivery: Mapping
    page_end_evidence: Mapping
    counts: Mapping
    denominators: Mapping
    consistent: bool
    reasons: tuple
    stop_reason: str
    notes: tuple

    def as_dict(self) -> dict:
        """A plain JSON-safe dict, and the shape render() consumes."""
        return {
            "scope": dict(self.scope),
            "overall": self.overall,
            "overall_reason": self.overall_reason,
            "enumeration": dict(self.enumeration),
            "decision": dict(self.decision),
            "delivery": dict(self.delivery),
            "page_end_evidence": dict(self.page_end_evidence),
            "counts": dict(self.counts),
            "denominators": dict(self.denominators),
            "consistent": self.consistent,
            "reasons": list(self.reasons),
            "inconsistent_reasons": list(self.reasons),
            "stop_reason": self.stop_reason,
            "notes": list(self.notes),
        }

    def lines(self) -> list:
        """The human summary, one sentence per line."""
        return render(self.as_dict())

    def text(self) -> str:
        """The same summary as one string."""
        return render_text(self.as_dict())


def build_coverage(facts: object, *, scope: object) -> FollowCoverage:
    """Assemble the coverage summary for one run's facts."""
    facts = _facts(facts)
    scope_map = _scope(scope)
    try:
        states = coverage_states(facts)
    except CoverageError:
        raise
    except ValueError as error:
        raise CoverageError("facts_rejected_by_state", str(error)) from error
    try:
        report = coverage_counts(facts)
    except CoverageError:
        raise
    except ValueError as error:
        raise CoverageError("facts_rejected_by_counts", str(error)) from error

    enumeration = _dimension(states, "enumeration")
    decision = _dimension(states, "decision")
    delivery = _dimension(states, "delivery")
    counts, denominators, consistent, reasons = _counters(report)
    page_end = _page_end_evidence(facts)
    overall, overall_reason = _overall(
        str(enumeration["state"]), str(decision["state"]), str(delivery["state"]), consistent
    )
    run = facts["run"] if isinstance(facts["run"], Mapping) else {}
    return FollowCoverage(
        scope=scope_map,
        overall=overall,
        overall_reason=overall_reason,
        enumeration=enumeration,
        decision=decision,
        delivery=delivery,
        page_end_evidence=page_end,
        counts=counts,
        denominators=denominators,
        consistent=consistent,
        reasons=reasons,
        stop_reason=_text(run.get("stop_reason")),
        notes=_notes(facts, counts, denominators, page_end),
    )
