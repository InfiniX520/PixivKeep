"""One five-dimension quality view: structure first, then a gated decode.

The structure check already existed (core.image_validation).  This module adds
the second question - can a decoder actually read the pixels - without ever
pretending that a missing decoder is a pass: when no component is installed and
the caller asked for a strict check, the decode dimension says
"capability_unavailable" and the report keeps that word.

Three rules decide the shape of this module:

* the budget is one file's, not the decoder's.  A stop flag that is already set
  means no hash at all, and a file whose known size is above the byte ceiling is
  refused before the first read rather than after four passes.  Every stage
  charges the bytes it really read - the two hashes, the structure walk and the
  decoder alike - and clamps each read to what is left, so the reported total is
  the sum of the reads and not an estimate of them;
* every answer describes one content version.  check_file() observes size and
  mtime before and after, hashes both ends when the budget allows, and says
  "changed" or "unknown" instead of handing back a verdict about bytes that are
  no longer at that path;
* the decoder's verdict is bound to the bytes it actually decoded.  Reading the
  path once for a hash and again for the decode proves only that two separate
  observations agreed, which is exactly the window an A->B->A swap lives in.
  The decode now reads through a pass that hashes every byte the decoder takes
  and that then takes the rest of the file itself, so the receipt names one
  content version: the content object reports it, and a decode whose bytes
  cannot be tied to that version is "undecided", never "ok";
* transport only says length_matched after comparing the expected length with
  the length actually observed.  A caller-supplied number is a claim to be
  checked, not evidence.

Nothing here writes to the file.
"""
from __future__ import annotations

import hashlib
import os
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

#: The stages one file check runs, in order.  The budget is entered before each
#: one and charged as it reads, so a stop or a ceiling is never discovered late.
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


def _structure(target: Path, *, expected_size, expected_media_type, stop_event,
               meter=None):
    """(evidence or None, Dimension) for the structure question."""
    try:
        evidence = inspect_image(
            target,
            expected_size=expected_size,
            expected_media_type=expected_media_type,
            stop_event=stop_event,
            meter=meter,
        )
    except ImageNeedsVerification as error:
        return None, Dimension("undecided", str(getattr(error, "code", error)))
    except ImageValidationStopped as error:
        return None, Dimension("undecided", getattr(error, "reason", "cancel") or "cancel")
    except ImageValidationError as error:
        return None, Dimension("failed", str(getattr(error, "code", error)))
    except OSError as error:
        return None, Dimension("failed", "unreadable:%s" % type(error).__name__)
    return evidence, Dimension("ok")


def _decode(target: Path, media_type: str | None, structure_state: str, *,
            policy: str, limits: DecodeLimits, stop,
            stream=None) -> tuple[Dimension, DecodeReceipt | None]:
    """The decode question, answered honestly even when nothing can answer it.

    stream is the content pass the bytes are taken through (see _ContentPass).
    Opening the path here instead would let a swap between the version hash and
    this call decide what gets decoded; the pass names the bytes, so the receipt
    can be tied to a version.  None keeps the path-opening behaviour for a
    caller that only wants the decoder's own verdict.
    """
    if policy == "structure":
        return Dimension("undecided", "not_requested"), None
    if structure_state != "ok":
        # A file the structure check already rejected is not decoded: two
        # failures would only blur which one found the problem.
        return Dimension("undecided", "structure_not_ok"), None
    if media_type == "image/png":
        started = time.perf_counter()
        try:
            if stream is None:
                with target.open("rb") as handle:
                    result = decode_png(handle, limits=limits, stop=stop)
            else:
                # The pass is a read-only view from the first byte; a decoder
                # that read from wherever the descriptor happened to be would
                # decode a tail as if it were the header.
                stream.seek(0)
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
                # The failure may have happened after some bytes were read, and
                # the decoder could not finish counting: that is unknown, not 0.
                bytes_read=None,
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
            # A stopped or failed decode still read bytes before it stopped;
            # the decoder counts them, so the budget charges what happened and
            # not only what was completed.
            bytes_read=_measured(result.get("bytes_read")),
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
        # No component was installed, so this stage provably read nothing: 0 is
        # a measurement here.  A decoder that did run belongs to another
        # module's accounting and did not report its bytes, so that stays
        # unknown rather than being guessed.
        bytes_read=0 if outcome.decode == "capability_unavailable" else None,
    )
    return Dimension(outcome.decode, outcome.reason), receipt


def _pages(evidence, decode_state: str) -> Dimension:
    """Page completeness needs a decode: counting frames is not reading them."""
    if evidence is None:
        return Dimension("unknown", "structure_not_ok")
    if decode_state == "ok":
        return Dimension("all_frames")
    return Dimension("unknown", "decoder_did_not_read_frames")


