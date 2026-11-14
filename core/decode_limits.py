"""Decode budgets: "too big / too slow / stopped" is a decision, not a crash.

A decoder that runs out of budget has not learned that the file is broken; it
has only learned that this run may not look any further.  This module keeps
those two apart.  Every check returns either None (go on) or one reason from a
**closed** set, and a reached limit never raises: the caller turns the reason
into "undecided", and only the decoder itself may report "failed".

Counting rules, chosen so the boundary is testable:

* bytes / pixels / frames are allowed while the running total is less than or
  equal to the limit, so a file of exactly the limit is decodable and one byte
  more is not;
* time is allowed while the elapsed time is strictly below the limit, because a
  clock that has reached the limit has nothing left to spend;
* multi-frame work accumulates: a thousand small frames are not "one small
  frame" a thousand times.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

#: Every reason a budget may return.  Closed on purpose: a decoder that wants a
#: new reason adds it here and to the tests instead of inventing a string that
#: nobody can switch on.
REASONS = frozenset({"bytes_limit", "pixels_limit", "frames_limit", "time_limit", "cancel"})

#: One decode's defaults.  Big enough for any single Pixiv page, small enough
#: that a decompression bomb stops before the machine does.
DEFAULT_MAX_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_PIXELS = 64_000_000
DEFAULT_MAX_FRAMES = 64
DEFAULT_MAX_SECONDS = 10.0


@runtime_checkable
class Stop(Protocol):
    """Anything that can answer "did someone ask us to stop?"."""

    def is_set(self) -> bool:  # pragma: no cover - protocol declaration
        ...


def _positive_int(name: str, value: object) -> int:
    """A positive int, with bool rejected (bool is an int in Python)."""
    if type(value) is not int or value <= 0:
        raise ValueError("%s must be a positive int, got %r" % (name, value))
    return int(value)


def _positive_seconds(name: str, value: object) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError("%s must be a positive finite number, got %r" % (name, value))
    return float(value)


def _count(value: object, name: str) -> int:
    """A non-negative int for one check's increment."""
    if type(value) is not int or value < 0:
        raise ValueError("%s must be a non-negative int, got %r" % (name, value))
    return int(value)


@dataclass(frozen=True, slots=True)
class DecodeLimits:
    """The four ceilings one decode runs under.  Immutable by construction."""

    max_bytes: int = DEFAULT_MAX_BYTES
    max_pixels: int = DEFAULT_MAX_PIXELS
    max_frames: int = DEFAULT_MAX_FRAMES
    max_seconds: float = DEFAULT_MAX_SECONDS

    def __post_init__(self) -> None:
        object.__setattr__(self, "max_bytes", _positive_int("max_bytes", self.max_bytes))
        object.__setattr__(self, "max_pixels", _positive_int("max_pixels", self.max_pixels))
        object.__setattr__(self, "max_frames", _positive_int("max_frames", self.max_frames))
        object.__setattr__(
            self, "max_seconds", _positive_seconds("max_seconds", self.max_seconds)
        )

    def as_dict(self) -> dict:
        return {
            "max_bytes": self.max_bytes,
            "max_pixels": self.max_pixels,
            "max_frames": self.max_frames,
            "max_seconds": self.max_seconds,
        }


class Budget:
    """Running totals for one decode, against one immutable DecodeLimits.

    The clock defaults to time.perf_counter, not time.monotonic: on Windows the
    latter ticks about every 15.6 ms, so a sub-tick limit would read as "no time
    spent" right up to the moment it jumps past the limit.  A caller may inject
    any zero-argument callable (tests do), but it has to be able to resolve the
    limit it is given.

    A budget is single-use and monotone: every check adds its increment first
    and then answers, so the totals in snapshot() describe everything that was
    looked at, including the step that hit a limit.  Two budgets built from the
    same limits never share counters.
    """

    __slots__ = ("limits", "_clock", "_started", "_bytes", "_pixels", "_frames")

    def __init__(self, limits: DecodeLimits, *, clock=time.perf_counter) -> None:
        if not isinstance(limits, DecodeLimits):
            raise TypeError("limits must be DecodeLimits, got %r" % (type(limits).__name__,))
        if not callable(clock):
            raise TypeError("clock must be callable, got %r" % (type(clock).__name__,))
        self.limits = limits
        self._clock = clock
        self._started = clock()
        self._bytes = 0
        self._pixels = 0
        self._frames = 0

    # -- checks ---------------------------------------------------------
    def check_bytes(self, count: int) -> str | None:
        """Account count more bytes; bytes_limit once the total passes."""
        self._bytes += _count(count, "count")
        return "bytes_limit" if self._bytes > self.limits.max_bytes else None

    def check_pixels(self, width: int, height: int) -> str | None:
        """Account one frame's width * height pixels, cumulatively."""
        self._pixels += _count(width, "width") * _count(height, "height")
        return "pixels_limit" if self._pixels > self.limits.max_pixels else None

    def check_frames(self, count: int = 1) -> str | None:
        """Account count more frames; multi-frame work accumulates."""
        self._frames += _count(count, "count")
        return "frames_limit" if self._frames > self.limits.max_frames else None

    def check_time(self) -> str | None:
        """time_limit once the injected clock has reached max_seconds."""
        if (self._clock() - self._started) >= self.limits.max_seconds:
            return "time_limit"
        return None

    def check_stop(self, stop: Stop | None) -> str | None:
        """cancel when the caller's stop signal is set; None otherwise."""
        if stop is None:
            return None
        return "cancel" if stop.is_set() else None

    # -- facts ----------------------------------------------------------
    def snapshot(self) -> dict:
        """What this budget has seen so far; for receipts, never for verdicts."""
        return {
            "bytes": self._bytes,
            "pixels": self._pixels,
            "frames": self._frames,
            "elapsed_seconds": round(self._clock() - self._started, 6),
            "limits": self.limits.as_dict(),
        }
