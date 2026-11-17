"""RA-06: a decode verdict must name the bytes the decoder actually read.

The old scheme hashed the path, decoded the path, then hashed the path again.
All three are separate opens, so an A->B->A swap during the decode left every
observation looking at A while the decoder read B: two equal hashes, a passing
decode and no evidence of the swap.  These tests drive the public entry points
(check_file, quality_evidence, the decode-check CLI) and assert the content
object the contract freezes (docs/optimization/2026-11-17/S0_CONTRACTS_R3.md
section 2), not the internals of the pass that produces it.

Everything here is synthetic: temp files, PNGs built in memory, no network.
"""
import json
import os
import struct
import threading
import zlib

import pytest

from core import library_maintenance as maintenance
from core import quality_check
from core.decode_limits import DecodeLimits


def _chunk(kind, data):
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))


def indexed_png(pixel_index, palette=1):
    """A 1x1 8-bit indexed PNG.  index >= palette is a real decode failure."""
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 3, 0, 0, 0))
            + _chunk(b"PLTE", b"\x00\x00\x00" * palette)
            + _chunk(b"IDAT", zlib.compress(bytes([0, pixel_index])))
            + _chunk(b"IEND", b""))


def rgb_png(red):
    """A 1x1 8-bit RGB PNG whose only pixel differs from its sibling."""
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(bytes([0, red, 0, 0])))
            + _chunk(b"IEND", b""))


BAD = indexed_png(1)
GOOD = indexed_png(0)
assert len(BAD) == len(GOOD)


def write(tmp_path, name, payload):
    path = tmp_path / name
    path.write_bytes(payload)
    return path


def reads_of(*targets):
    """A context manager that counts the bytes read from these paths."""
    import contextlib
    import unittest.mock as mock
    from pathlib import Path

    wanted = {Path(target) for target in targets}
    reads = []
    original = Path.open

    class Reader:
        def __init__(self, stream):
            self._stream = stream

        def read(self, *args):
            data = self._stream.read(*args)
            reads.append(len(data))
            return data

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self._stream.__exit__(*exc)

        def __getattr__(self, name):
            return getattr(self._stream, name)

    def opened(self, *args, **kwargs):
        stream = original(self, *args, **kwargs)
        return Reader(stream) if Path(self) in wanted else stream

    @contextlib.contextmanager
    def counted():
        with mock.patch.object(Path, "open", opened):
            yield reads

    return counted()


# --- the object the contract freezes -----------------------------------


def test_a_clean_file_names_one_content_version(tmp_path):
    path = write(tmp_path, "clean.png", GOOD)
    size = path.stat().st_size
    report = quality_check.check_file(path, decode_policy="strict")
    content = report["content"]
    assert set(content) == {"state", "sha256", "bytes", "complete", "source", "reason"}
    assert content["state"] == "observed"
    assert content["complete"] is True
    assert content["bytes"] == size
    assert content["source"] == "file_stream"
    assert content["reason"] == ""
    # One version, seen by every reader: the structure walk, the decode pass and
    # both version hashes agree.
    assert content["sha256"] == report["sha256"]
    assert content["sha256"] == report["sha256_before"] == report["sha256_after"]
    assert report["content_bound"] is True
    assert report["quality"]["decode"]["state"] == "ok"
    assert maintenance._decode_outcome([report]) == 0


def test_the_content_state_vocabulary_is_the_one_the_contract_names():
    assert quality_check.CONTENT_STATES == ("observed", "unknown", "mismatch")


def test_quality_evidence_alone_still_names_the_bytes_it_read(tmp_path):
    path = write(tmp_path, "clean.png", GOOD)
    report = quality_check.quality_evidence(path, decode_policy="strict")
    assert report["content"]["state"] == "observed"
    assert report["content"]["sha256"] == report["sha256"]
    assert report["content"]["complete"] is True


# --- the window the old two-hash scheme could not see ------------------


