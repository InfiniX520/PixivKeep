"""The three offline organizer commands: exit codes, words, and no writes."""
from __future__ import annotations

import ast
import json
import types
from pathlib import Path

import pytest

import organizer
from organizer import offline_cli
from organizer.cli import main


def write(tmp_path, name, payload):
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def pool(tmp_path, ids=("1001", "1002", "1003")):
    return write(tmp_path, "pool.json", {"artists": list(ids)})


def artist(artist_id, groups, *, visibility="0", is_following=None):
    row = {"artist_id": artist_id, "groups": list(groups), "visibility": visibility}
    if is_following is not None:
        row["is_following"] = is_following
    return row


def snapshot_payload(*, complete=True, memberships=None):
    return {
        "id": "snap-1",
        "status": "complete",
        "memberships_complete": complete,
        "artists": {
            "1001": artist("1001", ["landscape"]),
            "1002": artist("1002", ["portrait", "landscape"]),
            "1003": artist("1003", ["portrait"]),
        },
        "memberships": memberships or {},
    }


def batch_args(tmp_path, **overrides):
    values = {
        "--pool": str(pool(tmp_path)),
        "--max-artists": "2",
        "--max-requests": "40",
        "--max-bytes": "1000000",
        "--max-seconds": "60",
    }
    values.update(overrides)
    argv = ["batch-plan"]
    for key, value in values.items():
        argv.extend([key, value])
    return argv


def fake_report_module(monkeypatch, *, unknowns=()):
    module = types.ModuleType("organizer.report")

    def build_report(batch, decisions, scope, *, measured=None, generated_at=""):
        return {
            "counts": {"applied": 1, "pending": 1},
            "unknowns": list(unknowns),
            "planned_vs_measured": {"state": "unknown", "reason": "no_measurement"},
            "provenance": {
                "batch_fingerprint": (batch or {}).get("fingerprint", ""),
                "scope_completeness": (scope or {}).get("completeness", ""),
            },
        }

    module.build_report = build_report
    monkeypatch.setitem(__import__("sys").modules, "organizer.report", module)
    monkeypatch.delattr(organizer, "report", raising=False)
    return module


def test_batch_plan_prints_counts_status_and_fingerprint(tmp_path, capsys):
    code = main(batch_args(tmp_path))
    out = capsys.readouterr().out
    assert code == 0
    assert "画师: 2 位" in out
    assert "状态: " in out
    assert "指纹: " in out


