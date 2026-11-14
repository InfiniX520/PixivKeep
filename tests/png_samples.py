"""Deterministic synthetic image samples for the decode matrix.

Every builder here returns bytes and is pure: the same arguments produce the
same bytes, with no randomness and no filesystem access.  The PNG builders use
struct and zlib only, so the decode matrix can exercise a decoder without
installing anything.  Nothing here parses or validates an image -- that is the
tests' job, and they do it independently.
"""
from __future__ import annotations

import struct
import zlib

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

#: Adam7 pass geometry: (x_start, y_start, x_step, y_step).
ADAM7 = (
    (0, 0, 8, 8),
    (4, 0, 8, 8),
    (0, 4, 4, 8),
    (2, 0, 4, 4),
    (0, 2, 2, 4),
    (1, 0, 2, 2),
    (0, 1, 1, 2),
)

RGBA_BIT_DEPTH = 8
RGBA_COLOR_TYPE = 6
BYTES_PER_PIXEL = 4


def chunk(kind: bytes, payload: bytes) -> bytes:
    """One PNG chunk: length, type, payload, CRC over type+payload."""
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def pixel(x: int, y: int) -> bytes:
    """A deterministic RGBA pixel; no random source is used anywhere here."""
    return bytes(((x * 17 + 3) % 256, (y * 23 + 7) % 256, ((x + y) * 11) % 256, 255))


def columns_of(width: int, x_start: int, x_step: int) -> list[int]:
    return list(range(x_start, width, x_step))


def rows_of(height: int, y_start: int, y_step: int) -> list[int]:
    return list(range(y_start, height, y_step))


def raw_scanlines(
    width: int,
    height: int,
    *,
    interlaced: bool = False,
    bad_filter_row: int | None = None,
) -> bytes:
    """Uncompressed scanline stream: one filter byte, then the pixels of a row."""
    plan: list[tuple[list[int], int]] = []
    if not interlaced:
        for y in range(height):
            plan.append((list(range(width)), y))
    else:
        for x_start, y_start, x_step, y_step in ADAM7:
            columns = columns_of(width, x_start, x_step)
            if not columns:
                continue
            for y in rows_of(height, y_start, y_step):
                plan.append((columns, y))
    stream = bytearray()
    for index, (columns, y) in enumerate(plan):
        filter_type = 5 if bad_filter_row is not None and index == bad_filter_row else 0
        stream.append(filter_type)
        for x in columns:
            stream += pixel(x, y)
    return bytes(stream)


def build_png(
    width: int,
    height: int,
    *,
    interlaced: bool = False,
    bad_filter_row: int | None = None,
    idat_payload: bytes | None = None,
) -> bytes:
    """A complete PNG: signature, IHDR, one IDAT, IEND."""
    header = struct.pack(
        ">IIBBBBB",
        width,
        height,
        RGBA_BIT_DEPTH,
        RGBA_COLOR_TYPE,
        0,
        0,
        1 if interlaced else 0,
    )
    if idat_payload is None:
        idat_payload = zlib.compress(
            raw_scanlines(width, height, interlaced=interlaced, bad_filter_row=bad_filter_row),
            9,
        )
    return (
        PNG_SIGNATURE
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", idat_payload)
        + chunk(b"IEND", b"")
    )


def build_valid_rgba(width: int = 4, height: int = 4) -> bytes:
    """A small, fully valid non-interlaced RGBA PNG."""
    return build_png(width, height)


def build_interlaced() -> bytes:
    """8x8 interlaced RGBA PNG: every one of the seven Adam7 passes is non-empty."""
    return build_png(8, 8, interlaced=True)


def build_bad_filter() -> bytes:
    """A 4x4 PNG whose second row starts with filter type 5, which PNG forbids."""
    return build_png(4, 4, bad_filter_row=1)


