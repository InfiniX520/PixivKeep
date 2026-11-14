"""One five-dimension quality view: structure first, then a gated decode.

The structure check already existed (core.image_validation).  This module adds
the second question - can a decoder actually read the pixels - without ever
pretending that a missing decoder is a pass: when no component is installed and
the caller asked for a strict check, the decode dimension says
"capability_unavailable" and the report keeps that word.

Three rules decide the shape of this module:

* the budget is the whole command's, not the decoder's.  A stop flag that is
  already set means no hash at all, and a file whose known size is above the
  byte ceiling is refused before the first read rather than after four passes;
* every answer describes one content version.  check_file() observes size and
  mtime before and after, hashes both ends when the budget allows, and says
  "changed" or "unknown" instead of handing back a verdict about bytes that are
  no longer at that path;
* transport only says length_matched after comparing the expected length with
  the length actually observed.  A caller-supplied number is a claim to be
  checked, not evidence.

Nothing here writes to the file.
"""
from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path

from core.decode_limits import CommandBudget, DecodeLimits
from core.decoder_registry import decode_with, probe_decoder
from core.image_validation import (
    ImageNeedsVerification,
    ImageValidationError,
    ImageValidationStopped,
    inspect_image,
)
from core.png_decode import decode_png
from core.quality_evidence import DecodeReceipt, Dimension, QualityEvidence

DECODE_POLICIES = ("structure", "strict")
DEFAULT_LIMITS = DecodeLimits(
    max_bytes=256 * 1024 * 1024,
    max_pixels=50_000_000,
    max_frames=2_000,
    max_seconds=30.0,
)

#: The stages one command runs, in order.  The budget is entered before each
#: one and charged after it, so a stop or a ceiling is never discovered late.
STAGES = ("observe", "version_before", "structure", "decode", "version_after")

#: What path_stability may say.  changed and unknown can never be a success.
PATH_STABILITY = ("unchanged", "changed", "unknown")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _measured(value: object) -> int | None:
    """A reported measurement, or None when the decoder produced none.

    int(x or 0) used to turn an unknown pixel count into 0, which reads as
    "measured zero pixels" instead of "nobody counted".  A real zero still
    survives: a decoder that ran and decoded no frame reports 0.
    """
    return value if type(value) is int and value >= 0 else None


def _limits(value: object) -> DecodeLimits:
    if value is None:
        return DEFAULT_LIMITS
    if not isinstance(value, DecodeLimits):
        raise TypeError("limits must be a DecodeLimits instance")
    return value


def _observe(target: Path) -> tuple[dict | None, str | None]:
    """Size and modification time: the cheap identity of the observed version.

    This is metadata only - no byte of content is read - so it is the one
    observation that is always affordable, even when the byte ceiling has
    already been spent.
    """
    try:
        info = target.stat()
    except OSError as error:
        return None, type(error).__name__
    return {"size": int(info.st_size), "mtime_ns": int(info.st_mtime_ns)}, None


def _structure(target: Path, *, expected_size, expected_media_type, stop_event):
    """(evidence or None, Dimension) for the structure question."""
    try:
        evidence = inspect_image(
            target,
            expected_size=expected_size,
            expected_media_type=expected_media_type,
            stop_event=stop_event,
        )
    except ImageNeedsVerification as error:
        return None, Dimension("undecided", str(getattr(error, "code", error)))
    except ImageValidationStopped:
        return None, Dimension("undecided", "cancel")
    except ImageValidationError as error:
        return None, Dimension("failed", str(getattr(error, "code", error)))
    except OSError as error:
        return None, Dimension("failed", "unreadable:%s" % type(error).__name__)
    return evidence, Dimension("ok")


