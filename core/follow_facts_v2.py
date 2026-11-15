"""Read-only V2 facts for one follow job's coverage summary (EA-04).

The coverage engine (core.follow_coverage) judges three dimensions from a facts
dict.  Until this module existed the only producer was the legacy crawl-state
store (crawl_runs/artist_progress), which the desktop downloader no longer
writes: the current downloader records its work in the V2 tables of
library.sqlite3.  A summary that reads the legacy file therefore describes a
different database than the one the application is filling.

What V2 actually stores, and what it does not
---------------------------------------------
Measured on a synthetic job built through the real repository (see
.runtime/jpgpng_u1u2_e1_fix_20261115_01/probe/probe_v2_facts.py):

* the expected *page set* of a work is recorded: batch_items.required_pages_json
  ("[0,1]") or, when that is absent, page_count;
* the delivered page is recorded: deliveries(state, generation) plus
  artifacts(sha256, size, verification);
* the expected *content version* of a work page is **not** recorded anywhere.
  deliveries.source_identity looks like a digest but is a hash of
  {illust_id, page_index, url, original, validation_policy_version}
  (core/page_delivery.py:168), i.e. a reference identity, not a content digest;
  source_pages.content_digest is the digest of the follow *list* page.

So this module fills expected[].content_sha256 with None and observed[].content_sha256
with the artifact digest.  That is the honest pair: "we know which pages we
saved" and "we never recorded what the site said those pages should be".  The
engine turns that into delivery=unknown with the reason
expected_content_version_missing, which is why a V2 summary can report real page
numbers and still refuse to call the run complete.

Nothing here writes, creates a table, or switches a journal mode: the
connection is opened with SQLite's mode=ro URI and PRAGMA query_only, and every
read happens inside one transaction so a batch that advances while the report
runs cannot produce a mixed-generation answer.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

#: V2 job states mapped onto the engine's vocabulary.  Never pass a raw state
#: through: the engine treats "not failed/paused/partial/running" as complete,
#: so an unmapped "queued" would be read as a finished run.
RUN_STATE = {
    "completed": "completed",
    "running": "running",
    "partial": "partial",
    "failed": "failed",
    "paused": "paused",
    "blocked": "paused",
    "pausing": "paused",
    "cancelling": "paused",
    "settling": "paused",
    "cancelled": "paused",
    "queued": "running",
}

#: batch_items.result_state -> the artist-level vocabulary the engine knows
#: (completed/empty/pending/running/deferred/failed).  A work whose delivery
#: is unfinished keeps its artist unfinished; a work the filter decided is a
#: finished walk, not a missing one.
ARTIST_STATE = {
    "completed": "completed",
    "skipped_existing": "completed",
    "filtered": "empty",
    "metadata_unknown": "completed",
    "unavailable": "completed",
    "unsupported": "completed",
    "pending": "pending",
    "failed": "failed",
    "partial": "deferred",
    "needs_verification": "deferred",
    "cancelled": "deferred",
}

#: deliveries.state -> the engine's observed page state vocabulary.
PAGE_STATE = {
    "completed": "saved_new",
    "needs_verification": "pending_verify",
    "failed": "failed",
    "recovery_needed": "failed",
}

#: Delivery states that are known to be "not delivered yet": a page that still
#: owes work is parked for another look, never counted as saved.
PENDING_STATES = (
    "pending",
    "transferring",
    "validating",
    "prepared",
    "suspended",
    "retry_wait",
    "cancelled",
)

#: The state a known-pending delivery reports to the engine.
PENDING_PAGE = "pending_verify"


class V2FactsError(ValueError):
    """The V2 tables cannot answer for this job; never a coverage verdict."""

    def __init__(self, code: str, detail: object = "") -> None:
        self.code = str(code)
        self.detail = str(detail)
        super().__init__(self.code if not self.detail else "%s: %s" % (self.code, self.detail))


#: The two read-only modes this module may use, and what each one costs.
READ_ONLY = "ro"
READ_ONLY_IMMUTABLE = "ro+immutable"


def read_only_mode(path: str | Path) -> str:
    """Which read-only mode is safe for this database, and why.

    A WAL database keeps its -shm index for as long as any connection is open,
    and a read-only connection that finds none would create one.  That would
    make "read-only" a claim the command cannot honour: the directory listing
    changes because of the report itself.

    So the mode is chosen from the evidence on disk:

    * a -shm or -wal present means a writer may be live, and the index is
      already there: open mode=ro and take part in the locking protocol;
    * both absent means the store is quiescent (a live WAL writer cannot exist
      without them), so mode=ro&immutable=1 skips locking *and* creates nothing.

    immutable is a promise that the file does not change while we read it, so it
    is only used when the files on disk prove nobody is holding it open.
    """
    database = Path(path)
    siblings = (Path(str(database) + "-shm"), Path(str(database) + "-wal"))
    if any(sibling.exists() for sibling in siblings):
        return READ_ONLY
    return READ_ONLY_IMMUTABLE


def read_only_connection(path: str | Path) -> sqlite3.Connection:
    """A connection that provably cannot write: mode=ro plus query_only.

    A missing file is reported, not created: a reporting command that created
    the database it describes would answer about itself.
    """
    database = Path(path)
    if not database.is_file():
        raise V2FactsError("database_missing", database)
    mode = read_only_mode(database)
    suffix = "&immutable=1" if mode == READ_ONLY_IMMUTABLE else ""
    try:
        connection = sqlite3.connect(
            "file:%s?mode=ro%s" % (quote(str(database)), suffix),
            uri=True,
            timeout=5.0,
            isolation_level=None,
        )
    except sqlite3.Error as error:
        raise V2FactsError("database_unreadable", error) from None
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=1")
    return connection


def _rows(connection: sqlite3.Connection, sql: str, params: tuple = ()) -> list:
    try:
        return list(connection.execute(sql, params))
    except sqlite3.Error as error:
        raise V2FactsError("query_failed", error) from None


def _required_pages(value: object, page_count: object) -> tuple[int, ...] | None:
    """The work's page indices, or None when V2 does not say how many there are."""
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            raise V2FactsError("required_pages_invalid", value[:80]) from None
        if isinstance(parsed, list) and all(type(item) is int and item >= 0 for item in parsed):
            return tuple(sorted(set(parsed)))
        raise V2FactsError("required_pages_invalid", value[:80])
    if type(page_count) is int and page_count > 0:
        return tuple(range(page_count))
    return None


