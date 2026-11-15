"""Counters and denominators for one follow-history coverage summary.

Three families of numbers, deliberately kept apart because they count three
different things:

* **artists** - counted from facts["artists"], because that list *is* the
  enumeration this run performed.  Five buckets, one identity.
* **works** - works that were seen, excluded by the filter, deliverable, or
  undecided.  A work is not an image page: one work can be three pages, so no
  identity here may compare a work count with a page count.
* **pages** - image pages, counted by page identity and content version, and
  only from real page evidence (expected page -> required content version, plus
  per-page observations).  A page proves delivery only when one observation of
  it carries *the* required content version: a page whose requirement is
  unpinned cannot be proven, and neither can one observed with another version.
  Without page evidence at all the page side is unknown, and the delivery
  cannot be called complete - which is the honest answer for crawl facts alone.

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

#: Page states that can prove delivery.  Any other state is a gap, not a save.
PROVING_PAGE_STATES = ("saved_new", "already_verified")

#: One page's verdict against its *required* content version.  Deliberately a
#: closed vocabulary: "we saved something" and "we saved what was required"
#: are different answers, and the delivery formula must not merge them.
DELIVERED = "delivered"
MISSING = "missing"          # no observation, or one that cannot prove a save
STALE = "stale"              # observed content version is not the required one
UNPINNED = "unpinned"        # the requirement carries no content version
UNVERSIONED = "unversioned"  # the observation proves a save but carries none


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


def _content_version(item: Mapping) -> str | None:
    """The content version this row states, or None when it states none."""
    for key in _CONTENT_VERSION_KEYS:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _expected_pages(raw: object, reasons: list) -> dict[str, str | None]:
    """identity -> required content version (None when the row pins none).

    The expected side used to be reduced to a set of identities, which threw
    the requirement itself away: afterwards any non-empty observed version
    counted as delivered.  Keeping the mapping is what lets a page be proven
    or refused instead of merely observed.  Two rows for one identity that
    disagree on the version contradict each other, so that page becomes
    unpinned rather than silently taking whichever row came last.
    """
    if not isinstance(raw, (list, tuple)):
        raise ValueError("facts.pages.expected must be a list")
    expected: dict[str, str | None] = {}
    contradicted = 0
    for item in raw:
        identity = _page_identity(item)
        version = _content_version(item) if isinstance(item, Mapping) else None
        if identity in expected:
            if expected[identity] != version:
                contradicted += 1
                expected[identity] = None
            continue
        expected[identity] = version
    if contradicted:
        reasons.append("expected_content_conflict:%d" % contradicted)
    return expected


def _page_evidence(value: object, reasons: list):
    """(requirements | None, per-page observations, duplicates, conflicts)."""
    if value is None:
        return None, {}, 0, 0
    if not isinstance(value, Mapping):
        raise TypeError("facts.pages must be a mapping or None")

    expected = None
    raw = value.get("expected")
    if raw is not None:
        expected = _expected_pages(raw, reasons)

    observed = value.get("observed")
    if observed is None:
        observed = ()
    if not isinstance(observed, (list, tuple)):
        raise ValueError("facts.pages.observed must be a list")

    pages: dict[str, dict[str, Any]] = {}
    duplicates = 0
    state_conflicts = 0
    for item in observed:
        if not isinstance(item, Mapping):
            raise ValueError("facts.pages.observed entries must be mappings")
        identity = _page_identity(item)
        state = item.get("state")
        if state not in PAGE_STATE_BUCKETS:
            raise ValueError("unknown page state: %r" % (state,))
        version = _content_version(item)
        entry = pages.get(identity)
        if entry is None:
            pages[identity] = {
                "state": state,
                "versions": {version} if version else set(),
            }
            continue
        duplicates += 1
        if version:
            entry["versions"].add(version)
        if entry["state"] != state:
            state_conflicts += 1
            # Two observations, two answers: neither can be trusted to prove
            # delivery, so the page is parked in "needs another look".
            entry["state"] = "pending_verify"
    if state_conflicts:
        reasons.append("page_state_conflict:%d" % state_conflicts)

    # A page observed with two different content versions is a contradiction
    # even when both observations agree on the state: "one of them matches" is
    # not a proof, and picking the newer one would be guessing from list order.
    contradictory = sorted(
        identity for identity, entry in pages.items() if len(entry["versions"]) > 1
    )
    for identity in contradictory:
        pages[identity]["state"] = "pending_verify"
    if contradictory:
        reasons.append("page_content_conflict:%d" % len(contradictory))
    return expected, pages, duplicates, state_conflicts + len(contradictory)


def _page_verdict(identity: str, required: str | None, pages: Mapping[str, Any]) -> str:
    """Did this page deliver *the required* content version?

    The ways to fail are kept apart because they mean different things to the
    person who has to fix them: nothing was saved (MISSING), something else was
    saved (STALE), or the evidence cannot say which version was required or was
    saved (UNPINNED, UNVERSIONED).  Only DELIVERED proves the page.

    A page observed with two different versions never reaches here as a
    "delivered" candidate: _page_evidence parks it as pending_verify, so it is
    a MISSING page and the contradiction is counted separately.
    """
    entry = pages.get(identity)
    if entry is None:
        return MISSING
    if entry["state"] not in PROVING_PAGE_STATES:
        return MISSING
    versions = entry["versions"]
    if len(versions) > 1:  # pragma: no cover - parked before the verdict runs
        return MISSING
    if not versions:
        return UNPINNED if required is None else UNVERSIONED
    if required is None:
        return UNPINNED
    return DELIVERED if next(iter(versions)) == required else STALE


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
        extra = [identity for identity in pages if identity not in expected]
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
        verdicts = {
            identity: _page_verdict(identity, required, pages)
            for identity, required in expected.items()
        }
        missing = sorted(
            identity for identity, verdict in verdicts.items()
            if verdict in (MISSING, STALE)
        )
        stale = sorted(i for i, v in verdicts.items() if v == STALE)
        # A page carrying two different observed versions is a contradiction in
        # the evidence itself, whether or not its state still looks saved.
        contradictory = sorted(
            identity
            for identity in expected
            if len(pages.get(identity, {}).get("versions", ())) > 1
        )
        unpinned = sorted(i for i, v in verdicts.items() if v == UNPINNED)
        unversioned = sorted(i for i, v in verdicts.items() if v == UNVERSIONED)
        if stale:
            reasons.append("page_content_mismatch:%d" % len(stale))
        if missing:
            state, reason = "incomplete", "pages_not_delivered:%d" % len(missing)
        elif unpinned:
            state = "unknown"
            reason = "expected_content_version_missing:%d" % len(unpinned)
        elif unversioned:
            state = "unknown"
            reason = "observed_content_version_missing:%d" % len(unversioned)
        else:
            state, reason = "complete", None
        delivery = {
            "state": state,
            "reason": reason,
            "expected": len(expected),
            "missing": len(missing),
            "delivered": len(expected) - len(missing) - len(unpinned) - len(unversioned),
            "stale": len(stale),
            "conflict": len(contradictory),
            "unpinned": len(unpinned),
            "unversioned": len(unversioned),
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
