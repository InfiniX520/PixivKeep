"""Acceptance tests for the stdlib PNG decoder.

Every sample is built here from bytes: no downloads, no fixtures, no writes to
anything but the pytest temporary directory.
"""
from __future__ import annotations

import hashlib
import struct
import zlib

import pytest

from core import png_decode

SIG = b"\x89PNG\r\n\x1a\n"


class Limits:
    def __init__(self, max_bytes=None, max_pixels=None, max_frames=None, max_seconds=None):
        self.max_bytes = max_bytes
        self.max_pixels = max_pixels
        self.max_frames = max_frames
        self.max_seconds = max_seconds


class Stop:
    def __init__(self, after=0):
        self.calls = 0
        self.after = after

    def is_set(self):
        self.calls += 1
        return self.calls > self.after


def chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def header(width, height, *, bits=8, color=6, interlace=0) -> bytes:
    return chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, bits, color, 0, 0, interlace))


def png(parts: list[bytes], trailer: bytes = b"") -> bytes:
    return SIG + b"".join(parts) + trailer


def raw_png(width, height, rows, *, interlace=0, color=6, bits=8, idat=None, before_idat=()):
    payload = zlib.compress(b"".join(rows)) if idat is None else idat
    parts = [header(width, height, bits=bits, color=color, interlace=interlace)]
    parts.extend(before_idat)
    parts.append(chunk(b"IDAT", payload))
    parts.append(chunk(b"IEND", b""))
    return png(parts)


def paeth(left, up, upper_left):
    estimate = left + up - upper_left
    to_left = abs(estimate - left)
    to_up = abs(estimate - up)
    to_upper_left = abs(estimate - upper_left)
    if to_left <= to_up and to_left <= to_upper_left:
        return left
    if to_up <= to_upper_left:
        return up
    return upper_left


def apply_filter(kind, raw, previous, bpp):
    out = bytearray()
    for index, value in enumerate(raw):
        left = raw[index - bpp] if index >= bpp else 0
        up = previous[index] if previous else 0
        upper_left = previous[index - bpp] if previous and index >= bpp else 0
        if kind == 0:
            out.append(value)
        elif kind == 1:
            out.append((value - left) & 0xFF)
        elif kind == 2:
            out.append((value - up) & 0xFF)
        elif kind == 3:
            out.append((value - ((left + up) >> 1)) & 0xFF)
        else:
            out.append((value - paeth(left, up, upper_left)) & 0xFF)
    return bytes(out)


def rows_with_filters(pixels, bpp, filters):
    raw_rows = []
    filtered = []
    previous = b""
    for row, kind in zip(pixels, filters):
        filtered.append(bytes([kind]) + apply_filter(kind, row, previous, bpp))
        raw_rows.append(row)
        previous = row
    return filtered, b"".join(raw_rows)


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def test_a_valid_rgba_png_decodes_every_row():
    pixels = [bytes(range(0, 8)), bytes(range(8, 16))]
    rows, raw = rows_with_filters(pixels, 4, [0, 0])
    result = png_decode.decode_png(_stream(raw_png(2, 2, rows)))
    assert result["state"] == "ok"
    assert result["reason"] is None
    assert (result["width"], result["height"], result["frames"], result["pixels"]) == (2, 2, 1, 4)
    assert result["rows_unfiltered"] == 2
    assert result["pixels_sha256"] == digest(raw)


def test_every_filter_type_is_reversed():
    pixels = [bytes([10, 20, 30, 40]), bytes([11, 21, 31, 41]), bytes([12, 22, 32, 42]),
              bytes([13, 23, 33, 43]), bytes([14, 24, 34, 44])]
    rows, raw = rows_with_filters(pixels, 4, [0, 1, 2, 3, 4])
    result = png_decode.decode_png(_stream(raw_png(1, 5, rows)))
    assert result["state"] == "ok", result
    assert result["rows_unfiltered"] == 5
    assert result["pixels_sha256"] == digest(raw)


def _interlaced_2x2():
    """Adam7 for a 2x2 image: pass 1 is (0,0), pass 6 is (1,0), pass 7 is (0,1)+(1,1)."""
    top_left = bytes([1, 2, 3, 4])
    top_right = bytes([5, 6, 7, 8])
    bottom_left = bytes([9, 10, 11, 12])
    bottom_right = bytes([13, 14, 15, 16])
    rows = [b"\x00" + top_left, b"\x00" + top_right, b"\x00" + bottom_left + bottom_right]
    expected = top_left + top_right + bottom_left + bottom_right
    return raw_png(2, 2, rows, interlace=1), expected


def test_an_interlaced_png_decodes_all_passes():
    image, expected = _interlaced_2x2()
    result = png_decode.decode_png(_stream(image))
    assert result["state"] == "ok", result
    assert result["rows_unfiltered"] == 3
    assert result["pixels_sha256"] == digest(expected)


def test_a_bad_filter_byte_is_a_failure_not_a_guess():
    rows = [b"\x05" + bytes(range(8))]
    result = png_decode.decode_png(_stream(raw_png(2, 1, rows)))
    assert (result["state"], result["reason"]) == ("failed", "invalid_filter")
    assert result["rows_unfiltered"] == 0


def test_a_corrupt_deflate_stream_is_reported():
    payload = bytearray(zlib.compress(b"\x00" + bytes(range(8))))
    payload[0] = 0x00
    result = png_decode.decode_png(_stream(raw_png(2, 1, [b""], idat=bytes(payload))))
    assert (result["state"], result["reason"]) == ("failed", "bad_zlib")


def test_a_short_pixel_stream_is_reported():
    payload = zlib.compress(b"\x00" + bytes(range(8)))[:-6]
    result = png_decode.decode_png(_stream(raw_png(2, 1, [b""], idat=payload)))
    assert result["state"] == "failed"
    assert result["reason"] in {"truncated", "bad_zlib", "row_length_mismatch"}


