"""Acceptance tests for the stdlib PNG decoder.

Every sample is built here from bytes: no downloads, no fixtures, no writes to
anything but the pytest temporary directory.  Expected pixels are always
computed by the test (or by tests/png_samples, which is an encoder), never by
asking the decoder what the answer is.
"""
from __future__ import annotations

import hashlib
import struct
import zlib

import pytest

import png_samples as samples
from core import png_decode
from core.decode_limits import Budget, DecodeLimits

SIG = b"\x89PNG\r\n\x1a\n"


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
    filtered = []
    previous = b""
    for row, kind in zip(pixels, filters):
        filtered.append(bytes([kind]) + apply_filter(kind, row, previous, bpp))
        previous = row
    return filtered, b"".join(pixels)


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _stream(data: bytes):
    import io

    return io.BytesIO(data)


# -- happy paths ---------------------------------------------------------


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
    """Adam7 for a 2x2 image: pass 1 is (0,0), pass 6 is (1,0), pass 7 is the bottom row."""
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


# -- P1 regression: every pass starts without a previous row -------------


def test_every_pass_starts_without_a_previous_row():
    # 8x8 interlaced: all seven Adam7 passes are non-empty, and the first row of
    # each one uses filter 2 (Up).  A decoder that kept the previous pass's last
    # row would either read past its buffer (passes differ in width) or decode
    # the wrong pixels.
    image, expected = samples.build_interlaced_with_pass_filters(
        8, 8, first_row_filters={index: 2 for index in range(7)}
    )
    result = png_decode.decode_png(_stream(image))
    assert result["state"] == "ok", result
    # 8x8 over seven passes is 15 scanlines (1+1+1+2+2+4+4) and 64 pixels.
    assert result["rows_unfiltered"] == 15
    assert result["pixels"] == 64
    assert result["pixels_sha256"] == digest(expected)


def test_only_the_fourth_pass_first_row_uses_up():
    # The silent case: only pass 4's first row uses filter 2, and that pass has
    # the same row width as the pass before it, so nothing goes out of bounds --
    # the pixels simply come out wrong unless previous is reset per pass.
    image, expected = samples.build_interlaced_with_pass_filters(8, 8, first_row_filters={3: 2})
    result = png_decode.decode_png(_stream(image))
    assert result["state"] == "ok", result
    assert result["pixels_sha256"] == digest(expected)


# -- broken files --------------------------------------------------------


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


@pytest.mark.parametrize(
    "data, reason",
    [
        (SIG, "truncated"),
        (SIG + b"\x00\x00\x00\x0dIH", "truncated"),
        (SIG + struct.pack(">I4s", 0xFFFFFFFF, b"IHDR"), "bad_chunk"),
        (SIG + chunk(b"IDAT", b"") + chunk(b"IEND", b""), "missing_ihdr"),
        (SIG + chunk(b"IHDR", b"\x00" * 12), "bad_ihdr"),
        (SIG + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 5, 0, 0, 0)), "bad_color_type"),
        (SIG + chunk(b"IHDR", struct.pack(">IIBBBBB", 0, 4, 8, 6, 0, 0, 0)), "bad_dimensions"),
    ],
)
def test_malformed_inputs_return_a_result_instead_of_raising(data, reason):
    result = png_decode.decode_png(_stream(data))
    assert result["state"] == "failed"
    assert result["reason"] == reason


def test_an_unexpected_pixel_error_becomes_a_failed_result(monkeypatch):
    # The decoder must not let an IndexError escape even if a future change in
    # the unfiltering reintroduces one: the caller sees a result, not a crash.
    def explode(self, row_bytes):
        raise IndexError("synthetic out of range")

    monkeypatch.setattr(png_decode._Scanlines, "_finish", explode)
    image = raw_png(2, 1, [b"\x00" + bytes(range(8))])
    result = png_decode.decode_png(_stream(image))
    assert (result["state"], result["reason"]) == ("failed", "row_length_mismatch")


# -- budgets -------------------------------------------------------------


def test_a_huge_header_hits_the_pixel_budget():
    image = SIG + header(100000, 100000) + chunk(b"IDAT", zlib.compress(b"")) + chunk(b"IEND", b"")
    result = png_decode.decode_png(_stream(image), limits=DecodeLimits(max_pixels=1000))
    assert (result["state"], result["reason"]) == ("undecided", "pixels_limit")


