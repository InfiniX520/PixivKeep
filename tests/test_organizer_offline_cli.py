"""The three offline organizer commands: exit codes, words, and no writes."""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

from organizer import offline_cli
from organizer.batches import Batch, plan_batch
from organizer.cli import main
from organizer.conflicts import decide
from organizer.group_scope import GroupScope, build_scope


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


PLAN_ROW = {"artist_id": "1001", "groups": ["landscape"], "target": "landscape"}


def build_inputs(tmp_path, *, complete=True, moved=False):
    """Build the three JSON files with the engines themselves, not by hand."""
    before = snapshot_payload(complete=complete)
    after = snapshot_payload(complete=complete)
    if moved:
        after["artists"]["1002"]["groups"] = ["portrait"]
    batch = plan_batch(["1001", "1002"], account_id="a1", artists=2, requests=10)
    scope = build_scope(before, groups=["landscape"])
    plan = {
        "artists": [
            dict(PLAN_ROW),
            {"artist_id": "1002", "groups": ["portrait", "landscape"], "target": "landscape"},
        ]
    }
    decisions = decide(before, after, plan)
    return (
        write(tmp_path, "batch.json", batch.as_dict()),
        write(tmp_path, "decisions.json", decisions),
        write(tmp_path, "scope.json", scope.as_dict()),
    )


def report_args(batch, decisions, scope):
    return [
        "batch-report",
        "--batch", str(batch),
        "--decisions", str(decisions),
        "--scope", str(scope),
    ]


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


def test_batch_report_end_to_end_over_the_real_engines(tmp_path, capsys):
    batch, decisions, scope = build_inputs(tmp_path)
    code = main(report_args(batch, decisions, scope) + ["--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    counts = payload["report"]["counts"]
    assert counts["total"] == 2
    assert sum(counts["states"].values()) == 2
    assert payload["report"]["planned_vs_measured"]["state"] == "unknown"

    text_code = main(report_args(batch, decisions, scope))
    out = capsys.readouterr().out
    assert text_code == 0
    assert "状态计数: " in out
    assert "states.pending=2" in out
    measured = [line for line in out.splitlines() if line.startswith("计划对实测")][0]
    assert "未知" in measured
    assert "0" not in measured


def test_a_moved_artist_makes_the_report_unproven(tmp_path, capsys):
    batch, decisions, scope = build_inputs(tmp_path, moved=True)
    code = main(report_args(batch, decisions, scope))
    out = capsys.readouterr().out
    assert code == 5
    assert "待人工确认: 1 项" in out
    assert "1002" in out


def test_a_tampered_batch_is_refused_by_its_fingerprint(tmp_path, capsys):
    batch, decisions, scope = build_inputs(tmp_path)
    payload = json.loads(Path(batch).read_text(encoding="utf-8"))
    payload["policy"]["budget"]["requests"] = 99
    write(tmp_path, "batch.json", payload)
    code = main(report_args(batch, decisions, scope))
    captured = capsys.readouterr()
    assert code == 3
    assert "batch_invalid:batch_fingerprint_mismatch" in captured.err


def test_a_batch_missing_a_field_names_the_field(tmp_path, capsys):
    batch, decisions, scope = build_inputs(tmp_path)
    payload = json.loads(Path(batch).read_text(encoding="utf-8"))
    payload.pop("steps")
    write(tmp_path, "batch.json", payload)
    code = main(report_args(batch, decisions, scope))
    assert code == 3
    refused = capsys.readouterr().err
    assert "batch_field_missing" in refused
    assert "steps" in refused


def test_a_tampered_scope_is_refused_by_its_fingerprint(tmp_path, capsys):
    batch, decisions, scope = build_inputs(tmp_path)
    payload = json.loads(Path(scope).read_text(encoding="utf-8"))
    payload["artist_ids"] = payload["artist_ids"] + ["9999"]
    write(tmp_path, "scope.json", payload)
    code = main(report_args(batch, decisions, scope))
    captured = capsys.readouterr()
    assert code == 3
    assert "scope_invalid" in captured.err
    assert "scope_members_inconsistent" in captured.err


def test_a_decision_outside_the_batch_is_refused_not_summed(tmp_path, capsys):
    batch, decisions, scope = build_inputs(tmp_path)
    payload = json.loads(Path(decisions).read_text(encoding="utf-8"))
    payload.append(
        {"artist_id": "9999", "kind": "new_member", "action": "replan",
         "reason": "membership_missing", "needs_human": False, "evidence": {}}
    )
    write(tmp_path, "decisions.json", payload)
    code = main(report_args(batch, decisions, scope))
    captured = capsys.readouterr()
    assert code == 3
    assert "report_refused" in captured.err
    assert "Traceback" not in captured.err


def test_batch_report_refuses_when_the_engine_is_absent(tmp_path, capsys, monkeypatch):
    monkeypatch.setitem(sys.modules, "organizer.report", None)
    batch, decisions, scope = build_inputs(tmp_path)
    code = main(report_args(batch, decisions, scope))
    captured = capsys.readouterr()
    assert code == 3
    assert "report_engine_missing" in captured.err


def test_the_round_trip_keeps_both_fingerprints():
    batch = plan_batch(["1001", "1002"], account_id="a1", artists=2, requests=10)
    dumped = batch.as_dict()
    assert Batch.from_dict(dumped).as_dict() == dumped
    assert Batch.from_dict(dumped).fingerprint() == batch.fingerprint()

    scope = build_scope(snapshot_payload(), groups=["landscape"])
    dumped_scope = scope.as_dict()
    assert GroupScope.from_dict(dumped_scope).as_dict() == dumped_scope
    assert GroupScope.from_dict(dumped_scope).fingerprint == scope.fingerprint


def test_a_partial_scope_keeps_the_report_from_claiming_success(tmp_path, capsys):
    batch, decisions, scope = build_inputs(tmp_path, complete=False)
    code = main(report_args(batch, decisions, scope))
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
