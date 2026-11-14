"""A closed vocabulary for "why this candidate was excluded" (U2-B, lane B1).

The filter already exists: `core.candidate_filter.evaluate_filters` decides
filtered / metadata_unknown / allow and reports one of eight legacy codes.  This
module does **not** re-decide anything.  It gives that decision one closed
vocabulary so three different consumers - the explanation, the UI column and the
offline re-evaluation - cannot quietly invent a ninth code or a sixth field.

Three rules make the vocabulary trustworthy:

* the codes and the fields are tuples, so "is this a legal reason" is a set
  membership test rather than a naming convention;
* a reason is a *record*: code, field, evidence and an explicit `unknown`
  flag.  `unknown` is what a caller reads to tell "we know this is excluded"
  from "we could not tell"; it is never inferred from the code;
* `LEGACY_CODE_MAP` is data, and `check_legacy_map()` runs at import time, so
  a legacy code that nobody mapped is an ImportError-shaped failure instead of a
  silent passthrough of a free-form string.

Evidence is a JSON scalar only (str / int / bool / None).  Long text is
truncated to :data:`EVIDENCE_MAX_CHARS` characters and marked with
`"…(truncated)"`; the original length is deliberately *not* stored, because a
record that carries an undeclared second fact is exactly what this module is
trying to avoid.  If the full text matters, log it where logs live.
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

#: The five codes a reason may use, in display order.
REASON_CODES: tuple[str, ...] = (
    "favorites_below",
    "ai_excluded",
    "type_unsupported",
    "field_unknown",
    "access_denied",
)

#: The five fields a reason may point at, in display order.
FIELDS: tuple[str, ...] = (
    "bookmarks",
    "ai_flag",
    "type",
    "availability",
    "original_urls",
)

#: Raw evidence longer than this is truncated before it becomes a record.
EVIDENCE_MAX_CHARS = 200

#: Marker appended to truncated evidence; fixed length on purpose.
TRUNCATION_MARKER = "…(truncated)"

#: Longest legal evidence string inside a record (raw limit plus the marker).
MAX_EVIDENCE_CHARS = EVIDENCE_MAX_CHARS + len(TRUNCATION_MARKER)

#: The eight reason codes the existing filter can emit.
LEGACY_CODES: tuple[str, ...] = (
    "bookmarks_below_minimum",
    "bookmarks_unknown",
    "ai_excluded",
    "ai_flag_unknown",
    "ugoira_unsupported",
    "artwork_unavailable",
    "original_metadata_unknown",
    "invalid_original_metadata",
)

#: legacy code -> (code, field, unknown).  Data, not code: it is tested.
LEGACY_CODE_MAP: dict[str, tuple[str, str, bool]] = {
    "bookmarks_below_minimum": ("favorites_below", "bookmarks", False),
    "bookmarks_unknown": ("field_unknown", "bookmarks", True),
    "ai_excluded": ("ai_excluded", "ai_flag", False),
    "ai_flag_unknown": ("field_unknown", "ai_flag", True),
    "ugoira_unsupported": ("type_unsupported", "type", False),
    "artwork_unavailable": ("access_denied", "availability", False),
    "original_metadata_unknown": ("field_unknown", "original_urls", True),
    "invalid_original_metadata": ("field_unknown", "original_urls", True),
}


class FilterReasonError(ValueError):
    """A reason was built or validated outside the closed vocabulary."""

    def __init__(self, code: str, detail: object = None) -> None:
        self.code = str(code)
        self.detail = detail
        super().__init__(self.code if detail is None else "%s: %s" % (self.code, detail))


class FrozenReason(dict):
    """A plain dict that refuses to change, so it still serialises as JSON.

    `MappingProxyType` would be immutable too, but `json.dumps` does not accept
    it; a dict subclass with closed mutators keeps both properties.
    """

    def _refuse(self, *args: object, **kwargs: object) -> None:
        raise TypeError("a filter reason is immutable")

    __setitem__ = _refuse  # type: ignore[assignment]
    __delitem__ = _refuse  # type: ignore[assignment]
    clear = _refuse  # type: ignore[assignment]
    pop = _refuse  # type: ignore[assignment]
    popitem = _refuse  # type: ignore[assignment]
    setdefault = _refuse  # type: ignore[assignment]
    update = _refuse  # type: ignore[assignment]

    def __reduce__(self):  # keeps copy/deepcopy honest
        return (dict, (dict(self),))


def _scalar(value: object) -> bool:
    """The four JSON scalars the contract names: str / int / bool / None.

    Floats are refused on purpose: the contract enumerates four kinds, and a
    float in an evidence field is almost always a computed value nobody meant to
    freeze into a receipt.
    """
    return value is None or type(value) in (str, int, bool)


def _truncate(value: object) -> object:
    if type(value) is str and len(value) > EVIDENCE_MAX_CHARS:
        return value[:EVIDENCE_MAX_CHARS] + TRUNCATION_MARKER
    return value


def reason(
    code: str, field: str, evidence: object, *, unknown: bool = False
) -> FrozenReason:
    """Build one validated, immutable reason record."""
    if type(unknown) is not bool:
        raise FilterReasonError("reason_unknown_not_bool", type(unknown).__name__)
    if code not in REASON_CODES:
        raise FilterReasonError("reason_code_unknown", code)
    if field not in FIELDS:
        raise FilterReasonError("reason_field_unknown", field)
    value = _truncate(evidence)
    if not _scalar(value):
        raise FilterReasonError("reason_evidence_not_scalar", type(evidence).__name__)
    record = FrozenReason(
        code=code, field=field, evidence=value, unknown=unknown
    )
    validate_reason(record)
    return record


def validate_reason(record: object) -> None:
    """Raise unless `record` is a legal reason (closed sets, JSON scalars)."""
    if not isinstance(record, Mapping):
        raise FilterReasonError("reason_shape_invalid", type(record).__name__)
    keys = set(record)
    if keys != {"code", "field", "evidence", "unknown"}:
        raise FilterReasonError(
            "reason_shape_invalid", "missing=%s extra=%s" % (
                sorted({"code", "field", "evidence", "unknown"} - keys),
                sorted(keys - {"code", "field", "evidence", "unknown"}),
            )
        )
    code = record["code"]
    if not isinstance(code, str) or code not in REASON_CODES:
        raise FilterReasonError("reason_code_unknown", code)
    field = record["field"]
    if not isinstance(field, str) or field not in FIELDS:
        raise FilterReasonError("reason_field_unknown", field)
    if type(record["unknown"]) is not bool:
        raise FilterReasonError("reason_unknown_not_bool", type(record["unknown"]).__name__)
    evidence = record["evidence"]
    if not _scalar(evidence):
        raise FilterReasonError("reason_evidence_not_scalar", type(evidence).__name__)
    if type(evidence) is str and len(evidence) > MAX_EVIDENCE_CHARS:
        raise FilterReasonError("reason_evidence_too_long", len(evidence))


def verdict_of(reasons: Iterable[object]) -> str:
    """match / exclude / unknown, using the frozen precedence.

    A known rejection outranks an unknown one: "we know this is out" is stronger
    evidence than "we could not tell", and the existing filter behaves the same
    way (a filtered row never becomes "unknown" just because another field was
    missing).
    """
    items = list(reasons)
    for record in items:
        validate_reason(record)
    if any(record["unknown"] is False for record in items):
        return "exclude"
    if any(record["unknown"] is True for record in items):
        return "unknown"
    return "match"


def check_legacy_map(mapping: Mapping[str, object] | None = None) -> None:
    """Raise unless every legacy code maps onto the closed vocabulary."""
    table = LEGACY_CODE_MAP if mapping is None else mapping
    if not isinstance(table, Mapping):
        raise FilterReasonError("legacy_map_shape", type(table).__name__)
    missing = [code for code in LEGACY_CODES if code not in table]
    extra = [code for code in table if code not in LEGACY_CODES]
    if missing:
        raise FilterReasonError("legacy_map_incomplete", missing)
    if extra:
        raise FilterReasonError("legacy_map_extra", extra)
    for legacy in LEGACY_CODES:
        target = table[legacy]
        if not isinstance(target, tuple) or len(target) != 3:
            raise FilterReasonError("legacy_map_shape", legacy)
        code, field, unknown = target
        if code not in REASON_CODES:
            raise FilterReasonError("legacy_map_code_unknown", "%s->%r" % (legacy, code))
        if field not in FIELDS:
            raise FilterReasonError("legacy_map_field_unknown", "%s->%r" % (legacy, field))
        if type(unknown) is not bool:
            raise FilterReasonError(
                "legacy_map_unknown_not_bool", "%s->%r" % (legacy, unknown)
            )


def translate(legacy: object) -> FrozenReason:
    """One legacy code -> one reason record; unknown legacy codes refuse."""
    if not isinstance(legacy, str) or legacy not in LEGACY_CODE_MAP:
        raise FilterReasonError("legacy_code_unknown", legacy)
    code, field, unknown = LEGACY_CODE_MAP[legacy]
    # Evidence must stay a JSON scalar, so the legacy code itself is the value:
    # a dict here would break the record contract it is supposed to enforce.
    return reason(code, field, legacy, unknown=unknown)


check_legacy_map()
_LEGACY_CHECKED = True
