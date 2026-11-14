"""Three independent coverage states for one followed-artist walk.

The facts come from follow_crawl_state/follow_repository (see
docs/optimization/2026-11-14/U2A_FIVE_WAY.md).  This module only *judges* them:
it reads nothing, writes nothing and never invents a fact.

Why three states and not one boolean
------------------------------------
"the walk reached the end", "the filter decided" and "the pages were saved"
fail independently.  A walk can enumerate every artist and still have failed
pages; a filter can run and still meet unknown fields; pages can be saved for
an artist whose enumeration was cut short.  Collapsing them into one flag is
how a partial run starts looking complete.

Derivation table (every row has a test)
---------------------------------------
enumeration  failed    run.status == "failed"
             stopped   run.status == "paused"          (budget / rate limit / user pause)
             partial   run.status in {"running","partial"}, or any artist in
                       pending/running/deferred/failed
             complete  otherwise   (unfollowed artists never block completion)
decision     <given>   facts["decision"] carries a valid state
             unknown   totals.unknown_fields > 0
             applied   totals has excluded_by_filter (even 0) and no unknown fields
             skipped   no filter facts at all
delivery     <given>   facts["delivery"] carries a valid state
             partial   any artist in pending/running/deferred/failed (a known gap
                       outranks an unknown one), or run not completed, or a
                       finished run that saved fewer pages than it should have
             missing   the same shortfall when nothing at all was saved
             unknown   no usable pages_saved count: "saved" was never measured,
                       and an unmeasured fact is not a complete delivery
             complete  run finished, every artist finished, pages_saved present
                       and not below works_seen - excluded_by_filter
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

ENUMERATION_STATES = ("complete", "partial", "stopped", "failed")
DECISION_STATES = ("applied", "unknown_fields", "skipped")
DELIVERY_STATES = ("complete", "partial", "missing", "unknown")

ARTIST_RETRY_STATES = ("pending", "running", "deferred", "failed")
ARTIST_FINISHED_STATES = ("completed", "empty", "unfollowed")

_DIMENSIONS = {
    "enumeration": ENUMERATION_STATES,
    "decision": DECISION_STATES,
    "delivery": DELIVERY_STATES,
}


class CoverageStateError(ValueError):
    """The facts dict cannot be judged; this is an input error, not a verdict."""

    def __init__(self, code: str, detail: Any = None) -> None:
        self.code = code
        self.detail = detail
        super().__init__(code)


def _mapping(value: object, *, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CoverageStateError("facts_shape_invalid", what)
    return value


def _state(value: object) -> str:
    return "" if value is None else str(value).strip().lower()


def _given(facts: Mapping[str, Any], name: str) -> dict[str, Any] | None:
    """A directly supplied dimension, when the caller already knows it."""
    raw = facts.get(name)
    if raw is None:
        return None
    entry = _mapping(raw, what=name)
    state = _state(entry.get("state"))
    if state not in _DIMENSIONS[name]:
        raise CoverageStateError("dimension_state_invalid", (name, state))
    reason = entry.get("reason")
    return {"state": state, "reason": None if reason in (None, "") else str(reason)}


def _artists(facts: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = facts.get("artists")
    if raw is None:
        raise CoverageStateError("facts_missing_artists")
    if not isinstance(raw, (list, tuple)):
        raise CoverageStateError("facts_shape_invalid", "artists")
    artists: list[Mapping[str, Any]] = []
    for index, item in enumerate(raw):
        entry = _mapping(item, what="artists[%d]" % index)
        if not isinstance(entry.get("state"), str):
            raise CoverageStateError("artist_state_missing", index)
        artists.append(entry)
    return artists


def _totals(run: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = run.get("totals")
    if raw is None:
        return {}
    return _mapping(raw, what="run.totals")


def _first_retrying(artists: list[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    for artist in sorted(artists, key=lambda entry: str(entry.get("artist_id", ""))):
        if _state(artist.get("state")) in ARTIST_RETRY_STATES:
            return artist
    return None


def _enumeration(run: Mapping[str, Any], artists: list[Mapping[str, Any]]) -> dict[str, Any]:
    status = _state(run.get("status"))
    stop = str(run.get("stop_reason") or "").strip()
    if status == "failed":
        return {"state": "failed", "reason": stop or "run_failed"}
    if status == "paused":
        return {"state": "stopped", "reason": stop or "paused"}
    if status == "partial":
        return {"state": "partial", "reason": stop or "run_partial"}
    retrying = _first_retrying(artists)
    if retrying is not None:
        return {
            "state": "partial",
            "reason": "artist_%s:%s" % (_state(retrying.get("state")), retrying.get("artist_id")),
        }
    if status == "running":
        return {"state": "partial", "reason": "run_running"}
    if status not in {"completed", ""}:
        return {"state": "partial", "reason": "run_status_%s" % status}
    return {"state": "complete", "reason": None}


def _decision(run: Mapping[str, Any], facts: Mapping[str, Any]) -> dict[str, Any]:
    given = _given(facts, "decision")
    if given is not None:
        return given
    totals = _totals(run)
    unknown = totals.get("unknown_fields")
    excluded = totals.get("excluded_by_filter")
    if isinstance(unknown, int) and not isinstance(unknown, bool) and unknown > 0:
        return {"state": "unknown_fields", "reason": "unknown_fields:%d" % unknown}
    if isinstance(excluded, int) and not isinstance(excluded, bool):
        # The filter ran; zero exclusions is still a decision, not a missing fact.
        return {"state": "applied", "reason": None}
    return {"state": "skipped", "reason": "no_filter_facts"}


def _count(value: object) -> int | None:
    """A usable non-negative count, or None when the fact is not a measurement."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _expected_pages(totals: Mapping[str, Any]) -> int | None:
    """How many pages this scope should have produced, when it is provable."""
    seen = _count(totals.get("works_seen"))
    excluded = _count(totals.get("excluded_by_filter"))
    if seen is None or excluded is None:
        return None
    return max(0, seen - excluded)