def coverage_facts(connection: sqlite3.Connection, job_id: object) -> dict[str, Any]:
    """Build the coverage facts dict for one job from the V2 tables.

    The caller owns the transaction; this function only reads.  An empty cohort
    is a legitimate answer (a job that followed nobody), a job that is not in
    the table is not.
    """
    if not isinstance(job_id, str) or not job_id.strip():
        raise V2FactsError("job_id_invalid", job_id)
    job_id = job_id.strip()
    jobs = _rows(
        connection,
        "SELECT job_id, account_ref, state, request_json FROM jobs WHERE job_id = ?",
        (job_id,),
    )
    if not jobs:
        raise V2FactsError("job_not_found", job_id)
    job = jobs[0]
    batches = _rows(
        connection,
        "SELECT batch_id, phase, stop_reason, coverage_complete FROM batches "
        "WHERE job_id = ? ORDER BY rowid",
        (job_id,),
    )
    batch_ids = [row["batch_id"] for row in batches]
    items: list = []
    if batch_ids:
        marks = ",".join("?" for _ in batch_ids)
        items = _rows(
            connection,
            "SELECT batch_id, illust_id, artist_id, page_count, decision, "
            "required_pages_json, result_state FROM batch_items "
            "WHERE batch_id IN (%s) ORDER BY batch_id, illust_id" % marks,
            tuple(batch_ids),
        )
    deliveries = {
        (str(row["illust_id"]), int(row["page_index"])): row
        for row in _rows(
            connection,
            "SELECT delivery_id, illust_id, page_index, state, generation, completed_batch_id "
            "FROM deliveries WHERE job_id = ? ORDER BY generation",
            (job_id,),
        )
    }
    artifacts = {
        row["delivery_id"]: row
        for row in _rows(
            connection,
            "SELECT delivery_id, illust_id, page_index, sha256 FROM artifacts WHERE job_id = ?",
            (job_id,),
        )
    }

    expected: list[dict[str, Any]] = []
    page_gaps: list[str] = []
    artists: dict[str, dict[str, Any]] = {}
    works_seen = excluded = unknown_fields = 0
    for item in items:
        works_seen += 1
        decision = str(item["decision"])
        if decision == "filtered":
            excluded += 1
        elif decision == "metadata_unknown":
            unknown_fields += 1
        artist_id = "" if item["artist_id"] is None else str(item["artist_id"])
        if artist_id:
            entry = artists.setdefault(
                artist_id, {"artist_id": artist_id, "state": "completed", "works_seen": 0}
            )
            entry["works_seen"] += 1
            mapped = ARTIST_STATE.get(str(item["result_state"]), "deferred")
            # One unfinished work is enough to keep the artist unfinished: the
            # artist is not "done" just because its other works are.
            if entry["state"] == "completed" and mapped != "completed":
                entry["state"] = mapped
            elif entry["state"] == "empty" and mapped in ("pending", "failed", "deferred"):
                entry["state"] = mapped
        if decision != "download":
            continue
        pages = _required_pages(item["required_pages_json"], item["page_count"])
        if pages is None:
            page_gaps.append(str(item["illust_id"]))
            continue
        for page in pages:
            expected.append(
                {
                    "work_id": str(item["illust_id"]),
                    "page": page,
                    # Not "unknown because we forgot": V2 has no expected
                    # content version for a work page (see the module docstring).
                    "content_sha256": None,
                }
            )

    observed: list[dict[str, Any]] = []
    unmapped: set[str] = set()
    for (illust_id, page_index), row in sorted(deliveries.items()):
        raw_state = str(row["state"])
        state = PAGE_STATE.get(raw_state)
        if state is None:
            # An unknown state is not evidence of "waiting for a look", so the
            # name is recorded as well: the page is parked, but the report can
            # say which state V2 used instead of pretending it recognised it.
            state = PENDING_PAGE
            if raw_state not in PENDING_STATES:
                unmapped.add(raw_state)
        artifact = artifacts.get(str(row["delivery_id"]))
        observed.append(
            {
                "work_id": illust_id,
                "page": page_index,
                "state": state,
                # The digest of the file this run saved.  It is an *observed*
                # version; the expected one is not recorded anywhere in V2.
                "content_sha256": (
                    str(artifact["sha256"])
                    if artifact is not None and artifact["sha256"]
                    else None
                ),
            }
        )

    artists_rows = [artists[key] for key in sorted(artists)]
    incomplete = [
        {"artist_id": row["artist_id"], "reason": "state_%s" % row["state"]}
        for row in artists_rows
        if row["state"] not in ("completed", "empty")
    ]
    totals = {
        "artists_total": len(artists_rows),
        "works_seen": works_seen,
        "excluded_by_filter": excluded,
        "unknown_fields": unknown_fields,
    }
    stop_reason = ""
    for row in batches:
        if row["stop_reason"]:
            stop_reason = str(row["stop_reason"])
    run = {
        "job_id": job_id,
        "account_ref": str(job["account_ref"]),
        "status": RUN_STATE.get(str(job["state"]), "partial"),
        "state_v2": str(job["state"]),
        "stop_reason": stop_reason,
        "totals": totals,
    }
    facts: dict[str, Any] = {
        "run": run,
        "artists": artists_rows,
        "incomplete": incomplete,
        "budget": None,
        "pages": {"expected": expected, "observed": observed},
        "page_end_evidence": _page_end(batches, page_gaps),
    }
    if page_gaps:
        facts["page_gaps"] = page_gaps
    if unmapped:
        facts["unmapped_delivery_states"] = sorted(unmapped)
    # Works whose decision was not "download" contribute no expected page, while
    # the counter side calls every work minus the filtered ones deliverable.
    # The two populations differ; the count travels so a report can say so.
    # The filtered works are already the excluded_by_filter counter, so they are
    # not part of the difference this number explains.
    facts["works_without_expected_pages"] = sum(
        1
        for item in items
        if str(item["decision"]) not in ("download", "filtered")
    )
    return facts


def _page_end(batches: list, page_gaps: list) -> dict[str, str]:
    """Whether the run's batches claim their page lists were complete."""
    if page_gaps:
        return {"state": "partial", "reason": "pages_expected_missing:%d" % len(page_gaps)}
    declared = [row["coverage_complete"] for row in batches]
    if not declared or any(value is None for value in declared):
        return {"state": "unknown", "reason": "batch_coverage_not_recorded"}
    if all(int(value) == 1 for value in declared):
        return {"state": "complete", "reason": "every_batch_complete"}
    return {"state": "partial", "reason": "some_batch_incomplete"}