def test_batch_plan_json_carries_the_batch(tmp_path, capsys):
    code = main(batch_args(tmp_path) + ["--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["command"] == "batch-plan"
    assert set(payload["batch"]) >= {"batch_id", "steps", "status", "stop_reason"}
    assert len(payload["batch"]["steps"]) == 2


def test_a_zero_budget_plans_nothing_and_says_why(tmp_path, capsys):
    code = main(batch_args(tmp_path, **{"--max-requests": "0"}) + ["--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["batch"]["steps"] == {}
    assert payload["batch"]["status"] == "paused"
    assert payload["batch"]["stop_reason"] == "requests_limit"


def test_a_missing_pool_is_a_refusal_not_a_traceback(tmp_path, capsys):
    code = main(batch_args(tmp_path, **{"--pool": str(tmp_path / "nope.json")}))
    captured = capsys.readouterr()
    assert code == 3
    assert "被拒" in captured.err
    assert "Traceback" not in captured.err


def test_a_broken_pool_file_is_a_refusal(tmp_path, capsys):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    code = main(batch_args(tmp_path, **{"--pool": str(path)}))
    captured = capsys.readouterr()
    assert code == 3
    assert "input_not_json" in captured.err


def test_an_empty_pool_is_refused(tmp_path, capsys):
    empty = write(tmp_path, "empty.json", {"artists": []})
    code = main(batch_args(tmp_path, **{"--pool": str(empty)}))
    assert code == 3
    assert "pool_empty" in capsys.readouterr().err


def test_a_negative_limit_is_refused(tmp_path, capsys):
    code = main(batch_args(tmp_path, **{"--max-artists": "-1"}))
    assert code == 3
    assert "limit_invalid" in capsys.readouterr().err


def test_scope_plan_of_a_complete_snapshot_exits_zero(tmp_path, capsys):
    snap = write(tmp_path, "snap.json", snapshot_payload())
    code = main(["scope-plan", "--snapshot", str(snap), "--group", "landscape"])
    out = capsys.readouterr().out
    assert code == 0
    assert "画师: 2 位" in out
    assert "完整性: 完整" in out


def test_an_incomplete_snapshot_cannot_claim_a_whole_group(tmp_path, capsys):
    snap = write(tmp_path, "snap.json", snapshot_payload(complete=False))
    code = main(["scope-plan", "--snapshot", str(snap), "--group", "landscape"])
    out = capsys.readouterr().out
    assert code == 5
    assert "不能当作该组的全集" in out
    assert "完整性: 完整" not in out


def test_a_bare_name_on_both_sides_is_refused(tmp_path, capsys):
    same = snapshot_payload(
        memberships={"show": {"dup": {"ids": ["1001"]}}, "hide": {"dup": {"ids": ["1002"]}}}
    )
    snap = write(tmp_path, "snap.json", same)
    code = main(["scope-plan", "--snapshot", str(snap), "--group", "dup"])
    captured = capsys.readouterr()
    assert code == 3
    assert "group_ambiguous" in captured.err


def test_an_explicit_side_takes_only_that_side(tmp_path, capsys):
    same = snapshot_payload(
        memberships={"show": {"dup": {"ids": ["1001"]}}, "hide": {"dup": {"ids": ["1002"]}}}
    )
    # Both artists sit in a group called dup, but on different sides: the member
    # set is decided by the artist rows, so 1002 must carry the name and be
    # followed privately.
    same["artists"]["1001"]["groups"] = ["dup"]
    same["artists"]["1002"]["groups"] = ["dup"]
    same["artists"]["1002"]["visibility"] = "1"
    snap = write(tmp_path, "snap.json", same)
    code = main(["scope-plan", "--snapshot", str(snap), "--group", "hide:dup", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code in (0, 5)
    assert payload["scope"]["artist_ids"] == ["1002"]


def test_scope_plan_without_a_group_is_refused(tmp_path, capsys):
    snap = write(tmp_path, "snap.json", snapshot_payload())
    code = main(["scope-plan", "--snapshot", str(snap)])
    assert code == 3
    assert "group_argument_empty" in capsys.readouterr().err


def test_batch_report_uses_the_engine_and_never_writes_zero_for_unknown(
    tmp_path, capsys, monkeypatch
):
    fake_report_module(monkeypatch)
    batch = write(tmp_path, "batch.json", {"fingerprint": "abc", "steps": {"1001": "applied"}})
    decisions = write(tmp_path, "decisions.json", [])
    scope = write(tmp_path, "scope.json", {"completeness": "complete", "fingerprint": "def"})
    code = main([
        "batch-report", "--batch", str(batch), "--decisions", str(decisions),
        "--scope", str(scope),
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert "状态计数: " in out
    measured = [line for line in out.splitlines() if line.startswith("计划对实测")][0]
    assert "未知" in measured
    assert "0" not in measured


def test_batch_report_with_unknowns_cannot_exit_zero(tmp_path, capsys, monkeypatch):
    fake_report_module(monkeypatch, unknowns=[{"artist_id": "1002", "reason": "group_ambiguous"}])
    batch = write(tmp_path, "batch.json", {"fingerprint": "abc", "steps": {}})
    decisions = write(tmp_path, "decisions.json", [])
    scope = write(tmp_path, "scope.json", {"completeness": "complete"})
    code = main([
        "batch-report", "--batch", str(batch), "--decisions", str(decisions),
        "--scope", str(scope),
    ])
    out = capsys.readouterr().out
    assert code == 5
    assert "待人工确认: 1 项" in out


def test_batch_report_refuses_when_the_engine_is_absent(tmp_path, capsys, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "organizer.report", None)
    batch = write(tmp_path, "batch.json", {"fingerprint": "abc", "steps": {}})
    decisions = write(tmp_path, "decisions.json", [])
    scope = write(tmp_path, "scope.json", {"completeness": "complete"})
    code = main([
        "batch-report", "--batch", str(batch), "--decisions", str(decisions),
        "--scope", str(scope),
    ])
    captured = capsys.readouterr()
    assert code == 3
    assert "report_engine_missing" in captured.err


def test_a_partial_scope_cannot_make_the_report_say_zero(tmp_path, capsys, monkeypatch):
    fake_report_module(monkeypatch)
    batch = write(tmp_path, "batch.json", {"fingerprint": "abc", "steps": {}})
    decisions = write(tmp_path, "decisions.json", [])
    scope = write(tmp_path, "scope.json", {"completeness": "partial"})
    code = main([
        "batch-report", "--batch", str(batch), "--decisions", str(decisions),
        "--scope", str(scope),
    ])
    assert code == 5


def test_the_module_cannot_reach_the_network_or_write_files():
    source = (Path(offline_cli.__file__)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "importlib", "json", "sys", "pathlib", "organizer"}
    for forbidden in ("requests", "urllib", "socket", "http", "subprocess"):
        assert forbidden not in imported
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", "")
            assert name not in {"write_text", "write_bytes", "mkdir", "commit", "executescript"}
