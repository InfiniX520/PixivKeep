"""Synthetic follow-crawl facts for the U2-A coverage matrix.

Each builder returns a plain dict shaped like the frozen A1 contract::

    {"run": {run_id, account_ref, config_key, status, stop_reason, totals},
     "artists": [{artist_id, state, pages, newest_illust_id}],
     "incomplete": [{artist_id, reason}],
     "budget": {state, remaining} | None}

Two things the contract did not spell out, and how they are carried here:

* artist['pages'] is the number of works that artist showed this run - the
  real A1 reads it from artist_progress.works_seen.  It is NOT the number of
  pages saved: filters exclude works before any page is written, so
  sum(pages) and pages_saved are different numbers on purpose.
* run-level counters (works seen, pages saved, filtered, unknown fields,
  duplicate pages, already-verified pages) live in run['totals'].  A real
  FollowCrawlRun does not carry them either, so A1 is expected to fill
  totals from the run summary it already refreshes; A3 then prefers totals
  and falls back to summing the per-artist pages.  If that is the wrong home,
  the contract needs one more key rather than a guess here.

Every fixture keeps these two identities true:

    works_seen == sum(artist.pages)      (the artist list is the whole run)
    works_seen == pages_saved + excluded_by_filter

Nothing here touches a database or a real crawl.  The builders are
deterministic and independent: no fixture imports A1-A4.
"""
from __future__ import annotations

from typing import Any

RUN_COMPLETED = "completed"
RUN_PAUSED = "paused"
RUN_RUNNING = "running"
RUN_FAILED = "failed"

ARTIST_COMPLETED = "completed"
ARTIST_FAILED = "failed"
ARTIST_PENDING = "pending"
ARTIST_RUNNING = "running"


def _run(
    *,
    run_id: str,
    status: str,
    stop_reason: str = "",
    account_ref: str = "acct-sample",
    totals: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "account_ref": account_ref,
        "config_key": "follow-default",
        "status": status,
        "stop_reason": stop_reason,
        "totals": dict(totals or {}),
    }


def _artist(artist_id: str, state: str, works: int, newest: str = "") -> dict[str, Any]:
    return {
        "artist_id": artist_id,
        "state": state,
        "pages": int(works),  # works_seen for this artist
        "newest_illust_id": newest,
    }


def _facts(
    run: dict[str, Any],
    artists: list[dict[str, Any]],
    incomplete: list[dict[str, Any]] | None = None,
    budget: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "run": run,
        "artists": sorted(artists, key=lambda item: item["artist_id"]),
        "incomplete": list(incomplete or []),
        "budget": budget,
    }


def all_complete() -> dict[str, Any]:
    """Three artists finished; some works were filtered, some were saved."""
    artists = [
        _artist("1001", ARTIST_COMPLETED, 20, "9001"),
        _artist("1002", ARTIST_COMPLETED, 14, "9002"),
        _artist("1003", ARTIST_COMPLETED, 0, ""),
    ]
    totals = {
        "total_artists": 3,
        "works_seen": 34,
        "pages_saved": 19,
        "pages_already_verified": 0,
        "excluded_by_filter": 15,
        "unknown_fields": 0,
        "duplicate_pages": 0,
    }
    return _facts(_run(run_id="run-complete", status=RUN_COMPLETED, totals=totals), artists)


def partial_failed_pages() -> dict[str, Any]:
    """One artist saw works but its pages failed; the run still says completed."""
    artists = [
        _artist("1001", ARTIST_COMPLETED, 20, "9001"),
        _artist("1002", ARTIST_FAILED, 8, "9002"),
        _artist("1003", ARTIST_COMPLETED, 0, ""),
    ]
    totals = {
        "total_artists": 3,
        "works_seen": 28,
        "pages_saved": 13,
        "pages_already_verified": 0,
        "excluded_by_filter": 15,
        "unknown_fields": 0,
        "duplicate_pages": 0,
    }
    return _facts(
        _run(run_id="run-partial", status=RUN_COMPLETED, totals=totals),
        artists,
        incomplete=[{"artist_id": "1002", "reason": "page_failed"}],
    )


