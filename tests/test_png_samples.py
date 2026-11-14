"""Independent checks on the synthetic samples; nothing here trusts the builder.

Every assertion re-reads the bytes with struct/zlib and recomputes what the
sample claims, so a builder that silently produced the wrong geometry or a
wrong CRC would fail here instead of quietly weakening the decode matrix.
"""
from __future__ import annotations

import struct
import zlib

import png_samples as samples

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
RGBA_BYTES_PER_PIXEL = 4


def parse_chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    """Walk the chunk stream and verify every CRC independently."""
    assert data[:8] == PNG_SIGNATURE
    offset = 8
    chunks: list[tuple[bytes, bytes]] = []
    while offset < len(data):
        (length,) = struct.unpack(">I", data[offset : offset + 4])
        kind = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        (crc,) = struct.unpack(">I", data[offset + 8 + length : offset + 12 + length])
        assert crc == (zlib.crc32(kind + payload) & 0xFFFFFFFF), "bad CRC on " + repr(kind)
        chunks.append((kind, payload))
        offset += 12 + length
    return chunks


def header_of(chunks: list[tuple[bytes, bytes]]) -> tuple[int, int, int, int, int]:
    kinds = [kind for kind, _ in chunks]
    assert kinds[0] == b"IHDR" and kinds[-1] == b"IEND"
    width, height, depth, colour, compression, filtering, interlace = struct.unpack(
        ">IIBBBBB", chunks[0][1]
    )
    assert compression == 0 and filtering == 0
    return width, height, depth, colour, interlace


def idat_of(chunks: list[tuple[bytes, bytes]]) -> bytes:
    return b"".join(payload for kind, payload in chunks if kind == b"IDAT")


def adam7_rows(width: int, height: int) -> list[tuple[int, int]]:
    """(columns, rows) for every non-empty Adam7 pass, in encoder order."""
    plan: list[tuple[int, int]] = []
    for x_start, y_start, x_step, y_step in samples.ADAM7:
        columns = len(range(x_start, width, x_step))
        rows = len(range(y_start, height, y_step))
        if columns and rows:
            plan.append((columns, rows))
    return plan


def row_bytes(columns: int) -> int:
    return 1 + columns * RGBA_BYTES_PER_PIXEL


def test_valid_rgba_is_a_real_png_with_exact_pixel_data() -> None:
    data = samples.build_valid_rgba()
    chunks = parse_chunks(data)
    width, height, depth, colour, interlace = header_of(chunks)
    assert (width, height, depth, colour, interlace) == (4, 4, 8, 6, 0)
    raw = zlib.decompress(idat_of(chunks))
    assert len(raw) == height * row_bytes(width) == 68
    for index in range(height):
        assert raw[index * row_bytes(width)] == 0, "row %d is not filter type 0" % index


def test_interlaced_sample_carries_all_seven_passes() -> None:
    data = samples.build_interlaced()
    chunks = parse_chunks(data)
    width, height, depth, colour, interlace = header_of(chunks)
    assert (width, height, interlace) == (8, 8, 1)
    plan = adam7_rows(width, height)
    assert len(plan) == 7, "a sample meant for the interlaced path must fill all seven passes"
    raw = zlib.decompress(idat_of(chunks))
    assert len(raw) == sum(rows * row_bytes(columns) for columns, rows in plan)
    offset = 0
    for columns, rows in plan:
        for _ in range(rows):
            assert raw[offset] == 0
            offset += row_bytes(columns)
    assert offset == len(raw), "the passes do not line up with the stream"


def test_bad_filter_sample_really_contains_an_illegal_filter_byte() -> None:
    data = samples.build_bad_filter()
    chunks = parse_chunks(data)
    width, height, _, _, _ = header_of(chunks)
    raw = zlib.decompress(idat_of(chunks))
    filters = [raw[index * row_bytes(width)] for index in range(height)]
    assert filters == [0, 5, 0, 0]


def test_truncated_sample_is_short_but_well_framed() -> None:
    full = samples.build_valid_rgba()
    short = samples.build_truncated()
    full_chunks, short_chunks = parse_chunks(full), parse_chunks(short)
    full_idat, short_idat = idat_of(full_chunks), idat_of(short_chunks)
    assert len(short_idat) < len(full_idat)
    inflater = zlib.decompressobj()
    partial = inflater.decompress(short_idat)
    assert len(partial) < 68, "the sample must really stop before the last row"


def test_oversized_header_declares_the_huge_dimensions() -> None:
    chunks = parse_chunks(samples.build_oversized_header())
    width, height, depth, colour, interlace = header_of(chunks)
    assert (width, height, depth, colour, interlace) == (100000, 100000, 8, 6, 0)


def test_other_formats_are_recognisable_headers() -> None:
    gif = samples.build_gif_header()
    assert gif.startswith(b"GIF89a") and gif.endswith(b";")
    assert struct.unpack("<HH", gif[6:10]) == (8, 8)

    jpeg = samples.build_jpeg_header()
    assert jpeg.startswith(b"\xff\xd8") and jpeg.endswith(b"\xff\xd9")
    assert b"JFIF\x00" in jpeg
    (segment_length,) = struct.unpack(">H", jpeg[4:6])
    assert 4 + segment_length <= len(jpeg)

    webp = samples.build_webp_header()
    assert webp[:4] == b"RIFF" and webp[8:12] == b"WEBP" and webp[12:16] == b"VP8 "
    (riff_size,) = struct.unpack("<I", webp[4:8])
    assert riff_size + 8 == len(webp)


def test_samples_are_deterministic_and_small() -> None:
    builders = {
        "valid": samples.build_valid_rgba,
        "interlaced": samples.build_interlaced,
        "bad_filter": samples.build_bad_filter,
        "truncated": samples.build_truncated,
        "oversized": samples.build_oversized_header,
        "gif": samples.build_gif_header,
        "jpeg": samples.build_jpeg_header,
        "webp": samples.build_webp_header,
    }
    for name, builder in builders.items():
        first, second = builder(), builder()
        assert first == second, name + " is not deterministic"
        assert 8 <= len(first) <= 4096, name + " is not a small sample"


def test_valid_sample_dimensions_are_parameterised() -> None:
    chunks = parse_chunks(samples.build_valid_rgba(width=7, height=3))
    width, height, _, _, _ = header_of(chunks)
    assert (width, height) == (7, 3)
    raw = zlib.decompress(idat_of(chunks))
    assert len(raw) == 3 * row_bytes(7)
