"""Five independent quality dimensions for one delivered artifact.

A boolean "verified" hides the difference between *we did not look*, *the bytes
are intact* and *a decoder could reproduce every frame*.  This module keeps the
five questions apart so a caller can say which one it answered:

    source     was the current original declared, or is that unknown
    transport  did the length match the response contract
    structure  does the container/scan framing hold (what image_validation does)
    decode     did a decoder actually reproduce the frames
    pages      were all frames examined or only the first

Every dimension carries a state and, when something is not `ok`, a machine
readable reason.  `capability_unavailable` is a first class answer: it means no
decoder was installed for this format, and it must never be read as `ok`.

Nothing here reads a file, imports a decoder or decides an overall verdict.  In
particular there is deliberately **no** aggregate `ok`: five facts cannot be
folded into one without losing which question went unanswered (design U1-C,
table in docs/optimization/2026-11-14/U1C_QUALITY_EVIDENCE.md).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

__all__ = [
    "DIMENSIONS",
    "DECODE_OUTCOMES",
    "QUALITY_STATES",
    "DecodeReceipt",
    "Dimension",
    "QualityEvidence",
    "QualityEvidenceError",
    "is_known_state",
    "states_of",
]

#: The closed vocabulary of every dimension.  A state outside its own tuple is a
#: programming error, not a runtime surprise, so construction refuses it.
QUALITY_STATES: dict[str, tuple[str, ...]] = {
    "source": ("declared_original", "unknown"),
    "transport": ("length_matched", "unknown"),
    "structure": ("ok", "failed", "undecided", "unsupported"),
    "decode": ("ok", "failed", "undecided", "capability_unavailable"),
    "pages": ("all_frames", "first_frame_only", "unknown"),
}

#: Dimensions in display order; also the key order of :meth:`QualityEvidence.as_dict`.
DIMENSIONS: tuple[str, ...] = tuple(QUALITY_STATES)

#: What a decode receipt may say.  Same vocabulary as the `decode` dimension:
#: a receipt that could not decode is still a receipt, and it says so.
DECODE_OUTCOMES: tuple[str, ...] = QUALITY_STATES["decode"]


class QualityEvidenceError(ValueError):
    """A dimension, an evidence record or a receipt was built from bad input."""

    def __init__(self, code: str, detail: object = None) -> None:
        self.code = str(code)
        self.detail = None if detail is None else str(detail)
        message = self.code if self.detail is None else "%s: %s" % (self.code, self.detail)
        super().__init__(message)


def states_of(kind: str) -> tuple[str, ...]:
    """The allowed states of one dimension (empty tuple when the kind is unknown)."""
    return QUALITY_STATES.get(str(kind), ())


def is_known_state(kind: str, state: object) -> bool:
    """True only for a state that belongs to *this* dimension's vocabulary."""
    return type(state) is str and state in states_of(kind)


def _text(value: object, *, code: str, what: str) -> str:
    """A non-empty, already-trimmed string; anything else is refused by name."""
    if type(value) is not str:
        raise QualityEvidenceError(code, "%s is not text: %r" % (what, value))
    text = value.strip()
    if not text or text != value:
        raise QualityEvidenceError(code, "%s is empty or padded: %r" % (what, value))
    return text


@dataclass(frozen=True, slots=True)
class Dimension:
    """One dimension: a state from its vocabulary plus an optional reason.

    The vocabulary depends on which dimension this is, so a bare Dimension only
    checks its own shape.  :class:`QualityEvidence` checks that the state
    belongs to the field it was placed in - `Dimension("ok")` is legal on its
    own and illegal as `source`.
    """

    state: str
    reason: str | None = None

    def __post_init__(self) -> None:
        _text(self.state, code="dimension_state_invalid", what="state")
        if self.reason is not None:
            _text(self.reason, code="dimension_reason_invalid", what="reason")

    def as_dict(self) -> dict[str, Any]:
        return {"state": self.state, "reason": self.reason}

    @classmethod
    def from_dict(cls, value: object) -> "Dimension":
        if not isinstance(value, Mapping):
            raise QualityEvidenceError("dimension_shape_invalid", repr(value)[:80])
        unknown = set(value) - {"state", "reason"}
        if unknown or "state" not in value:
            raise QualityEvidenceError("dimension_shape_invalid", repr(sorted(unknown))[:80])
        return cls(value["state"], value.get("reason"))


