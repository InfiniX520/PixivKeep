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
from organizer.conflicts import ACTION_FOR, ACTIONS, KINDS
from organizer.group_scope import COMPLETENESS

#: Bumped when a consumer would have to read a different shape.
#: 2: scope.superset and the provenance identities were added, and covers_batch
#:    can no longer be false in a report that exists - the combination is a
#:    refusal now, not a field.
#: 3: the scope's account is carried (scope.account_id, proof_limits.account,
#:    proof_limits.complete, provenance.scope_account_id).  A consumer that
#:    read 2 and ignored the account would have called an unproven report a
#:    finished one, so the shape version moves with it (RA-05).
REPORT_VERSION = 3

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
    """Index decisions by artist, refusing two answers for one artist.

    Each field is checked against the closed tables, and then the *combination*
    is checked against the one truth table the conflict pass already uses
    (organizer.conflicts.ACTION_FOR).  Seven legal kinds and three legal actions
    are 21 combinations of which 7 make sense; "unchanged" with "replan" (or
    "needs_human": true on an unchanged artist) used to pass because each field
    was valid on its own, which is how a report ends up describing a decision
    nobody could have made (EA-09, S0 section 8).
    """
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
        expected = ACTION_FOR.get(kind)
        if expected is None:
            # KINDS and ACTION_FOR are two closed tables; a kind in one and not
            # the other is a code defect, and guessing an action would hide it.
            raise ReportError("decision_kind_unmapped", (artist_id, kind))
        if action != expected:
            raise ReportError(
                "decision_action_mismatch", (artist_id, kind, action, expected)
            )
        needs_human = row.get("needs_human", False)
        if not isinstance(needs_human, bool):
            raise ReportError("decision_needs_human_invalid", (artist_id, needs_human))
        if needs_human != (expected == "needs_human"):
            raise ReportError(
                "decision_needs_human_mismatch", (artist_id, kind, needs_human)
            )
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


def _scope_account(scope: Any) -> str:
    """The account the scope says it came from, or "" when it never said.

    The field is authoritative; the source bag is the fallback for a scope built
    by hand (or restored from a dump written before the field existed).  An
    empty answer is unknown, and unknown is never read as "the same account as
    the batch" - that is the gate RA-05 found open.
    """
    account = str(getattr(scope, "account_id", "") or "").strip()
    if account:
        return account
    source = getattr(scope, "source", None)
    if isinstance(source, Mapping):
        declared = source.get("account_id")
        if isinstance(declared, str) and declared.strip():
            return declared.strip()
    return ""


def _inputs_agree(batch: Batch, scope: Any, rules_version: object,
                  account_id: object) -> None:
    """Refuse three inputs that describe different accounts, snapshots or rules.

    Only facts both sides actually carry are compared: a batch planned without a
    snapshot id is *unknown*, not a contradiction, and an empty value must never
    be read as agreement.  Each disagreement has its own code so a caller can
    tell "the scope came from another snapshot" from "the rules moved".
    """
    batch_snapshot = str((batch.scope or {}).get("snapshot_id") or "")
    scope_snapshot = str(getattr(scope, "snapshot_id", "") or "")
    if batch_snapshot and scope_snapshot and batch_snapshot != scope_snapshot:
        raise ReportError("snapshot_mismatch", (batch_snapshot, scope_snapshot))
    batch_rules = str((batch.scope or {}).get("rules_version") or "")
    given_rules = str(rules_version or "")
    if given_rules and batch_rules and given_rules != batch_rules:
        raise ReportError("rules_version_mismatch", (given_rules, batch_rules))
    if account_id is not None:
        if not isinstance(account_id, str) or not account_id.strip():
            raise ReportError("account_invalid", account_id)
        if account_id != batch.account_id:
            raise ReportError("account_mismatch", (account_id, batch.account_id))
    declared = _scope_account(scope)
    if declared and declared != batch.account_id:
        # A known, different account: the two inputs do not describe one
        # catalog, whatever the snapshot labels happen to say (RA-05).
        raise ReportError("account_scope_mismatch", (declared, batch.account_id))