def test_an_aba_swap_during_the_decode_is_not_a_pass(tmp_path, monkeypatch):
    """A bad file, swapped for a good one only while the decoder runs.

    The disk holds the bad bytes again afterwards, with the original mtime, so
    both version hashes still agree - and the receipt is still not about the
    file.  The digest of the bytes the decoder read is the only witness.
    """
    path = write(tmp_path, "same.png", BAD)
    baseline = quality_check.check_file(path, decode_policy="strict")
    assert baseline["quality"]["decode"]["state"] == "failed"
    stamp = path.stat()
    real = quality_check._decode

    def swap_during_decode(*args, **kwargs):
        path.write_bytes(GOOD)
        try:
            return real(*args, **kwargs)
        finally:
            path.write_bytes(BAD)
            os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))

    monkeypatch.setattr(quality_check, "_decode", swap_during_decode)
    report = quality_check.check_file(path, decode_policy="strict")
    assert path.read_bytes() == BAD
    assert report["content"]["state"] == "mismatch"
    assert report["content"]["reason"] == "content_changed"
    assert report["quality"]["decode"] == {"state": "undecided",
                                           "reason": "content_changed"}
    # The two version hashes really do agree here: that is why the content
    # digest, and not a third path check, has to decide.
    assert report["sha256_before"] == report["sha256_after"]
    assert report["path_stability"] == "unchanged"
    assert report["content_bound"] is False
    assert maintenance._decode_outcome([report]) != 0


def test_a_rewrite_before_the_decode_leaves_the_old_verdict_unusable(tmp_path, monkeypatch):
    path = write(tmp_path, "clean.png", GOOD)
    real = quality_check._decode
    replacement = rgb_png(1)

    def rewrite_then_decode(*args, **kwargs):
        path.write_bytes(replacement)
        return real(*args, **kwargs)

    monkeypatch.setattr(quality_check, "_decode", rewrite_then_decode)
    report = quality_check.check_file(path, decode_policy="strict")
    assert report["content"]["state"] == "mismatch"
    assert report["quality"]["decode"]["state"] == "undecided"
    assert maintenance._decode_outcome([report]) != 0


def test_the_structure_walk_and_the_decode_must_see_the_same_bytes(tmp_path, monkeypatch):
    """Swap the file for another valid image between the two readers.

    The version hashes agree (the file is restored, mtime included) and each
    reader saw a perfectly valid image - but not the same one, so there is no
    single content version to certify.
    """
    path = write(tmp_path, "clean.png", GOOD)
    stamp = path.stat()
    other = rgb_png(0)
    real_structure = quality_check._structure

    def walk_something_else(*args, **kwargs):
        path.write_bytes(other)
        try:
            return real_structure(*args, **kwargs)
        finally:
            path.write_bytes(GOOD)
            os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))

    monkeypatch.setattr(quality_check, "_structure", walk_something_else)
    report = quality_check.check_file(path, decode_policy="strict")
    assert report["sha256_before"] == report["sha256_after"]
    assert report["content"]["state"] == "mismatch"
    assert report["content"]["reason"] == "content_changed"
    assert report["quality"]["decode"]["state"] == "undecided"
    assert maintenance._decode_outcome([report]) != 0


def test_a_decode_that_never_read_the_pass_cannot_be_ok(tmp_path, monkeypatch):
    """A decoder that opens the path itself names bytes this check never hashed.

    Its receipt may look perfect - a frame, a pixel, a byte count - but nothing
    ties it to the bytes the version hashes describe, so it is unproven.
    """
    path = write(tmp_path, "clean.png", GOOD)
    size = path.stat().st_size
    from core.quality_evidence import DecodeReceipt, Dimension

    def decode_elsewhere(*args, **kwargs):
        receipt = DecodeReceipt(
            format="png", frames=1, pixels=1,
            limits={"max_bytes": size, "max_pixels": 1, "max_frames": 1,
                    "max_seconds": 1.0},
            decoder="third-party", outcome="ok", seconds=0.0,
            checked_at="2026-11-17T00:00:00+00:00", bytes_read=size,
        )
        return Dimension("ok"), receipt

    monkeypatch.setattr(quality_check, "_decode", decode_elsewhere)
    report = quality_check.check_file(path, decode_policy="strict")
    assert report["content"]["state"] == "unknown"
    assert report["content"]["reason"] == "decode_stream_not_observed"
    assert report["quality"]["decode"] == {"state": "undecided",
                                           "reason": "content_unbound"}
    assert maintenance._decode_outcome([report]) != 0


# --- the pass covers the whole file, once ------------------------------


