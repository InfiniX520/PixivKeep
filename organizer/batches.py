"""Batch planning and progress for the follow organizer (E1-3 / E1-4).

Pure logic, deliberately: no network, no database, no file writes, no clock of
its own.  A batch says which artists one review pass covers, how far each of
them got, and why it stopped; whoever runs it persists the result.

Two rules shape the whole module.

* A state is a fact, not a guess.  An artist nobody touched is pending.  There
  is no "0" and no derived value for "we did not look".
* Running out of budget is not finishing.  When a limit stops the batch the
  status is paused and stop_reason names the limit, so a caller cannot mistake
  "we did ten of forty" for "the pool is done".

The step vocabulary is the one frozen in docs/optimization/2026-11-15/
E1_PLAN.md section 3.1 (pending, sampled, reviewed, planned, applied, verified,
skipped) plus failed, because an artist whose sampling errored must be
expressible: without it the artist would stay pending and a resume would retry
it as if nothing had happened.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterable, Mapping

#: Every state one artist can be in inside a batch.
STATES = (
    "pending",
    "sampled",
    "reviewed",
    "planned",
    "applied",
    "verified",
    "skipped",
    "failed",
)

#: States that still owe work (an interrupted batch resumes into these).
OPEN_STATES = ("pending", "sampled", "reviewed", "planned", "failed")

#: States that need no further work.
FINAL_STATES = ("applied", "verified", "skipped")

#: Batch-level status.  paused means "there is work left and a limit stopped us".
STATUSES = ("active", "paused", "complete")

#: Why a batch stopped planning where it did.
STOP_REASONS = (
    "artists_limit",
    "requests_limit",
    "bytes_limit",
    "seconds_limit",
    "pool_exhausted",
    "none",
)

#: The only legal moves.  An event whose target equals the current state is a
#: no-op (idempotent), which is why repeated advances do not create a second
#: sample.
TRANSITIONS = {
    "pending": {"sample": "sampled", "skip": "skipped", "fail": "failed"},
    "sampled": {"review": "reviewed", "skip": "skipped", "fail": "failed"},
    "reviewed": {"plan": "planned", "skip": "skipped", "fail": "failed"},
    "planned": {"apply": "applied", "skip": "skipped", "fail": "failed"},
    "applied": {"verify": "verified", "fail": "failed"},
    "verified": {},
    "skipped": {},
    "failed": {"retry": "pending", "skip": "skipped"},
}

#: Where each event lands when it is applied from the state it belongs to.
EVENT_TARGET = {
    "sample": "sampled",
    "review": "reviewed",
    "plan": "planned",
    "apply": "applied",
    "verify": "verified",
    "skip": "skipped",
    "fail": "failed",
    "retry": "pending",
}


class BatchError(ValueError):
    """A caller error, never a verdict: bad ids, bad states, bad transitions."""

    def __init__(self, code: str, detail: object = None) -> None:
        self.code = str(code)
        self.detail = detail
        super().__init__(self.code if detail is None else "%s: %s" % (self.code, detail))


def _identifier(value: object, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BatchError("invalid_%s" % what, value)
    return value.strip()


def _count(value: object, what: str) -> int:
    if type(value) is not int or value < 0:
        raise BatchError("invalid_%s" % what, value)
    return int(value)


def _seconds(value: object, what: str) -> float:
    if value is None:
        return None
    if type(value) not in (int, float) or value < 0:
        raise BatchError("invalid_%s" % what, value)
    return float(value)


@dataclass(frozen=True, slots=True)
class ArtistCost:
    """Planning estimate for one artist, not a measurement.

    The batch records that it planned with an estimate; the report is where the
    measured numbers live.  Zero means "this dimension does not constrain".
    """

    requests: int = 1
    bytes_per_artist: int = 0
    seconds_per_artist: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "requests", _count(self.requests, "cost_requests"))
        object.__setattr__(
            self, "bytes_per_artist", _count(self.bytes_per_artist, "cost_bytes")
        )
        object.__setattr__(
            self, "seconds_per_artist", _seconds(self.seconds_per_artist, "cost_seconds")
        )

    def as_dict(self) -> dict:
        return {
            "requests": self.requests,
            "bytes_per_artist": self.bytes_per_artist,
            "seconds_per_artist": self.seconds_per_artist,
        }


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(scope: Mapping, policy: Mapping) -> str:
    payload = _canonical({"scope": dict(scope), "policy": dict(policy)})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _frozen(value: Mapping) -> Mapping:
    return MappingProxyType(dict(value))


@dataclass(frozen=True, slots=True)
class Batch:
    """One review pass over a fixed set of artists.  Immutable by construction."""

    batch_id: str
    account_id: str
    created_at: str
    scope: Mapping
    policy: Mapping
    steps: Mapping
    status: str
    stop_reason: str

    def __post_init__(self) -> None:
        _identifier(self.batch_id, "batch_id")
        _identifier(self.account_id, "account_id")
        object.__setattr__(self, "scope", _frozen(self.scope))
        object.__setattr__(self, "policy", _frozen(self.policy))
        object.__setattr__(self, "steps", _frozen(self.steps))
        if self.status not in STATUSES:
            raise BatchError("invalid_status", self.status)
        if self.stop_reason not in STOP_REASONS:
            raise BatchError("invalid_stop_reason", self.stop_reason)
        for artist_id, state in self.steps.items():
            if state not in STATES:
                raise BatchError("invalid_step_state", (artist_id, state))

    @property
    def artist_ids(self) -> tuple:
        return tuple(self.scope["artist_ids"])

    @property
    def open_artists(self) -> tuple:
        return tuple(
            artist_id
            for artist_id in self.scope["artist_ids"]
            if self.steps[artist_id] in OPEN_STATES
        )

    def state_of(self, artist_id: str) -> str:
        try:
            return self.steps[artist_id]
        except KeyError:
            raise BatchError("artist_not_in_batch", artist_id) from None

    def fingerprint(self) -> str:
        """The identity of what this batch covers, not of how far it got."""
        return _fingerprint(self.scope, self.policy)

    def as_dict(self) -> dict:
        return {
            "batch_id": self.batch_id,
            "account_id": self.account_id,
            "created_at": self.created_at,
            "scope": dict(self.scope),
            "policy": dict(self.policy),
            "steps": dict(self.steps),
            "status": self.status,
            "stop_reason": self.stop_reason,
            "fingerprint": self.fingerprint(),
        }

    @classmethod
    def from_dict(cls, value: object) -> "Batch":
        """Rebuild a batch from as_dict(), refusing a dump that does not match.

        The fingerprint travels in the dump so a hand-edited file cannot pass
        as the batch it claims to be: a present-but-different fingerprint is a
        refusal here, not something to silently recompute and forget.
        """
        if not isinstance(value, Mapping):
            raise BatchError("batch_not_a_mapping", type(value).__name__)
        required = (
            "batch_id",
            "account_id",
            "created_at",
            "scope",
            "policy",
            "steps",
            "status",
            "stop_reason",
        )
        missing = [key for key in required if key not in value]
        if missing:
            raise BatchError("batch_field_missing", ",".join(missing))
        batch = cls(
            batch_id=value["batch_id"],
            account_id=value["account_id"],
            created_at=value["created_at"],
            scope=value["scope"],
            policy=value["policy"],
            steps=value["steps"],
            status=value["status"],
            stop_reason=value["stop_reason"],
        )
        claimed = value.get("fingerprint")
        if claimed is not None and claimed != batch.fingerprint():
            raise BatchError("batch_fingerprint_mismatch", claimed)
        return batch


def _capacity(*, cap: int, requests: int, bytes_budget: int, seconds: float,
              cost: ArtistCost):
    """How many artists fit, and which dimension set the ceiling."""
    limits = [
        ("artists_limit", cap),
        ("requests_limit", requests // cost.requests if cost.requests else cap),
        (
            "bytes_limit",
            bytes_budget // cost.bytes_per_artist if cost.bytes_per_artist else cap,
        ),
        (
            "seconds_limit",
            int(seconds // cost.seconds_per_artist) if cost.seconds_per_artist else cap,
        ),
    ]
    reason, fit = min(limits, key=lambda item: (item[1], STOP_REASONS.index(item[0])))
    return max(0, fit), reason


def plan_batch(
    pool: Iterable,
    *,
    account_id: object,
    artists: object,
    requests: object,
    bytes_budget: object = None,
    seconds: object = None,
    done_ids: Iterable = (),
    batch_id: object = None,
    created_at: object = "",
    snapshot_id: object = "",
    group: object = None,
    rules_version: object = "",
    count_per_artist: object = 16,
    cost: ArtistCost | None = None,
) -> Batch:
    """Plan one batch out of a pool.

    The pool order never matters: ids are de-duplicated and sorted, so the same
    pool in any order produces the same batch and the same fingerprint.
    """
    known = {_identifier(item, "artist_id") for item in pool}
    done = {_identifier(item, "artist_id") for item in done_ids}
    candidates = sorted(known - done)
    cap = _count(artists, "artists")
    measured = ArtistCost() if cost is None else cost
    if not isinstance(measured, ArtistCost):
        raise BatchError("invalid_cost", measured)
    request_budget = _count(requests, "requests")
    bytes_value = 0 if bytes_budget is None else _count(bytes_budget, "bytes")
    seconds_value = 0.0 if seconds is None else _seconds(seconds, "seconds")

    fit, reason = _capacity(
        cap=cap,
        requests=request_budget,
        bytes_budget=bytes_value,
        seconds=seconds_value,
        cost=measured,
    )
    if not candidates:
        chosen = []
        stop_reason = "pool_exhausted"
    elif fit >= len(candidates):
        chosen = candidates
        stop_reason = "none"
    else:
        chosen = candidates[:fit]
        stop_reason = reason

    if stop_reason == "pool_exhausted":
        status = "complete"
    elif stop_reason == "none":
        status = "active"
    else:
        status = "paused"

    scope = {
        "snapshot_id": "" if snapshot_id is None else str(snapshot_id),
        "group": "" if group is None else str(group),
        "artist_ids": chosen,
        "rules_version": "" if rules_version is None else str(rules_version),
    }
    policy = {
        "count_per_artist": _count(count_per_artist, "count_per_artist"),
        "max_artists": cap,
        "budget": {
            "artists": cap,
            "requests": request_budget,
            "bytes": bytes_value,
            "seconds": seconds_value,
        },
        "cost": measured.as_dict(),
    }
    identifier = batch_id
    if identifier is None:
        identifier = "batch-" + _fingerprint(scope, policy)[:16]
    return Batch(
        batch_id=_identifier(identifier, "batch_id"),
        account_id=_identifier(account_id, "account_id"),
        created_at=str(created_at or ""),
        scope=scope,
        policy=policy,
        steps={artist_id: "pending" for artist_id in chosen},
        status=status,
        stop_reason=stop_reason,
    )


def plan_batches(
    pool: Iterable,
    *,
    account_id: object,
    artists: object,
    requests: object,
    bytes_budget: object = None,
    seconds: object = None,
    done_ids: Iterable = (),
    **options: Any,
) -> tuple:
    """Split a pool into consecutive batches of at most the given size.

    Every batch gets its own fingerprint, the batches never overlap, and their
    union is exactly the pool minus the finished artists.
    """
    remaining = sorted(
        {_identifier(item, "artist_id") for item in pool}
        - {_identifier(item, "artist_id") for item in done_ids}
    )
    size = _count(artists, "artists")
    if size == 0:
        raise BatchError("invalid_artists", artists)
    batches = []
    for offset in range(0, len(remaining), size):
        chunk = remaining[offset:offset + size]
        batches.append(
            plan_batch(
                chunk,
                account_id=account_id,
                artists=size,
                requests=requests,
                bytes_budget=bytes_budget,
                seconds=seconds,
                **options,
            )
        )
    return tuple(batches)


def advance(batch: Batch, artist_id: object, event: object) -> Batch:
    """Move one artist one step.  Illegal moves raise; repeats are no-ops."""
    if not isinstance(batch, Batch):
        raise BatchError("invalid_batch", batch)
    identifier = _identifier(artist_id, "artist_id")
    if event not in EVENT_TARGET:
        raise BatchError("event_unknown", event)
    if identifier not in batch.steps:
        raise BatchError("artist_not_in_batch", identifier)
    current = batch.steps[identifier]
    if current == EVENT_TARGET[event]:
        # Idempotent by contract: sampling an already sampled artist must not
        # create a second sample or a second state.
        return batch
    target = TRANSITIONS[current].get(event)
    if target is None:
        raise BatchError("transition_not_allowed", (current, event))
    steps = dict(batch.steps)
    steps[identifier] = target
    status = batch.status
    if status == "paused" and all(state in FINAL_STATES for state in steps.values()):
        status = "complete"
    return Batch(
        batch_id=batch.batch_id,
        account_id=batch.account_id,
        created_at=batch.created_at,
        scope=batch.scope,
        policy=batch.policy,
        steps=steps,
        status=status,
        stop_reason=batch.stop_reason,
    )


def rebuild(batch: Batch, progress: Mapping) -> Batch:
    """Rebuild the batch from persisted progress.

    Progress may not invent artists outside the batch (that means it belongs to
    a different batch), may not use a state outside the vocabulary, and may not
    reopen an artist that already finished.
    """
    if not isinstance(batch, Batch):
        raise BatchError("invalid_batch", batch)
    if not isinstance(progress, Mapping):
        raise BatchError("progress_invalid", progress)
    outside = sorted(set(progress) - set(batch.steps))
    if outside:
        raise BatchError("progress_artist_outside_batch", outside)
    steps = dict(batch.steps)
    for artist_id, state in progress.items():
        if state not in STATES:
            raise BatchError("progress_state_invalid", (artist_id, state))
        current = steps[artist_id]
        if current in FINAL_STATES and state != current:
            raise BatchError(
                "progress_would_reopen_finished", (artist_id, current, state)
            )
        steps[artist_id] = state
    return Batch(
        batch_id=batch.batch_id,
        account_id=batch.account_id,
        created_at=batch.created_at,
        scope=batch.scope,
        policy=batch.policy,
        steps=steps,
        status=batch.status,
        stop_reason=batch.stop_reason,
    )


def resume(batch: Batch, progress: Mapping) -> tuple:
    """The artists a resumed run still owes work on, in batch order."""
    return rebuild(batch, progress).open_artists
