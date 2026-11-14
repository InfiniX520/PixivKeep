"""Budget behaviour: boundaries, accumulation, cancel, and no exceptions."""
import dataclasses
import math

import pytest

from core.decode_limits import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_FRAMES,
    DEFAULT_MAX_PIXELS,
    DEFAULT_MAX_SECONDS,
    REASONS,
    Budget,
    DecodeLimits,
    Stop,
)


class FakeClock:
    """A clock the test moves by hand; nothing here waits for real time."""

    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        return self.now


class StopFlag:
    def __init__(self, value=False):
        self.value = value

    def is_set(self):
        return self.value


def budget(**limits):
    clock = FakeClock()
    return Budget(DecodeLimits(**limits), clock=clock), clock


# -- the limit object ----------------------------------------------------


def test_defaults_are_positive_and_exported():
    limits = DecodeLimits()
    assert limits.as_dict() == {
        "max_bytes": DEFAULT_MAX_BYTES,
        "max_pixels": DEFAULT_MAX_PIXELS,
        "max_frames": DEFAULT_MAX_FRAMES,
        "max_seconds": DEFAULT_MAX_SECONDS,
    }
    assert all(value > 0 for value in limits.as_dict().values())


def test_limits_are_immutable():
    limits = DecodeLimits(max_bytes=10)
    with pytest.raises(dataclasses.FrozenInstanceError):
        limits.max_bytes = 20


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_bytes": 0},
        {"max_bytes": -1},
        {"max_bytes": True},
        {"max_bytes": 1.5},
        {"max_pixels": 0},
        {"max_pixels": False},
        {"max_frames": 0},
        {"max_frames": "8"},
        {"max_seconds": 0},
        {"max_seconds": -0.5},
        {"max_seconds": float("nan")},
        {"max_seconds": float("inf")},
        {"max_seconds": True},
    ],
)
def test_invalid_limits_are_rejected(kwargs):
    with pytest.raises(ValueError):
        DecodeLimits(**kwargs)


def test_seconds_accept_int_and_are_stored_as_float():
    assert isinstance(DecodeLimits(max_seconds=3).max_seconds, float)
    assert DecodeLimits(max_seconds=3).max_seconds == 3.0


# -- bytes ---------------------------------------------------------------


def test_bytes_exactly_at_the_limit_is_allowed():
    b, _ = budget(max_bytes=10)
    assert b.check_bytes(10) is None


def test_bytes_one_over_the_limit_is_refused():
    b, _ = budget(max_bytes=10)
    assert b.check_bytes(11) == "bytes_limit"


def test_bytes_accumulate():
    b, _ = budget(max_bytes=10)
    assert b.check_bytes(6) is None
    assert b.check_bytes(6) == "bytes_limit"
    assert b.snapshot()["bytes"] == 12


def test_zero_bytes_is_a_noop():
    b, _ = budget(max_bytes=10)
    assert b.check_bytes(0) is None
    assert b.snapshot()["bytes"] == 0


# -- pixels --------------------------------------------------------------


def test_pixels_single_frame_boundary():
    b, _ = budget(max_pixels=100)
    assert b.check_pixels(10, 10) is None
    assert b.check_pixels(10, 10) == "pixels_limit"


def test_pixels_accumulate_across_frames():
    # 4 frames of 6x6 = 144 pixels against a 100 pixel ceiling: the third frame
    # is the one that crosses it, and a per-frame check alone would never notice.
    b, _ = budget(max_pixels=100)
    assert b.check_pixels(6, 6) is None
    assert b.check_pixels(6, 6) is None
    assert b.check_pixels(6, 6) == "pixels_limit"
    assert b.snapshot()["pixels"] == 108


def test_pixels_zero_width_is_a_noop():
    b, _ = budget(max_pixels=10)
    assert b.check_pixels(0, 1000) is None
    assert b.snapshot()["pixels"] == 0


# -- frames --------------------------------------------------------------


def test_frames_accumulate():
    b, _ = budget(max_frames=3)
    assert b.check_frames() is None
    assert b.check_frames(2) is None
    assert b.check_frames() == "frames_limit"
    assert b.snapshot()["frames"] == 4


def test_frames_exact_boundary_is_allowed():
    b, _ = budget(max_frames=2)
    assert b.check_frames(2) is None


# -- time ----------------------------------------------------------------


def test_time_below_and_at_the_limit():
    b, clock = budget(max_seconds=5)
    clock.advance(4.999)
    assert b.check_time() is None
    clock.advance(0.001)
    assert b.check_time() == "time_limit"


def test_time_after_the_limit_stays_refused():
    b, clock = budget(max_seconds=1)
    clock.advance(100)
    assert b.check_time() == "time_limit"
    assert b.snapshot()["elapsed_seconds"] == 100.0


def test_time_only_uses_the_injected_clock():
    b, clock = budget(max_seconds=10)
    assert b.snapshot()["elapsed_seconds"] == 0.0
    clock.advance(2.5)
    assert b.snapshot()["elapsed_seconds"] == 2.5
    assert b.check_time() is None


# -- cancel --------------------------------------------------------------


def test_cancel_is_a_reason_not_an_exception():
    b, _ = budget()
    assert b.check_stop(None) is None
    assert b.check_stop(StopFlag(False)) is None
    assert b.check_stop(StopFlag(True)) == "cancel"


