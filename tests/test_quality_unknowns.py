"""Re-audit regressions: unmeasured is not zero, and errors stay bounded.

Three findings from the read-only adversary are pinned here:

* P2-3 a receipt used to write `frames=0 pixels=0 seconds=0.0` when nothing was
  measured at all; here every "nothing ran" case must say `None`;
* P3-3 an artifact that disappears after the structure question crashed with a
  bare `OSError`; it must become an unreadable dimension instead;
* P3-4 a decoder exception used to be pasted into the receipt verbatim; it must
  become a bounded, content-free code.

Expectations are written from the samples themselves (a 4x4 PNG has 16 pixels),
never from the module under test.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import png_samples as samples
from core import decoder_registry, quality_check
from core.decode_limits import DecodeLimits
from core.quality_evidence import DecodeReceipt


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _strict(path, **kwargs):
    return quality_check.quality_evidence(path, decode_policy="strict", **kwargs)


# --- P2-3: nothing measured is None, never zero ------------------------------


def test_an_unavailable_decoder_measures_nothing(tmp_path):
    report = _strict(_write(tmp_path, "tiny.gif", samples.build_minimal_gif()))
    assert report["quality"]["decode"]["state"] == "capability_unavailable"
    receipt = report["receipt"]
    assert receipt is not None
    assert receipt["frames"] is None
    assert receipt["pixels"] is None
    assert receipt["seconds"] is None
    # The old bug wrote zeros here; a zero would claim a measurement.
    assert receipt["frames"] != 0 or receipt["frames"] is None
    assert "null" in json.dumps(receipt)


def test_a_limited_decode_leaves_the_pixel_count_unknown(tmp_path):
    limits = DecodeLimits(max_bytes=1 << 20, max_pixels=4, max_frames=10, max_seconds=5.0)
    report = _strict(_write(tmp_path, "valid.png", samples.build_valid_rgba()), limits=limits)
    assert report["quality"]["decode"]["reason"] == "pixels_limit"
    receipt = report["receipt"]
    assert receipt["pixels"] is None
    # A stopped decode produced no frame count either: png_decode's 0 is a
    # placeholder, not a measurement, so the receipt says unknown.
    assert receipt["frames"] is None
    assert receipt["seconds"] is not None and receipt["seconds"] >= 0.0


def test_a_structure_failure_produces_no_receipt_at_all(tmp_path):
    report = _strict(_write(tmp_path, "bad_crc.png", samples.build_bad_crc()))
    assert report["quality"]["structure"]["state"] == "failed"
    assert report["quality"]["decode"]["reason"] == "structure_not_ok"
    # Nothing was attempted, so there is no receipt to make a false claim in.
    assert report["receipt"] is None


def test_a_real_decode_reports_the_numbers_the_sample_has(tmp_path):
    report = _strict(_write(tmp_path, "valid.png", samples.build_valid_rgba()))
    receipt = report["receipt"]
    assert receipt["frames"] == 1
    assert receipt["pixels"] == 4 * 4  # the sample is 4x4 RGBA, computed by hand
    assert receipt["seconds"] > 0.0
    assert receipt["outcome"] == "ok"
    json.dumps(report)


def test_a_receipt_may_say_unknown_but_not_negative_or_boolean():
    base = dict(
        format="png",
        limits={"max_bytes": 10},
        decoder="stdlib-zlib-png",
        outcome="undecided",
        checked_at="2026-11-14T00:00:00+00:00",
    )
    assert DecodeReceipt(frames=None, pixels=None, seconds=None, **base).as_dict()["pixels"] is None
    for bad in (-1, True):
        with pytest.raises(Exception):
            DecodeReceipt(frames=bad, pixels=None, seconds=None, **base)
    with pytest.raises(Exception):
        DecodeReceipt(frames=None, pixels=None, seconds=-0.5, **base)


# --- P3-3: a vanished artifact is unreadable, not a crash -------------------


def test_a_missing_file_is_an_unreadable_dimension(tmp_path):
    report = _strict(tmp_path / "absent.png")
    assert report["quality"]["structure"]["state"] == "failed"
    assert report["quality"]["structure"]["reason"].startswith("unreadable:")
    assert report["size"] is None
    assert report["receipt"] is None


def test_a_file_that_vanishes_before_the_decode_is_unreadable(tmp_path, monkeypatch):
    path = _write(tmp_path, "valid.png", samples.build_valid_rgba())
    real_inspect = quality_check.inspect_image

    def inspect_then_delete(target, **kwargs):
        evidence = real_inspect(target, **kwargs)
        Path(target).unlink()  # the artifact is gone after the structure question
        return evidence

    monkeypatch.setattr(quality_check, "inspect_image", inspect_then_delete)
    report = quality_check.quality_evidence(path, decode_policy="strict")
    assert report["quality"]["structure"]["state"] == "ok"
    assert report["quality"]["decode"]["state"] == "failed"
    assert report["quality"]["decode"]["reason"] == "unreadable:FileNotFoundError"
    receipt = report["receipt"]
    assert receipt["outcome"] == "failed"
    assert receipt["frames"] is None and receipt["pixels"] is None and receipt["seconds"] is None
    assert report["size"] == len(samples.build_valid_rgba())  # from the evidence, not a stat


# --- P3-4: a decoder error is a bounded code --------------------------------


class _FakeDecoder:
    name = "fake"
    formats = ("png",)

    def __init__(self, *, error=None, result=None):
        self._error = error
        self._result = result

    def decode(self, path, *, limits, stop):
        if self._error is not None:
            raise self._error
        return self._result


def test_a_decoder_error_keeps_the_type_and_errno_only():
    secret = "/home/someone/private/album.png"
    error = OSError(28, "no space for " + secret)
    outcome = decoder_registry.decode_with(
        _FakeDecoder(error=error), "x.png", policy="strict"
    )
    assert outcome.decode == decoder_registry.DECODE_FAILED
    assert outcome.reason == "decoder_error:OSError:errno-28"
    assert secret not in outcome.reason
    assert "/" not in outcome.reason and "\n" not in outcome.reason
    assert len(outcome.reason) <= 96


def test_a_long_decoder_reason_is_cut_with_a_visible_marker():
    outcome = decoder_registry.decode_with(
        _FakeDecoder(result={"decode": "undecided", "reason": "x" * 500}),
        "x.png",
        policy="strict",
    )
    assert len(outcome.reason) == decoder_registry.REASON_LIMIT
    assert outcome.reason.endswith(decoder_registry.TRUNCATED)
    assert set(outcome.reason[:-len(decoder_registry.TRUNCATED)]) == {"x"}


def test_a_weird_exception_name_falls_back_to_exception():
    odd = type("Bad Name\nwith newline", (Exception,), {})
    outcome = decoder_registry.decode_with(_FakeDecoder(error=odd()), "x.png", policy="strict")
    assert outcome.reason == "decoder_error:Exception"