def _pages_after(structure_state: str, decode_state: str) -> Dimension:
    """The pages dimension for a decode whose verdict was replaced later.

    _pages() answers the same question while the evidence is in hand; the
    reconciliation in check_file() runs after that, so it asks again from the
    two states it still has.  Counting frames needs a decode: a demoted decode
    cannot prove the pages were read.
    """
    if structure_state != "ok":
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
        # Nothing was read, so there is no content to name.  The reason is the
        # one that stopped the check, not a verdict about the file.
        "content": {"state": "unknown", "sha256": None, "bytes": None,
                    "complete": False, "source": "file_stream", "reason": reason},
        "content_version": {"state": "unknown", "reason": reason},
    }


def quality_evidence(path, *, decode_policy: str = "structure", limits=None, stop=None,
                     expected_size=None, expected_media_type=None, budget=None,
                     observed=None) -> dict:
    """Run both questions on one file and return the five dimensions.

    budget may be a CommandBudget shared with the caller (check_file does that
    so the version hashes it takes are charged to the same counters).  When it
    is None a fresh one is built from limits and stop.

    The returned report names the bytes these dimensions describe in its
    content object: the digest of the pass the decoder read through, or of the
    structure walk when no decode ran.  check_file() reconciles it with the two
    version hashes; on its own this report has no version to compare against, so
    it reports which content it looked at and stops there.
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
    # The walk charges every block it really reads through this meter; charging
    # the size the stat reported instead was an estimate, and it silently went
    # wrong whenever the file changed between the stat and the walk.
    evidence, structure = _structure(
        target,
        expected_size=expected_size,
        expected_media_type=expected_media_type,
        stop_event=stop,
        meter=command.meter(),
    )

    granted = command.grant() if structure.state == "ok" else None
    # The pass the decode read through, when one was opened.  None means no
    # bytes were read for the decode, so no content version can be named.
    passed = None
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
            # The decode reads through a pass that hashes what it takes, so the
            # receipt can name the bytes it judged.  A file that vanished before
            # this open is left to _decode's own open, which reports it as an
            # unreadable artifact instead of a crash (there is no content to
            # name either way).
            handle = None
            try:
                handle = target.open("rb")
            except OSError:
                handle = None
            if handle is not None:
                passed = _ContentPass(handle)
            try:
                decode, receipt = _decode(
                    target,
                    getattr(evidence, "media_type", None),
                    structure.state,
                    policy=decode_policy,
                    limits=granted,
                    stop=stop,
                    stream=passed,
                )
                if passed is not None:
                    if decode.state == "ok" and passed.reads == 0:
                        # The decoder answered without reading through the pass
                        # (a decoder that opens the path itself), so its verdict
                        # cannot be tied to any bytes this check hashed.
                        passed.reason = "decode_stream_not_observed"
                    elif decode.state == "ok":
                        # A decoder may stop at the end of the image data while
                        # the file continues (trailing bytes): the rest is part
                        # of the version and is taken through the file budget.
                        passed.finish(command)
                    elif decode.state == "failed":
                        passed.reason = "decode_failed"
                    else:
                        passed.reason = str(decode.reason or "decode_undecided")
            finally:
                if handle is not None:
                    handle.close()
            if receipt is not None:
                spent = _receipt_bytes(receipt)
                if spent is None:
                    # A decoder that ran without saying how much it read leaves
                    # the total unknown, and an unknown total may not pass.  A
                    # decode that already failed keeps its own verdict: the
                    # unknown total is about the budget, not about the file.
                    command.mark_unknown("decoder_did_not_report_bytes")
                    if decode.state == "ok":
                        decode = Dimension("undecided", "decoder_did_not_report_bytes")
                else:
                    refused_charge = command.charge(spent)
                    if refused_charge is not None:
                        decode = Dimension("undecided", refused_charge)

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
        # The bytes this check read, from the pass that read them.  The
        # version hashes are reconciled against it in check_file(); on its own
        # this says which content the dimensions above describe.
        "content": _content_facts(passed, getattr(evidence, "sha256", None), size),
        "content_version": (
            {"state": "observed", "sha256": getattr(evidence, "sha256", None),
             "size": size}
            if getattr(evidence, "sha256", None)
            else {"state": "unknown", "reason": "structure_not_observed"}
        ),
    }


def _receipt_bytes(receipt: DecodeReceipt) -> int | None:
    """How much the decode stage read, or None when it did not say.

    A receipt that carries no count is not a zero-byte read: getattr(..., 0)
    used to turn "nobody reported" into "measured nothing", which made the
    command total look smaller than the work actually done.  None says the fact
    is missing, and the caller turns that into an unknown total.
    """
    value = getattr(receipt, "bytes_read", None)
    return value if type(value) is int and value >= 0 else None


#: How many bytes one hash read asks for.  Small enough that a cancel set while
#: hashing is noticed within one chunk, large enough that the per-chunk overhead
#: stays invisible next to the hashing itself.
_HASH_CHUNK = 256 * 1024

#: What a content observation may say about the bytes a check certified.
#:
#:   observed  one pass reached the end of the file and every digest taken in
#:             this run agrees, so the verdict names one content version
#:   unknown   something ended the pass early (a spent ceiling, a cancel, a
#:             decoder that read through its own path): nothing is claimed
#:   mismatch  two full passes saw different bytes, so no single version exists
CONTENT_STATES = ("observed", "unknown", "mismatch")


class _ContentPass:
    """The bytes one decode really consumed, hashed as they are read.

    A decoder given a bare path opens whatever that name points at *when it
    starts*; a first hash taken before and a second one after can both see an
    unchanged file while the decode in between read something else (A->B->A with
    the same size and restored mtime).  Handing the decoder this object removes
    that window from the receipt: every read it makes is hashed and counted as
    it goes, so the digest describes the bytes it decoded.

    Nothing is charged twice.  The decoder's own Budget already clamps and
    charges its reads (see core.png_decode._Source); finish() takes whatever the
    decoder left unread through the file budget, which is also where a cancel or
    a spent ceiling ends the pass with a reason instead of a digest.  A prefix is
    never returned as a content version: digest is None unless the pass reached
    the end of the file.
    """

    __slots__ = ("_stream", "_hasher", "bytes", "reason", "complete", "reads")

    def __init__(self, stream) -> None:
        self._stream = stream
        self._hasher = hashlib.sha256()
        #: Bytes the decoder took through this pass.
        self.bytes = 0
        #: Why the pass stopped short, or "" when it reached the end.
        self.reason = ""
        #: True only when the last byte of the file has been hashed.
        self.complete = False
        #: How many read() calls the decoder made; 0 means it never used it.
        self.reads = 0

    @property
    def digest(self) -> str | None:
        """The digest of the whole file, or None while the pass is short."""
        return self._hasher.hexdigest() if self.complete else None

    def read(self, amount=-1):
        data = self._stream.read(amount)
        if data:
            self.bytes += len(data)
            self.reads += 1
            self._hasher.update(data)
        return data

    # A decoder that asks for the descriptor (png_decode only reads, but the
    # structure walk and tests ask for fileno) keeps working through this.
    def __getattr__(self, name):
        return getattr(self._stream, name)

    def _at_end(self) -> bool:
        """True when the file has no bytes beyond the ones already hashed."""
        try:
            return self.bytes >= int(os.fstat(self._stream.fileno()).st_size)
        except OSError:
            return False

    def finish(self, command: "CommandBudget") -> None:
        """Hash whatever the decoder did not read, charging the file budget."""
        if self.complete:
            return
        if self._at_end():
            self.complete = True
            return
        meter = command.meter()
        while True:
            reason = meter.poll()
            if reason is not None:
                self.reason = reason
                return
            allowed = meter.size(_HASH_CHUNK)
            if allowed <= 0:
                # A file that ends exactly at the ceiling is not a truncation:
                # asking once more is how the pass learns it is at the end.
                if self._at_end():
                    self.complete = True
                    return
                self.reason = "bytes_limit"
                return
            chunk = self._stream.read(allowed)
            if not chunk:
                self.complete = True
                return
            self.bytes += len(chunk)
            self.reads += 1
            self._hasher.update(chunk)
            reason = meter.spent(len(chunk))
            if reason is not None:
                self.reason = reason
                return


def _content_facts(passed: "_ContentPass | None", structure_digest: str | None,
                   size: int | None) -> dict:
    """The content object: what bytes this check certified, or why it cannot.

    The digest is taken from the pass that read the file, never from the
    metadata: size and mtime are not a content version.  When no decode ran, the
    structure walk's digest is used - it read the whole file too - and when
    nothing read the file at all the state is unknown with the reason.
    """
    if passed is None:
        if structure_digest and size is not None:
            return {"state": "observed", "sha256": structure_digest, "bytes": size,
                    "complete": True, "source": "file_stream", "reason": ""}
        return {"state": "unknown", "sha256": None, "bytes": None, "complete": False,
                "source": "file_stream", "reason": "structure_not_observed"}
    if passed.complete and passed.digest is not None:
        if structure_digest is not None and structure_digest != passed.digest:
            # Two full passes read different bytes: there is no single content
            # version this check could name.
            return {"state": "mismatch", "sha256": passed.digest, "bytes": passed.bytes,
                    "complete": True, "source": "file_stream",
                    "reason": "content_changed"}
        return {"state": "observed", "sha256": passed.digest, "bytes": passed.bytes,
                "complete": True, "source": "file_stream", "reason": ""}
    return {"state": "unknown", "sha256": None, "bytes": passed.bytes, "complete": False,
            "source": "file_stream", "reason": passed.reason or "partial_read"}


def _reached_end(stream, hashed: int) -> bool:
    """True when this open stream has no more bytes than the ones already read.

    Asked only after the allowance reached zero, so it costs one fstat at the
    boundary and never turns a spent budget into a paid-for extra read.
    """
    try:
        return hashed >= os.fstat(stream.fileno()).st_size
    except OSError:
        return False


def _sha256(target: Path, meter) -> tuple[str | None, str | None]:
    """sha256 of one file read through a meter, in bounded charged chunks.

    The name is the one the frozen UX-04 check wraps (q._sha256), which is why
    a function that returns two values still carries it: that check asserts a
    cancelled run never reaches this call at all.

    Returns (digest, None), or (None, reason) when a stop flag or the byte
    ceiling ended the read.  A partial digest is never returned: half a file is
    not a content version, and handing one back would let a truncated read look
    like a measurement.  A meter is mandatory - there is no unmetered read path
    in this module any more.
    """
    hasher = hashlib.sha256()
    hashed = 0
    with target.open("rb") as stream:
        while True:
            reason = meter.poll()
            if reason is not None:
                return None, reason
            allowed = meter.size(_HASH_CHUNK)
            if allowed <= 0:
                # The ceiling is reached: asking for more would read bytes this
                # run may not look at.  A file that ended exactly at the ceiling
                # is the one case where nothing is missing.
                if _reached_end(stream, hashed):
                    break
                return None, "bytes_limit"
            chunk = stream.read(allowed)
            if not chunk:
                break
            hashed += len(chunk)
            reason = meter.spent(len(chunk))
            hasher.update(chunk)
            if reason is not None:
                return None, reason
    return hasher.hexdigest(), None


def _stability(before, after, sha_before, sha_after, hash_reason=None) -> tuple[str, str]:
    """Did the path still hold the bytes that were checked?"""
    if before is None or after is None:
        return "unknown", "observation_missing"
    if sha_before is None or sha_after is None:
        # The reason the hash never finished (cancel, bytes_limit) is more
        # useful than the flat "not affordable", which also covered a stop.
        return "unknown", hash_reason or "hash_not_affordable"
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

    A decode is only "ok" when its receipt is bound to the bytes this check
    hashed (content.state == "observed", content.sha256 == sha256_before).  A
    contradicting observation - a structure walk or a closing hash that saw
    different bytes - demotes it to undecided with content_changed, which is the
    one verdict an A->B->A swap cannot produce.
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
    hash_before_reason = None
    if not refused and size is not None and size <= outer.max_bytes:
        refused = command.enter("version_before")
        if not refused:
            sha_before, hash_before_reason = _sha256(target, command.meter())

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
    hash_after_reason = None
    if sha_before is not None and not command.refusal():
        refused = command.enter("version_after")
        if not refused:
            try:
                sha_after, hash_after_reason = _sha256(target, command.meter())
            except OSError:
                sha_after = None
    after, _after_error = _observe(target)
    stability, stability_reason = _stability(
        before, after, sha_before, sha_after,
        hash_before_reason or hash_after_reason,
    )

    # The content object and the version hashes answer one question from two
    # directions.  Two full-file digests that disagree mean no single version
    # was certified, so a decode "ok" cannot describe the file at this path:
    # that is the A->B->A window the old two-hash scheme could not see, because
    # both of its hashes could observe the same restored bytes while the decode
    # in between read something else.
    content = dict(report.get("content") or {})
    digests = {value for value in (content.get("sha256"), sha_before, sha_after) if value}
    if content.get("state") == "observed" and len(digests) > 1:
        content["state"] = "mismatch"
        content["reason"] = "content_changed"
    report["content"] = content
    report["content_bound"] = content.get("state") == "observed"
    decode = report["quality"]["decode"]
    if decode["state"] == "ok":
        if content.get("state") == "mismatch":
            reason = "content_changed"
        elif content.get("state") != "observed":
            reason = "content_unbound"
        elif sha_before is None or content.get("sha256") != sha_before:
            # No version hash to compare with: the decode names bytes nobody
            # took a version of, which is unproven rather than a pass.
            reason = "content_unbound" if sha_before is None else "content_changed"
        elif sha_after is not None and sha_after != content.get("sha256"):
            reason = "content_changed"
        else:
            reason = ""
        if reason:
            report["quality"]["decode"] = Dimension("undecided", reason).as_dict()
            report["quality"]["pages"] = _pages_after(
                report["quality"]["structure"]["state"], "undecided"
            ).as_dict()

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