def _delivery(run: Mapping[str, Any], artists: list[Mapping[str, Any]], facts: Mapping[str, Any]) -> dict[str, Any]:
    given = _given(facts, "delivery")
    if given is not None:
        return given
    if not artists:
        # Nothing was followed: there is nothing this walk failed to deliver.
        return {"state": "complete", "reason": "no_artists"}
    retrying = _first_retrying(artists)
    if retrying is not None:
        # A known gap outranks an unknown one: do not hide "still running"
        # behind "we did not measure the saving side".
        return {
            "state": "partial",
            "reason": "artist_%s:%s" % (_state(retrying.get("state")), retrying.get("artist_id")),
        }
    if _state(run.get("status")) != "completed":
        return {"state": "partial", "reason": "run_not_completed"}
    unfinished = [
        artist
        for artist in artists
        if _state(artist.get("state")) not in ARTIST_FINISHED_STATES
    ]
    if unfinished:
        return {
            "state": "partial",
            "reason": "artist_%s:%s" % (_state(unfinished[0].get("state")), unfinished[0].get("artist_id")),
        }
    totals = _totals(run)
    saved = _count(totals.get("pages_saved"))
    if saved is None:
        # The enumeration may be finished, but "saved" was never measured:
        # an unmeasured side cannot be reported as a complete delivery.
        return {"state": "unknown", "reason": "saving_not_measured"}
    expected = _expected_pages(totals)
    if expected is not None and expected > saved:
        short = expected - saved
        if saved == 0:
            return {"state": "missing", "reason": "pages_not_saved:%d" % short}
        return {"state": "partial", "reason": "pages_not_saved:%d" % short}
    return {"state": "complete", "reason": None}


def coverage_states(facts: object) -> dict[str, dict[str, Any]]:
    """Judge the three states of one walk.  Reads the facts, changes nothing."""
    mapping = _mapping(facts, what="facts")
    run = _mapping(mapping.get("run"), what="run")
    artists = _artists(mapping)
    return {
        "enumeration": _enumeration(run, artists),
        "decision": _decision(run, mapping),
        "delivery": _delivery(run, artists, mapping),
    }
