"""W2: the budget must account for every byte the command really reads.

Each test drives the public entry points (check_file, quality_evidence,
inspect_image, the decode-check CLI) and counts the bytes the *stream* gave
through a real wrapper around Path.open.  No test replaces _sha256 or the
decoder wholesale to "prove" cancellability: a mocked reader cannot show that
a read was bounded.

Frozen reference: docs/audits/2026-11-15-u1u2-e1-independent/evidence/
test_boundaries.py (the two ea02 cases).  This file adds the cases that
boundary file does not carry: each stage exhausted, cancel and clock expiry,
decode receipts without a byte count, and the multi-file CLI.
"""
import contextlib
import json
import struct
import threading
import unittest.mock as mock
import zlib
from pathlib import Path

import pytest

from core import library_maintenance as maintenance
from core import quality_check
from core.decode_limits import (
    FILE_SCOPE,
    CommandBudget,
    DecodeLimits,
    ReadMeter,
)
from core.image_validation import ImageValidationStopped, inspect_image
from core.quality_evidence import DecodeReceipt, Dimension


def _chunk(kind: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))


def png_bytes(width: int = 1, height: int = 1, color: int = 0, bits: int = 8) -> bytes:
    channels = {0: 1, 2: 3, 4: 2, 6: 4}[color]
    rows = b"".join(b"\x00" + bytes(width * channels * bits // 8)
                    for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, bits, color,
                                          0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(rows))
            + _chunk(b"IEND", b""))


@contextlib.contextmanager
def counted_reads(*targets: Path, stop=None, stop_after_first=False):
    """Count the bytes read from these paths, through the real stream.

    With stop= the wrapper can also set a cancel flag from inside the first
    read, which is the only way to prove that a read loop notices it *during*
    the file instead of after the whole file is in memory.
    """
    wanted = {Path(target) for target in targets}
    reads: list[int] = []
    original = Path.open

    class Reader:
        def __init__(self, stream):
            self._stream = stream

        def read(self, *args):
            data = self._stream.read(*args)
            reads.append(len(data))
            if stop is not None and stop_after_first and data:
                stop.set()
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

    with mock.patch.object(Path, "open", opened):
        yield reads


def write(tmp_path, name, payload) -> Path:
    path = tmp_path / name
    path.write_bytes(payload)
    return path


def receipt(**overrides) -> DecodeReceipt:
    values = dict(
        format="png",
        frames=1,
        pixels=1,
        limits={"max_bytes": 1, "max_pixels": 1, "max_frames": 1, "max_seconds": 1.0},
        decoder="stdlib-zlib-png",
        outcome="ok",
        seconds=0.0,
        checked_at="2026-11-15T00:00:00+00:00",
        bytes_read=1,
    )
    values.update(overrides)
    return DecodeReceipt(**values)


# --- the label ---------------------------------------------------------


def test_the_budget_scope_is_one_file_call():
    budget = CommandBudget(DecodeLimits(max_bytes=100, max_seconds=60.0))
    assert budget.scope == FILE_SCOPE == "file_call"
    assert budget.snapshot()["scope"] == "file_call"
    # Two budgets never share counters: ten files are ten of these, not one
    # command-wide pool.
    other = CommandBudget(DecodeLimits(max_bytes=100, max_seconds=60.0))
    assert budget.charge(40) is None
    assert budget.snapshot()["bytes"] == 40 and other.snapshot()["bytes"] == 0


# --- the meter ---------------------------------------------------------


def test_a_read_is_clamped_to_what_is_left():
    budget = CommandBudget(DecodeLimits(max_bytes=100, max_seconds=60.0))
    meter = budget.meter()
    assert isinstance(meter, ReadMeter)
    assert meter.size(4096) == 100          # clamped before the read
    assert meter.spent(40) is None
    assert meter.size(4096) == 60
    assert meter.spent(60) is None          # exactly the ceiling is still legal
    assert meter.size(1) == 0               # 0 means "stop", never "EOF"
    assert budget.snapshot()["bytes"] == 100


