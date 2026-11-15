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

    def remaining_bytes(self) -> int:
        """How much of the byte ceiling this budget has not spent yet."""
        return max(0, int(self.limits.max_bytes) - self._bytes)

    def read_size(self, amount: int) -> int:
        """How many bytes one read may ask for now; 0 means none are left.

        A read that asks for more than the allowance has already spent the
        budget by the time it can be charged, so the size has to be clamped
        *before* the stream is asked.  The clamp is what makes "we never read
        past the ceiling" a property of the code and not a hope.
        """
        return min(_count(amount, "amount"), self.remaining_bytes())

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


#: The scope of a budget that covers one check_file() call: exactly one file.
#: A multi-file command builds one of these per file, so a total printed as
#: "the command's budget" was never true; the label says file_call now.
FILE_SCOPE = "file_call"
#: Old name, same value.  Kept so an out-of-tree import keeps working; it does
#: not mean a command-wide total and no output may describe it that way.
COMMAND_SCOPE = FILE_SCOPE


class CommandBudget:
    """One file check's budget: every stage charges the same counters.

    A Budget answers for one decode.  A quality check is not one decode: it
    hashes the file for a content version, walks the structure, then decodes -
    and a budget that only covers the last stage lets the first two spend
    unbounded work before any limit is consulted.  This wrapper keeps one
    counter set for the whole check, names each stage as it starts, and hands
    the *remaining* allowance to the next stage.

    The scope is one file, not one command: a CLI given ten paths builds ten of
    these, so a snapshot may be added up but must never be printed as "the
    command's budget".

    The counters are single-use and monotone, exactly like Budget: a snapshot
    describes everything that was looked at, including the stage that hit a
    limit.  A stage that cannot say how much it read leaves the byte total
    unknown - a fact, not a zero.
    """

    scope = FILE_SCOPE

    __slots__ = ("limits", "_budget", "_stop", "_stage", "_stages", "_unknown")

    def __init__(self, limits: DecodeLimits, *, stop: Stop | None = None,
                 clock=time.perf_counter) -> None:
        if not isinstance(limits, DecodeLimits):
            raise TypeError("limits must be DecodeLimits, got %r" % (type(limits).__name__,))
        self.limits = limits
        self._budget = Budget(limits, clock=clock)
        self._stop = stop
        self._stage = "created"
        self._stages: list[str] = []
        #: Why the byte total is unknown, or None while it is still exact.
        self._unknown: str | None = None

    # -- the stage boundary ---------------------------------------------
    def enter(self, stage: str) -> str | None:
        """Name the stage that is about to run and answer stop/time."""
        self._stage = str(stage)
        self._stages.append(self._stage)
        return self.check()

    def check(self) -> str | None:
        """cancel / time_limit, or None when the stage may start."""
        reason = self._budget.check_stop(self._stop)
        if reason is not None:
            return reason
        return self._budget.check_time()

    # -- what a stage spent ---------------------------------------------
    def charge(self, count: int) -> str | None:
        """Account count more bytes read by the current stage."""
        return self._budget.check_bytes(count)

    def charge_pixels(self, width: int, height: int) -> str | None:
        return self._budget.check_pixels(width, height)

    def mark_unknown(self, reason: str) -> None:
        """The byte total can no longer be stated; keep the reason, not a zero."""
        if not isinstance(reason, str) or not reason:
            raise ValueError("reason must be a non-empty str, got %r" % (reason,))
        if self._unknown is None:
            self._unknown = reason

    @property
    def unknown_reason(self) -> str | None:
        return self._unknown

    # -- metering the real reads ----------------------------------------
    def meter(self) -> "ReadMeter":
        """A read meter bound to this budget; see ReadMeter for the protocol."""
        return ReadMeter(self)

    def read_size(self, amount: int) -> int:
        return 0 if self._unknown else self._budget.read_size(amount)

    # -- what is left for the next stage --------------------------------
    def remaining_bytes(self) -> int:
        if self._unknown:
            return 0
        return self._budget.remaining_bytes()

    def remaining_seconds(self) -> float:
        return self.limits.max_seconds - self._budget.snapshot()["elapsed_seconds"]

    def grant(self) -> DecodeLimits | None:
        """The allowance a further stage may spend, or None when it has none.

        The pixel and frame ceilings are not consumed by the earlier stages, so
        they pass through unchanged; bytes and seconds are what the version
        hash and the structure walk already spent.
        """
        remaining_bytes = self.remaining_bytes()
        remaining_seconds = self.remaining_seconds()
        if remaining_bytes <= 0 or remaining_seconds <= 0:
            return None
        return DecodeLimits(
            max_bytes=max(1, int(remaining_bytes)),
            max_pixels=self.limits.max_pixels,
            max_frames=self.limits.max_frames,
            max_seconds=max(1e-6, float(remaining_seconds)),
        )

    def refusal(self) -> str | None:
        """Why a further stage cannot start, or None when it can."""
        if self._unknown:
            return "budget_unknown"
        if self.remaining_bytes() <= 0:
            return "bytes_limit"
        if self.remaining_seconds() <= 0:
            return "time_limit"
        return None

    # -- facts ----------------------------------------------------------
    def snapshot(self) -> dict:
        """What this file's check has spent so far; for receipts, not verdicts."""
        facts = self._budget.snapshot()
        facts["scope"] = FILE_SCOPE
        facts["stages"] = list(self._stages)
        facts["stage"] = self._stage
        if self._unknown:
            # A stage that could not report its reads makes the total unknown.
            # None is the honest value; 0 would read as "we measured nothing".
            facts["bytes"] = None
            facts["unknown_reason"] = self._unknown
        return facts


class ReadMeter:
    """The calls every read site makes, in this order, around every chunk:

        reason = meter.poll()       # cancel / time_limit, before the read
        allowed = meter.size(cap)   # clamp: never ask for more than is left
        data = stream.read(allowed)
        reason = meter.spent(len(data))

    The meter keeps no counters of its own: it is a view of one CommandBudget,
    so the total cannot drift away from what the stream really gave.  The
    protocol is deliberately this small because a reader that owns its own
    buffer (the structure walk) has to be able to follow it without learning
    anything about budgets.
    """

    __slots__ = ("_budget",)

    def __init__(self, budget: CommandBudget) -> None:
        if not isinstance(budget, CommandBudget):
            raise TypeError(
                "budget must be a CommandBudget, got %r" % (type(budget).__name__,)
            )
        self._budget = budget

    def poll(self) -> str | None:
        """cancel / time_limit for the chunk that is about to be read."""
        return self._budget.check()

    def size(self, cap: int) -> int:
        """How many bytes this read may ask for; 0 means stop, not EOF."""
        return self._budget.read_size(cap)

    def spent(self, count: int) -> str | None:
        """Charge what the stream returned; bytes_limit once the total passes."""
        return self._budget.charge(count)