def budget_stopped() -> dict[str, Any]:
    """The batch hit its request budget; two artists were never reached."""
    artists = [
        _artist("1001", ARTIST_COMPLETED, 12, "9001"),
        _artist("1002", ARTIST_PENDING, 0, ""),
        _artist("1003", ARTIST_PENDING, 0, ""),
    ]
    totals = {
        "total_artists": 3,
        "works_seen": 12,
        "pages_saved": 12,
        "pages_already_verified": 0,
        "excluded_by_filter": 0,
        "unknown_fields": 0,
        "duplicate_pages": 0,
    }
    return _facts(
        _run(run_id="run-budget", status=RUN_PAUSED, stop_reason="budget", totals=totals),
        artists,
        incomplete=[
            {"artist_id": "1002", "reason": "budget"},
            {"artist_id": "1003", "reason": "budget"},
        ],
        budget={"state": "exhausted", "remaining": 0},
    )


def rate_limited_paused() -> dict[str, Any]:
    """A cooldown stopped the pass while the budget still had room."""
    artists = [
        _artist("1001", ARTIST_COMPLETED, 5, "9001"),
        _artist("1002", ARTIST_RUNNING, 3, ""),
    ]
    totals = {
        "total_artists": 2,
        "works_seen": 8,
        "pages_saved": 6,
        "pages_already_verified": 0,
        "excluded_by_filter": 2,
        "unknown_fields": 0,
        "duplicate_pages": 0,
    }
    return _facts(
        _run(run_id="run-rate", status=RUN_PAUSED, stop_reason="rate_limited", totals=totals),
        artists,
        incomplete=[{"artist_id": "1002", "reason": "rate_limited"}],
        budget={"state": "cooldown", "remaining": 17},
    )


def duplicate_pages() -> dict[str, Any]:
    """The same pages were accepted twice; duplicates must not inflate the total."""
    artists = [
        _artist("1001", ARTIST_COMPLETED, 12, "9001"),
        _artist("1002", ARTIST_COMPLETED, 8, "9002"),
    ]
    totals = {
        "total_artists": 2,
        "works_seen": 20,
        "pages_saved": 17,
        "pages_already_verified": 0,
        "excluded_by_filter": 3,
        "unknown_fields": 0,
        "duplicate_pages": 3,
    }
    return _facts(_run(run_id="run-duplicate", status=RUN_COMPLETED, totals=totals), artists)


def all_filtered() -> dict[str, Any]:
    """Thirty works seen, all filtered out: sum(pages) is far from pages_saved."""
    artists = [
        _artist("1001", ARTIST_COMPLETED, 18, "9001"),
        _artist("1002", ARTIST_COMPLETED, 12, "9007"),
    ]
    totals = {
        "total_artists": 2,
        "works_seen": 30,
        "pages_saved": 0,
        "pages_already_verified": 0,
        "excluded_by_filter": 30,
        "unknown_fields": 0,
        "duplicate_pages": 0,
    }
    return _facts(_run(run_id="run-filtered", status=RUN_COMPLETED, totals=totals), artists)


def private_scope_missing() -> dict[str, Any]:
    """The run has no account reference, so the scope itself is unknown."""
    artists = [
        _artist("1001", ARTIST_COMPLETED, 6, "9001"),
    ]
    totals = {
        "total_artists": 1,
        "works_seen": 6,
        "pages_saved": 4,
        "pages_already_verified": 0,
        "excluded_by_filter": 2,
        "unknown_fields": 1,
        "duplicate_pages": 0,
    }
    return _facts(
        _run(run_id="run-scope", status=RUN_COMPLETED, account_ref="", totals=totals),
        artists,
    )


def empty_following() -> dict[str, Any]:
    """The account follows nobody; complete, with a real zero denominator."""
    totals = {
        "total_artists": 0,
        "works_seen": 0,
        "pages_saved": 0,
        "pages_already_verified": 0,
        "excluded_by_filter": 0,
        "unknown_fields": 0,
        "duplicate_pages": 0,
    }
    return _facts(_run(run_id="run-empty", status=RUN_COMPLETED, totals=totals), [])


ALL_BUILDERS = {
    "all_complete": all_complete,
    "partial_failed_pages": partial_failed_pages,
    "budget_stopped": budget_stopped,
    "rate_limited_paused": rate_limited_paused,
    "duplicate_pages": duplicate_pages,
    "all_filtered": all_filtered,
    "private_scope_missing": private_scope_missing,
    "empty_following": empty_following,
}
