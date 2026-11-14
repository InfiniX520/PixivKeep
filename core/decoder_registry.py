"""Which decoder, if any, may look inside a file this build was given.

This project deliberately ships **no** image decoder.  Pillow, numpy and every
other external decoder are absent on this machine, and this module must never be
the place that changes that: it never imports one, never installs one and never
pretends that a missing component is a clean file.

Two questions are answered separately, because they fail differently:

* *capability* -- is there a decoder this build can use for this format?
* *outcome*   -- given whatever decoder exists (possibly none), what may the
  caller record about the bytes?

The only honest answer for a format with no decoder is
`capability_unavailable`: a structure-level inspection may still have passed, but
nothing was decoded, and no caller may turn that into "ok".
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable

#: The four outcomes a decode attempt may have.  `ok` is never produced without
#: a decoder actually returning it.
DECODE_OK = "ok"
DECODE_FAILED = "failed"
DECODE_UNDECIDED = "undecided"
DECODE_UNAVAILABLE = "capability_unavailable"

#: Strict knows about two policies: structure only, or insist on decoding.
POLICIES = ("structure", "strict")

#: Reason codes this module itself produces.
NO_DECODER = "no_decoder_component"
STRUCTURE_ONLY = "structure_only"
RESULT_INVALID = "decoder_result_invalid"

#: The longest reason this module will hand out, marker included.  A receipt is
#: an artefact people read and store, so it must not carry an unbounded string
#: from a decoder that misbehaved (core.candidate_filter keeps the same rule).
REASON_LIMIT = 120
TRUNCATED = ":truncated"

#: Tokens accepted by :func:`normalise_format`; media types and suffixes map onto
#: the same short names so a caller cannot create a second vocabulary by accident.
_ALIASES = {
    "png": "png",
    "image/png": "png",
    "jpg": "jpeg",
    "jpe": "jpeg",
    "jpeg": "jpeg",
    "image/jpeg": "jpeg",
    "image/jpg": "jpeg",
    "gif": "gif",
    "image/gif": "gif",
    "webp": "webp",
    "image/webp": "webp",
}


@runtime_checkable
class Decoder(Protocol):
    """The smallest thing a real decoder has to provide.

    A decoder reads one file and reports what it saw.  It may raise (that is
    turned into `failed`), it may decline (that is `undecided`), and it must not
    be asked to write, rotate or re-encode anything.
    """

    name: str
    formats: tuple[str, ...] | frozenset[str]

    def decode(self, path: Any, *, limits: Any, stop: Any) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class DecodeOutcome:
    """One decode attempt, normalised so callers cannot disagree about it."""

    decode: str
    reason: str | None = None
    decoder: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"decode": self.decode, "reason": self.reason, "decoder": self.decoder}


def normalise_format(value: object) -> str | None:
    """Map a suffix or media type onto this module's short format name.

    Unknown input returns None rather than a guess: a format nobody here can
    name is not a format this build can decode.
    """
    if not isinstance(value, str):
        return None
    token = value.strip().lower()
    if token.startswith("."):
        token = token[1:]
    return _ALIASES.get(token)


def probe_decoder() -> Decoder | None:
    """The decoder this build can use, or None.

    This build has no external decoder installed and installing one is outside
    this project's rules, so the honest answer is None.  The function exists as
    the single seam a future build would fill in; tests inject their own decoder
    into :func:`decode_with` instead of changing this answer.
    """
    return None


def capability(value: object, *, decoder: Decoder | None = None, injected: bool = False) -> str:
    """`available` only when a decoder is present *and* claims this format.

    `decoder`/`injected` exist so tests (and a future caller that already holds
    a decoder) can ask the question without re-probing; the public contract is
    `capability(format)`.
    """
    chosen = decoder if injected else probe_decoder()
    if chosen is None:
        return "unavailable"
    fmt = normalise_format(value)
    if fmt is None:
        return "unavailable"
    formats = getattr(chosen, "formats", ()) or ()
    return "available" if fmt in {normalise_format(item) for item in formats} else "unavailable"


def _decoder_name(decoder: Decoder | None) -> str | None:
    if decoder is None:
        return None
    name = getattr(decoder, "name", None)
    return name if isinstance(name, str) and name else type(decoder).__name__


def _safe_type_name(error: BaseException) -> str:
    """The exception type, and only when it looks like a short identifier."""
    name = type(error).__name__
    if not isinstance(name, str) or not name.isidentifier() or len(name) > 64:
        return "Exception"
    return name


def _safe_error_code(error: BaseException) -> str:
    """A bounded code for a decoder that raised; never the raw message.

    The message can hold an absolute path, a user name, a fragment of the file
    or several kilobytes of text, and none of that belongs in a receipt.  What
    survives is the exception *type* (as an identifier) plus, when the error
    carries one, the numeric errno - both bounded and content free.
    """
    code = "decoder_error:" + _safe_type_name(error)
    errno = getattr(error, "errno", None)
    if type(errno) is int:
        code += ":errno-%d" % errno
    return code


def _bounded_reason(value: object) -> str | None:
    """A decoder supplied reason, cut to a visible, bounded length."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if len(text) <= REASON_LIMIT:
        return text
    return text[: REASON_LIMIT - len(TRUNCATED)] + TRUNCATED


def _outcome_from(decoder: Decoder, result: object) -> DecodeOutcome:
    """Normalise whatever the decoder returned; a strange shape is a failure."""
    name = _decoder_name(decoder)
    if not isinstance(result, Mapping):
        return DecodeOutcome(DECODE_FAILED, RESULT_INVALID, name)
    state = result.get("decode")
    if state not in {DECODE_OK, DECODE_FAILED, DECODE_UNDECIDED}:
        return DecodeOutcome(DECODE_FAILED, RESULT_INVALID, name)
    return DecodeOutcome(str(state), _bounded_reason(result.get("reason")), name)


def decode_with(
    decoder: Decoder | None,
    path: Any,
    *,
    policy: str = "structure",
    limits: Any = None,
    stop: Any = None,
) -> DecodeOutcome:
    """Decode `path` with `decoder`, or report honestly that it could not.

    * no decoder: `capability_unavailable`, with a reason that says whether the
      caller asked for strict decoding or accepted the structure level;
    * decoder that raises an `Exception`: `failed` (the bytes were not decoded);
    * decoder that returns something this module cannot read: `failed` with
      `decoder_result_invalid` -- a verdict nobody can interpret is not `ok`;
    * `KeyboardInterrupt`/`SystemExit`: never swallowed, they keep their type.

    `ok` is only ever the decoder's own claim, and only when it named the state
    explicitly.
    """
    if policy not in POLICIES:
        raise ValueError("policy must be one of %s" % (POLICIES,))
    if decoder is None:
        return DecodeOutcome(
            DECODE_UNAVAILABLE, NO_DECODER if policy == "strict" else STRUCTURE_ONLY, None
        )
    try:
        result = decoder.decode(path, limits=limits, stop=stop)
    except Exception as error:  # noqa: BLE001 - a decoder failure is data, not a crash
        return DecodeOutcome(DECODE_FAILED, _safe_error_code(error), _decoder_name(decoder))
    return _outcome_from(decoder, result)