def test_the_meter_polls_the_stop_flag_and_the_clock():
    stop = threading.Event()
    budget = CommandBudget(DecodeLimits(max_bytes=100, max_seconds=60.0), stop=stop)
    assert budget.meter().poll() is None
    stop.set()
    assert budget.meter().poll() == "cancel"
    now = {"seconds": 0.0}
    timed = CommandBudget(DecodeLimits(max_bytes=100, max_seconds=1.0),
                          clock=lambda: now["seconds"])
    assert timed.meter().poll() is None
    now["seconds"] = 5.0
    assert timed.meter().poll() == "time_limit"


# --- the total equals the real reads -----------------------------------


def test_a_successful_check_reports_exactly_the_bytes_it_read(tmp_path):
    path = write(tmp_path, "valid.png", png_bytes())
    size = path.stat().st_size
    with counted_reads(path) as reads:
        report = quality_check.check_file(path, decode_policy="strict")
    assert report["quality"]["decode"]["state"] == "ok"
    assert report["path_stability"] == "unchanged"
    # Four passes: version_before, structure, decode, version_after.
    assert report["budget"]["bytes"] == sum(reads) > 3 * size
    assert report["budget"]["scope"] == "file_call"


def test_a_corrupt_png_fails_and_is_still_accounted(tmp_path):
    # Chunks with correct CRCs whose pixel rows use filter type 5.  The
    # structure walk inflates the same stream and rejects it, so the decode
    # stage never runs - and the bytes it took to find that out are still on
    # the receipt.
    header = _chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 0, 0, 0, 0))
    path = write(tmp_path, "broken.png",
                 b"\x89PNG\r\n\x1a\n" + header
                 + _chunk(b"IDAT", zlib.compress(b"\x05\x00\x00" * 2))
                 + _chunk(b"IEND", b""))
    with counted_reads(path) as reads:
        report = quality_check.check_file(path, decode_policy="strict")
    assert report["quality"]["structure"] == {"state": "failed",
                                              "reason": "png_invalid_filter"}
    assert report["quality"]["decode"] == {"state": "undecided",
                                           "reason": "structure_not_ok"}
    assert report["budget"]["bytes"] == sum(reads)
    assert maintenance._decode_outcome([report]) == 2


def test_an_unknown_format_reads_nothing_in_the_decode_stage(tmp_path):
    path = write(tmp_path, "note.bin", b"not an image, just text\n")
    with counted_reads(path) as reads:
        report = quality_check.check_file(path, decode_policy="strict")
    assert report["quality"]["structure"]["state"] == "failed"
    assert report["quality"]["decode"] == {"state": "undecided",
                                           "reason": "structure_not_ok"}
    assert report["receipt"] is None
    assert report["budget"]["bytes"] == sum(reads)


# --- ceilings ----------------------------------------------------------


def test_one_byte_less_than_the_file_refuses_before_any_read(tmp_path):
    path = write(tmp_path, "valid.png", png_bytes())
    size = path.stat().st_size
    with counted_reads(path) as reads:
        report = quality_check.check_file(
            path, decode_policy="strict", limits=DecodeLimits(max_bytes=size - 1)
        )
    assert reads == []
    assert report["budget"]["bytes"] == 0
    assert report["quality"]["structure"] == {"state": "undecided",
                                              "reason": "bytes_limit"}
    assert report["quality"]["decode"]["reason"] == "bytes_limit"
    assert report["content_version"]["state"] == "unknown"
    assert maintenance._decode_outcome([report]) != 0