@dataclass(frozen=True, slots=True)
class QualityEvidence:
    """The five dimensions of one artifact, each answered separately."""

    source: Dimension
    transport: Dimension
    structure: Dimension
    decode: Dimension
    pages: Dimension

    def __post_init__(self) -> None:
        for name in DIMENSIONS:
            dimension = getattr(self, name)
            if not isinstance(dimension, Dimension):
                raise QualityEvidenceError("evidence_shape_invalid", name)
            if not is_known_state(name, dimension.state):
                raise QualityEvidenceError(
                    "dimension_state_invalid",
                    "%s does not accept %r (allowed: %s)"
                    % (name, dimension.state, ", ".join(states_of(name))),
                )

    def as_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name).as_dict() for name in DIMENSIONS}

    @classmethod
    def from_dict(cls, value: object) -> "QualityEvidence":
        if not isinstance(value, Mapping):
            raise QualityEvidenceError("evidence_shape_invalid", repr(value)[:80])
        missing = [name for name in DIMENSIONS if name not in value]
        extra = sorted(set(value) - set(DIMENSIONS))
        if missing or extra:
            raise QualityEvidenceError(
                "evidence_shape_invalid", "missing=%s extra=%s" % (missing, extra)
            )
        return cls(**{name: Dimension.from_dict(value[name]) for name in DIMENSIONS})

    def states(self) -> dict[str, str]:
        """Just the states, in display order; handy for logs and tests."""
        return {name: getattr(self, name).state for name in DIMENSIONS}


@dataclass(frozen=True, slots=True)
class DecodeReceipt:
    """What one decode attempt measured, including the attempt that could not run.

    A measurement that never happened is `None`, never `0`: a missing decoder or
    a limit stop has no frame count, no pixel count and no duration, and writing
    zero there would read as "measured zero pixels in zero seconds" - a fact we
    do not have.  Zero stays legal for a real measurement (a decoder did run and
    produced no frame), so `None` and `0` keep different meanings.
    """

    format: str
    frames: int | None
    pixels: int | None
    limits: Mapping[str, int | float]
    decoder: str
    outcome: str
    seconds: float | None
    checked_at: str

    def __post_init__(self) -> None:
        _text(self.format, code="receipt_field_invalid", what="format")
        for name in ("frames", "pixels"):
            value = getattr(self, name)
            if value is None:
                continue
            if type(value) is not int or value < 0:
                raise QualityEvidenceError("receipt_field_invalid", "%s=%r" % (name, value))
        if not isinstance(self.limits, Mapping):
            raise QualityEvidenceError("receipt_field_invalid", "limits is not a mapping")
        limits: dict[str, int | float] = {}
        for key, value in self.limits.items():
            if type(value) not in (int, float) or value < 0:
                raise QualityEvidenceError("receipt_field_invalid", "limit %r=%r" % (key, value))
            limits[str(key)] = value
        object.__setattr__(self, "limits", limits)
        _text(self.decoder, code="receipt_field_invalid", what="decoder")
        if self.outcome not in DECODE_OUTCOMES:
            raise QualityEvidenceError(
                "receipt_outcome_invalid",
                "%r (allowed: %s)" % (self.outcome, ", ".join(DECODE_OUTCOMES)),
            )
        if self.seconds is not None:
            if type(self.seconds) not in (int, float) or self.seconds < 0:
                raise QualityEvidenceError("receipt_field_invalid", "seconds=%r" % (self.seconds,))
            object.__setattr__(self, "seconds", float(self.seconds))
        _text(self.checked_at, code="receipt_field_invalid", what="checked_at")

    def as_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "frames": self.frames,
            "pixels": self.pixels,
            "limits": dict(sorted(self.limits.items())),
            "decoder": self.decoder,
            "outcome": self.outcome,
            "seconds": self.seconds,
            "checked_at": self.checked_at,
        }

    @classmethod
    def from_dict(cls, value: object) -> "DecodeReceipt":
        if not isinstance(value, Mapping):
            raise QualityEvidenceError("receipt_shape_invalid", repr(value)[:80])
        expected = {
            "format",
            "frames",
            "pixels",
            "limits",
            "decoder",
            "outcome",
            "seconds",
            "checked_at",
        }
        missing = sorted(expected - set(value))
        extra = sorted(set(value) - expected)
        if missing or extra:
            raise QualityEvidenceError(
                "receipt_shape_invalid", "missing=%s extra=%s" % (missing, extra)
            )
        return cls(
            format=value["format"],
            frames=value["frames"],
            pixels=value["pixels"],
            limits=value["limits"],
            decoder=value["decoder"],
            outcome=value["outcome"],
            seconds=value["seconds"],
            checked_at=value["checked_at"],
        )
