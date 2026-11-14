"""Read-only gathering of the facts one follow-crawl coverage summary needs.

The summary must not invent anything, so this module only reads: it asks a
duck-typed source for the run row, the artist rows, the incomplete-artist list
and the source budget, and copies what it finds into plain dictionaries.  It
never marks, appends, updates, refreshes or creates anything - the test suite
asserts that with an AST scan, because "I only call getters" is a claim that
should be checkable rather than trusted.

A source is anything with:

    run                                   # run row, or a callable returning it
    artists(run_id)                       # iterable of artist rows
    incomplete_artists(run_id)            # optional, iterable of rows
    source_budget_status(run_id)          # optional, mapping or None

Missing required pieces raise TypeError: a source that cannot answer is not
the same thing as a run with no artists.

Two of those four calls do not take a run id in the real code base.  The crawl
state store keys everything by run_id, but FollowRepositoryMixin keys
incomplete_artists by job_id and source_budget_status by batch_id.  This module
forwards whatever the caller hands it, so wiring the repository straight in -
StateSource(repository, run_id, incomplete=repository.incomplete_artists) -
does not fail loudly: it returns an empty list for every run, and the summary
quietly loses its "which artists did not finish" note.  Counters and verdicts
are not affected (they come from the artist rows), which is exactly why the
mistake is easy to miss.  Pass a provider closed over the right id space, or
leave the optional reads out and let the summary say less.

Each artist row also carries ``swept_at``: the moment this run finished the
incremental sweep for that artist, or the empty string when the run has no
such evidence.  This module only passes the value through - it does not parse
it, compare it, or decide whether a sweep "counts".
"""
from __future__ import annotations

from typing import Any, Mapping

RUN_FIELDS = ("run_id", "account_id", "config_key", "status", "stop_reason", "total_artists")


_MISSING = object()


def _lookup(source: object, name: str) -> object:
    """getattr without the double read: hasattr would evaluate a property twice."""
    return getattr(source, name, _MISSING)


def _member(source: object, name: str, *args: object) -> object:
    """Read a name from an object, calling it when it is a method."""
    value = _lookup(source, name)
    if value is _MISSING:
        raise TypeError("facts source has no %r" % (name,))
    return value(*args) if callable(value) else value


def _optional(source: object, name: str, *args: object) -> object:
    value = _lookup(source, name)
    if value is _MISSING:
        return None
    return value(*args) if callable(value) else value


def _field(row: object, name: str) -> object:
    if isinstance(row, Mapping):
        return row.get(name)
    return getattr(row, name, None)


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _run_facts(row: object) -> dict[str, Any]:
    if row is None:
        raise TypeError("facts source returned no run")
    for name in ("run_id", "status"):
        if _field(row, name) is None:
            raise TypeError("run row has no %r" % (name,))
    total = _field(row, "total_artists")
    return {
        "run_id": _text(_field(row, "run_id")),
        "account_ref": _text(_field(row, "account_id")),
        "config_key": _text(_field(row, "config_key")),
        "status": _text(_field(row, "status")),
        "stop_reason": _text(_field(row, "stop_reason")),
        "totals": {"artists_total": None if total is None else int(total)},
    }


def _artist_facts(row: object) -> dict[str, Any]:
    artist_id = _field(row, "artist_id")
    if artist_id is None:
        raise TypeError("artist row has no artist_id")
    works = _field(row, "works_seen")
    return {
        "artist_id": _text(artist_id),
        "state": _text(_field(row, "state")),
        "pages": None if works is None else int(works),
        "newest_illust_id": _text(_field(row, "newest_illust_id")),
        # Passed through verbatim: empty string means "no sweep evidence in
        # this run", and interpreting that is the summariser's job, not ours.
        "swept_at": _text(_field(row, "swept_at")),
    }


def _incomplete_facts(rows: object) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for row in rows or ():  # type: ignore[union-attr]
        artist_id = _field(row, "artist_id")
        if artist_id is None:
            continue
        reason = _field(row, "reason")
        if reason is None:
            reason = _field(row, "last_error_kind") or _field(row, "state")
        result.append({"artist_id": _text(artist_id), "reason": _text(reason)})
    return sorted(result, key=lambda item: item["artist_id"])


def _budget_facts(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError("budget must be a mapping or None")
    remaining = value.get("remaining")
    return {
        "state": _text(value.get("state")),
        "remaining": None if remaining is None else int(remaining),
    }


def gather_facts(source: object) -> dict[str, Any]:
    """Copy the run, artist, incomplete and budget facts out of one source."""
    run = _run_facts(_member(source, "run"))
    artists = [
        _artist_facts(row)
        for row in _member(source, "artists", run["run_id"]) or ()  # type: ignore[union-attr]
    ]
    artists.sort(key=lambda item: item["artist_id"])
    incomplete = _incomplete_facts(_optional(source, "incomplete_artists", run["run_id"]))
    budget = _budget_facts(_optional(source, "source_budget_status", run["run_id"]))
    return {"run": run, "artists": artists, "incomplete": incomplete, "budget": budget}


class StateSource:
    """Adapter: a crawl-state repository plus the optional budget/incomplete reads.

    The state repository knows the run and its artist rows; the library
    repository knows which artists are incomplete and how much source budget
    is left.  This adapter keeps both behind the same duck-typed source, and
    it only ever calls their getters.
    """

    def __init__(self, repository, run_id, *, incomplete=None, budget=None):
        self._repository = repository
        self._run_id = str(run_id)
        self._incomplete = incomplete
        self._budget = budget

    @property
    def run(self):
        return self._repository.get_run(self._run_id)

    def artists(self, run_id):
        return self._repository.list_artists(run_id)

    def incomplete_artists(self, run_id):
        if callable(self._incomplete):
            return self._incomplete(run_id)
        return self._incomplete

    def source_budget_status(self, run_id):
        if callable(self._budget):
            return self._budget(run_id)
        return self._budget