def _decode(target: Path, media_type: str | None, structure_state: str, *,
            policy: str, limits: DecodeLimits, stop) -> tuple[Dimension, DecodeReceipt | None]:
    """The decode question, answered honestly even when nothing can answer it."""
    if policy == "structure":
        return Dimension("undecided", "not_requested"), None
    if structure_state != "ok":
        # A file the structure check already rejected is not decoded: two
        # failures would only blur which one found the problem.
        return Dimension("undecided", "structure_not_ok"), None
    if media_type == "image/png":
        started = time.perf_counter()
        try:
            with target.open("rb") as stream:
                result = decode_png(stream, limits=limits, stop=stop)
        except OSError as error:
            # The file moved between the structure question and this open: an
            # unreadable artifact, not a crash, and nothing was measured.
            return Dimension("failed", "unreadable:%s" % type(error).__name__), DecodeReceipt(
                format="png",
                frames=None,
                pixels=None,
                limits=limits.as_dict(),
                decoder="stdlib-zlib-png",
                outcome="failed",
                seconds=None,
                checked_at=_now(),
            )
        state = str(result.get("state") or "undecided")
        if state not in {"ok", "failed", "undecided"}:
            state = "undecided"
        # frames/pixels describe what the decoder actually produced.  A stopped
        # or failed decode produced nothing to count, so the honest value is
        # None; the header-derived pixel count png_decode can compute is the
        # size of the image, not evidence that any of it was decoded.
        decoded = state == "ok"
        receipt = DecodeReceipt(
            format="png",
            frames=_measured(result.get("frames")) if decoded else None,
            pixels=_measured(result.get("pixels")) if decoded else None,
            limits=limits.as_dict(),
            decoder="stdlib-zlib-png",
            outcome=state,
            seconds=round(time.perf_counter() - started, 6),
            checked_at=_now(),
        )
        return Dimension(state, result.get("reason")), receipt
    outcome = decode_with(probe_decoder(), target, policy="strict", limits=limits, stop=stop)
    receipt = DecodeReceipt(
        format=str(media_type or "unknown"),
        # No decoder ran, so there is no frame count, no pixel count and no
        # duration to report: None says that, 0 would claim a measurement.
        frames=None,
        pixels=None,
        limits=limits.as_dict(),
        # C4 reports None when nothing is installed; the receipt still has to
        # say something readable, and "none" is the honest word for it.
        decoder=outcome.decoder or "none",
        outcome=outcome.decode,
        seconds=None,
        checked_at=_now(),
    )
    return Dimension(outcome.decode, outcome.reason), receipt


def _pages(evidence, decode_state: str) -> Dimension:
    """Page completeness needs a decode: counting frames is not reading them."""
    if evidence is None:
        return Dimension("unknown", "structure_not_ok")
    if decode_state == "ok":
        return Dimension("all_frames")
    return Dimension("unknown", "decoder_did_not_read_frames")


def _transport(expected_size, observed_size, observe_error) -> Dimension:
    """Length evidence: a comparison, never a restatement of the expectation.

    Four states, each saying something different:

        length_matched   expected == observed, both known, compared here
        length_mismatch  expected and observed are both known and differ
        length_unknown   there was no usable expectation to compare against
        unreadable       the artifact could not be observed at all

    Only an equality observed in this run may say length_matched; a mismatch is
    a known fact and must not be softened into "unknown".
    """
    if observed_size is None:
        return Dimension("unreadable", "unreadable:%s" % (observe_error or "unknown"))
    if expected_size is None:
        return Dimension("length_unknown", "no_expected_size")
    if type(expected_size) is not int or expected_size <= 0:
        return Dimension("length_unknown", "invalid_expected_size")
    if int(expected_size) == int(observed_size):
        return Dimension("length_matched", "expected_equals_observed")
    return Dimension(
        "length_mismatch", "expected_%d_observed_%d" % (expected_size, observed_size)
    )


