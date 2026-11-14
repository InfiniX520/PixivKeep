"""Counters and denominators for one follow-history coverage summary.

Two families of numbers, deliberately kept apart:

* **artist level** - counted from `facts["artists"]`, because that list *is* the
  enumeration this run performed.  Five buckets, one identity.
* **run level** - only from `facts["run"]["totals"]`.  The artists list is not
  known to be exhaustive over the range, so summing it or scaling it up would
  be an estimate wearing a counter's clothes.  A missing key is None.

The one exception is `works_seen`: when the artist list can be *proven* to be
the whole run (its length equals the declared total and every artist carries a
usable page count), the sum of those counts is the number of works seen.  That
is arithmetic on proven-complete rows, not scaling, so the report names the
source (`works_seen_from_artist_sum`) instead of hiding it.  Nothing else may be
derived this way: no other run level number has a per-artist carrier.

`consistent` is True only when every check that can be made with the numbers at
hand holds: the artist buckets add up, no unknown state was silently filed, the
run totals agree with each other, and the denominators are not contradicted by
what was actually seen.
"""
from __future__ import annotations

from typing import Any, Mapping

#: Recorded crawl states to their bucket.  A state outside this table is
#: `unverified` **and** makes the report inconsistent: an unrecognised state is
#: not ordinary progress, and dropping it silently would hide work that nobody
#: accounted for.
ARTIST_BUCKETS: dict[str, str] = {
    "completed": "confirmed",
    "empty": "confirmed",
    "pending": "pending",
    "running": "pending",
    "deferred": "pending",
    "failed": "failed_pages",
}

ARTIST_COUNT_KEYS = (
    "artists_checked",
    "artists_confirmed",
    "artists_pending",
    "artists_failed_pages",
    "artists_coverage_unverified",
)

#: Only a run level total may fill these; nothing else is accepted.
RUN_COUNT_KEYS = (
    "works_seen",
    "pages_saved",
    "pages_already_verified",
    "excluded_by_filter",
    "unknown_fields",
    "duplicate_pages",
)

COUNT_KEYS = ARTIST_COUNT_KEYS + RUN_COUNT_KEYS

#: Two spellings of the same run total are accepted; nothing else is.
_WORKS_TOTAL_ALIASES = ("artworks_total", "works_total")


