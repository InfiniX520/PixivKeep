"""Full PNG decode with the standard library only.

The structure walker in core.image_validation answers "are the chunks well
formed?".  This module answers a different question: "does the compressed pixel
stream actually decode into the pixels the header promises?".  It inflates
IDAT, reverses every scanline filter (None/Sub/Up/Average/Paeth) for every pass
(Adam7 included) and requires the decompressed length to match the header
exactly.  Nothing is written: the caller's bytes are only read.

Two outcome vocabularies are kept apart on purpose:

* failed    -- the file is wrong (bad signature, bad CRC, invalid filter, short
               pixel stream, unknown critical chunk, ...);
* undecided -- this run could not finish (byte/pixel/time budget, cancel),
               which says nothing about the file.

A malformed file never raises.  Every data-driven problem, including an
inconsistent pixel stream that would otherwise surface as IndexError or
ValueError, is turned into a result.  A TypeError (a wrong argument type, or a
stream without read()) is still a programming error and is allowed to escape.

Budgets and the stop flag come from core.decode_limits: this module owns no
limits of its own.  decode_png(stream) runs under DecodeLimits(); a caller that
processes full-size pages (core.quality_check.DEFAULT_LIMITS) passes its own
explicit object.  Both are named constants in their own module, so there is
exactly one definition of each and no third copy hides here.
"""
from __future__ import annotations

import hashlib
import re
import struct
import zlib

from core.decode_limits import Budget, DecodeLimits

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

_BLOCK = 64 * 1024
#: How many unfiltered bytes may pass between two cancellation checks.  One row
#: of a large image is millions of bytes, and a stop that is only noticed at the
#: row boundary is not a bound on the delay.
_POLL_EVERY = 64 * 1024
_MAX_CHUNK_LENGTH = 0x7FFFFFFF
_KIND = re.compile(rb"[A-Za-z]{4}")
_VALID_BITS = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8}, 4: {8, 16}, 6: {8, 16}}
_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
_ADAM7 = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
          (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))


class _Halt(Exception):
    """Ends the decode with a state/reason pair; never escapes this module."""

    def __init__(self, state: str, reason: str):
        self.state = state
        self.reason = reason
        super().__init__("%s: %s" % (state, reason))


def _poll(budget: Budget, stop) -> None:
    """Cancel and time are answers from the budget, not exceptions."""
    reason = budget.check_stop(stop) or budget.check_time()
    if reason is not None:
        raise _Halt("undecided", reason)


