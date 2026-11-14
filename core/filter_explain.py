"""Explain one filtering decision without changing it.

`core.candidate_filter.evaluate_filters` already decides whether a candidate is
allowed, filtered or unknown.  This module only turns that decision into a
closed vocabulary so a page, a report or a log can say *why* a work was left
out, and it keeps the three states one-to-one with the existing ones:

    match   <-> allow             no known and no unknown reason
    exclude <-> filtered          at least one known reason
    unknown <-> metadata_unknown  no known reason, at least one unknown one

Two facts that `evaluate_filters` does not look at are added here, because they
are part of the same user question: an unsupported anime work (`kind == "ugoira"`)
and an artwork the source says is not visible (`available is False`).

Rules that are deliberately *not* invented here:

* a known rejection still wins over an unknown field, exactly as before;
* `available is None` produces no reason - "nobody told us" is not "blocked"
  (`make_candidate` behaves the same way, and that is on purpose);
* `kind == "unknown"` is reported as an unknown *type*, not as an unsupported
  one: we do not know that the type is wrong, only that we could not read it.

Zero I/O: no HTTP, no file, no database, no clock.
"""
from __future__ import annotations

from collections.abc import Mapping

from core.candidate_filter import ArtworkMetadata, evaluate_filters
from core.task_contracts import FilterSpec

#: The three answers this module can give.
VERDICTS = ("match", "exclude", "unknown")

#: The closed set of reason codes (B1 owns the shared table; this module keeps
#: its own copy so the two lanes can be developed without importing each other).
REASON_CODES = (
    "favorites_below",
    "ai_excluded",
    "type_unsupported",
    "field_unknown",
    "access_denied",
)

#: The closed set of fields a reason may point at.
FIELDS = ("bookmarks", "ai_flag", "type", "availability", "original_urls")

#: Legacy code -> (new code, field, unknown).  A code that is not here is a bug
#: in the caller, not something to pass through as a free string.
LEGACY_CODE_MAP = {
    "bookmarks_below_minimum": ("favorites_below", "bookmarks", False),
    "bookmarks_unknown": ("field_unknown", "bookmarks", True),
    "ai_excluded": ("ai_excluded", "ai_flag", False),
    "ai_flag_unknown": ("field_unknown", "ai_flag", True),
    "ugoira_unsupported": ("type_unsupported", "type", False),
    "artwork_unavailable": ("access_denied", "availability", False),
    "original_metadata_unknown": ("field_unknown", "original_urls", True),
    "invalid_original_metadata": ("field_unknown", "original_urls", True),
}

#: FilterEvaluation state -> verdict.  Kept public so tests (and the matrix in
#: the integration step) can check that the mapping is the only translation.
STATE_MAP = {"allow": "match", "filtered": "exclude", "metadata_unknown": "unknown"}

_KNOWN_KINDS = ("illust", "manga", "ugoira")
_SCALARS = (str, int, bool, type(None))


def _reason(code: object, field: object, evidence: object, *, unknown: bool = False) -> dict:
    """One reason record, validated against the closed sets."""
    if code not in REASON_CODES:
        raise ValueError("invalid_reason_code: %r" % (code,))
    if field not in FIELDS:
        raise ValueError("invalid_reason_field: %r" % (field,))
    if type(unknown) is not bool:
        raise ValueError("invalid_reason_unknown")
    if not isinstance(evidence, _SCALARS):
        raise ValueError("invalid_reason_evidence: %r" % (evidence,))
    return {"code": str(code), "field": str(field), "evidence": evidence, "unknown": unknown}


def _conditions(value: object) -> FilterSpec:
    """Accept a FilterSpec or a same-shaped mapping; refuse anything else."""
    if isinstance(value, FilterSpec):
        return value
    if not isinstance(value, Mapping):
        raise ValueError("invalid_conditions: FilterSpec or mapping required")
    unknown = set(value) - {"min_bookmarks", "exclude_ai"}
    if unknown:
        raise ValueError("invalid_conditions: unknown keys %s" % sorted(unknown))
    try:
        return FilterSpec(
            min_bookmarks=value.get("min_bookmarks"), exclude_ai=value.get("exclude_ai", False)
        )
    except ValueError as error:  # ContractError is a ValueError
        raise ValueError("invalid_conditions: %s" % error) from None


def _field_of(candidate: object, name: str, default: object = None) -> object:
    if isinstance(candidate, ArtworkMetadata):
        return getattr(candidate, name, default)
    return candidate.get(name, default)  # type: ignore[union-attr]


def _kind(candidate: object) -> str:
    """Normalise the artwork kind; anything unreadable counts as unknown."""
    value = _field_of(candidate, "kind", "unknown")
    return value if value in _KNOWN_KINDS else "unknown"


def _availability(candidate: object) -> object:
    value = _field_of(candidate, "available", None)
    if value is None or type(value) is bool:
        return value
    # A field that is neither True, False nor None is a caller bug rather than
    # a filtering outcome; refusing it is safer than letting a broken
    # "availability" slip through as if the work were usable.
    raise ValueError("invalid_candidate: availability must be bool or None")


def explain(candidate: object, conditions: object) -> dict:
    """Explain one candidate under one set of conditions.

    `candidate` is an :class:`ArtworkMetadata` or a mapping with the same field
    names; it is only read, never modified.  `conditions` is a
    :class:`FilterSpec` or a mapping with `min_bookmarks` / `exclude_ai`.

    Returns `{"verdict": ..., "reasons": [...]}` where every reason is
    `{"code", "field", "evidence", "unknown"}`.  A known reason always wins over
    an unknown one, so a candidate that is both "below the bookmark minimum" and
    "AI flag unreadable" is excluded, not merely unknown.
    """
    if not isinstance(candidate, (ArtworkMetadata, Mapping)):
        raise ValueError("invalid_candidate: ArtworkMetadata or mapping required")
    spec = _conditions(conditions)
    bookmarks = _field_of(candidate, "bookmarks", None)
    ai_flag = _field_of(candidate, "ai_flag", None)
    availability = _availability(candidate)

    evaluation = evaluate_filters(spec, bookmarks=bookmarks, ai_flag=ai_flag)
    reasons: list[dict] = []
    for legacy in evaluation.reasons:
        mapped = LEGACY_CODE_MAP.get(legacy)
        if mapped is None:
            # evaluate_filters grew a code this module does not know: that is a
            # contract break, not something to show to a user as a free string.
            raise ValueError("unmapped_reason_code: %r" % (legacy,))
        code, field, unknown = mapped
        evidence = bookmarks if code == "favorites_below" else (
            True if code == "ai_excluded" else None
        )
        reasons.append(_reason(code, field, evidence, unknown=unknown))

    kind = _kind(candidate)
    if kind == "ugoira":
        reasons.append(_reason("type_unsupported", "type", "ugoira"))
    elif kind == "unknown":
        reasons.append(_reason("field_unknown", "type", None, unknown=True))

    if availability is False:
        reasons.append(_reason("access_denied", "availability", False))

    known = [item for item in reasons if not item["unknown"]]
    unknown_reasons = [item for item in reasons if item["unknown"]]
    if known:
        verdict = "exclude"
    elif unknown_reasons:
        verdict = "unknown"
    else:
        verdict = "match"
    return {"verdict": verdict, "reasons": reasons}