def _int_or_none(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _split(value: object) -> tuple[list[Mapping[str, Any]], Mapping[str, Any]]:
    if not isinstance(value, Mapping):
        raise TypeError("facts must be a mapping")
    artists = value.get("artists")
    if not isinstance(artists, list):
        raise ValueError("facts.artists must be a list")
    for item in artists:
        if not isinstance(item, Mapping):
            raise ValueError("facts.artists entries must be mappings")
        if not isinstance(item.get("artist_id"), str) or not item.get("artist_id"):
            raise ValueError("facts.artists entries need a non-empty artist_id")
        if not isinstance(item.get("state"), str):
            raise ValueError("facts.artists entries need a state string")
    run = value.get("run")
    totals = run.get("totals") if isinstance(run, Mapping) else None
    return list(artists), totals if isinstance(totals, Mapping) else {}


def _artists(artists: list[Mapping[str, Any]], reasons: list[str]) -> dict[str, int]:
    seen: dict[str, str] = {}
    duplicates = 0
    for item in artists:
        artist_id = str(item["artist_id"])
        if artist_id in seen:
            duplicates += 1
            if seen[artist_id] != str(item["state"]):
                reasons.append("duplicate_artist_state_conflict:%s" % artist_id)
            continue
        seen[artist_id] = str(item["state"])
    if duplicates:
        reasons.append("duplicate_artist:%d" % duplicates)

    buckets = {"confirmed": 0, "pending": 0, "failed_pages": 0, "unverified": 0}
    unknown_states: list[str] = []
    for state in seen.values():
        bucket = ARTIST_BUCKETS.get(state)
        if bucket is None:
            buckets["unverified"] += 1
            if state not in unknown_states:
                unknown_states.append(state)
        else:
            buckets[bucket] += 1
    if unknown_states:
        # Counted as unverified *and* inconsistent: the numbers add up only
        # because we invented a bucket for a state nobody recognises.
        reasons.append("unknown_artist_state:" + ",".join(sorted(unknown_states)))
    return {
        "artists_checked": len(seen),
        "artists_confirmed": buckets["confirmed"],
        "artists_pending": buckets["pending"],
        "artists_failed_pages": buckets["failed_pages"],
        "artists_coverage_unverified": buckets["unverified"],
        "_unknown_states": len(unknown_states),
        "_duplicates": duplicates,
    }


def coverage_counts(facts: object) -> dict[str, object]:
    """Buckets, the identity checks, and the denominators that are provable."""
    artists, totals = _split(facts)
    reasons: list[str] = []
    artist_counts = _artists(artists, reasons)

    counts: dict[str, int | None] = {key: artist_counts[key] for key in ARTIST_COUNT_KEYS}
    for key in RUN_COUNT_KEYS:
        if key == "works_seen":
            continue  # resolved below: a declared total, or a proven-complete sum
        counts[key] = _int_or_none(totals.get(key))
        if counts[key] is None:
            reasons.append("not_measured:" + key)

    consistent = True

    identity = (
        counts["artists_confirmed"]
        + counts["artists_pending"]
        + counts["artists_failed_pages"]
        + counts["artists_coverage_unverified"]
    )
    if identity != counts["artists_checked"]:
        consistent = False
        reasons.append(
            "artist_buckets_do_not_add_up:%d!=%d" % (identity, counts["artists_checked"])
        )
    if artist_counts["_unknown_states"]:
        consistent = False

    declared = _int_or_none(totals.get("total_artists"))
    artists_total = declared
    if declared is None:
        # A list length is not a denominator: the list may be a page of a
        # larger set, so the honest answer is "unknown", not the number we saw.
        reasons.append("denominator_unknown:artists_total")
    elif declared != len(artists):
        consistent = False
        reasons.append("total_artists_mismatch:%d!=%d" % (declared, len(artists)))

    artworks_total = None
    for key in _WORKS_TOTAL_ALIASES:
        if key in totals:
            artworks_total = _int_or_none(totals[key])
            break

    works_seen = _int_or_none(totals.get("works_seen"))
    if works_seen is None:
        provable = (
            declared is not None
            and declared == len(artists)
            and all(
                type(item.get("pages")) is int and item.get("pages") >= 0
                for item in artists
            )
        )
        if provable:
            works_seen = sum(int(item["pages"]) for item in artists)
            reasons.append("works_seen_from_artist_sum")
        else:
            reasons.append("works_seen_unknown:artist_list_not_proven_complete")
    counts["works_seen"] = works_seen
    pages_saved = counts["pages_saved"]
    excluded = counts["excluded_by_filter"]
    if works_seen is not None and pages_saved is not None and excluded is not None:
        if works_seen != pages_saved + excluded:
            consistent = False
            reasons.append(
                "works_seen_does_not_split:%d!=%d+%d" % (works_seen, pages_saved, excluded)
            )
    duplicate_pages = counts["duplicate_pages"]
    if duplicate_pages is not None and pages_saved is not None and duplicate_pages > pages_saved:
        consistent = False
        reasons.append("duplicate_pages_exceed_pages_saved")

    for key in COUNT_KEYS:
        value = counts[key]
        if value is not None and (type(value) is not int or value < 0):
            consistent = False
            reasons.append("not_a_count:" + key)

    return {
        "counts": {key: counts[key] for key in COUNT_KEYS},
        "denominators": {"artists_total": artists_total, "artworks_total": artworks_total},
        "consistent": bool(consistent),
        "reasons": sorted(set(reasons)),
    }
