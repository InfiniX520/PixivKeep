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


def chunk_records(data: bytes) -> list[tuple[bytes, bytes, int, int]]:
    """Walk the chunk stream; return kind, payload, stored CRC, computed CRC."""
    assert data[:8] == PNG_SIGNATURE
    offset = 8
    records: list[tuple[bytes, bytes, int, int]] = []
    while offset < len(data):
        (length,) = struct.unpack(">I", data[offset : offset + 4])
        kind = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        (stored,) = struct.unpack(">I", data[offset + 8 + length : offset + 12 + length])
        records.append((kind, payload, stored, zlib.crc32(kind + payload) & 0xFFFFFFFF))
        offset += 12 + length
    return records


def parse_chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    """Walk the chunk stream and verify every CRC independently."""
    records = chunk_records(data)
    for kind, _payload, stored, computed in records:
        assert stored == computed, "bad CRC on " + repr(kind)
    return [(kind, payload) for kind, payload, _stored, _computed in records]


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


def _tmp_path(suffix: str = ".bin"):
    """A file under pytest tmp_path, created empty so the test owns it."""
    import tempfile
    from pathlib import Path

    handle, name = tempfile.mkstemp(suffix=suffix)
    import os

    os.close(handle)
    return Path(name)


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
        "bad_crc": samples.build_bad_crc,
        "missing_iend": samples.build_missing_iend,
        "oversized": samples.build_oversized_header,
        "gif": samples.build_gif_header,
        "minimal_gif": samples.build_minimal_gif,
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


def test_bad_crc_sample_decompresses_but_fails_its_crc() -> None:
    records = chunk_records(samples.build_bad_crc())
    kinds = [kind for kind, _payload, _stored, _computed in records]
    assert kinds[0] == b"IHDR" and kinds[-1] == b"IEND" and b"IDAT" in kinds
    mismatched = [
        kind
        for kind, _payload, stored, computed in records
        if stored != computed
    ]
    assert mismatched == [b"IDAT"], "exactly the IDAT CRC has to be wrong"
    idat = b"".join(
        payload for kind, payload, _stored, _computed in records if kind == b"IDAT"
    )
    assert len(zlib.decompress(idat)) == 68, "the pixel data itself stays intact"


def test_missing_iend_sample_has_no_end_marker() -> None:
    records = chunk_records(samples.build_missing_iend())
    kinds = [kind for kind, _payload, _stored, _computed in records]
    assert b"IEND" not in kinds
    assert kinds[-1] == b"IDAT", "the stream stops right after IDAT"
    for kind, _payload, stored, computed in records:
        assert stored == computed, "every remaining chunk keeps a valid CRC: " + repr(kind)
    idat = b"".join(
        payload for kind, payload, _stored, _computed in records if kind == b"IDAT"
    )
    assert len(zlib.decompress(idat)) == 68


def test_minimal_gif_is_structurally_valid_for_the_existing_parser() -> None:
    """The only non-PNG sample that passes the structure parser (no decoder needed)."""
    from core.image_validation import inspect_image

    data = samples.build_minimal_gif()
    assert data.startswith(b"GIF89a") and data.endswith(b";")
    tmp = _tmp_path()
    try:
        tmp.write_bytes(data)
        evidence = inspect_image(tmp)
        assert evidence.media_type == "image/gif"
        assert (evidence.width, evidence.height, evidence.frames) == (1, 1, 1)
        assert evidence.verification == "structure_checked"
    finally:
        tmp.unlink(missing_ok=True)


def test_header_only_samples_are_rejected_by_the_structure_parser() -> None:
    """They recognise the signature and fail, which is exactly what they are for."""
    from core.image_validation import ImageValidationError, inspect_image

    cases = {
        "gif": (samples.build_gif_header(), "gif_missing_image_data"),
        "jpeg": (samples.build_jpeg_header(), "jpeg_missing_image_data"),
        "webp": (samples.build_webp_header(), "webp_invalid_vp8_header"),
    }
    for name, (data, expected) in cases.items():
        tmp = _tmp_path(suffix="." + name)
        try:
            tmp.write_bytes(data)
            try:
                inspect_image(tmp)
            except ImageValidationError as error:
                assert error.code == expected, name + " rejected as " + str(error.code)
            else:
                raise AssertionError(name + " must not pass structure validation")
        finally:
            tmp.unlink(missing_ok=True)
