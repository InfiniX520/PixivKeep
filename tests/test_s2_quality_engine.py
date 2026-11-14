"""S2: the quality engine's evidence boundaries (UX-02, UX-03, UX-04).

Every PNG here is built by this file: chunk framing, sample packing, the five
filters and the Adam7 pass geometry are written from the specification, so the
expected pixels and the expected digest never come from the decoder under
test.  All files live in tmp_path; nothing touches a real library.
"""
from __future__ import annotations

import hashlib
import struct
import zlib
from pathlib import Path

import pytest

from core import png_decode, quality_check
from core.decode_limits import CommandBudget, DecodeLimits

SIGNATURE = b"\x89PNG\r\n\x1a\n"
ADAM7 = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
         (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))


# --- an independent encoder ------------------------------------------------


def chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(
        ">I", zlib.crc32(kind + data) & 0xFFFFFFFF
    )


def pack(samples, bits: int) -> bytes:
    """One row of samples packed the way the spec says, padding bits zero."""
    if bits == 16:
        return b"".join(struct.pack(">H", value) for value in samples)
    if bits == 8:
        return bytes(samples)
    per_byte = 8 // bits
    mask = (1 << bits) - 1
    out = bytearray()
    accumulator = 0
    used = 0
    for value in samples:
        accumulator = (accumulator << bits) | (value & mask)
        used += 1
        if used == per_byte:
            out.append(accumulator)
            accumulator = 0
            used = 0
    if used:
        out.append(accumulator << (bits * (per_byte - used)))
    return bytes(out)


def _paeth(left: int, up: int, corner: int) -> int:
    estimate = left + up - corner
    to_left = abs(estimate - left)
    to_up = abs(estimate - up)
    to_corner = abs(estimate - corner)
    if to_left <= to_up and to_left <= to_corner:
        return left
    if to_up <= to_corner:
        return up
    return corner


def filter_row(raw: bytes, previous: bytes, bpp: int, kind: int) -> bytes:
    """Forward filter (the inverse of what the decoder must do)."""
    out = bytearray()
    for index, value in enumerate(raw):
        left = raw[index - bpp] if index >= bpp else 0
        up = previous[index] if index < len(previous) else 0
        corner = previous[index - bpp] if index >= bpp and index - bpp < len(previous) else 0
        if kind == 0:
            predictor = 0
        elif kind == 1:
            predictor = left
        elif kind == 2:
            predictor = up
        elif kind == 3:
            predictor = (left + up) >> 1
        else:
            predictor = _paeth(left, up, corner)
        out.append((value - predictor) & 0xFF)
    return bytes(out)


