"""Full PNG decode with the standard library only.

The structure walker in `core.image_validation` answers "are the chunks well
formed?".  This module answers a different question: "does the compressed pixel
stream actually decode into the pixels the header promises?".  It inflates
IDAT, reverses every scanline filter (None/Sub/Up/Average/Paeth) for every pass
(Adam7 included) and requires the decompressed length to match the header
exactly.  Nothing is written: the caller's bytes are only read.

Two outcome vocabularies are kept apart on purpose:

* `failed`    -- the file is wrong (bad signature, bad CRC, invalid filter,
                  short pixel stream, unknown critical chunk, ...);
* `undecided` -- this run could not finish (byte/pixel/time budget, cancel,
                  frame limit), which says nothing about the file.

Limits and the stop flag are duck-typed (`limits.max_bytes`,
`limits.max_pixels`, `limits.max_frames`, `limits.max_seconds`;
`stop.is_set()`) so this module never imports another lane's file.
"""
from __future__ import annotations

import hashlib
import re
import struct
import time
import zlib

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

_BLOCK = 64 * 1024
_MAX_CHUNK_LENGTH = 0x7FFFFFFF
_KIND = re.compile(rb"[A-Za-z]{4}")
_VALID_BITS = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8}, 4: {8, 16}, 6: {8, 16}}
_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
_ADAM7 = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
          (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))
_DEFAULT_MAX_BYTES = 1 << 31
_DEFAULT_MAX_PIXELS = 1 << 28


class _Halt(Exception):
    """Ends the decode with a state/reason pair; never escapes this module."""

    def __init__(self, state: str, reason: str):
        self.state = state
        self.reason = reason
        super().__init__("%s: %s" % (state, reason))


def _now() -> float:
    """Indirection so a test can drive the time budget deterministically."""
    return time.monotonic()


def _budget_value(limits, name, default):
    value = getattr(limits, name, None) if limits is not None else None
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("limits.%s must be a number" % name)
    return value


class _Budget:
    """Byte, pixel, frame and time budget plus the caller's stop flag."""

    def __init__(self, limits, stop):
        self.max_bytes = int(_budget_value(limits, "max_bytes", _DEFAULT_MAX_BYTES))
        self.max_pixels = int(_budget_value(limits, "max_pixels", _DEFAULT_MAX_PIXELS))
        self.max_frames = int(_budget_value(limits, "max_frames", 1))
        seconds = _budget_value(limits, "max_seconds", None)
        self.max_seconds = None if seconds is None else float(seconds)
        self.stop = stop
        self.started = _now()
        self.count = 0

    def check(self) -> None:
        if self.stop is not None and getattr(self.stop, "is_set", None) and self.stop.is_set():
            raise _Halt("undecided", "cancel")
        if self.max_seconds is not None and _now() - self.started > self.max_seconds:
            raise _Halt("undecided", "time_limit")

    def count_bytes(self, total: int) -> None:
        if total > self.max_bytes:
            raise _Halt("undecided", "bytes_limit")

    def check_pixels(self, pixels: int) -> None:
        if pixels > self.max_pixels:
            raise _Halt("undecided", "pixels_limit")


class _Source:
    """Reads the stream in bounded pieces and enforces the byte budget."""

    def __init__(self, stream, budget: _Budget):
        if not hasattr(stream, "read"):
            raise TypeError("decode_png needs a binary stream")
        self.stream = stream
        self.budget = budget

    def take(self, amount: int) -> bytes:
        pieces = []
        remaining = amount
        while remaining:
            self.budget.check()
            piece = self.stream.read(remaining)
            if not piece:
                raise _Halt("failed", "truncated")
            self.budget.count += len(piece)
            self.budget.count_bytes(self.budget.count)
            pieces.append(piece)
            remaining -= len(piece)
        return b"".join(pieces)


def _paeth(left: int, up: int, upper_left: int) -> int:
    estimate = left + up - upper_left
    to_left = abs(estimate - left)
    to_up = abs(estimate - up)
    to_upper_left = abs(estimate - upper_left)
    if to_left <= to_up and to_left <= to_upper_left:
        return left
    if to_up <= to_upper_left:
        return up
    return upper_left