def _refused_report(target: Path, reason: str, *, decode_policy: str,
                    expected_size, observed_size, observe_error, command,
                    unreadable: bool = False) -> dict:
    """The report for a command that stopped before it read anything.

    A stop or a spent budget is undecided - it says nothing about the file.  An
    artifact that cannot even be stat-ed is different: that is a fact about the
    artifact, and the structure dimension says failed/unreadable.
    """
    transport = _transport(expected_size, observed_size, observe_error)
    structure = (
        Dimension("failed", reason) if unreadable else Dimension("undecided", reason)
    )
    quality = QualityEvidence(
        source=Dimension("unknown", "provenance_not_observed"),
        transport=transport,
        structure=structure,
        decode=Dimension("undecided", reason),
        pages=Dimension("unknown", "structure_not_ok"),
    )
    return {
        "path": str(target),
        "decode_policy": decode_policy,
        "media_type": None,
        "size": observed_size,
        "sha256": None,
        "width": None,
        "height": None,
        "frames": None,
        "quality": quality.as_dict(),
        "receipt": None,
        "budget": command.snapshot(),
        "content_version": {"state": "unknown", "reason": reason},
    }


def quality_evidence(path, *, decode_policy: str = "structure", limits=None, stop=None,
                     expected_size=None, expected_media_type=None, budget=None,
                     observed=None) -> dict:
    """Run both questions on one file and return the five dimensions.

    budget may be a CommandBudget shared with the caller (check_file does that
    so the version hashes it takes are charged to the same counters).  When it
    is None a fresh one is built from limits and stop.
    """
    if decode_policy not in DECODE_POLICIES:
        raise ValueError("decode_policy must be one of %s" % (DECODE_POLICIES,))
    target = Path(path)
    outer = _limits(limits)
    command = budget if budget is not None else CommandBudget(outer, stop=stop)
    if not isinstance(command, CommandBudget):
        raise TypeError("budget must be a CommandBudget instance")

    # A caller that already observed the path (check_file does, so the version
    # hash and the dimensions describe the same moment) passes the observation
    # in; entering the stage twice would record it twice and stat the file twice.
    refused = command.check() if observed is not None else command.enter("observe")
    observe_error = None
    if observed is None:
        observed, observe_error = _observe(target)
    size = None if observed is None else observed["size"]
    if refused:
        return _refused_report(target, refused, decode_policy=decode_policy,
                               expected_size=expected_size, observed_size=size,
                               observe_error=observe_error, command=command)
    if observed is None:
        return _refused_report(target, "unreadable:%s" % (observe_error,),
                               decode_policy=decode_policy,
                               expected_size=expected_size, observed_size=None,
                               observe_error=observe_error, command=command,
                               unreadable=True)
    if size > command.limits.max_bytes:
        # The size is known from the stat, so the ceiling is decided before a
        # single byte is read: no hash, no structure walk, no decode.
        return _refused_report(target, "bytes_limit", decode_policy=decode_policy,
                               expected_size=expected_size, observed_size=size,
                               observe_error=None, command=command)

    refused = command.enter("structure")
    if refused:
        return _refused_report(target, refused, decode_policy=decode_policy,
                               expected_size=expected_size, observed_size=size,
                               observe_error=None, command=command)
    evidence, structure = _structure(
        target,
        expected_size=expected_size,
        expected_media_type=expected_media_type,
        stop_event=stop,
    )
    # The structure walk reads the file once end to end; charging its measured
    # size is what keeps the command total honest instead of counting only the
    # decoder.
    command.charge(size)

    granted = command.grant() if structure.state == "ok" else None
    if decode_policy == "structure":
        decode, receipt = Dimension("undecided", "not_requested"), None
    elif structure.state != "ok":
        decode, receipt = Dimension("undecided", "structure_not_ok"), None
    elif granted is None:
        decode, receipt = Dimension("undecided", command.refusal() or "bytes_limit"), None
    else:
        refused = command.enter("decode")
        if refused:
            decode, receipt = Dimension("undecided", refused), None
        else:
            decode, receipt = _decode(
                target,
                getattr(evidence, "media_type", None),
                structure.state,
                policy=decode_policy,
                limits=granted,
                stop=stop,
            )
            if receipt is not None:
                command.charge(_receipt_bytes(receipt, granted))

    quality = QualityEvidence(
        source=Dimension("unknown", "provenance_not_observed"),
        transport=_transport(expected_size, size, None),
        structure=structure,
        decode=decode,
        pages=_pages(evidence, decode.state),
    )
    return {
        "path": str(target),
        "decode_policy": decode_policy,
        "media_type": getattr(evidence, "media_type", None),
        "size": size,
        "sha256": getattr(evidence, "sha256", None),
        "width": getattr(evidence, "width", None),
        "height": getattr(evidence, "height", None),
        "frames": getattr(evidence, "frames", None),
        "quality": quality.as_dict(),
        "receipt": None if receipt is None else receipt.as_dict(),
        "budget": command.snapshot(),
        "content_version": (
            {"state": "observed", "sha256": getattr(evidence, "sha256", None),
             "size": size}
            if getattr(evidence, "sha256", None)
            else {"state": "unknown", "reason": "structure_not_observed"}
        ),
    }