def test_a_budget_that_runs_out_mid_check_still_matches_the_real_reads(tmp_path):
    path = write(tmp_path, "valid.png", png_bytes())
    size = path.stat().st_size
    # Enough for the first hash and the structure walk, not for both hashes
    # and the decode: the check must stop instead of reading past the ceiling.
    limit = 2 * size + 7
    with counted_reads(path) as reads:
        report = quality_check.check_file(
            path, decode_policy="strict", limits=DecodeLimits(max_bytes=limit)
        )
    assert sum(reads) <= limit
    assert report["budget"]["bytes"] == sum(reads)
    assert report["path_stability"] == "unknown"
    assert maintenance._decode_outcome([report]) != 0


def test_every_stage_can_be_the_one_that_runs_out(tmp_path):
    """The ceiling is honoured wherever it lands, not only in the decoder."""
    path = write(tmp_path, "valid.png", png_bytes())
    size = path.stat().st_size
    seen = {}
    for allowance in (size, 2 * size, 3 * size, 4 * size - 1):
        with counted_reads(path) as reads:
            report = quality_check.check_file(
                path, decode_policy="strict", limits=DecodeLimits(max_bytes=allowance)
            )
        assert sum(reads) <= allowance, allowance
        assert report["budget"]["bytes"] == sum(reads), allowance
        seen[allowance] = (report["quality"]["structure"].get("reason"),
                           report["quality"]["decode"].get("reason"),
                           report["path_stability"])
    # One pass runs out inside the structure walk, two inside the decode (the
    # decoder had an allowance and used it up), three and four-minus-one inside
    # the closing hash: the same ceiling, four different stages.
    assert seen[size][0] == "bytes_limit"
    assert seen[2 * size][1] == "bytes_limit"
    assert seen[3 * size] == (None, None, "unknown")
    assert seen[4 * size - 1] == (None, None, "unknown")


def test_the_first_hash_alone_can_run_out(tmp_path):
    path = write(tmp_path, "valid.png", png_bytes())
    size = path.stat().st_size
    budget = CommandBudget(DecodeLimits(max_bytes=size - 1, max_seconds=60.0))
    with counted_reads(path) as reads:
        digest, reason = quality_check._sha256(path, budget.meter())
    assert digest is None and reason == "bytes_limit"
    assert sum(reads) == size - 1
    assert budget.snapshot()["bytes"] == sum(reads)


def test_the_decoder_that_runs_out_reports_the_bytes_it_already_read(tmp_path):
    path = write(tmp_path, "valid.png", png_bytes())
    size = path.stat().st_size
    # Two full passes plus 30 bytes: enough for the PNG signature, IHDR and the
    # start of IDAT, not for the pixels.
    allowance = 2 * size + 30
    with counted_reads(path) as reads:
        report = quality_check.check_file(
            path, decode_policy="strict", limits=DecodeLimits(max_bytes=allowance)
        )
    assert sum(reads) == allowance
    assert report["budget"]["bytes"] == sum(reads)
    assert report["quality"]["decode"] == {"state": "undecided",
                                           "reason": "bytes_limit"}
    # The bytes the decoder had already taken are on the receipt, so the total
    # is the real work and not only the completed work.
    assert report["receipt"]["bytes_read"] == 30
    assert maintenance._decode_outcome([report]) != 0


def test_exactly_enough_budget_finishes_and_one_byte_less_does_not(tmp_path):
    """The boundary is exact: four passes of the file is a pass, minus one is not."""
    path = write(tmp_path, "valid.png", png_bytes())
    size = path.stat().st_size
    with counted_reads(path) as reads:
        exact = quality_check.check_file(
            path, decode_policy="strict", limits=DecodeLimits(max_bytes=4 * size)
        )
    assert exact["quality"]["structure"]["state"] == "ok"
    assert exact["quality"]["decode"]["state"] == "ok"
    assert exact["path_stability"] == "unchanged"
    assert exact["budget"]["bytes"] == sum(reads) == 4 * size
    assert maintenance._decode_outcome([exact]) == 0

    with counted_reads(path) as short:
        under = quality_check.check_file(
            path, decode_policy="strict", limits=DecodeLimits(max_bytes=4 * size - 1)
        )
    assert under["budget"]["bytes"] == sum(short) == 4 * size - 1
    assert under["path_stability"] == "unknown"
    assert maintenance._decode_outcome([under]) != 0