class _Scanlines:
    """Reverses the per-row filters of every pass with bounded memory."""

    def __init__(self, width: int, height: int, bits: int, channels: int, interlace: int):
        self.bpp = max(1, bits * channels // 8)
        passes = _ADAM7 if interlace else ((0, 0, 1, 1),)
        self.plan: list[tuple[int, int]] = []
        for x, y, dx, dy in passes:
            columns = 0 if width <= x else (width - x + dx - 1) // dx
            rows = 0 if height <= y else (height - y + dy - 1) // dy
            if columns and rows:
                self.plan.append(((columns * bits * channels + 7) // 8, rows))
        self.expected_rows = sum(rows for _row_bytes, rows in self.plan)
        self.index = 0
        self.rows_left = self.plan[0][1] if self.plan else 0
        self.row = bytearray()
        self.previous = bytearray()
        self.rows_unfiltered = 0
        #: Digest of the reconstructed pixel rows, in pass order.  It is the
        #: only externally checkable proof that the filters were reversed.
        self.hasher = hashlib.sha256()

    def feed(self, data: bytes) -> None:
        offset = 0
        while offset < len(data):
            if self.index >= len(self.plan):
                raise _Halt("failed", "extra_scanline_data")
            row_bytes = self.plan[self.index][0]
            need = 1 + row_bytes - len(self.row)
            take = min(need, len(data) - offset)
            self.row += data[offset:offset + take]
            offset += take
            if len(self.row) == 1 + row_bytes:
                self._finish(row_bytes)
                self.rows_left -= 1
                if self.rows_left == 0:
                    self.index += 1
                    self.rows_left = self.plan[self.index][1] if self.index < len(self.plan) else 0
                self.row = bytearray()

    def _finish(self, row_bytes: int) -> None:
        filter_type = self.row[0]
        if filter_type > 4:
            raise _Halt("failed", "invalid_filter")
        raw = self.row[1:]
        if filter_type == 0:
            out = bytearray(raw)
        else:
            out = bytearray(row_bytes)
            previous = self.previous
            bpp = self.bpp
            for index in range(row_bytes):
                value = raw[index]
                left = out[index - bpp] if index >= bpp else 0
                up = previous[index] if previous else 0
                upper_left = previous[index - bpp] if previous and index >= bpp else 0
                if filter_type == 1:
                    value += left
                elif filter_type == 2:
                    value += up
                elif filter_type == 3:
                    value += (left + up) >> 1
                else:
                    value += _paeth(left, up, upper_left)
                out[index] = value & 0xFF
        self.previous = out
        self.hasher.update(out)
        self.rows_unfiltered += 1

    def complete(self) -> bool:
        return self.rows_unfiltered == self.expected_rows and self.index >= len(self.plan)


def _result(state, reason, width, height, scanlines, budget, started):
    return {
        "state": state,
        "reason": reason,
        "width": width,
        "height": height,
        "frames": 1 if state == "ok" else 0,
        "pixels": None if width is None or height is None else width * height,
        "rows_unfiltered": 0 if scanlines is None else scanlines.rows_unfiltered,
        "pixels_sha256": (
            scanlines.hasher.hexdigest()
            if scanlines is not None and state == "ok"
            else None
        ),
        "bytes_read": budget.count,
        "seconds": round(_now() - started, 6),
    }


def decode_png(stream, *, limits=None, stop=None) -> dict:
    """Decode every row of a PNG; report the outcome without writing anything."""
    budget = _Budget(limits, stop)
    started = budget.started
    width = height = None
    scanlines = None
    try:
        if budget.max_frames < 1:
            raise _Halt("undecided", "frames_limit")
        source = _Source(stream, budget)
        if source.take(8) != PNG_SIGNATURE:
            raise _Halt("failed", "bad_signature")
        inflater = zlib.decompressobj()
        seen_data = False
        first_chunk = True
        while True:
            length, kind = struct.unpack(">I4s", source.take(8))
            budget.check()
            if length > _MAX_CHUNK_LENGTH or not _KIND.fullmatch(kind):
                raise _Halt("failed", "bad_chunk")
            if first_chunk and kind != b"IHDR":
                raise _Halt("failed", "missing_ihdr")
            crc = zlib.crc32(kind)
            if kind == b"IHDR":
                if not first_chunk or length != 13:
                    raise _Halt("failed", "bad_ihdr")
                header = source.take(13)
                crc = zlib.crc32(header, crc)
                width, height, bits, color, compression, filtering, interlace = struct.unpack(
                    ">IIBBBBB", header
                )
                if not 0 < width < 1 << 31 or not 0 < height < 1 << 31:
                    raise _Halt("failed", "bad_dimensions")
                if color not in _CHANNELS or bits not in _VALID_BITS[color]:
                    raise _Halt("failed", "bad_color_type")
                if compression or filtering or interlace not in (0, 1):
                    raise _Halt("failed", "bad_interlace")
                budget.check_pixels(width * height)
                scanlines = _Scanlines(width, height, bits, _CHANNELS[color], interlace)
            elif kind == b"IDAT":
                if scanlines is None:
                    raise _Halt("failed", "missing_ihdr")
                seen_data = True
                remaining = length
                while remaining:
                    piece = source.take(min(remaining, _BLOCK))
                    crc = zlib.crc32(piece, crc)
                    remaining -= len(piece)
                    pending = piece
                    try:
                        while pending:
                            budget.check()
                            decoded = inflater.decompress(pending, _BLOCK)
                            scanlines.feed(decoded)
                            if inflater.unused_data:
                                raise _Halt("failed", "extra_compressed_data")
                            pending = inflater.unconsumed_tail
                    except zlib.error:
                        raise _Halt("failed", "bad_zlib") from None
            elif kind == b"IEND":
                if length:
                    raise _Halt("failed", "bad_iend")
                if not seen_data:
                    raise _Halt("failed", "missing_idat")
                try:
                    tail = inflater.flush()
                except zlib.error:
                    raise _Halt("failed", "bad_zlib") from None
                if tail:
                    scanlines.feed(tail)
                if not inflater.eof:
                    raise _Halt("failed", "truncated")
                if not scanlines.complete():
                    raise _Halt("failed", "row_length_mismatch")
                budget.check()
                return _result("ok", None, width, height, scanlines, budget, started)
            elif kind == b"acTL":
                raise _Halt("failed", "apng_unsupported")
            elif kind == b"PLTE" or kind[0] & 0x20:
                if kind == b"PLTE" and seen_data:
                    raise _Halt("failed", "bad_chunk_order")
                remaining = length
                while remaining:
                    piece = source.take(min(remaining, _BLOCK))
                    crc = zlib.crc32(piece, crc)
                    remaining -= len(piece)
            else:
                raise _Halt("failed", "unknown_critical_chunk")
            expected_crc = struct.unpack(">I", source.take(4))[0]
            if expected_crc != crc & 0xFFFFFFFF:
                raise _Halt("failed", "bad_crc")
            first_chunk = False
    except _Halt as halt:
        return _result(halt.state, halt.reason, width, height, scanlines, budget, started)
    except MemoryError:
        return _result("undecided", "memory_limit", width, height, scanlines, budget, started)
