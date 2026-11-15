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
             <either>  the reason is pages_content_mismatch:<n> when n of the
                       missing pages were saved with another content version:
                       "required bytes absent" and "nothing saved" differ
             unknown   saving_not_measured: pages_saved itself is absent;
                       saving_not_provable:<key>: works_seen or excluded_by_filter
                       is absent, so "how many pages should exist" cannot be
                       computed and pages_saved cannot be called complete
             complete  run finished, every artist finished, all three counts
                       present and works_seen - excluded_by_filter <= pages_saved
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


def _delivery(
    run: Mapping[str, Any],
    artists: list[Mapping[str, Any]],
    facts: Mapping[str, Any],
    counts: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
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
    # Completeness is *page* evidence, never a work-minus-page arithmetic: one
    # work can be three pages, so "works seen minus excluded <= pages saved"
    # compared two different units and could call an undelivered work finished.
    # The one formula lives in the counters module; this function only maps its
    # answer onto the three delivery states.
    evidence = None
    if isinstance(counts, Mapping):
        evidence = counts.get("delivery_evidence")
    if isinstance(evidence, Mapping):
        state = str(evidence.get("state") or "")
        if state == "complete":
            return {"state": "complete", "reason": None}
        if state == "incomplete":
            missing = evidence.get("missing")
            expected = evidence.get("expected")
            stale = evidence.get("stale")
            missing_count = missing if type(missing) is int else 0
            expected_count = expected if type(expected) is int else 0
            stale_count = stale if type(stale) is int else 0
            # A page saved with another content version is not the page that was
            # required, so it stays a gap; but "nothing was saved" would be a
            # different, false statement, so the reason names the mismatch.
            reason = (
                "pages_content_mismatch:%d" % stale_count
                if stale_count
                else "pages_not_delivered:%d" % missing_count
            )
            if missing_count and missing_count == expected_count:
                return {"state": "missing", "reason": reason}
            return {"state": "partial", "reason": reason}
        return {
            "state": "unknown",
            "reason": str(evidence.get("reason") or "page_evidence_missing:pages_expected"),
        }
    return {"state": "unknown", "reason": "page_evidence_missing:pages_expected"}


def coverage_states(
    facts: object, *, counts: Mapping[str, Any] | None = None
) -> dict[str, dict[str, Any]]:
    """Judge the three states of one walk.  Reads the facts, changes nothing.

    counts is the report from core.follow_coverage_counts.  Passing it in keeps
    one formula for completeness instead of two copies; when it is omitted the
    counters are computed here, so a caller cannot get a delivery verdict that
    disagrees with the numbers it was shown.
    """
    mapping = _mapping(facts, what="facts")
    run = _mapping(mapping.get("run"), what="run")
    artists = _artists(mapping)
    if counts is None:
        from core.follow_coverage_counts import coverage_counts

        counts = coverage_counts(mapping)
    return {
        "enumeration": _enumeration(run, artists),
        "decision": _decision(run, mapping),
        "delivery": _delivery(run, artists, mapping, counts),
    }