def test_zero_allowance_left_is_zero_reads_and_not_unknown(tmp_path):
    path = write(tmp_path, "valid.png", png_bytes())
    size = path.stat().st_size
    with counted_reads(path) as reads:
        report = quality_check.check_file(
            path, decode_policy="strict", limits=DecodeLimits(max_bytes=size)
        )
    assert report["budget"]["bytes"] == sum(reads)
    assert report["budget"]["bytes"] is not None
    assert "unknown_reason" not in report["budget"]


# --- cancel and clock --------------------------------------------------


def test_a_cancel_during_the_hash_stops_after_one_chunk(tmp_path):
    path = write(tmp_path, "big.bin", b"x" * (3 * 1024 * 1024))
    stop = threading.Event()
    # The cancel is set from inside the first read, i.e. while the hash is
    # running: the loop must notice it before asking for the next chunk.
    with counted_reads(path, stop=stop, stop_after_first=True) as reads:
        report = quality_check.check_file(path, decode_policy="strict", stop=stop)
    assert sum(reads) < path.stat().st_size
    assert report["budget"]["bytes"] == sum(reads)
    assert report["content_version"]["state"] == "unknown"
    assert report["path_stability"] == "unknown"
    assert maintenance._decode_outcome([report]) != 0


def test_the_hash_reads_in_bounded_chunks_and_polls_between_them(tmp_path):
    path = write(tmp_path, "big.bin", b"y" * (1024 * 1024 + 5))
    stop = threading.Event()
    budget = CommandBudget(DecodeLimits(max_bytes=4 * 1024 * 1024, max_seconds=60.0),
                           stop=stop)
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
        return Reader(stream) if Path(self) == path else stream

    with mock.patch.object(Path, "open", opened):
        digest, reason = quality_check._sha256(path, budget.meter())
    assert reason is None and digest is not None
    # 256 KiB chunks: four full chunks, then the five remaining bytes, then one
    # empty read that reports the end of the file.
    assert [count for count in reads if count] == [256 * 1024] * 4 + [5]
    assert max(reads) <= 256 * 1024
    assert sum(reads) == path.stat().st_size
    assert budget.snapshot()["bytes"] == sum(reads)


def test_the_clock_can_end_the_hash_mid_file(tmp_path):
    path = write(tmp_path, "big.bin", b"z" * (4 * 256 * 1024))
    now = {"seconds": 0.0}
    budget = CommandBudget(DecodeLimits(max_bytes=8 * 1024 * 1024, max_seconds=1.0),
                           clock=lambda: now["seconds"])
    calls = []
    original = Path.open

    class Reader:
        def __init__(self, stream):
            self._stream = stream

        def read(self, *args):
            data = self._stream.read(*args)
            calls.append(len(data))
            now["seconds"] = 5.0      # the chunk took longer than the ceiling
            return data

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self._stream.__exit__(*exc)

        def __getattr__(self, name):
            return getattr(self._stream, name)

    def opened(self, *args, **kwargs):
        stream = original(self, *args, **kwargs)
        return Reader(stream) if Path(self) == path else stream

    with mock.patch.object(Path, "open", opened):
        digest, reason = quality_check._sha256(path, budget.meter())
    assert digest is None and reason == "time_limit"
    assert sum(calls) < path.stat().st_size


def test_each_stage_refuses_to_start_after_a_cancel(tmp_path):
    path = write(tmp_path, "valid.png", png_bytes())
    stop = threading.Event()
    captured = {}
    real = quality_check._structure

    def structure_then_stop(*args, **kwargs):
        outcome = real(*args, **kwargs)
        stop.set()
        captured["structure"] = outcome[1]
        return outcome

    with mock.patch.object(quality_check, "_structure", structure_then_stop):
        report = quality_check.check_file(path, decode_policy="strict", stop=stop)
    assert captured["structure"].state == "ok"
    assert report["quality"]["decode"] == {"state": "undecided", "reason": "cancel"}
    assert report["path_stability"] == "unknown"


