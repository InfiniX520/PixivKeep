"""One five-dimension quality view: structure first, then a gated decode.

The structure check already existed (core.image_validation).  This module adds
the second question - can a decoder actually read the pixels - without ever
pretending that a missing decoder is a pass: when no component is installed and
the caller asked for a strict check, the decode dimension says
"capability_unavailable" and the report keeps that word.

Nothing here writes to the file.  check_file() hashes before and after so a
caller can see that the bytes were preserved, rather than take it on trust.
"""
from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path

from core.decode_limits import DecodeLimits
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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _limits(value: object) -> DecodeLimits:
    if value is None:
        return DEFAULT_LIMITS
    if not isinstance(value, DecodeLimits):
        raise TypeError("limits must be a DecodeLimits instance")
    return value


def _structure(path: Path, *, expected_size, expected_media_type, stop_event):
    """(evidence or None, Dimension) for the structure question."""
    try:
        evidence = inspect_image(
            path,
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


def _decode(path: Path, media_type: str | None, structure_state: str, *,
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
        with path.open("rb") as stream:
            result = decode_png(stream, limits=limits, stop=stop)
        state = str(result.get("state") or "undecided")
        if state not in {"ok", "failed", "undecided"}:
            state = "undecided"
        receipt = DecodeReceipt(
            format="png",
            frames=int(result.get("frames") or 0),
            pixels=int(result.get("pixels") or 0),
            limits=limits.as_dict(),
            decoder="stdlib-zlib-png",
            outcome=state,
            seconds=round(time.perf_counter() - started, 6),
            checked_at=_now(),
        )
        return Dimension(state, result.get("reason")), receipt
    outcome = decode_with(probe_decoder(), path, policy="strict", limits=limits, stop=stop)
    receipt = DecodeReceipt(
        format=str(media_type or "unknown"),
        frames=0,
        pixels=0,
        limits=limits.as_dict(),
        # C4 reports None when nothing is installed; the receipt still has to
        # say something readable, and "none" is the honest word for it.
        decoder=outcome.decoder or "none",
        outcome=outcome.decode,
        seconds=0.0,
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


def quality_evidence(path, *, decode_policy: str = "structure", limits=None, stop=None,
                     expected_size=None, expected_media_type=None) -> dict:
    """Run both questions on one file and return the five dimensions."""
    if decode_policy not in DECODE_POLICIES:
        raise ValueError("decode_policy must be one of %s" % (DECODE_POLICIES,))
    target = Path(path)
    budget = _limits(limits)
    evidence, structure = _structure(
        target,
        expected_size=expected_size,
        expected_media_type=expected_media_type,
        stop_event=stop,
    )
    decode, receipt = _decode(
        target,
        getattr(evidence, "media_type", None),
        structure.state,
        policy=decode_policy,
        limits=budget,
        stop=stop,
    )
    transport = Dimension("length_matched") if expected_size is not None else Dimension("unknown", "no_expected_size")
    quality = QualityEvidence(
        source=Dimension("unknown", "provenance_not_observed"),
        transport=transport,
        structure=structure,
        decode=decode,
        pages=_pages(evidence, decode.state),
    )
    return {
        "path": str(target),
        "decode_policy": decode_policy,
        "media_type": getattr(evidence, "media_type", None),
        "size": int(getattr(evidence, "size", target.stat().st_size)),
        "sha256": getattr(evidence, "sha256", None),
        "width": getattr(evidence, "width", None),
        "height": getattr(evidence, "height", None),
        "frames": getattr(evidence, "frames", None),
        "quality": quality.as_dict(),
        "receipt": None if receipt is None else receipt.as_dict(),
    }


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def check_file(path, *, decode_policy: str = "structure", limits=None, stop=None,
               expected_size=None, expected_media_type=None) -> dict:
    """quality_evidence plus proof that the bytes did not move."""
    target = Path(path)
    before = _sha256(target)
    report = quality_evidence(
        target,
        decode_policy=decode_policy,
        limits=limits,
        stop=stop,
        expected_size=expected_size,
        expected_media_type=expected_media_type,
    )
    after = _sha256(target)
    report["unchanged"] = before == after
    report["sha256_before"] = before
    report["sha256_after"] = after
    return report