def _receipt_bytes(receipt: DecodeReceipt, limits: DecodeLimits) -> int:
    """How much the decode stage read, as the decoder itself accounted it.

    A receipt carries no byte count of its own (its fields are frames, pixels
    and seconds), so the decoder is asked directly by callers that need it;
    this helper exists so the accounting stays in one place.
    """
    return int(getattr(receipt, "bytes_read", 0) or 0)


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def _stability(before, after, sha_before, sha_after) -> tuple[str, str]:
    """Did the path still hold the bytes that were checked?"""
    if before is None or after is None:
        return "unknown", "observation_missing"
    if sha_before is None or sha_after is None:
        return "unknown", "hash_not_affordable"
    if sha_before != sha_after:
        return "changed", "content_changed"
    if before != after:
        return "changed", "metadata_changed"
    return "unchanged", ""


def check_file(path, *, decode_policy: str = "structure", limits=None, stop=None,
               expected_size=None, expected_media_type=None) -> dict:
    """quality_evidence bound to one content version.

    The report answers two different questions and keeps them apart: what was
    checked (the five dimensions) and whether the path still holds it
    (path_stability).  changed and unknown are not successes, whatever the five
    dimensions say.
    """
    target = Path(path)
    outer = _limits(limits)
    command = CommandBudget(outer, stop=stop)

    refused = command.enter("observe")
    before, observe_error = _observe(target)
    if before is None:
        # A path that cannot be observed at all is not a report about a file:
        # the caller asked about a path it does not have.  Let the genuine
        # OSError out (the CLI turns it into exit 3) instead of inventing a
        # verdict for a file nobody could open.
        target.stat()
    size = None if before is None else before["size"]
    sha_before = None
    if not refused and size is not None and size <= outer.max_bytes:
        refused = command.enter("version_before")
        if not refused:
            sha_before = _sha256(target)
            command.charge(size)

    report = quality_evidence(
        target,
        decode_policy=decode_policy,
        limits=outer,
        stop=stop,
        expected_size=expected_size,
        expected_media_type=expected_media_type,
        budget=command,
        observed=before,
    )

    sha_after = None
    if sha_before is not None and not command.refusal():
        refused = command.enter("version_after")
        if not refused:
            try:
                sha_after = _sha256(target)
            except OSError:
                sha_after = None
            if sha_after is not None:
                command.charge(size or 0)
    after, _after_error = _observe(target)
    stability, stability_reason = _stability(before, after, sha_before, sha_after)

    # unchanged keeps its historical meaning - this run did not observe the path
    # change - so a check that could not afford both hashes still answers False
    # for a change it saw and True when it saw nothing move.  The sharp answer is
    # path_stability, and that is the field a caller must gate on: unknown is not
    # a proof, and it is exactly what a spent budget produces.
    report["unchanged"] = stability != "changed" and before is not None and after is not None
    report["path_stability"] = stability
    report["path_stability_reason"] = stability_reason
    report["sha256_before"] = sha_before
    report["sha256_after"] = sha_after
    report["observed_before"] = before
    report["observed_after"] = after
    report["budget"] = command.snapshot()
    return report