def test_a_missing_iend_is_truncated():
    image = SIG + header(2, 1) + chunk(b"IDAT", zlib.compress(b"\x00" + bytes(range(8))))
    result = png_decode.decode_png(_stream(image))
    assert (result["state"], result["reason"]) == ("failed", "truncated")


def test_a_bad_crc_is_reported():
    image = bytearray(raw_png(2, 1, [b"\x00" + bytes(range(8))]))
    image[29] ^= 0xFF  # inside the IHDR CRC
    result = png_decode.decode_png(_stream(bytes(image)))
    assert (result["state"], result["reason"]) == ("failed", "bad_crc")


def test_an_unknown_critical_chunk_is_refused():
    image = raw_png(2, 1, [b"\x00" + bytes(range(8))], before_idat=[chunk(b"ZZZZ", b"")])
    result = png_decode.decode_png(_stream(image))
    assert (result["state"], result["reason"]) == ("failed", "unknown_critical_chunk")


def test_a_bad_signature_is_reported():
    result = png_decode.decode_png(_stream(b"not a png at all"))
    assert (result["state"], result["reason"]) == ("failed", "bad_signature")


def test_an_apng_control_chunk_is_refused():
    image = raw_png(2, 1, [b"\x00" + bytes(range(8))], before_idat=[chunk(b"acTL", b"\x00" * 8)])
    result = png_decode.decode_png(_stream(image))
    assert (result["state"], result["reason"]) == ("failed", "apng_unsupported")


def test_a_huge_header_hits_the_pixel_budget():
    image = SIG + header(100000, 100000) + chunk(b"IDAT", zlib.compress(b"")) + chunk(b"IEND", b"")
    result = png_decode.decode_png(_stream(image), limits=Limits(max_pixels=1000))
    assert (result["state"], result["reason"]) == ("undecided", "pixels_limit")


def test_a_small_byte_budget_is_undecided():
    image = raw_png(2, 1, [b"\x00" + bytes(range(8))])
    result = png_decode.decode_png(_stream(image), limits=Limits(max_bytes=10))
    assert (result["state"], result["reason"]) == ("undecided", "bytes_limit")


def test_a_zero_frame_budget_is_undecided():
    image = raw_png(2, 1, [b"\x00" + bytes(range(8))])
    result = png_decode.decode_png(_stream(image), limits=Limits(max_frames=0))
    assert (result["state"], result["reason"]) == ("undecided", "frames_limit")


def test_a_cancel_is_undecided():
    image = raw_png(2, 1, [b"\x00" + bytes(range(8))])
    result = png_decode.decode_png(_stream(image), stop=Stop(after=1))
    assert (result["state"], result["reason"]) == ("undecided", "cancel")


def test_a_time_budget_is_undecided(monkeypatch):
    clock = iter([0.0] + [100.0] * 50)
    monkeypatch.setattr(png_decode, "_now", lambda: next(clock))
    image = raw_png(2, 1, [b"\x00" + bytes(range(8))])
    result = png_decode.decode_png(_stream(image), limits=Limits(max_seconds=1))
    assert (result["state"], result["reason"]) == ("undecided", "time_limit")


def test_decoding_a_file_does_not_modify_it(tmp_path):
    image = raw_png(2, 2, [b"\x00" + bytes(range(8)), b"\x00" + bytes(range(8, 16))])
    target = tmp_path / "sample.png"
    target.write_bytes(image)
    before = hashlib.sha256(target.read_bytes()).hexdigest()
    with target.open("rb") as stream:
        result = png_decode.decode_png(stream)
    after = hashlib.sha256(target.read_bytes()).hexdigest()
    assert result["state"] == "ok"
    assert before == after


def test_the_stream_position_is_not_required_to_be_zero():
    image = raw_png(2, 1, [b"\x00" + bytes(range(8))])
    padded = b"junk" + image
    stream = _stream(padded)
    stream.read(4)
    result = png_decode.decode_png(stream)
    assert result["state"] == "ok"


def _stream(data: bytes):
    import io

    return io.BytesIO(data)


def test_a_grayscale_png_uses_one_channel():
    row = bytes([7, 8, 9])
    result = png_decode.decode_png(_stream(raw_png(3, 1, [b"\x00" + row], color=0, bits=8)))
    assert result["state"] == "ok", result
    assert result["rows_unfiltered"] == 1
    assert result["pixels_sha256"] == digest(row)


def test_a_sixteen_bit_rgb_png_uses_six_bytes_per_pixel():
    row = bytes(range(0, 12))
    filtered = b"\x01" + apply_filter(1, row, b"", 6)
    result = png_decode.decode_png(_stream(raw_png(2, 1, [filtered], color=2, bits=16)))
    assert result["state"] == "ok", result
    assert result["pixels_sha256"] == digest(row)


def test_a_one_bit_grayscale_row_is_bits_not_bytes():
    row = bytes([0b10101010])
    result = png_decode.decode_png(_stream(raw_png(8, 1, [b"\x00" + row], color=0, bits=1)))
    assert result["state"] == "ok", result
    assert result["pixels_sha256"] == digest(row)


def test_a_palette_png_skips_plte_and_decodes_rows():
    palette = chunk(b"PLTE", bytes([0, 0, 0, 255, 0, 0]))
    row = bytes([0, 1])
    image = SIG + header(2, 1, bits=8, color=3) + palette + chunk(b"IDAT", zlib.compress(b"\x00" + row)) + chunk(b"IEND", b"")
    result = png_decode.decode_png(_stream(image))
    assert result["state"] == "ok", result
    assert result["pixels_sha256"] == digest(row)
