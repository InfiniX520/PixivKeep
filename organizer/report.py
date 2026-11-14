"""One batch report: what was planned, what happened, and what it cannot prove.

The report is the only place where plan and measurement meet, so the two are
kept apart on purpose: the batch carries an *estimate* (what an artist is
expected to cost) and the caller may bring *measurements*.  When no
measurement is supplied the section says unknown with a reason - it never
prints zero, because "we did not measure" and "we measured nothing" are
different answers.

It also says out loud what it does not prove: writing groups on the website is
confirmed elsewhere, and coverage is only as good as the snapshot the scope
came from.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from organizer.batches import STATES, Batch, BatchError
from organizer.conflicts import ACTIONS, KINDS
from organizer.group_scope import COMPLETENESS

#: Bumped when a consumer would have to read a different shape.
REPORT_VERSION = 1

#: The measurement keys a caller may supply.  A closed set: an unknown key is
#: a typo, and reporting it as a measurement would be worse than failing.
MEASURED_KEYS = ("requests", "bytes", "seconds")


class ReportError(ValueError):
    """The inputs cannot be turned into an honest report."""

    def __init__(self, code: str, detail: object = None) -> None:
        self.code = str(code)
        self.detail = detail
        super().__init__(self.code if detail is None else "%s: %s" % (self.code, detail))


def _plain(value: Any) -> Any:
    """Tuples become lists so the report survives json.dumps unchanged."""
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _number(value: object, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReportError("measurement_invalid", (what, value))
    if value < 0:
        raise ReportError("measurement_negative", (what, value))
    return float(value)


def _measured(value: object) -> dict | None:
    """Validate what the caller measured.  None stays None, never zero."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ReportError("measurement_invalid", value)
    unknown = [key for key in value if key not in MEASURED_KEYS]
    if unknown:
        raise ReportError("measurement_unknown_key", sorted(unknown))
    return {key: _number(value[key], key) for key in MEASURED_KEYS if key in value}


def _decision_rows(decisions: object) -> dict:
    """Index decisions by artist, refusing two answers for one artist."""
    if decisions is None:
        return {}
    rows: dict = {}
    for row in decisions:
        if not isinstance(row, Mapping):
            raise ReportError("decision_invalid", row)
        artist_id = row.get("artist_id")
        if not isinstance(artist_id, str) or not artist_id:
            raise ReportError("decision_invalid", "artist_id")
        kind = row.get("kind")
        if kind not in KINDS:
            raise ReportError("decision_kind_invalid", (artist_id, kind))
        action = row.get("action")
        if action not in ACTIONS:
            raise ReportError("decision_action_invalid", (artist_id, action))
        if artist_id in rows:
            raise ReportError("decision_duplicate", artist_id)
        rows[artist_id] = row
    return rows


def _groups_for(scope: Any, artist_id: str) -> list:
    overlap = getattr(scope, "overlap", None)
    if isinstance(overlap, Mapping):
        hit = overlap.get(artist_id)
        if hit is not None:
            return _plain(hit)
    return []


def counts_from(per_artist: Mapping) -> dict:
    """Recount the totals from the rows, so nobody has to trust a sum.

    Every state and every kind is present even at zero: these are counts of an
    enumerated set, so zero here is a measured zero, not an unknown.
    """
    states = {state: 0 for state in STATES}
    kinds = {kind: 0 for kind in KINDS}
    needs_human = 0
    undecided = 0
    for row in per_artist.values():
        states[row["state"]] = states.get(row["state"], 0) + 1
        kind = row.get("kind")
        if kind is None:
            undecided += 1
        else:
            kinds[kind] = kinds.get(kind, 0) + 1
        if row.get("needs_human"):
            needs_human += 1
    return {
        "total": len(per_artist),
        "states": states,
        "decisions": kinds,
        "needs_human": needs_human,
        "undecided": undecided,
    }


def _planned(batch: Batch) -> dict:
    policy = dict(batch.policy)
    budget = dict(policy.get("budget") or {})
    cost = dict(policy.get("cost") or {})
    count = len(batch.artist_ids)
    # The estimate is the per-artist cost times the artists; the budget is the
    # ceiling the batch was planned against.  Two different numbers, both useful.
    return {
        "artists": count,
        "budget": {key: budget.get(key) for key in ("artists", "requests", "bytes", "seconds")},
        "estimate": {
            "requests": float(cost.get("requests", 0)) * count,
            "bytes": float(cost.get("bytes_per_artist", 0)) * count,
            "seconds": float(cost.get("seconds_per_artist", 0)) * count,
        },
        "count_per_artist": policy.get("count_per_artist"),
    }


