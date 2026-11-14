"""Facts gathering: duck typing, stable ordering and a read-only promise."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from core.follow_facts import gather_facts

_FORBIDDEN = ("mark_", "append_", "update_", "refresh_", "create_", "delete_", "insert_", "save_", "write_")


class Row:
    """A stand-in for the dataclass rows the real repository returns."""

    def __init__(self, **fields):
        self.__dict__.update(fields)


class Source:
    """A duck-typed source that records which getters were used."""

    def __init__(self, run, artists=(), incomplete=(), budget=None):
        self.run = run
        self._artists = list(artists)
        self._incomplete = list(incomplete)
        self._budget = budget
        self.calls = []

    def artists(self, run_id):
        self.calls.append(("artists", run_id))
        return list(self._artists)

    def incomplete_artists(self, run_id):
        self.calls.append(("incomplete_artists", run_id))
        return list(self._incomplete)

    def source_budget_status(self, run_id):
        self.calls.append(("source_budget_status", run_id))
        return self._budget


def _run(**overrides):
    fields = dict(
        run_id="run-1",
        account_id="account-7",
        config_key="cfg-1",
        status="running",
        stop_reason="",
        total_artists=3,
    )
    fields.update(overrides)
    return Row(**fields)


def test_it_copies_the_run_fields():
    facts = gather_facts(Source(_run()))
    assert facts["run"] == {
        "run_id": "run-1",
        "account_ref": "account-7",
        "config_key": "cfg-1",
        "status": "running",
        "stop_reason": "",
        "totals": {"artists_total": 3},
    }


def test_artists_are_sorted_by_id_and_keep_their_counters():
    source = Source(
        _run(),
        artists=[
            Row(artist_id="b", state="done", works_seen=12, newest_illust_id="9"),
            Row(artist_id="a", state="failed", works_seen=0, newest_illust_id=""),
        ],
    )
    facts = gather_facts(source)
    assert [item["artist_id"] for item in facts["artists"]] == ["a", "b"]
    assert facts["artists"][1] == {
        "artist_id": "b",
        "state": "done",
        "pages": 12,
        "newest_illust_id": "9",
    }


def test_incomplete_rows_carry_a_reason_and_are_sorted():
    source = Source(
        _run(),
        incomplete=[
            Row(artist_id="z", reason="failed_page"),
            Row(artist_id="c", last_error_kind="rate_limited"),
        ],
    )
    facts = gather_facts(source)
    assert facts["incomplete"] == [
        {"artist_id": "c", "reason": "rate_limited"},
        {"artist_id": "z", "reason": "failed_page"},
    ]


def test_a_budget_is_copied_and_a_missing_one_is_none():
    stopped = gather_facts(Source(_run(status="paused", stop_reason="budget"), budget={"state": "exhausted", "remaining": 0}))
    assert stopped["budget"] == {"state": "exhausted", "remaining": 0}
    assert gather_facts(Source(_run()))["budget"] is None


def test_an_empty_following_list_is_not_an_error():
    facts = gather_facts(Source(_run(total_artists=0)))
    assert facts["artists"] == []
    assert facts["incomplete"] == []


def test_missing_required_pieces_are_type_errors():
    class NoRun:
        def artists(self, run_id):
            return []

    with pytest.raises(TypeError):
        gather_facts(NoRun())

    class NoArtists:
        run = _run()

    with pytest.raises(TypeError):
        gather_facts(NoArtists())


def test_an_artist_row_without_an_id_is_refused():
    with pytest.raises(TypeError):
        gather_facts(Source(_run(), artists=[Row(state="done")]))


def test_a_budget_that_is_not_a_mapping_is_refused():
    with pytest.raises(TypeError):
        gather_facts(Source(_run(), budget=["not", "a", "mapping"]))


def test_gathering_only_touches_getters():
    source = Source(_run(), artists=[Row(artist_id="a", state="done", works_seen=1)], incomplete=[], budget=None)
    gather_facts(source)
    assert [name for name, _ in source.calls] == ["artists", "incomplete_artists", "source_budget_status"]


def test_the_module_never_calls_a_write_method():
    path = Path(__file__).resolve().parents[1] / "core" / "follow_facts.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if isinstance(name, str) and name.startswith(_FORBIDDEN):
                offenders.append(name)
    assert offenders == []


class RepositorySpy:
    """Only the two getters a read-only gather may use."""

    def __init__(self, run, artists):
        self._run = run
        self._artists = list(artists)
        self.calls = []

    def get_run(self, run_id):
        self.calls.append(("get_run", run_id))
        return self._run

    def list_artists(self, run_id):
        self.calls.append(("list_artists", run_id))
        return list(self._artists)


def test_the_state_source_adapter_reads_through_the_repository():
    from core.follow_facts import StateSource

    repository = RepositorySpy(_run(), [Row(artist_id="a1", state="done", works_seen=2)])
    source = StateSource(
        repository,
        "run-1",
        incomplete=lambda run_id: [Row(artist_id="a2", reason="failed_page")],
        budget=lambda run_id: {"state": "ok", "remaining": 5},
    )
    facts = gather_facts(source)
    assert facts["run"]["run_id"] == "run-1"
    assert [item["artist_id"] for item in facts["artists"]] == ["a1"]
    assert facts["incomplete"] == [{"artist_id": "a2", "reason": "failed_page"}]
    assert facts["budget"] == {"state": "ok", "remaining": 5}
    assert repository.calls == [("get_run", "run-1"), ("list_artists", "run-1")]


def test_the_state_source_adapter_accepts_plain_values_too():
    from core.follow_facts import StateSource

    repository = RepositorySpy(_run(), [])
    source = StateSource(repository, "run-1", incomplete=[], budget=None)
    facts = gather_facts(source)
    assert facts["artists"] == []
    assert facts["incomplete"] == []
    assert facts["budget"] is None