def test_bytes_exactly_at_the_limit_are_allowed():
    image = raw_png(2, 1, [b"\x00" + bytes(range(8))])
    result = png_decode.decode_png(_stream(image), limits=DecodeLimits(max_bytes=len(image)))
    assert result["state"] == "ok", result
    assert result["bytes_read"] == len(image)


def test_one_byte_over_the_limit_is_undecided():
    image = raw_png(2, 1, [b"\x00" + bytes(range(8))])
    result = png_decode.decode_png(_stream(image), limits=DecodeLimits(max_bytes=len(image) - 1))
    assert (result["state"], result["reason"]) == ("undecided", "bytes_limit")


def test_a_cancel_is_undecided():
    image = raw_png(2, 1, [b"\x00" + bytes(range(8))])
    result = png_decode.decode_png(_stream(image), stop=Stop(after=1))
    assert (result["state"], result["reason"]) == ("undecided", "cancel")


def test_the_time_budget_uses_the_shared_budget_and_an_injectable_clock(monkeypatch):
    class StepClock:
        def __init__(self):
            self.calls = 0

        def __call__(self):
            self.calls += 1
            return 0.0 if self.calls == 1 else 100.0

    clock = StepClock()
    real = Budget
    seen = []

    def factory(limits):
        seen.append(limits)
        return real(limits, clock=clock)

    monkeypatch.setattr(png_decode, "Budget", factory)
    limits = DecodeLimits(max_seconds=10)
    image = raw_png(2, 1, [b"\x00" + bytes(range(8))])
    result = png_decode.decode_png(_stream(image), limits=limits)
    assert (result["state"], result["reason"]) == ("undecided", "time_limit")
    assert seen == [limits]


def test_the_decoder_owns_no_limits_and_no_private_budget():
    assert not hasattr(png_decode, "_Budget")
    assert not hasattr(png_decode, "DEFAULT_MAX_BYTES")
    assert not hasattr(png_decode, "_DEFAULT_MAX_BYTES")


def test_limits_must_be_the_shared_type():
    class Duck:
        max_bytes = 1000
        max_pixels = 1000
        max_frames = 1
        max_seconds = 1.0

    image = raw_png(2, 1, [b"\x00" + bytes(range(8))])
    with pytest.raises(TypeError):
        png_decode.decode_png(_stream(image), limits=Duck())


def test_the_result_reports_the_limits_that_were_applied():
    image = raw_png(2, 1, [b"\x00" + bytes(range(8))])
    limits = DecodeLimits(max_bytes=1_000_000, max_pixels=1_000, max_frames=1, max_seconds=1.0)
    result = png_decode.decode_png(_stream(image), limits=limits)
    assert result["limits"] == limits.as_dict()
    default = png_decode.decode_png(_stream(image))
    assert default["limits"] == DecodeLimits().as_dict()


# -- formats -------------------------------------------------------------


def test_a_grayscale_png_uses_one_channel():
    row = bytes([7, 8, 9])
    result = png_decode.decode_png(_stream(raw_png(3, 1, [b"\x00" + row], color=0, bits=8)))
    assert result["state"] == "ok", result
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


def test_idat_split_over_several_chunks_is_one_stream():
    row = bytes(range(0, 8))
    compressed = zlib.compress(b"\x00" + row + b"\x00" + row)
    cut = len(compressed) // 2
    parts = [header(2, 2),
             chunk(b"IDAT", compressed[:cut]),
             chunk(b"IDAT", compressed[cut:]),
             chunk(b"IEND", b"")]
    result = png_decode.decode_png(_stream(png(parts)))
    assert result["state"] == "ok", result
    assert result["rows_unfiltered"] == 2
    assert result["pixels_sha256"] == digest(row + row)


def test_a_large_image_crosses_the_block_boundary():
    width = height = 400
    row = bytes((index * 7) & 0xFF for index in range(width * 3))
    raw = (b"\x00" + row) * height
    assert len(raw) > 64 * 1024
    image = raw_png(width, height, [b""], color=2, bits=8, idat=zlib.compress(raw))
    result = png_decode.decode_png(_stream(image))
    assert result["state"] == "ok", result
    assert result["rows_unfiltered"] == height
    expected = b"".join(raw[index + 1:index + 1 + width * 3] for index in range(0, len(raw), width * 3 + 1))
    assert result["pixels_sha256"] == digest(expected)


# -- read-only proof -----------------------------------------------------


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
    stream = _stream(b"junk" + image)
    stream.read(4)
    result = png_decode.decode_png(stream)
    assert result["state"] == "ok"
