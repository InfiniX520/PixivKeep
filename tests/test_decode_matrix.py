"""Cross-module decode matrix: structure, gate, limits and read-only proof.

Everything here is a synthetic sample built by tests/png_samples.py, so the
matrix never touches a real gallery file.  Each case states which dimension it
is exercising; a case that cannot reach the decode question says so instead of
being counted as a decode result.
"""
from __future__ import annotations

import json

import pytest

import png_samples as samples
from core import quality_check
from core.decode_limits import DecodeLimits
from core.quality_evidence import QualityEvidence


def _write(tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _run(tmp_path, name, data, **kwargs):
    report = quality_check.check_file(_write(tmp_path, name, data), **kwargs)
    QualityEvidence.from_dict(report["quality"])  # the shape must survive a round trip
    return report


def test_a_valid_png_decodes_and_proves_the_bytes_stayed(tmp_path):
    report = _run(tmp_path, "valid.png", samples.build_valid_rgba(), decode_policy="strict")
    assert report["quality"]["structure"]["state"] == "ok"
    assert report["quality"]["decode"]["state"] == "ok"
    assert report["quality"]["pages"]["state"] == "all_frames"
    assert report["receipt"]["outcome"] == "ok"
    assert report["receipt"]["decoder"] == "stdlib-zlib-png"
    assert report["unchanged"] is True
    assert report["sha256_before"] == report["sha256_after"]


def test_an_interlaced_png_decodes_every_pass(tmp_path):
    report = _run(tmp_path, "interlaced.png", samples.build_interlaced(), decode_policy="strict")
    assert report["quality"]["decode"]["state"] == "ok"
    assert report["receipt"]["frames"] == 1
    assert report["unchanged"] is True


def test_the_structure_check_rejects_a_bad_crc_before_any_decode(tmp_path):
    report = _run(tmp_path, "bad_crc.png", samples.build_bad_crc(), decode_policy="strict")
    assert report["quality"]["structure"]["state"] == "failed"
    # The decode question was never reached; saying "failed" twice would blur
    # which check found the problem.
    assert report["quality"]["decode"]["state"] == "undecided"
    assert report["quality"]["decode"]["reason"] == "structure_not_ok"
    assert report["receipt"] is None
    assert report["unchanged"] is True


def test_a_truncated_png_never_looks_decoded(tmp_path):
    report = _run(tmp_path, "truncated.png", samples.build_truncated(), decode_policy="strict")
    assert report["quality"]["structure"]["state"] == "failed"
    assert report["quality"]["pages"]["state"] == "unknown"


def test_a_missing_iend_is_a_structure_failure(tmp_path):
    report = _run(tmp_path, "no_iend.png", samples.build_missing_iend(), decode_policy="strict")
    assert report["quality"]["structure"]["state"] == "failed"
    assert report["quality"]["decode"]["state"] == "undecided"


def test_an_oversized_header_stops_at_the_limit_not_as_damage(tmp_path):
    report = _run(tmp_path, "oversized.png", samples.build_oversized_header(), decode_policy="strict")
    assert report["quality"]["structure"]["state"] == "undecided"
    assert report["quality"]["structure"]["reason"] == "png_expansion_limit"


def test_a_structurally_valid_gif_without_a_decoder_says_capability_unavailable(tmp_path):
    report = _run(tmp_path, "tiny.gif", samples.build_minimal_gif(), decode_policy="strict")
    assert report["quality"]["structure"]["state"] == "ok"
    assert report["quality"]["decode"]["state"] == "capability_unavailable"
    assert report["quality"]["decode"]["reason"] == "no_decoder_component"
    assert report["receipt"]["outcome"] == "capability_unavailable"
    assert report["receipt"]["decoder"] == "none"


def test_header_only_samples_are_rejected_by_the_structure_check(tmp_path):
    for name, builder in (
        ("header.gif", samples.build_gif_header),
        ("header.jpg", samples.build_jpeg_header),
        ("header.webp", samples.build_webp_header),
    ):
        report = _run(tmp_path, name, builder(), decode_policy="strict")
        assert report["quality"]["structure"]["state"] == "failed", name
        assert report["quality"]["decode"]["state"] == "undecided", name


def test_the_structure_policy_does_not_claim_a_decode(tmp_path):
    report = _run(tmp_path, "valid.png", samples.build_valid_rgba())
    assert report["quality"]["decode"]["state"] == "undecided"
    assert report["quality"]["decode"]["reason"] == "not_requested"
    assert report["quality"]["pages"]["state"] == "unknown"
    assert report["receipt"] is None


def test_a_cancelled_check_is_undecided_not_damaged(tmp_path):
    class Stop:
        def is_set(self):
            return True

    report = _run(tmp_path, "valid.png", samples.build_valid_rgba(), decode_policy="strict", stop=Stop())
    assert report["quality"]["structure"]["state"] == "undecided"
    assert report["quality"]["decode"]["state"] == "undecided"
    assert report["unchanged"] is True


def test_a_pixel_budget_stops_the_decode_without_calling_it_damage(tmp_path):
    limits = DecodeLimits(max_bytes=1 << 20, max_pixels=4, max_frames=10, max_seconds=5.0)
    report = _run(tmp_path, "valid.png", samples.build_valid_rgba(), decode_policy="strict", limits=limits)
    assert report["quality"]["structure"]["state"] == "ok"
    assert report["quality"]["decode"]["state"] == "undecided"
    assert report["quality"]["decode"]["reason"] == "pixels_limit"


def test_the_report_is_json_safe_and_keeps_five_dimensions(tmp_path):
    report = _run(tmp_path, "valid.png", samples.build_valid_rgba(), decode_policy="strict")
    json.dumps(report)
    assert sorted(report["quality"]) == ["decode", "pages", "source", "structure", "transport"]
    assert report["quality"]["decode"]["state"] != "ok" or report["quality"]["structure"]["state"] == "ok"


def test_an_unknown_policy_is_refused(tmp_path):
    path = _write(tmp_path, "valid.png", samples.build_valid_rgba())
    with pytest.raises(ValueError):
        quality_check.quality_evidence(path, decode_policy="paranoid")