def test_stop_protocol_is_satisfied_by_a_plain_object():
    assert isinstance(StopFlag(), Stop)


# -- the closed reason set ----------------------------------------------


def test_every_reason_comes_from_the_closed_set():
    seen = set()
    b, clock = budget(max_bytes=1, max_pixels=1, max_frames=1, max_seconds=1)
    seen.add(b.check_bytes(2))
    seen.add(b.check_pixels(2, 2))
    seen.add(b.check_frames(2))
    clock.advance(2)
    seen.add(b.check_time())
    seen.add(b.check_stop(StopFlag(True)))
    assert seen == REASONS


def test_no_check_raises_when_the_limit_is_reached():
    b, clock = budget(max_bytes=1, max_pixels=1, max_frames=1, max_seconds=0.001)
    clock.advance(1)
    # Reaching or passing a limit must be an answer, never an exception: the
    # decoder turns it into "undecided" and only it may say "failed".
    for call in (
        lambda: b.check_bytes(10 ** 9),
        lambda: b.check_pixels(10 ** 6, 10 ** 6),
        lambda: b.check_frames(10 ** 6),
        b.check_time,
        lambda: b.check_stop(StopFlag(True)),
    ):
        assert call() in REASONS


# -- argument validation -------------------------------------------------


@pytest.mark.parametrize("value", [-1, True, 1.5, "10", None])
def test_invalid_byte_increments_raise(value):
    b, _ = budget()
    with pytest.raises(ValueError):
        b.check_bytes(value)


@pytest.mark.parametrize("width,height", [(-1, 10), (10, -1), (True, 10), (10, 1.5)])
def test_invalid_pixel_dimensions_raise(width, height):
    b, _ = budget()
    with pytest.raises(ValueError):
        b.check_pixels(width, height)


@pytest.mark.parametrize("value", [-1, True, 2.0])
def test_invalid_frame_increments_raise(value):
    b, _ = budget()
    with pytest.raises(ValueError):
        b.check_frames(value)


def test_budget_requires_limits_and_a_callable_clock():
    with pytest.raises(TypeError):
        Budget({"max_bytes": 1})
    with pytest.raises(TypeError):
        Budget(DecodeLimits(), clock=5)


# -- isolation and facts -------------------------------------------------


def test_two_budgets_from_one_limit_object_are_independent():
    limits = DecodeLimits(max_bytes=10)
    first = Budget(limits, clock=FakeClock())
    second = Budget(limits, clock=FakeClock())
    assert first.check_bytes(10) is None
    assert second.snapshot()["bytes"] == 0
    assert second.check_bytes(10) is None


def test_snapshot_reports_the_limits_it_ran_under():
    b, _ = budget(max_bytes=7, max_pixels=9, max_frames=2, max_seconds=1.5)
    b.check_bytes(3)
    b.check_pixels(1, 2)
    b.check_frames()
    snapshot = b.snapshot()
    assert snapshot["limits"] == {
        "max_bytes": 7,
        "max_pixels": 9,
        "max_frames": 2,
        "max_seconds": 1.5,
    }
    assert snapshot["bytes"] == 3 and snapshot["pixels"] == 2 and snapshot["frames"] == 1
    assert math.isclose(snapshot["elapsed_seconds"], 0.0)

def test_the_default_clock_resolves_a_fractional_limit():
    # Windows time.monotonic ticks about every 15.6 ms, so a default clock of
    # monotonic would report "no time spent" for any smaller limit.  The default
    # is pinned here so a refactor cannot quietly go back to it; the behaviour is
    # checked with a real (short) sleep rather than by reading the signature.
    import time
    from core.decode_limits import Budget
    import inspect

    default = inspect.signature(Budget.__init__).parameters["clock"].default
    assert default is time.perf_counter

    budget_ = Budget(DecodeLimits(max_seconds=0.01))
    time.sleep(0.05)
    assert budget_.check_time() == "time_limit"
    assert budget_.snapshot()["elapsed_seconds"] >= 0.01


# -- the decoder delegates to this budget (P2-1 convergence) -------------


def test_the_decoder_owns_no_limits_of_its_own():
    """One definition of the defaults: this module. Anything else is a fork."""
    import inspect

    from core import png_decode

    for name in (
        "DEFAULT_MAX_BYTES",
        "DEFAULT_MAX_PIXELS",
        "_DEFAULT_MAX_BYTES",
        "_DEFAULT_MAX_PIXELS",
        "_Budget",
    ):
        assert not hasattr(png_decode, name), name

    default = inspect.signature(png_decode.decode_png).parameters["limits"].default
    assert default is None  # "no argument" means DecodeLimits(), not a hidden copy


def test_the_maintenance_limits_are_an_explicit_named_object():
    """quality_check uses its own larger limits; both are named, neither is hidden."""
    from core import quality_check

    maintenance = quality_check.DEFAULT_LIMITS
    assert isinstance(maintenance, DecodeLimits)
    library = DecodeLimits()
    assert maintenance.as_dict() != library.as_dict()
    assert maintenance.max_bytes > library.max_bytes
    # The difference is deliberate: full-size pages versus a library default.
    assert maintenance.max_bytes == 256 * 1024 * 1024
