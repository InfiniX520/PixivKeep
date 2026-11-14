"""Counters and denominators for one follow-history coverage summary.

This module answers two questions and refuses a third:

* how many artists fall in each bucket, derived only from the states the crawl
  repository actually records;
* which totals can be divided by, taken only from a *run level* total - a list
  length is not a denominator, because the list may be a page of a larger set;
* and it never estimates.  A number nobody measured is None, and the caller has
  to render "unknown" instead of a plausible-looking count.

The identity confirmed + pending + failed_pages + unverified == checked is the
one check that can always be made, so it is the one that decides
`consistent`.  Everything else (totals agreeing with the sums) is checked when
the facts carry it, and failures are reported as reasons rather than silence.
"""
from __future__ import annotations

from typing import Any, Mapping

#: Which bucket each recorded artist state belongs to.  The crawl state machine
#: uses these seven; anything else is "unverified" - not "pending", because a
#: state nobody here recognises must not be reported as ordinary progress.
ARTIST_BUCKETS: dict[str, str] = {
    "completed": "confirmed",
    "empty": "confirmed",
    "pending": "pending",
    "running": "pending",
    "deferred": "pending",
    "failed": "failed_pages",
}

COUNT_KEYS = (
    "artists_checked",
    "artists_confirmed",
    "artists_pending",
    "artists_failed_pages",
    "artists_coverage_unverified",
    "artworks_seen",
    "pages_saved",
    "pages_already_verified",
    "excluded_by_filter",
    "unknown_fields",
)

#: Counts that only a run level total can answer; without one they stay None.
_TOTAL_BACKED = ("artworks_seen", "pages_saved", "pages_already_verified",
                 "excluded_by_filter", "unknown_fields")


def _int_or_none(value: object) -> int | None:
    if type(value) is int and value >= 0:
        return value
    return None


def _facts(value: object) -> tuple[list[Mapping[str, Any]], Mapping[str, Any]]:
    if not isinstance(value, Mapping):
        raise TypeError("facts must be a mapping")
    artists = value.get("artists")
    if not isinstance(artists, list):
        raise ValueError("facts.artists must be a list")
    run = value.get("run")
    if not isinstance(run, Mapping):
        run = {}
    totals = run.get("totals")
    if not isinstance(totals, Mapping):
        totals = {}
    for item in artists:
        if not isinstance(item, Mapping):
            raise ValueError("facts.artists entries must be mappings")
        if not isinstance(item.get("artist_id"), str) or not item.get("artist_id"):
            raise ValueError("facts.artists entries need a non-empty artist_id")
        if not isinstance(item.get("state"), str):
            raise ValueError("facts.artists entries need a state string")
    return list(artists), totals


def coverage_counts(facts: object) -> dict[str, object]:
    """Buckets, the identity check, and the denominators that are provable."""
    artists, totals = _facts(facts)
    reasons: list[str] = []

    seen: dict[str, str] = {}
    duplicates = 0
    for item in artists:
        artist_id = str(item["artist_id"])
        if artist_id in seen:
            duplicates += 1
            # Two rows for one artist cannot both be counted; the first one wins
            # and the duplicate is a fact about the input, not about coverage.
            if seen[artist_id] != str(item["state"]):
                reasons.append("duplicate_artist_state_conflict:%s" % artist_id)
            continue
        seen[artist_id] = str(item["state"])
    if duplicates:
        reasons.append("duplicate_artist:%d" % duplicates)

    buckets = {"confirmed": 0, "pending": 0, "failed_pages": 0, "unverified": 0}
    unknown_states: list[str] = []
    for artist_id, state in seen.items():
        bucket = ARTIST_BUCKETS.get(state)
        if bucket is None:
            buckets["unverified"] += 1
            if state not in unknown_states:
                unknown_states.append(state)
        else:
            buckets[bucket] += 1
    if unknown_states:
        reasons.append("unknown_artist_state:" + ",".join(sorted(unknown_states)))

    counts: dict[str, int | None] = {
        "artists_checked": len(seen),
        "artists_confirmed": buckets["confirmed"],
        "artists_pending": buckets["pending"],
        "artists_failed_pages": buckets["failed_pages"],
        "artists_coverage_unverified": buckets["unverified"],
    }
    for key in _TOTAL_BACKED:
        counts[key] = _int_or_none(totals.get(key))
    for key in _TOTAL_BACKED:
        if counts[key] is None:
            reasons.append("not_measured:" + key)
    for key in COUNT_KEYS:
        value = counts[key]
        if value is not None and value < 0:
            reasons.append("negative:" + key)

    identity = (
        counts["artists_confirmed"]
        + counts["artists_pending"]
        + counts["artists_failed_pages"]
        + counts["artists_coverage_unverified"]
    )
    consistent = identity == counts["artists_checked"]
    if not consistent:
        reasons.append(
            "artist_buckets_do_not_add_up:%d!=%d" % (identity, counts["artists_checked"])
        )
    pages_saved = counts["pages_saved"]
    artworks_seen = counts["artworks_seen"]
    if artworks_seen is not None and pages_saved is not None and pages_saved > artworks_seen:
        consistent = False
        reasons.append("pages_saved_exceeds_artworks_seen")
    for key in COUNT_KEYS:
        if counts[key] is not None and type(counts[key]) is not int:
            consistent = False
            reasons.append("not_a_count:" + key)

    denominators = {
        "artists_total": _int_or_none(totals.get("artists_total")),
        "artworks_total": _int_or_none(totals.get("artworks_total")),
    }
    for key, value in denominators.items():
        if value is None:
            reasons.append("denominator_unknown:" + key)
        if value is not None and value < 0:
            consistent = False
            reasons.append("negative:" + key)
    if denominators["artists_total"] is not None and denominators["artists_total"] < len(seen):
        consistent = False
        reasons.append("denominator_below_seen_artists")

    return {
        "counts": {key: counts[key] for key in COUNT_KEYS},
        "denominators": denominators,
        "consistent": bool(consistent),
        "reasons": sorted(set(reasons)),
    }