def build_truncated() -> bytes:
    """A 4x4 PNG whose IDAT holds only the first half of the compressed stream.

    The chunk itself is well formed (its CRC is recomputed), so a reader cannot
    reject it on framing alone: the pixel data simply stops early.
    """
    full = zlib.compress(raw_scanlines(4, 4), 9)
    return build_png(4, 4, idat_payload=full[: len(full) // 2])


def build_oversized_header(width: int = 100000, height: int = 100000) -> bytes:
    """A PNG that declares an enormous image but carries almost no pixel data."""
    return build_png(width, height, idat_payload=zlib.compress(b"\x00" + pixel(0, 0), 9))




def _flip_crc_of(data: bytes, kind: bytes) -> bytes:
    """Return the same PNG with one chunk CRC deliberately wrong."""
    out = bytearray(data)
    offset = 8
    while offset < len(out):
        (length,) = struct.unpack(">I", out[offset : offset + 4])
        current = bytes(out[offset + 4 : offset + 8])
        if current == kind:
            crc_at = offset + 8 + length
            (value,) = struct.unpack(">I", out[crc_at : crc_at + 4])
            out[crc_at : crc_at + 4] = struct.pack(">I", value ^ 0xFFFFFFFF)
            return bytes(out)
        offset += 12 + length
    raise ValueError("chunk not found")


def build_bad_crc() -> bytes:
    """A structurally complete PNG whose IDAT chunk carries a wrong CRC.

    The pixel stream is intact: inflating the IDAT payload still yields all 68
    bytes, so only the CRC check can reject this file.  That is the point of
    the sample: it separates "the bytes decompress" from "the file is intact".
    """
    return _flip_crc_of(build_valid_rgba(), b"IDAT")


def build_missing_iend() -> bytes:
    """A PNG with intact IHDR/IDAT and correct CRCs, but no IEND at the end.

    A reader that stops after the last chunk it can parse would accept this;
    a reader that requires the end marker must refuse it.
    """
    return build_valid_rgba()[:-12]  # the IEND chunk is exactly twelve bytes

def build_minimal_gif() -> bytes:
    """A structure-valid 1x1 GIF89a: header, palette, one image, trailer.

    This is the only sample here that the existing structure parser accepts
    for a non-PNG format (image/gif, 1x1, one frame), so a matrix can use it
    to reach the decode branch and confirm capability_unavailable honestly:
    the file is fine, the decoder simply does not exist on this host.
    """
    return bytes((
        0x47, 0x49, 0x46, 0x38, 0x39, 0x61,  # GIF89a
        0x01, 0x00, 0x01, 0x00, 0x80, 0x00, 0x00,  # 1x1, global colour table
        0x00, 0x00, 0x00, 0xFF, 0xFF, 0xFF,  # black and white
        0x21, 0xF9, 0x04, 0x01, 0x00, 0x00, 0x00, 0x00,  # graphic control
        0x2C, 0x00, 0x00, 0x00, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00,  # image
        0x02, 0x02, 0x44, 0x01, 0x00,  # LZW data
        0x3B,  # trailer
    ))

def build_gif_header() -> bytes:
    """Format-detection sample only: a GIF89a header with no image data.

    The existing structure parser recognises the signature but rejects the
    file with gif_missing_image_data, so this sample never reaches a decoder.
    Use build_minimal_gif() when a structure-valid GIF is needed; this one is
    only for "the format was recognised and nothing else".
    """
    return (
        b"GIF89a"
        + struct.pack("<HH", 8, 8)
        + bytes((0x00, 0x00, 0x00))
        + b";"
    )


def build_jpeg_header() -> bytes:
    """Format-detection sample only: SOI, a JFIF APP0 segment, then EOI.

    The structure parser rejects it with jpeg_missing_image_data: the signature
    is recognised, the file is not an image.  There is no JPEG decoder on this
    host, and no structure-valid JPEG sample exists here, so the no-decoder
    branch for JPEG has to be exercised at the registry level, not with this
    file.
    """
    app0 = b"JFIF\x00" + bytes((1, 1, 0)) + struct.pack(">HH", 1, 1) + bytes((0, 0))
    segment = b"\xff\xe0" + struct.pack(">H", len(app0) + 2) + app0
    return b"\xff\xd8" + segment + b"\xff\xd9"


def build_webp_header() -> bytes:
    """Format-detection sample only: a RIFF/WEBP container with one VP8 chunk.

    The structure parser rejects it with webp_invalid_vp8_header: the container
    is recognised, the payload is filler.  There is no WebP decoder on this
    host and no structure-valid WebP sample here, so the no-decoder branch for
    WebP has to be exercised at the registry level.
    """
    payload = bytes(range(8))
    chunk_bytes = b"VP8 " + struct.pack("<I", len(payload)) + payload
    riff_size = 4 + len(chunk_bytes)
    return b"RIFF" + struct.pack("<I", riff_size) + b"WEBP" + chunk_bytes
