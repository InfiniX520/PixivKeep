"""Counters and denominators for one follow-history coverage summary.

Three families of numbers, deliberately kept apart because they count three
different things:

* **artists** - counted from facts["artists"], because that list *is* the
  enumeration this run performed.  Five buckets, one identity.
* **works** - works that were seen, excluded by the filter, deliverable, or
  undecided.  A work is not an image page: one work can be three pages, so no
  identity here may compare a work count with a page count.
* **pages** - image pages, counted by page identity and content version, and
  only from real page evidence (expected page set + per-page observations).
  Without that evidence the page side is unknown, and the delivery cannot be
  called complete - which is the honest answer for crawl facts alone.

A run level total may fill a work or a legacy page number; nothing may be
scaled up from the artists list except artworks_seen, and only when that list
is *proven* to be the whole run (declared total equals its length and every
artist carries a usable work count).  That is arithmetic on complete rows, not
an estimate, and the report names the source.

`consistent` is True only when the numbers at hand do not contradict each
other.  It never compares works with pages: the old
"works_seen == pages_saved + excluded_by_filter" check was wrong for exactly
that reason.
"""
from __future__ import annotations

from typing import Any, Mapping

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

#: Work level: unit is one work, never one image page.
WORK_COUNT_KEYS = (
    "artworks_seen",
    "excluded_by_filter",
    "works_deliverable",
    "works_unknown",
)

#: Page level: unit is one image page, counted by page identity.
PAGE_COUNT_KEYS = (
    "pages_expected",
    "pages_saved",
    "pages_saved_new",
    "pages_already_verified",
    "pages_failed",
    "pages_pending_verify",
    "duplicate_pages",
)

#: Raw source facts kept for the words and the decision, not for completeness.
RAW_COUNT_KEYS = ("unknown_fields",)

COUNT_KEYS = ARTIST_COUNT_KEYS + WORK_COUNT_KEYS + PAGE_COUNT_KEYS + RAW_COUNT_KEYS
DENOMINATOR_KEYS = ("artists_total", "artworks_total")

#: Observed page states.  A state outside this table is a caller bug.
PAGE_STATE_BUCKETS: dict[str, str] = {
    "saved_new": "pages_saved_new",
    "already_verified": "pages_already_verified",
    "failed": "pages_failed",
    "pending_verify": "pages_pending_verify",
}

_WORKS_TOTAL_ALIASES = ("works_seen", "artworks_seen")
_WORKS_GRAND_TOTAL_ALIASES = ("artworks_total", "works_total")
_CONTENT_VERSION_KEYS = ("content_sha256", "content_version", "sha256")