def _planned_vs_measured(batch: Batch, measured: object) -> dict:
    observed = _measured(measured)
    planned = _planned(batch)
    if observed is None:
        return {
            "state": "unknown",
            "reason": "no_measurement",
            "planned": planned,
            "measured": None,
            "delta": None,
        }
    delta = {
        key: round(observed[key] - planned["estimate"][key], 6)
        for key in MEASURED_KEYS
        if key in observed
    }
    return {
        "state": "measured",
        "reason": "",
        "planned": planned,
        "measured": observed,
        "delta": delta,
    }


def build_report(
    batch: object,
    decisions: object,
    scope: object,
    *,
    measured: object = None,
    generated_at: object = "",
    rules_version: object = "",
) -> dict:
    """Turn one batch, its decisions and its scope into a report dict."""
    if not isinstance(batch, Batch):
        raise ReportError("batch_invalid", type(batch).__name__)
    for name in ("fingerprint", "completeness", "artist_ids"):
        if not hasattr(scope, name):
            raise ReportError("scope_invalid", name)
    if getattr(scope, "completeness") not in COMPLETENESS:
        raise ReportError("scope_invalid", "completeness")

    rows = _decision_rows(decisions)
    artist_ids = list(batch.artist_ids)
    outside = sorted(set(rows) - set(artist_ids))
    if outside:
        # A decision about an artist this batch never covered would make the
        # totals describe a different set than the batch does.
        raise ReportError("decision_outside_batch", outside)

    per_artist: dict = {}
    unknowns: list = []
    for artist_id in sorted(artist_ids):
        try:
            state = batch.state_of(artist_id)
        except BatchError as error:
            raise ReportError("batch_shape_invalid", str(error)) from None
        row = rows.get(artist_id)
        entry = {
            "state": state,
            "kind": None if row is None else row["kind"],
            "action": None if row is None else row["action"],
            "reason": "" if row is None else str(row.get("reason") or ""),
            "needs_human": False if row is None else bool(row.get("needs_human")),
            "groups": _groups_for(scope, artist_id),
        }
        per_artist[artist_id] = entry
        if row is None:
            unknowns.append({
                "artist_id": artist_id,
                "state": state,
                "kind": "unknown",
                "reason": "no_decision_for_artist",
            })
        elif entry["kind"] == "unknown" or entry["needs_human"] or state == "failed":
            unknowns.append({
                "artist_id": artist_id,
                "state": state,
                "kind": entry["kind"],
                "reason": entry["reason"] or ("state_failed" if state == "failed" else ""),
            })

    counts = counts_from(per_artist)
    if counts["total"] != len(artist_ids):
        raise ReportError("counts_mismatch", (counts["total"], len(artist_ids)))
    if sum(counts["states"].values()) != counts["total"]:
        raise ReportError("counts_mismatch", "states")

    coverage = str(getattr(scope, "completeness"))
    covered = set(getattr(scope, "artist_ids") or ())
    return {
        "report_version": REPORT_VERSION,
        "batch": {
            "batch_id": batch.batch_id,
            "account_id": batch.account_id,
            "status": batch.status,
            "stop_reason": batch.stop_reason,
            "created_at": batch.created_at,
        },
        "scope": {
            "snapshot_id": getattr(scope, "snapshot_id", ""),
            "group_pairs": _plain(getattr(scope, "group_pairs", ())),
            "groups": _plain(getattr(scope, "groups", ())),
            "completeness": coverage,
            "unknown_reason": getattr(scope, "unknown_reason", ""),
            "artist_count": len(covered),
            "covers_batch": set(artist_ids) <= covered,
        },
        "per_artist": per_artist,
        "counts": counts,
        "unknowns": unknowns,
        "planned_vs_measured": _planned_vs_measured(batch, measured),
        "proof_limits": {
            "website_write_back": "not_proven_here",
            "coverage": coverage,
            "coverage_note": (
                "" if coverage == "complete"
                else "scope_not_complete:snapshot_cannot_prove_every_membership"
            ),
        },
        "provenance": {
            "batch_fingerprint": batch.fingerprint(),
            "scope_fingerprint": str(getattr(scope, "fingerprint")),
            "rules_version": str(
                rules_version or (batch.scope.get("rules_version") if batch.scope else "") or ""
            ),
            "generated_at": str(generated_at or ""),
        },
    }


def report_json(report: Mapping) -> str:
    """Stable JSON: sorted keys, no whitespace surprises, UTF-8 text."""
    return json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