def _receipt(bytes_read, outcome="ok"):
    from core.quality_evidence import DecodeReceipt

    return DecodeReceipt(
        format="png", frames=1 if outcome == "ok" else None,
        pixels=1 if outcome == "ok" else None,
        limits={"max_bytes": 1, "max_pixels": 1, "max_frames": 1,
                "max_seconds": 1.0},
        decoder="stdlib-zlib-png", outcome=outcome, seconds=0.0,
        checked_at="2026-11-17T00:00:00+00:00", bytes_read=bytes_read,
    )


def test_a_decoder_that_stops_early_still_names_the_whole_file(tmp_path, monkeypatch):
    """A decoder may stop before the last byte; the version still covers it all.

    The structure walk has already read every byte (a PNG with trailing data is
    rejected there), so the pass finishes the file itself, through the same
    budget, and the digest is of the whole file and not of the prefix.
    """
    path = write(tmp_path, "clean.png", GOOD)
    size = path.stat().st_size
    from core.quality_evidence import Dimension

    def read_a_prefix(*args, **kwargs):
        kwargs["stream"].read(16)
        return Dimension("ok"), _receipt(16)

    monkeypatch.setattr(quality_check, "_decode", read_a_prefix)
    with reads_of(path) as reads:
        report = quality_check.check_file(path, decode_policy="strict")
    content = report["content"]
    assert content["state"] == "observed"
    assert content["complete"] is True
    assert content["bytes"] == size
    assert content["sha256"] == report["sha256_before"] == report["sha256"]
    # 16 bytes charged on the receipt, the remaining size-16 through the meter:
    # the pass is charged once and the total is four real passes.
    assert report["budget"]["bytes"] == sum(reads) == 4 * size
    assert maintenance._decode_outcome([report]) == 0


def test_a_cancel_in_the_rest_of_the_file_leaves_the_content_unknown(tmp_path, monkeypatch):
    path = write(tmp_path, "clean.png", GOOD)
    stop = threading.Event()
    from core.quality_evidence import Dimension

    def read_a_prefix_then_cancel(*args, **kwargs):
        kwargs["stream"].read(16)
        stop.set()
        return Dimension("ok"), _receipt(16)

    monkeypatch.setattr(quality_check, "_decode", read_a_prefix_then_cancel)
    report = quality_check.check_file(path, decode_policy="strict", stop=stop)
    assert report["content"]["state"] == "unknown"
    assert report["content"]["complete"] is False
    assert report["content"]["reason"] == "cancel"
    assert report["quality"]["decode"] == {"state": "undecided",
                                           "reason": "content_unbound"}
    assert maintenance._decode_outcome([report]) != 0


def test_a_failed_decode_names_no_content_version(tmp_path):
    path = write(tmp_path, "bad.png", BAD)
    report = quality_check.check_file(path, decode_policy="strict")
    assert report["quality"]["decode"]["state"] == "failed"
    assert report["content"]["state"] == "unknown"
    assert report["content"]["sha256"] is None
    assert report["content"]["complete"] is False
    assert report["content"]["reason"] == "decode_failed"
    assert maintenance._decode_outcome([report]) == 2


def test_a_refused_check_carries_the_same_content_shape(tmp_path):
    path = write(tmp_path, "clean.png", GOOD)
    size = path.stat().st_size
    report = quality_check.check_file(
        path, decode_policy="strict", limits=DecodeLimits(max_bytes=size - 1)
    )
    assert set(report["content"]) == {"state", "sha256", "bytes", "complete",
                                      "source", "reason"}
    assert report["content"]["state"] == "unknown"
    assert report["content"]["reason"] == "bytes_limit"
    assert report["content_bound"] is False


# --- the CLI answers the same question as the API -----------------------


def test_the_cli_refuses_an_aba_swap_and_says_so(capsys, tmp_path, monkeypatch):
    path = write(tmp_path, "same.png", BAD)
    stamp = path.stat()
    real = quality_check._decode

    def swap_during_decode(*args, **kwargs):
        path.write_bytes(GOOD)
        try:
            return real(*args, **kwargs)
        finally:
            path.write_bytes(BAD)
            os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))

    monkeypatch.setattr(quality_check, "_decode", swap_during_decode)
    code = maintenance.main(["decode-check", str(path), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == payload["exit_code"] != 0
    entry = payload["files"][0]
    assert entry["quality"]["decode"] == {"state": "undecided",
                                          "reason": "content_changed"}
    assert entry["content"]["state"] == "mismatch"
    assert entry["content_bound"] is False
