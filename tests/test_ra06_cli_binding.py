"""The CLI answer has to follow the content block, not only the decode state.

The binding itself is proved in tests/test_ra06_content_binding.py.  This file
covers the seam the audit round left open: a report that says "decode ok" while
its content block cannot name the bytes is not a pass, and neither the exit code
nor the human text may present it as one (RA-06, S0_CONTRACTS_R3 section 2).
"""
from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

import pytest

from core import library_maintenance as m
from core.quality_check import check_file


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _png(pixel_index: int) -> bytes:
    """A 1x1 indexed PNG; an out-of-range pixel index is a real decode failure."""
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 3, 0, 0, 0))
        + _chunk(b"PLTE", b"\0\0\0")
        + _chunk(b"IDAT", zlib.compress(bytes([0, pixel_index])))
        + _chunk(b"IEND", b"")
    )


def _unbound(report: dict) -> dict:
    """The same report as if the content block could not name the bytes.

    This is the shape check_file() produces when its four observations disagree
    or when the read never completed; it is built by hand here so the wiring can
    be tested without arranging a race inside the decoder.
    """
    forged = dict(report)
    forged["content"] = {
        "state": "unknown",
        "sha256": None,
        "bytes": None,
        "complete": False,
        "source": "file_stream",
        "reason": "content_unbound",
    }
    forged["content_bound"] = False
    return forged


def test_a_bound_pass_is_zero_and_says_which_bytes(tmp_path):
    target = tmp_path / "good.png"
    target.write_bytes(_png(0))
    report = check_file(target, decode_policy="strict")
    assert report["content"]["state"] == "observed"
    assert report["content"]["complete"] is True
    assert report["content_bound"] is True
    assert report["quality"]["decode"]["state"] == "ok"
    assert m._decode_outcome([report]) == 0
    text = "\n".join(m._decode_lines([report], 0))
    assert "内容: 已绑定" in text
    assert report["content"]["sha256"][:12] in text


def test_an_ok_decode_whose_content_is_unknown_is_not_a_pass(tmp_path):
    target = tmp_path / "good.png"
    target.write_bytes(_png(0))
    report = _unbound(check_file(target, decode_policy="strict"))
    assert report["quality"]["decode"]["state"] == "ok"
    assert report["path_stability"] == "unchanged"
    assert m._decode_outcome([report]) == 5


def test_the_text_says_unbound_and_keeps_the_reason(tmp_path):
    target = tmp_path / "good.png"
    target.write_bytes(_png(0))
    report = _unbound(check_file(target, decode_policy="strict"))
    text = "\n".join(m._decode_lines([report], 5))
    assert "内容: 未绑定" in text
    assert "content_unbound" in text
    assert "已绑定" not in text


def test_a_decode_failure_still_wins_over_the_content_question(tmp_path):
    # A file the decoder rejected is known bad (2): the content block cannot make
    # a rejected file look like "we could not judge it".
    target = tmp_path / "bad.png"
    target.write_bytes(_png(1))
    report = check_file(target, decode_policy="strict")
    assert report["quality"]["decode"]["state"] == "failed"
    assert m._decode_outcome([report]) == 2


def test_the_cli_exit_code_and_its_json_agree(tmp_path, capsys):
    good = tmp_path / "good.png"
    bad = tmp_path / "bad.png"
    good.write_bytes(_png(0))
    bad.write_bytes(_png(1))

    assert m.main(["decode-check", str(good), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_code"] == 0
    assert payload["files"][0]["content_bound"] is True

    assert m.main(["decode-check", str(bad), "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_code"] == 2
    assert payload["files"][0]["quality"]["decode"]["state"] == "failed"


def test_the_cli_text_carries_the_content_line(tmp_path, capsys):
    good = tmp_path / "good.png"
    good.write_bytes(_png(0))
    assert m.main(["decode-check", str(good)]) == 0
    text = capsys.readouterr().out
    assert "内容: 已绑定" in text
