"""Batch planning and progress for the follow organizer (E1-3 / E1-4).

Pure logic, deliberately: no network, no database, no file writes, no clock of
its own.  A batch says which artists one review pass covers, how far each of
them got, and why it stopped; whoever runs it persists the result.

Three rules shape the whole module.

* A state is a fact, not a guess.  An artist nobody touched is pending.  There
  is no "0" and no derived value for "we did not look".
* Running out of budget is not finishing.  When a limit stops the batch the
  status is paused and stop_reason names the limit, so a caller cannot mistake
  "we did ten of forty" for "the pool is done".
* Writing back is not verifying.  An artist whose write-back was requested but
  whose result was never read back is *applied*: no longer planned, still owed
  a verification.  Treating applied as finished is how a resumed run dropped
  the very artists nobody had confirmed yet (audit EA-05), so the states are
  split three ways below and a resumed run keeps picking applied artists up.

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

#: States that still owe fresh work: the pipeline has not asked for the
#: write-back yet, including failed, which owes a decision before it moves.
OPEN_STATES = ("pending", "sampled", "reviewed", "planned", "failed")

#: Write-back requested, result not read back.  Not open (nothing may be sent
#: again without a reconcile) and not final (nobody has confirmed it).
AWAITING_VERIFY_STATES = ("applied",)

#: States that need no further work.
FINAL_STATES = ("verified", "skipped")

#: States a resumed run still owes something on.  applied is here on purpose:
#: the verification is outstanding work, not a conclusion (audit EA-05).
RESUMABLE_STATES = OPEN_STATES + AWAITING_VERIFY_STATES

#: Stop reasons that mean "a limit stopped us, there is work left".
LIMIT_REASONS = ("artists_limit", "requests_limit", "bytes_limit", "seconds_limit")

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

#: Plan-level stop reasons: the six above, plus the case where no batch could
#: be formed at all.  A batch never carries capacity_zero, because no batch
#: exists; the plan carries it so the caller sees why nothing was planned.
PLAN_STOP_REASONS = STOP_REASONS + ("capacity_zero",)

#: The action that is safe next, per state.  "reconcile" is deliberately not an
#: event: a failed artist may have failed after its write-back was sent, so the
#: earlier attempt has to be read back before anything is sent again.
NEXT_ACTION = {
    "pending": "sample",
    "sampled": "review",
    "reviewed": "plan",
    "planned": "apply",
    "applied": "verify",
    "failed": "reconcile",
    "verified": "",
    "skipped": "",
}

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
    # The digest is taken over the thawed form, so it is the digest the earlier
    # shallow-freeze dumps produced: freezing changes what a caller may do to
    # the data, never what the data is.
    payload = _canonical({"scope": _thaw(scope), "policy": _thaw(policy)})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _freeze(value):
    """The same data with every list and mapping made unwritable.

    A frozen dataclass freezes its own attributes and nothing inside them:
    `scope["artist_ids"]` used to be a list shared with the caller, so
    `as_dict()["scope"]["artist_ids"].append(...)` edited the batch's member
    set behind its fingerprint (audit EA-06).  Freezing is therefore recursive,
    and a tuple stands in for a list because a tuple cannot be appended to.
    """
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value):
    """A fresh, mutable, JSON-shaped copy: an export never hands out the source.

    Whatever as_dict() returns may be edited by the caller; the object it came
    from must not change when that happens.
    """
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


#: The order the pipeline moves in, used to decide what "forward" means.
_PIPELINE = ("pending", "sampled", "reviewed", "planned", "applied", "verified")


def _status(steps: Mapping, stop_reason: str) -> str:
    """The one place a batch status is decided.

    Computed from the steps every time, so no caller can leave a batch saying
    "active" while every artist in it is verified, and a pause cannot outlive
    the work it paused.  A batch with no artists is complete only when the pool
    itself was empty; an empty batch stopped by a limit is paused, because the
    work it was going to do still exists.
    """
    states = tuple(steps.values())
    if states and all(state in FINAL_STATES for state in states):
        return "complete"
    if not states and stop_reason == "pool_exhausted":
        return "complete"
    if stop_reason in LIMIT_REASONS:
        return "paused"
    return "active"


def _is_forward(current: str, target: str) -> bool:
    """May persisted progress move one artist from current to target?

    Forward is the pipeline order, not raw graph reachability: failed can be
    retried into pending, so a reachability walk would "prove" that applied may
    fall back to planned, which would queue a write-back nobody read back.
    """
    if target == current:
        return True
    if current in FINAL_STATES:
        return False
    if target == "skipped":
        return current in OPEN_STATES
    if target == "failed":
        return current in RESUMABLE_STATES
    if current == "failed":
        return target in _PIPELINE
    return _PIPELINE.index(target) > _PIPELINE.index(current)


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
        object.__setattr__(self, "scope", _freeze(self.scope))
        object.__setattr__(self, "policy", _freeze(self.policy))
        object.__setattr__(self, "steps", _freeze(self.steps))
        if self.status not in STATUSES:
            raise BatchError("invalid_status", self.status)
        if self.stop_reason not in STOP_REASONS:
            raise BatchError("invalid_stop_reason", self.stop_reason)
        for artist_id, state in self.steps.items():
            if state not in STATES:
                raise BatchError("invalid_step_state", (artist_id, state))
        # Every member has exactly one state.  A member without a step is not
        # "pending": it is a batch whose state cannot be read, and a step for a
        # non-member belongs to a different batch.
        if set(self.steps) != set(self.scope["artist_ids"]):
            missing = sorted(set(self.scope["artist_ids"]) - set(self.steps))
            extra = sorted(set(self.steps) - set(self.scope["artist_ids"]))
            raise BatchError("steps_do_not_match_scope", (missing, extra))
        # One status, derived from the steps: a caller may pass one, but the
        # facts decide.  from_dict() refuses a dump whose stored status
        # disagrees instead of quietly repairing it.
        object.__setattr__(self, "status", _status(self.steps, self.stop_reason))

    @property
    def artist_ids(self) -> tuple:
        return tuple(self.scope["artist_ids"])

    @property
    def open_artists(self) -> tuple:
        """Members a resumed run still owes work on, in batch order.

        applied belongs here: its write-back was requested but its result was
        never read back, so the artist is owed a verification, not finished.
        """
        return tuple(
            artist_id
            for artist_id in self.scope["artist_ids"]
            if self.steps[artist_id] in RESUMABLE_STATES
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
            "scope": _thaw(self.scope),
            "policy": _thaw(self.policy),
            "steps": _thaw(self.steps),
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
        if batch.status != value["status"]:
            # The steps say one thing and the dump another: that is a fact to
            # refuse, not a value to overwrite.
            raise BatchError("batch_status_inconsistent", (value["status"], batch.status))
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

    steps = {artist_id: "pending" for artist_id in chosen}
    status = _status(steps, stop_reason)
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
        steps=steps,
        status=status,
        stop_reason=stop_reason,
    )


class BatchPlan:
    """What one planning call covered, and what it left for the next call.

    Iterating a plan yields its batches, so `for batch in plan_batches(...)`
    keeps working; `remaining` is the explicit list of artists no batch covers.
    That is what turns "the pool is split" from a hope into a checkable fact:
    done, planned and remaining are the pool, with no overlap and no hole
    (audit EA-05).
    """

    __slots__ = ("batches", "remaining", "stop_reason")

    def __init__(self, batches=(), remaining=(), stop_reason="none") -> None:
        self.batches = tuple(batches)
        for batch in self.batches:
            if not isinstance(batch, Batch):
                raise BatchError("invalid_batch", batch)
        self.remaining = tuple(_identifier(item, "artist_id") for item in remaining)
        reason = str(stop_reason)
        if reason not in PLAN_STOP_REASONS:
            raise BatchError("invalid_stop_reason", reason)
        self.stop_reason = reason

    @property
    def planned(self) -> tuple:
        """The artists the batches cover, in plan order."""
        return tuple(artist for batch in self.batches for artist in batch.artist_ids)

    def __iter__(self):
        return iter(self.batches)

    def __len__(self) -> int:
        return len(self.batches)

    def __getitem__(self, index):
        return self.batches[index]

    def __eq__(self, other):
        if not isinstance(other, BatchPlan):
            return NotImplemented
        return (self.batches, self.remaining, self.stop_reason) == (
            other.batches,
            other.remaining,
            other.stop_reason,
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "BatchPlan(%d batches, %d planned, %d remaining, %s)" % (
            len(self.batches),
            len(self.planned),
            len(self.remaining),
            self.stop_reason,
        )

    def as_dict(self) -> dict:
        return {
            "batches": [batch.as_dict() for batch in self.batches],
            "remaining": list(self.remaining),
            "stop_reason": self.stop_reason,
            "counts": {
                "batches": len(self.batches),
                "planned": len(self.planned),
                "remaining": len(self.remaining),
            },
        }

    @classmethod
    def from_dict(cls, value) -> "BatchPlan":
        """Rebuild a plan from as_dict(); a shape that cannot answer is refused."""
        if not isinstance(value, Mapping):
            raise BatchError("plan_not_a_mapping", type(value).__name__)
        missing = [key for key in ("batches", "remaining", "stop_reason") if key not in value]
        if missing:
            raise BatchError("plan_field_missing", ",".join(missing))
        raw = value["batches"]
        if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple)):
            raise BatchError("plan_batches_invalid", type(raw).__name__)
        rest = value["remaining"]
        if isinstance(rest, (str, bytes)) or not isinstance(rest, (list, tuple)):
            raise BatchError("plan_remaining_invalid", type(rest).__name__)
        return cls(
            batches=[Batch.from_dict(item) for item in raw],
            remaining=rest,
            stop_reason=value["stop_reason"],
        )


def _check_conservation(known, done, planned, remaining) -> None:
    """done + planned + remaining is the pool: no overlap, no hole, no stray.

    The plan is built to satisfy this, so the check exists to make the
    invariant falsifiable: a future cursor change that drops an artist fails
    here instead of silently shrinking the download set.
    """
    seen: set = set()
    duplicated = []
    for artist in planned:
        if artist in seen:
            duplicated.append(artist)
        seen.add(artist)
    if duplicated:
        raise BatchError("batch_plan_duplicate", sorted(set(duplicated))[:5])
    overlap = sorted(set(planned) & set(remaining))
    if overlap:
        raise BatchError("batch_plan_overlap", overlap[:5])
    covered = set(done) | set(planned) | set(remaining)
    missing = sorted(set(known) - covered)
    if missing:
        raise BatchError("batch_plan_incomplete", missing[:5])
    stray = sorted(covered - set(known))
    if stray:
        raise BatchError("batch_plan_overreach", stray[:5])


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
) -> BatchPlan:
    """Split a pool into consecutive batches, and say what is left over.

    Every batch gets its own fingerprint, the batches never overlap, and
    done + planned + remaining is exactly the pool.  The cursor advances by the
    number of artists a batch actually took, not by the requested size: a batch
    cut short by its budget must not make the next batch start after artists
    nobody ever planned (audit EA-05).

    A plan that can fit nothing is an empty batch list with the whole queue in
    `remaining` and stop_reason capacity_zero.  It never loops and never loses
    a name.
    """
    known = sorted({_identifier(item, "artist_id") for item in pool})
    done = {_identifier(item, "artist_id") for item in done_ids}
    queue = [artist_id for artist_id in known if artist_id not in done]
    size = _count(artists, "artists")
    if size == 0:
        raise BatchError("invalid_artists", artists)
    batches: list = []
    cursor = 0
    stop_reason = "pool_exhausted" if not queue else "none"
    while cursor < len(queue):
        chunk = queue[cursor:cursor + size]
        batch = plan_batch(
            chunk,
            account_id=account_id,
            artists=size,
            requests=requests,
            bytes_budget=bytes_budget,
            seconds=seconds,
            **options,
        )
        taken = len(batch.artist_ids)
        if taken == 0:
            # Zero capacity is not a batch: an empty one would look like work
            # was planned and would lose the names on the next call.
            stop_reason = "capacity_zero"
            break
        batches.append(batch)
        cursor += taken
    plan = BatchPlan(batches=batches, remaining=queue[cursor:], stop_reason=stop_reason)
    _check_conservation(known, done, plan.planned, plan.remaining)
    return plan


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
    # The status is not copied forward: the Batch recomputes it from the steps,
    # so finishing the last artist completes the batch whether it was active or
    # paused (the old code only did that on the paused branch).
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
        if not _is_forward(current, state):
            # Backwards is not "reopening a finished artist" only: applied ->
            # planned would queue a second write-back for an artist whose first
            # one was never read back.
            raise BatchError("progress_not_forward", (artist_id, current, state))
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
    """The artists a resumed run still owes work on, in batch order.

    applied artists are included.  Their write-back was requested but never
    read back, so resuming means verifying them, not starting over and not
    skipping them (audit EA-05).
    """
    return rebuild(batch, progress).open_artists


def next_action(batch: Batch, artist_id: str) -> str:
    """The safe next move for one artist, or "" when there is none.

    "reconcile" is not an advance event: a failed artist may have failed after
    its write-back was sent, so the earlier attempt has to be read back before
    anything is sent again.  A caller that only advances answers from
    EVENT_TARGET therefore cannot re-send a write by accident.
    """
    if not isinstance(batch, Batch):
        raise BatchError("invalid_batch", batch)
    return NEXT_ACTION[batch.state_of(_identifier(artist_id, "artist_id"))]