def _containment(batch: Batch, scope: Any, superset_ok: bool) -> dict:
    """The batch must sit inside the scope, and a wider scope must be declared.

    Missing artists are always a refusal: a report row for an artist the scope
    never selected would describe somebody the scope does not cover, and its
    group list would come from a different member set.  A *wider* scope is the
    ordinary case when one group is planned in several batches, but it changes
    what the totals mean, so it is allowed only when the caller says so and the
    extra artists are recorded (EA-09, S0 section 8).
    """
    batch_ids = set(batch.artist_ids)
    covered = set(getattr(scope, "artist_ids") or ())
    missing = sorted(batch_ids - covered)
    extra = sorted(covered - batch_ids)
    if missing:
        raise ReportError("scope_excludes_batch", missing)
    if extra and not superset_ok:
        raise ReportError("scope_broader_than_batch", extra)
    return {
        "covers_batch": True,
        "superset": {"allowed": bool(extra), "extra_artists": extra},
    }


def build_report(
    batch: object,
    decisions: object,
    scope: object,
    *,
    measured: object = None,
    generated_at: object = "",
    rules_version: object = "",
    superset_ok: bool = False,
    account_id: object = None,
) -> dict:
    """Turn one batch, its decisions and its scope into a report dict.

    The three inputs have to describe the same thing before a single row is
    counted: the batch must sit inside the scope (a wider scope only with
    superset_ok=True, and then it is recorded), the snapshot/rules/account the
    inputs carry must agree where both sides actually carry them, and every
    decision must be a combination its own truth table allows.  A report that
    cannot pass those checks is a refusal with a code, never a successful
    document with a warning inside it.
    """
    if not isinstance(batch, Batch):
        raise ReportError("batch_invalid", type(batch).__name__)
    if type(superset_ok) is not bool:
        raise ReportError("scope_option_invalid", superset_ok)
    for name in ("fingerprint", "completeness", "artist_ids"):
        if not hasattr(scope, name):
            raise ReportError("scope_invalid", name)
    if getattr(scope, "completeness") not in COMPLETENESS:
        raise ReportError("scope_invalid", "completeness")
    _inputs_agree(batch, scope, rules_version, account_id)
    containment = _containment(batch, scope, superset_ok)

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
    # The account is a fact about provenance, not about the member set: a scope
    # whose members are provably whole is still a report nobody can tie to an
    # account when the snapshot never recorded one.  That is an unknown the
    # reader has to see, not a reason to refuse the whole report (RA-05).
    artist_unknowns = len(unknowns)
    scope_account = _scope_account(scope)
    account_state = "matched" if scope_account else "unknown"
    if account_state == "unknown":
        # Appended after the per-artist entries so a reader that looks at [0]
        # still finds the first artist it was asking about.
        unknowns.append({
            "scope": "account",
            "state": "unknown",
            "reason": "scope_account_unknown",
        })
    incomplete = []
    if coverage != "complete":
        incomplete.append("coverage_not_complete")
    if account_state != "matched":
        incomplete.append("scope_account_unknown")
    if artist_unknowns:
        incomplete.append("unknowns_present")
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
            "account_id": scope_account,
            "account_id_state": "recorded" if scope_account else "not_recorded",
            "group_pairs": _plain(getattr(scope, "group_pairs", ())),
            "groups": _plain(getattr(scope, "groups", ())),
            "completeness": coverage,
            "unknown_reason": getattr(scope, "unknown_reason", ""),
            "artist_count": len(covered),
            "covers_batch": containment["covers_batch"],
            "superset": containment["superset"],
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
            # "matched" means the scope named an account and the batch named the
            # same one.  "unknown" means the scope never said: silence is not
            # agreement, so it can never make this report complete.
            "account": account_state,
            "complete": not incomplete,
            "incomplete_reasons": incomplete,
        },
        "provenance": {
            "batch_fingerprint": batch.fingerprint(),
            "scope_fingerprint": str(getattr(scope, "fingerprint")),
            "rules_version": str(
                rules_version or (batch.scope.get("rules_version") if batch.scope else "") or ""
            ),
            "scope_snapshot_id": str(getattr(scope, "snapshot_id", "") or ""),
            "scope_account_id": scope_account,
            "scope_account_state": "recorded" if scope_account else "not_recorded",
            "scope_identity_fingerprint": str(
                (getattr(scope, "source", None) or {}).get("identity_fingerprint") or ""
            ) if isinstance(getattr(scope, "source", None), Mapping) else "",
            "scope_completeness": coverage,
            "generated_at": str(generated_at or ""),
        },
    }


def report_json(report: Mapping) -> str:
    """Stable JSON: sorted keys, no whitespace surprises, UTF-8 text."""
    return json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