# --- receipts without a byte count -------------------------------------


def test_a_receipt_without_bytes_makes_the_total_unknown_not_zero(tmp_path):
    path = write(tmp_path, "valid.png", png_bytes())
    silent = receipt(bytes_read=None)

    def decoded(*args, **kwargs):
        return Dimension("ok"), silent

    with mock.patch.object(quality_check, "_decode", decoded):
        report = quality_check.check_file(path, decode_policy="strict")
    assert report["budget"]["bytes"] is None
    assert report["budget"]["unknown_reason"] == "decoder_did_not_report_bytes"
    assert report["quality"]["decode"] == {"state": "undecided",
                                           "reason": "decoder_did_not_report_bytes"}
    assert report["path_stability"] == "unknown"
    assert maintenance._decode_outcome([report]) != 0


def test_a_failed_decode_keeps_its_verdict_when_the_total_is_unknown(tmp_path):
    path = write(tmp_path, "valid.png", png_bytes())
    silent = receipt(bytes_read=None, outcome="failed", frames=None, pixels=None,
                     seconds=None)

    def decoded(*args, **kwargs):
        return Dimension("failed", "bad_pixels"), silent

    with mock.patch.object(quality_check, "_decode", decoded):
        report = quality_check.quality_evidence(path, decode_policy="strict")
    assert report["quality"]["decode"]["state"] == "failed"
    assert report["quality"]["decode"]["reason"] == "bad_pixels"
    assert report["budget"]["bytes"] is None
    assert maintenance._decode_outcome([report]) == 2


def test_the_receipt_carries_the_bytes_the_decoder_reported(tmp_path):
    path = write(tmp_path, "valid.png", png_bytes())
    report = quality_check.check_file(path, decode_policy="strict")
    assert report["receipt"]["bytes_read"] == path.stat().st_size
    assert isinstance(report["receipt"]["bytes_read"], int)


# --- the structure walk ------------------------------------------------


def test_the_structure_walk_charges_what_it_reads(tmp_path):
    path = write(tmp_path, "valid.png", png_bytes())
    size = path.stat().st_size
    budget = CommandBudget(DecodeLimits(max_bytes=10 * size, max_seconds=60.0))
    evidence = inspect_image(path, meter=budget.meter())
    assert evidence.size == size
    assert budget.snapshot()["bytes"] == size


def test_the_structure_walk_stops_when_its_allowance_is_spent(tmp_path):
    path = write(tmp_path, "valid.png", png_bytes())
    size = path.stat().st_size
    budget = CommandBudget(DecodeLimits(max_bytes=size - 1, max_seconds=60.0))
    with pytest.raises(ImageValidationStopped) as raised:
        inspect_image(path, meter=budget.meter())
    assert raised.value.reason == "bytes_limit"
    assert budget.snapshot()["bytes"] <= size - 1


# --- the CLI -----------------------------------------------------------


def test_the_cli_reports_one_budget_per_file_and_no_command_total(tmp_path, capsys):
    first = write(tmp_path, "one.png", png_bytes())
    second = write(tmp_path, "two.png", png_bytes(2, 2, color=6))
    with counted_reads(first, second) as reads:
        code = maintenance.main(
            ["decode-check", str(first), str(second), "--json"]
        )
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert code == 0
    budgets = [item["budget"] for item in payload["files"]]
    assert [budget["scope"] for budget in budgets] == ["file_call", "file_call"]
    assert sum(budget["bytes"] for budget in budgets) == sum(reads)
    # The payload may not present the sum as one command-wide ceiling.
    assert "command" not in out
    assert "鏁存潯鍛戒护" not in out