class _Source:
    """Reads the stream in bounded pieces and accounts every byte."""

    def __init__(self, stream, budget: Budget, stop):
        if not hasattr(stream, "read"):
            raise TypeError("decode_png needs a binary stream")
        self.stream = stream
        self.budget = budget
        self.stop = stop

    def take(self, amount: int) -> bytes:
        pieces = []
        remaining = amount
        while remaining:
            _poll(self.budget, self.stop)
            # Clamp before asking: a read that overshoots is already spent by
            # the time it could be charged, and the stream cannot give it back.
            allowed = self.budget.read_size(remaining)
            if allowed <= 0:
                raise _Halt("undecided", "bytes_limit")
            piece = self.stream.read(allowed)
            if not piece:
                raise _Halt("failed", "truncated")
            reason = self.budget.check_bytes(len(piece))
            if reason is not None:
                raise _Halt("undecided", reason)
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
    """Reverses the per-row filters of every pass with bounded memory.

    PNG requires each pass to be filtered independently: the first row of a
    pass has no previous row.  previous is therefore cleared at every pass
    boundary; keeping the last row of the previous pass would decode the first
    row of the next pass against the wrong neighbour (and, when the two passes
    have different widths, read past the buffer).
    """

    def __init__(self, width: int, height: int, bits: int, channels: int, interlace: int,
                 *, indexed: bool = False):
        self.bpp = max(1, bits * channels // 8)
        self.bits = bits
        self.indexed = indexed
        #: How many entries the PLTE chunk declared; None until it is read.
        self.palette_entries: int | None = None
        passes = _ADAM7 if interlace else ((0, 0, 1, 1),)
        #: One entry per non-empty pass: (row bytes, rows, pixels in a row).
        self.plan: list[tuple[int, int, int]] = []
        for x, y, dx, dy in passes:
            columns = 0 if width <= x else (width - x + dx - 1) // dx
            rows = 0 if height <= y else (height - y + dy - 1) // dy
            if columns and rows:
                self.plan.append(((columns * bits * channels + 7) // 8, rows, columns))
        self.expected_rows = sum(rows for _row_bytes, rows, _columns in self.plan)
        self.index = 0
        self.rows_left = self.plan[0][1] if self.plan else 0
        self.row = bytearray()
        self.previous = bytearray()
        self.rows_unfiltered = 0
        #: Called every _POLL_EVERY bytes of a row so a cancel or a time limit is
        #: noticed inside a long row, not only between rows.  None means the
        #: caller did not ask for polling.
        self.poll = None
        #: Digest of the reconstructed pixel rows, in pass order: the only
        #: externally checkable proof that the filters were reversed.
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
                    # A new pass starts with no previous row.
                    self.previous = bytearray()
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
            poll = self.poll
            for index in range(row_bytes):
                if poll is not None and index % _POLL_EVERY == 0:
                    poll()
                value = raw[index]
                left = out[index - bpp] if index >= bpp else 0
                up = previous[index] if index < len(previous) else 0
                upper_left = (
                    previous[index - bpp]
                    if index >= bpp and index - bpp < len(previous)
                    else 0
                )
                if filter_type == 1:
                    value += left
                elif filter_type == 2:
                    value += up
                elif filter_type == 3:
                    value += (left + up) >> 1
                else:
                    value += _paeth(left, up, upper_left)
                out[index] = value & 0xFF
        self._check_indices(out, self.plan[self.index][2])
        self.previous = out
        self.hasher.update(out)
        self.rows_unfiltered += 1

    def set_palette(self, entries: int) -> None:
        """The palette size is only known once PLTE is read; rows may follow it."""
        self.palette_entries = int(entries)

    def _check_indices(self, out: bytearray, columns: int) -> None:
        """Every decoded sample must address an entry that PLTE declared.

        PNG 11.2.2 makes an index outside the palette an error.  A decoder is
        allowed to *display* something for it, but this check is the strict one:
        "recoverable" is not "valid", and a strict pass that silently repairs is
        how a wrong image gets a green receipt.

        Padding bits at the end of a row are not samples: the loop stops at the
        pixel count of this pass, which is what differs between Adam7 passes.
        """
        palette = self.palette_entries
        if not self.indexed or palette is None or columns <= 0:
            return
        bits = self.bits
        if bits == 8:
            if max(memoryview(out)[:columns]) >= palette:
                raise _Halt("failed", "palette_index_out_of_range")
            return
        per_byte = 8 // bits
        mask = (1 << bits) - 1
        limit = min(len(out), (columns + per_byte - 1) // per_byte)
        last = columns - 1
        for position in range(limit):
            byte = out[position]
            first = position * per_byte
            for slot in range(per_byte):
                index = first + slot
                if index > last:
                    return
                shift = 8 - bits * (slot + 1)
                if (byte >> shift) & mask >= palette:
                    raise _Halt("failed", "palette_index_out_of_range")

    def complete(self) -> bool:
        return self.rows_unfiltered == self.expected_rows and self.index >= len(self.plan)


def _result(state, reason, width, height, scanlines, budget, limits):
    snapshot = budget.snapshot()
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
        "bytes_read": snapshot["bytes"],
        "seconds": snapshot["elapsed_seconds"],
        "limits": limits.as_dict(),
    }


def decode_png(stream, *, limits=None, stop=None) -> dict:
    """Decode every row of a PNG; report the outcome without writing anything.

    Returns state (ok/failed/undecided), reason, the geometry, the number of
    rows that were actually unfiltered, a digest of those rows, the bytes read,
    the elapsed seconds and the limits that were applied.  A malformed file
    produces a result; it does not raise.
    """
    if limits is None:
        limits = DecodeLimits()
    elif not isinstance(limits, DecodeLimits):
        raise TypeError(
            "limits must be a DecodeLimits instance, got %r" % (type(limits).__name__,)
        )
    budget = Budget(limits)
    width = height = None
    scanlines = None
    try:
        reason = budget.check_frames(1)
        if reason is not None:
            raise _Halt("undecided", reason)
        source = _Source(stream, budget, stop)
        if source.take(8) != PNG_SIGNATURE:
            raise _Halt("failed", "bad_signature")
        inflater = zlib.decompressobj()
        seen_data = False
        first_chunk = True
        completed = False
        palette_entries = None
        while True:
            length, kind = struct.unpack(">I4s", source.take(8))
            _poll(budget, stop)
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
                reason = budget.check_pixels(width, height)
                if reason is not None:
                    raise _Halt("undecided", reason)
                scanlines = _Scanlines(width, height, bits, _CHANNELS[color], interlace,
                                       indexed=(color == 3))
                scanlines.poll = lambda: _poll(budget, stop)
            elif kind == b"IDAT":
                if scanlines is None:
                    raise _Halt("failed", "missing_ihdr")
                if color == 3 and palette_entries is None:
                    # An indexed image without a palette has nothing to address.
                    raise _Halt("failed", "missing_plte")
                seen_data = True
                remaining = length
                while remaining:
                    piece = source.take(min(remaining, _BLOCK))
                    crc = zlib.crc32(piece, crc)
                    remaining -= len(piece)
                    pending = piece
                    try:
                        while pending:
                            _poll(budget, stop)
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
                _poll(budget, stop)
                # The IEND chunk has its own CRC: it is read (and checked) below
                # like every other chunk, so a file that stops right after the
                # IEND data is still a truncated file.
                completed = True
            elif kind == b"acTL":
                raise _Halt("failed", "apng_unsupported")
            elif kind == b"PLTE":
                if seen_data:
                    raise _Halt("failed", "bad_chunk_order")
                if palette_entries is not None:
                    raise _Halt("failed", "duplicate_plte")
                if color in (0, 4):
                    # A palette is meaningless for greyscale; the spec forbids it.
                    raise _Halt("failed", "plte_not_allowed")
                if length == 0 or length % 3 or length > 768:
                    raise _Halt("failed", "bad_plte")
                remaining = length
                while remaining:
                    piece = source.take(min(remaining, _BLOCK))
                    crc = zlib.crc32(piece, crc)
                    remaining -= len(piece)
                palette_entries = length // 3
                if scanlines is not None:
                    scanlines.set_palette(palette_entries)
            elif kind[0] & 0x20:
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
            if completed:
                return _result("ok", None, width, height, scanlines, budget, limits)
            first_chunk = False
    except _Halt as halt:
        return _result(halt.state, halt.reason, width, height, scanlines, budget, limits)
    except zlib.error:
        return _result("failed", "bad_zlib", width, height, scanlines, budget, limits)
    except (IndexError, ValueError, struct.error, OverflowError):
        # An inconsistent pixel stream is a fact about the file, not a bug in
        # the caller: it must come back as a result, never as a traceback.
        return _result(
            "failed", "row_length_mismatch", width, height, scanlines, budget, limits
        )
    except MemoryError:
        return _result("undecided", "memory_limit", width, height, scanlines, budget, limits)