def _int_or_none(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _aliased(totals: Mapping[str, Any], names: tuple, *, what: str) -> int | None:
    """One total under several spellings; two different answers is a bug."""
    found = [(name, totals[name]) for name in names if name in totals]
    if not found:
        return None
    first_name, first = found[0]
    for name, value in found[1:]:
        if value != first:
            raise ValueError(
                "totals carries both %s=%r and %s=%r" % (first_name, first, name, value)
            )
    return _int_or_none(first)


def _split(value: object):
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


def _artists(artists: list, reasons: list):
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
        reasons.append("unknown_artist_state:" + ",".join(sorted(unknown_states)))
    return {
        "artists_checked": len(seen),
        "artists_confirmed": buckets["confirmed"],
        "artists_pending": buckets["pending"],
        "artists_failed_pages": buckets["failed_pages"],
        "artists_coverage_unverified": buckets["unverified"],
        "_unknown_states": len(unknown_states),
    }


def _page_identity(item: object) -> str:
    """One page, named so that two observations of it collapse into one."""
    if isinstance(item, str) and item.strip():
        return item.strip()
    if isinstance(item, Mapping):
        named = item.get("page_identity", item.get("id"))
        if isinstance(named, str) and named.strip():
            return named.strip()
        work = item.get("work_id", item.get("illust_id"))
        page = item.get("page", item.get("page_index"))
        if work is None or page is None:
            raise ValueError("a page needs page_identity or work_id+page")
        return "%s#%s" % (work, int(page))
    raise ValueError("a page identity must be a string or a mapping")


def _content_version(item: Mapping) -> str:
    for key in _CONTENT_VERSION_KEYS:
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _page_evidence(value: object, reasons: list):
    """(expected identities | None, per-page state, duplicates, conflicts)."""
    if value is None:
        return None, {}, 0, 0
    if not isinstance(value, Mapping):
        raise TypeError("facts.pages must be a mapping or None")

    expected = None
    raw = value.get("expected")
    if raw is not None:
        if not isinstance(raw, (list, tuple)):
            raise ValueError("facts.pages.expected must be a list")
        expected = tuple(sorted({_page_identity(item) for item in raw}))

    observed = value.get("observed")
    if observed is None:
        observed = ()
    if not isinstance(observed, (list, tuple)):
        raise ValueError("facts.pages.observed must be a list")

    pages: dict[str, dict[str, Any]] = {}
    duplicates = 0
    conflicts = 0
    for item in observed:
        if not isinstance(item, Mapping):
            raise ValueError("facts.pages.observed entries must be mappings")
        identity = _page_identity(item)
        state = item.get("state")
        if state not in PAGE_STATE_BUCKETS:
            raise ValueError("unknown page state: %r" % (state,))
        content = _content_version(item)
        entry = pages.get(identity)
        if entry is None:
            pages[identity] = {"state": state, "content": {content} if content else set()}
            continue
        duplicates += 1
        if content:
            entry["content"].add(content)
        if entry["state"] != state:
            conflicts += 1
            # Two observations, two answers: neither can be trusted to prove
            # delivery, so the page is parked in "needs another look".
            entry["state"] = "pending_verify"
    if conflicts:
        reasons.append("page_state_conflict:%d" % conflicts)
    return expected, pages, duplicates, conflicts


def _delivered(identity: str, pages: Mapping[str, Any]) -> bool:
    entry = pages.get(identity)
    if entry is None:
        return False
    if entry["state"] not in ("saved_new", "already_verified"):
        return False
    return bool(entry["content"])


def coverage_counts(facts: object) -> dict[str, object]:
    """Buckets, the identity checks, the page evidence and the denominators."""
    artists, totals = _split(facts)
    reasons: list[str] = []
    counts: dict[str, int | None] = {}

    artist_counts = _artists(artists, reasons)
    for key in ARTIST_COUNT_KEYS:
        counts[key] = artist_counts[key]

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

    declared = _aliased(totals, ("artists_total", "total_artists"), what="artists_total")
    artists_total = declared
    if declared is None:
        reasons.append("denominator_unknown:artists_total")
    elif declared < len(artists):
        consistent = False
        reasons.append("total_artists_mismatch:%d<%d" % (declared, len(artists)))

    artworks_total = _aliased(totals, _WORKS_GRAND_TOTAL_ALIASES, what="artworks_total")

    # ---- works layer -------------------------------------------------
    works_seen = _aliased(totals, _WORKS_TOTAL_ALIASES, what="artworks_seen")
    if works_seen is None:
        provable = (
            declared is not None
            and declared == len(artists)
            and all(
                type(item.get("works_seen")) is int and item.get("works_seen") >= 0
                for item in artists
            )
        )
        if provable:
            works_seen = sum(int(item["works_seen"]) for item in artists)
            reasons.append("artworks_seen_from_artist_sum")
        else:
            reasons.append("artworks_seen_unknown:artist_list_not_proven_complete")
    counts["artworks_seen"] = works_seen

    excluded = _int_or_none(totals.get("excluded_by_filter"))
    counts["excluded_by_filter"] = excluded
    if excluded is None:
        reasons.append("not_measured:excluded_by_filter")

    deliverable = None
    if works_seen is not None and excluded is not None:
        if works_seen < excluded:
            consistent = False
            reasons.append("excluded_by_filter_exceeds_works_seen:%d>%d" % (excluded, works_seen))
        else:
            deliverable = works_seen - excluded
    counts["works_deliverable"] = deliverable

    counts["works_unknown"] = _int_or_none(totals.get("works_unknown"))

    # ---- pages layer -------------------------------------------------
    expected, pages, duplicates, _conflicts = _page_evidence((facts or {}).get("pages"), reasons)
    page_counts = {key: 0 for key in PAGE_STATE_BUCKETS.values()}
    for entry in pages.values():
        page_counts[PAGE_STATE_BUCKETS[entry["state"]]] += 1

    if expected is None:
        counts["pages_expected"] = None
        for key in ("pages_saved_new", "pages_failed", "pages_pending_verify"):
            counts[key] = page_counts[key] if pages else None
        counts["pages_already_verified"] = (
            page_counts["pages_already_verified"]
            if pages
            else _int_or_none(totals.get("pages_already_verified"))
        )
        counts["duplicate_pages"] = duplicates if pages else _int_or_none(totals.get("duplicate_pages"))
        for key in (
            "pages_expected",
            "pages_saved_new",
            "pages_failed",
            "pages_pending_verify",
            "pages_already_verified",
            "duplicate_pages",
        ):
            if counts.get(key) is None:
                reasons.append("not_measured:" + key)
        reasons.append("page_evidence_missing:pages_expected")
    else:
        counts["pages_expected"] = len(expected)
        counts["pages_saved_new"] = page_counts["pages_saved_new"]
        counts["pages_already_verified"] = page_counts["pages_already_verified"]
        counts["pages_failed"] = page_counts["pages_failed"]
        counts["pages_pending_verify"] = page_counts["pages_pending_verify"]
        counts["duplicate_pages"] = duplicates
        covered = (
            counts["pages_saved_new"]
            + counts["pages_already_verified"]
            + counts["pages_failed"]
            + counts["pages_pending_verify"]
        )
        if covered > len(expected):
            consistent = False
            reasons.append("page_buckets_exceed_expected:%d>%d" % (covered, len(expected)))
        extra = [identity for identity in pages if identity not in set(expected)]
        if extra:
            reasons.append("observed_page_outside_expected:%d" % len(extra))

    # Legacy crawl total: displayed, never used to prove completeness.
    counts["pages_saved"] = _int_or_none(totals.get("pages_saved"))
    if counts["pages_saved"] is None:
        reasons.append("not_measured:pages_saved")
    if counts["duplicate_pages"] is not None and counts["pages_saved"] is not None:
        if counts["duplicate_pages"] > counts["pages_saved"]:
            consistent = False
            reasons.append("duplicate_pages_exceed_pages_saved")

    counts["unknown_fields"] = _int_or_none(totals.get("unknown_fields"))
    if counts["unknown_fields"] is None:
        reasons.append("not_measured:unknown_fields")

    # ---- delivery evidence: the one formula the verdicts read --------
    if expected is None:
        delivery = {
            "state": "unknown",
            "reason": "page_evidence_missing:pages_expected",
            "expected": None,
            "missing": None,
        }
    else:
        missing = [identity for identity in expected if not _delivered(identity, pages)]
        if not missing:
            delivery = {"state": "complete", "reason": None, "expected": len(expected), "missing": 0}
        else:
            delivery = {
                "state": "incomplete",
                "reason": "pages_not_delivered:%d" % len(missing),
                "expected": len(expected),
                "missing": len(missing),
            }

    for key in COUNT_KEYS:
        value = counts.get(key)
        if value is not None and (type(value) is not int or value < 0):
            consistent = False
            reasons.append("not_a_count:" + key)

    return {
        "counts": {key: counts.get(key) for key in COUNT_KEYS},
        "denominators": {"artists_total": artists_total, "artworks_total": artworks_total},
        "consistent": bool(consistent),
        "reasons": sorted(set(reasons)),
        "delivery_evidence": delivery,
    }