def encode(rows, *, width, height, color=0, bits=8, palette=None, interlace=0,
           filters=None, splits=1, extra_before_idat=(), extra_data=b"",
           trailer=True, grouped=False) -> bytes:
    """Build a PNG from raw sample rows, one filter kind per row.

    grouped=True means rows is a list of passes, each a list of rows; the forward
    filter then restarts at every pass, which is what the spec requires and what
    a decoder that keeps the previous pass's last row would get wrong.
    """
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
    bpp = max(1, bits * channels // 8)
    kinds = list(filters or ())
    stream = bytearray()
    groups = rows if grouped else [rows]
    seen = 0
    for group in groups:
        previous = b""
        for raw in group:
            kind = kinds[seen % len(kinds)] if kinds else 0
            seen += 1
            stream.append(kind)
            stream += filter_row(raw, previous, bpp, kind)
            previous = raw
    header = struct.pack(">IIBBBBB", width, height, bits, color, 0, 0, interlace)
    body = SIGNATURE + chunk(b"IHDR", header)
    if palette is not None:
        body += chunk(b"PLTE", palette)
    for kind, data in extra_before_idat:
        body += chunk(kind, data)
    compressed = zlib.compress(bytes(stream))
    body += extra_data
    pieces = max(1, splits)
    step = max(1, (len(compressed) + pieces - 1) // pieces)
    for start in range(0, len(compressed), step):
        body += chunk(b"IDAT", compressed[start:start + step])
    if trailer:
        body += chunk(b"IEND", b"")
    return body


def rows_of(width: int, height: int, samples_at, bits: int):
    """Plain rows: raw sample list per row, packed, in row order."""
    rows = []
    for y in range(height):
        samples = []
        for x in range(width):
            samples.extend(samples_at(x, y))
        rows.append(pack(samples, bits))
    return rows


def adam7_rows(width: int, height: int, samples_at, bits: int):
    """Adam7 rows in pass order, skipping the passes a small image does not use."""
    rows = []
    for x0, y0, dx, dy in ADAM7:
        _pass = []
        columns = 0 if width <= x0 else (width - x0 + dx - 1) // dx
        lines = 0 if height <= y0 else (height - y0 + dy - 1) // dy
        if not columns or not lines:
            continue
        for row in range(lines):
            samples = []
            for column in range(columns):
                samples.extend(samples_at(x0 + column * dx, y0 + row * dy))
            _pass.append(pack(samples, bits))
        rows.append(_pass)
    return rows


def digest(rows) -> str:
    hasher = hashlib.sha256()
    for raw in rows:
        hasher.update(raw)
    return hasher.hexdigest()


def decode_bytes(data: bytes, **kwargs) -> dict:
    import io

    return png_decode.decode_png(io.BytesIO(data), **kwargs)


def write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


# --- the valid matrix ------------------------------------------------------


def test_all_five_filters_round_trip_through_a_gray_png():
    width, height = 4, 5
    rows = rows_of(width, height, lambda x, y: [(x * 17 + y * 41) & 0xFF], 8)
    data = encode(rows, width=width, height=height, color=0, bits=8,
                  filters=(0, 1, 2, 3, 4))
    result = decode_bytes(data)
    assert (result["state"], result["reason"]) == ("ok", None)
    assert result["pixels_sha256"] == digest(rows)
    assert result["rows_unfiltered"] == height


def test_multiple_idat_chunks_are_one_stream():
    width, height = 6, 4
    rows = rows_of(width, height, lambda x, y: [(x * 31 + y) & 0xFF], 8)
    data = encode(rows, width=width, height=height, filters=(2,), splits=4)
    assert data.count(b"IDAT") == 4
    result = decode_bytes(data)
    assert result["state"] == "ok" and result["pixels_sha256"] == digest(rows)


def test_adam7_every_pass_is_independent_and_the_digest_matches():
    width, height = 8, 8
    passes = adam7_rows(width, height, lambda x, y: [(x * 3 + y * 7) & 0xFF], 8)
    flat = [raw for group in passes for raw in group]
    data = encode(passes, width=width, height=height, interlace=1,
                  filters=(0, 1, 2, 3, 4), grouped=True)
    result = decode_bytes(data)
    assert (result["state"], result["reason"]) == ("ok", None)
    assert result["pixels_sha256"] == digest(flat)


@pytest.mark.parametrize("bits", [1, 2, 4, 8, 16])
def test_every_legal_gray_bit_depth_decodes(bits):
    width, height = 3, 2
    top = (1 << bits) - 1
    rows = rows_of(width, height, lambda x, y: [(x + y) % (top + 1)], bits)
    data = encode(rows, width=width, height=height, color=0, bits=bits)
    result = decode_bytes(data)
    assert result["state"] == "ok" and result["pixels_sha256"] == digest(rows)


@pytest.mark.parametrize("bits", [1, 2, 4, 8])
def test_every_legal_indexed_bit_depth_decodes_inside_its_palette(bits):
    width, height = 5, 3
    entries = 4
    palette = bytes([0, 0, 0, 10, 20, 30, 40, 50, 60, 70, 80, 90][:entries * 3])
    top = min(entries - 1, (1 << bits) - 1)
    rows = rows_of(width, height, lambda x, y: [(x + y) % (top + 1)], bits)
    data = encode(rows, width=width, height=height, color=3, bits=bits,
                  palette=palette)
    result = decode_bytes(data)
    assert result["state"] == "ok", result
    assert result["pixels_sha256"] == digest(rows)


# --- UX-02: a palette index outside PLTE is a failed decode ----------------


def test_an_index_at_the_palette_size_is_out_of_range():
    width = height = 1
    rows = rows_of(width, height, lambda x, y: [1], 8)
    data = encode(rows, width=width, height=height, color=3, bits=8,
                  palette=bytes([1, 2, 3]))
    result = decode_bytes(data)
    assert (result["state"], result["reason"]) == ("failed", "palette_index_out_of_range")


def test_the_control_index_zero_is_inside_a_single_entry_palette():
    width = height = 1
    rows = rows_of(width, height, lambda x, y: [0], 8)
    data = encode(rows, width=width, height=height, color=3, bits=8,
                  palette=bytes([9, 9, 9]))
    assert decode_bytes(data)["state"] == "ok"


@pytest.mark.parametrize("bits,value,entries", [(1, 1, 1), (2, 3, 2), (4, 9, 4), (8, 200, 4)])
def test_out_of_range_indices_are_caught_at_every_bit_depth(bits, value, entries):
    width = height = 1
    rows = rows_of(width, height, lambda x, y: [value], bits)
    palette = bytes([1, 2, 3]) * entries
    data = encode(rows, width=width, height=height, color=3, bits=bits,
                  palette=palette)
    result = decode_bytes(data)
    assert (result["state"], result["reason"]) == ("failed", "palette_index_out_of_range")


@pytest.mark.parametrize("bits", [1, 2, 4])
def test_row_end_padding_bits_are_not_samples(bits):
    """Three pixels of a sub-byte depth leave padding bits in the last byte.

    The spec does not define them as samples, so a value that would be out of
    range there must not fail the decode.
    """
    width, height = 3, 1
    # Written out per depth so the padding bits are visible in the test itself:
    # three zero samples, then every remaining bit of the row byte set to one.
    packed = {1: bytes([0b00011111]), 2: bytes([0b00000011]), 4: bytes([0x00, 0x0F])}
    rows = [packed[bits]]
    data = encode(rows, width=width, height=height, color=3, bits=bits,
                  palette=bytes([0, 0, 0]))
    result = decode_bytes(data)
    assert result["state"] == "ok", result


def test_adam7_palette_check_uses_each_pass_width():
    width, height = 8, 8
    palette = bytes([0, 0, 0, 1, 1, 1])

    def samples_at(x, y):
        # Only the last Adam7 pass (x odd, y odd) carries a bad index.
        return [2 if (x % 2 and y % 2) else 0]

    passes = adam7_rows(width, height, samples_at, 8)
    data = encode(passes, width=width, height=height, color=3, bits=8,
                  palette=palette, interlace=1, grouped=True)
    result = decode_bytes(data)
    assert (result["state"], result["reason"]) == ("failed", "palette_index_out_of_range")


def test_a_missing_palette_is_not_decodable():
    width = height = 1
    rows = rows_of(width, height, lambda x, y: [0], 8)
    data = encode(rows, width=width, height=height, color=3, bits=8)
    assert decode_bytes(data)["reason"] == "missing_plte"


def test_a_duplicate_palette_is_refused():
    width = height = 1
    rows = rows_of(width, height, lambda x, y: [0], 8)
    palette = bytes([0, 0, 0])
    data = encode(rows, width=width, height=height, color=3, bits=8, palette=palette,
                  extra_before_idat=((b"PLTE", palette),))
    assert decode_bytes(data)["reason"] == "duplicate_plte"


def test_a_palette_that_is_not_whole_entries_is_refused():
    width = height = 1
    rows = rows_of(width, height, lambda x, y: [0], 8)
    data = encode(rows, width=width, height=height, color=3, bits=8,
                  palette=bytes([0, 0]))
    assert decode_bytes(data)["reason"] == "bad_plte"


def test_a_palette_on_a_gray_image_is_refused():
    width = height = 1
    rows = rows_of(width, height, lambda x, y: [0], 8)
    data = encode(rows, width=width, height=height, color=0, bits=8,
                  palette=bytes([0, 0, 0]))
    assert decode_bytes(data)["reason"] == "plte_not_allowed"


def test_a_palette_after_the_pixels_is_refused():
    width = height = 1
    rows = rows_of(width, height, lambda x, y: [0], 8)
    data = encode(rows, width=width, height=height, color=0, bits=8)
    broken = data.replace(chunk(b"IEND", b""), chunk(b"PLTE", bytes([0, 0, 0]))
                          + chunk(b"IEND", b""))
    assert decode_bytes(broken)["reason"] == "bad_chunk_order"


# --- the invalid matrix ----------------------------------------------------


def _valid() -> bytes:
    rows = rows_of(2, 2, lambda x, y: [x * 16 + y], 8)
    return encode(rows, width=2, height=2, color=0, bits=8)


def test_a_truncated_file_is_failed_not_a_crash():
    data = _valid()
    assert decode_bytes(data[:-6])["state"] == "failed"


def test_a_wrong_crc_is_failed():
    data = bytearray(_valid())
    # signature(8) + length(4) + "IHDR"(4) + data(13) => the IHDR CRC starts here.
    data[29] ^= 0xFF
    assert decode_bytes(bytes(data))["reason"] == "bad_crc"


def test_an_unknown_filter_type_is_failed():
    header = struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0)
    stream = zlib.compress(bytes([5, 0x00]))  # filter 5 does not exist
    data = (SIGNATURE + chunk(b"IHDR", header) + chunk(b"IDAT", stream)
            + chunk(b"IEND", b""))
    assert decode_bytes(data)["reason"] == "invalid_filter"


def test_an_unknown_critical_chunk_is_failed():
    width = height = 1
    rows = rows_of(width, height, lambda x, y: [0], 8)
    data = encode(rows, width=width, height=height, color=0, bits=8,
                  extra_before_idat=((b"ZZZZ", b"\x00"),))
    assert decode_bytes(data)["reason"] == "unknown_critical_chunk"


def test_an_apng_is_reported_unsupported():
    width = height = 1
    rows = rows_of(width, height, lambda x, y: [0], 8)
    data = encode(rows, width=width, height=height, color=0, bits=8,
                  extra_before_idat=((b"acTL", struct.pack(">II", 1, 0)),))
    assert decode_bytes(data)["reason"] == "apng_unsupported"


# --- UX-04: the budget covers the whole command ----------------------------


def test_command_budget_charges_stages_and_hands_out_the_remainder():
    budget = CommandBudget(DecodeLimits(max_bytes=100, max_seconds=60.0))
    assert budget.scope == "command"
    assert budget.enter("observe") is None
    assert budget.charge(40) is None
    grant = budget.grant()
    assert grant is not None and grant.max_bytes == 60
    assert budget.charge(70) == "bytes_limit"
    assert budget.refusal() == "bytes_limit"
    assert budget.grant() is None
    assert budget.snapshot()["bytes"] == 110
    assert budget.snapshot()["stages"] == ["observe"]


def test_a_stop_before_the_first_read_means_no_hash_at_all(tmp_path, monkeypatch):
    import threading

    path = write(tmp_path, "valid.png", _valid())
    stop = threading.Event()
    stop.set()
    calls = []
    original = quality_check._sha256

    def counted(target):
        calls.append(target)
        return original(target)

    monkeypatch.setattr(quality_check, "_sha256", counted)
    report = quality_check.check_file(path, decode_policy="strict", stop=stop)
    assert calls == []
    assert report["quality"]["structure"] == {"state": "undecided", "reason": "cancel"}
    assert report["content_version"]["state"] == "unknown"
    assert report["path_stability"] == "unknown"


def test_a_size_above_the_byte_ceiling_is_refused_before_any_read(tmp_path):
    path = write(tmp_path, "valid.png", _valid())
    size = path.stat().st_size
    opened = []
    original = Path.open

    def counted(self, *args, **kwargs):
        opened.append(str(self))
        return original(self, *args, **kwargs)

    import unittest.mock as mock

    with mock.patch.object(Path, "open", counted):
        report = quality_check.check_file(
            path, decode_policy="strict", limits=DecodeLimits(max_bytes=size - 1)
        )
    assert opened == []
    assert report["quality"]["structure"]["state"] == "undecided"
    assert report["quality"]["structure"]["reason"] == "bytes_limit"
    assert report["quality"]["decode"]["reason"] == "bytes_limit"
    assert report["size"] == size
    assert report["content_version"]["state"] == "unknown"


def test_the_receipt_reports_the_whole_command_spend_not_just_the_decoder(tmp_path):
    path = write(tmp_path, "valid.png", _valid())
    size = path.stat().st_size
    report = quality_check.check_file(path, decode_policy="strict")
    budget = report["budget"]
    assert budget["scope"] == "command"
    # observe, version_before, structure, decode, version_after
    assert budget["stages"] == ["observe", "version_before", "structure", "decode",
                                "version_after"]
    # the two hashes and the structure walk each read the file once, and the
    # decoder read it again: the total is far above one pass.
    assert budget["bytes"] >= 3 * size
    assert budget["elapsed_seconds"] >= 0


def test_a_stop_between_stages_stops_the_command(tmp_path, monkeypatch):
    import threading

    path = write(tmp_path, "valid.png", _valid())
    stop = threading.Event()
    real = quality_check.inspect_image

    def inspect_then_stop(target, **kwargs):
        evidence = real(target, **kwargs)
        stop.set()
        return evidence

    monkeypatch.setattr(quality_check, "inspect_image", inspect_then_stop)
    report = quality_check.check_file(path, decode_policy="strict", stop=stop)
    assert report["quality"]["structure"]["state"] == "ok"
    assert report["quality"]["decode"] == {"state": "undecided", "reason": "cancel"}
    assert report["path_stability"] == "unknown"


def test_a_cancelled_decode_is_undecided_and_measures_nothing(tmp_path):
    width = height = 64
    rows = rows_of(width, height, lambda x, y: [(x + y) & 0xFF], 8)
    path = write(tmp_path, "big.png", encode(rows, width=width, height=height))
    import threading

    stop = threading.Event()
    stop.set()
    report = quality_check.check_file(path, decode_policy="strict", stop=stop)
    assert report["quality"]["decode"]["state"] == "undecided"
    assert report["receipt"] is None


def test_the_time_ceiling_is_checked_between_stages(tmp_path, monkeypatch):
    path = write(tmp_path, "valid.png", _valid())
    now = {"seconds": 0.0}

    def clock():
        return now["seconds"]

    real = quality_check.inspect_image

    def slow_structure(target, **kwargs):
        evidence = real(target, **kwargs)
        now["seconds"] = 5.0  # the structure walk took longer than the ceiling
        return evidence

    monkeypatch.setattr(quality_check, "inspect_image", slow_structure)
    budget = CommandBudget(DecodeLimits(max_seconds=1.0), clock=clock)
    report = quality_check.quality_evidence(path, decode_policy="strict", budget=budget)
    # The structure walk finished before the ceiling was noticed; what the
    # ceiling forbids is the next stage, and that is what the report says.
    assert report["quality"]["structure"]["state"] == "ok"
    assert report["quality"]["decode"] == {"state": "undecided", "reason": "time_limit"}
    assert report["budget"]["scope"] == "command"


# --- UX-03: one content version, one comparison ----------------------------


def test_an_untouched_file_is_stable_and_fully_described(tmp_path):
    path = write(tmp_path, "valid.png", _valid())
    report = quality_check.check_file(path, decode_policy="strict")
    assert report["path_stability"] == "unchanged"
    assert report["unchanged"] is True
    assert report["content_version"]["state"] == "observed"
    assert report["sha256_before"] == report["sha256_after"] == report["sha256"]
    assert report["quality"]["decode"]["state"] == "ok"


def test_a_file_replaced_during_the_check_is_changed(tmp_path, monkeypatch):
    path = write(tmp_path, "valid.png", _valid())
    real = quality_check.quality_evidence
    replacement = encode(rows_of(2, 2, lambda x, y: [0], 8), width=2, height=2)

    def replace(target, **kwargs):
        report = real(target, **kwargs)
        target.write_bytes(replacement)
        return report

    monkeypatch.setattr(quality_check, "quality_evidence", replace)
    report = quality_check.check_file(path, decode_policy="strict")
    assert report["path_stability"] == "changed"
    assert report["path_stability_reason"] == "content_changed"
    assert report["unchanged"] is False
    assert report["sha256_before"] != report["sha256_after"]


def test_a_metadata_change_alone_is_changed_too(tmp_path, monkeypatch):
    path = write(tmp_path, "valid.png", _valid())
    real = quality_check.quality_evidence
    original_bytes = path.read_bytes()

    def rewrite_identical(target, **kwargs):
        report = real(target, **kwargs)
        target.write_bytes(original_bytes)  # same content, new mtime
        return report

    monkeypatch.setattr(quality_check, "quality_evidence", rewrite_identical)
    report = quality_check.check_file(path, decode_policy="strict")
    assert report["path_stability"] == "changed"
    assert report["sha256_before"] == report["sha256_after"]


def test_a_length_that_was_never_observed_is_not_matched(tmp_path):
    path = write(tmp_path, "valid.png", _valid())
    size = path.stat().st_size
    matched = quality_check.quality_evidence(path, expected_size=size)
    assert matched["quality"]["transport"]["state"] == "length_matched"
    short = quality_check.quality_evidence(path, expected_size=size - 1)
    assert short["quality"]["transport"]["state"] == "unknown"
    assert short["quality"]["transport"]["reason"].startswith("length_mismatch:")
    absent = quality_check.quality_evidence(path)
    assert absent["quality"]["transport"]["state"] == "unknown"
    assert absent["quality"]["transport"]["reason"].startswith("length_unknown:")


def test_an_unreadable_path_has_no_length_evidence(tmp_path):
    report = quality_check.quality_evidence(tmp_path / "absent.png")
    assert report["quality"]["transport"]["state"] == "unknown"
    assert report["quality"]["transport"]["reason"].startswith("unreadable:")
    assert report["quality"]["structure"]["state"] == "failed"
    assert report["content_version"]["state"] == "unknown"


def test_the_expected_media_type_is_checked_not_echoed(tmp_path):
    path = write(tmp_path, "valid.png", _valid())
    good = quality_check.quality_evidence(path, expected_media_type="image/png")
    assert good["quality"]["structure"]["state"] == "ok"
    assert good["media_type"] == "image/png"
    wrong = quality_check.quality_evidence(path, expected_media_type="image/jpeg")
    assert wrong["quality"]["structure"]["state"] == "failed"
    assert wrong["quality"]["structure"]["reason"] == "image_media_type_mismatch"


def test_a_wrong_expected_size_fails_the_structure_but_not_the_transport(tmp_path):
    path = write(tmp_path, "valid.png", _valid())
    size = path.stat().st_size
    report = quality_check.quality_evidence(path, expected_size=size + 1)
    assert report["quality"]["structure"]["reason"] == "image_size_mismatch"
    assert report["quality"]["transport"]["state"] == "unknown"
    assert report["quality"]["transport"]["reason"].startswith("length_mismatch:")
