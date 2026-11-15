"""EA-09: a report whose own inputs do not line up is not a success.

The offline command used to print covers_batch=false and exit 0.  These tests
pin the three things that replaced that: a containment refusal, a decision truth
table, and provenance agreement, plus the exit codes 0 / 5 / 3 that a caller is
allowed to branch on (S0_CONTRACTS_R2 section 8).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from organizer.batches import plan_batch
from organizer.cli import main
from organizer.group_scope import GroupScope, build_scope
from organizer.report import ReportError, build_report


def snapshot(ids=("1", "2"), *, complete=True):
    return {
        "id": "snap-1",
        "status": "complete",
        "memberships_complete": complete,
        "artists": {
            artist_id: {"visibility": "0", "groups": ["g"], "is_following": True}
            for artist_id in ids
        },
        "memberships": {"show": {"g": {"ids": list(ids)}}, "hide": {}},
    }


def batch_of(ids=("1", "2"), *, snapshot_id="snap-1", rules="v1"):
    return plan_batch(
        list(ids),
        account_id="acct-1",
        artists=10,
        requests=20,
        snapshot_id=snapshot_id,
        group="g",
        rules_version=rules,
    )


def decision(artist_id, kind="unchanged", *, action=None, needs_human=None):
    table = {
        "unchanged": ("keep", False),
        "moved": ("needs_human", True),
        "group_renamed": ("needs_human", True),
        "group_deleted": ("needs_human", True),
        "artist_gone": ("needs_human", True),
        "new_member": ("replan", False),
        "unknown": ("needs_human", True),
    }
    expected_action, expected_human = table[kind]
    return {
        "artist_id": artist_id,
        "kind": kind,
        "action": expected_action if action is None else action,
        "reason": "same_as_planned",
        "needs_human": expected_human if needs_human is None else needs_human,
        "evidence": {"before": ["g"], "after": ["g"], "observed_at": "2026-11-15"},
    }


def scope_of(ids=("1", "2"), *, complete=True):
    return build_scope(snapshot(ids, complete=complete), groups=["g"])


# -- containment -------------------------------------------------------------


def test_a_batch_artist_outside_the_scope_is_refused_not_reported():
    scope = scope_of(("2",))
    with pytest.raises(ReportError) as caught:
        build_report(batch_of(("1",)), [], scope)
    assert caught.value.code == "scope_excludes_batch"
    assert caught.value.detail == ["1"]


def test_a_wider_scope_is_refused_by_default():
    scope = scope_of(("1", "2"))
    with pytest.raises(ReportError) as caught:
        build_report(batch_of(("1",)), [], scope)
    assert caught.value.code == "scope_broader_than_batch"
    assert caught.value.detail == ["2"]


def test_a_wider_scope_is_recorded_when_the_caller_declares_it():
    scope = scope_of(("1", "2"))
    report = build_report(batch_of(("1",)), [], scope, superset_ok=True)
    assert report["scope"]["covers_batch"] is True
    assert report["scope"]["superset"] == {"allowed": True, "extra_artists": ["2"]}
    assert report["scope"]["artist_count"] == 2


def test_an_equal_scope_is_not_a_superset():
    report = build_report(batch_of(("1", "2")), [], scope_of(("1", "2")))
    assert report["scope"]["superset"] == {"allowed": False, "extra_artists": []}


def test_the_superset_flag_must_be_a_real_boolean():
    with pytest.raises(ReportError) as caught:
        build_report(batch_of(("1",)), [], scope_of(("1", "2")), superset_ok=1)
    assert caught.value.code == "scope_option_invalid"


# -- the decision truth table ------------------------------------------------


def test_a_kind_and_an_action_that_contradict_each_other_are_refused():
    # Both are legal values on their own; the pair is not a decision anybody
    # could have made.
    with pytest.raises(ReportError) as caught:
        build_report(
            batch_of(("1",)),
            [decision("1", "unchanged", action="replan")],
            scope_of(("1",)),
        )
    assert caught.value.code == "decision_action_mismatch"


def test_needs_human_must_agree_with_the_action():
    with pytest.raises(ReportError) as caught:
        build_report(
            batch_of(("1",)),
            [decision("1", "unchanged", needs_human=True)],
            scope_of(("1",)),
        )
    assert caught.value.code == "decision_needs_human_mismatch"


def test_needs_human_must_be_a_boolean():
    with pytest.raises(ReportError) as caught:
        build_report(
            batch_of(("1",)),
            [decision("1", "unchanged", needs_human="yes")],
            scope_of(("1",)),
        )
    assert caught.value.code == "decision_needs_human_invalid"


def test_a_consistent_decision_passes():
    report = build_report(
        batch_of(("1", "2")),
        [decision("1"), decision("2", "new_member")],
        scope_of(("1", "2")),
    )
    assert report["per_artist"]["2"]["action"] == "replan"


# -- provenance --------------------------------------------------------------


def test_a_scope_from_another_snapshot_is_refused():
    scope = build_scope(snapshot(("1", "2")), groups=["g"])
    with pytest.raises(ReportError) as caught:
        build_report(batch_of(("1", "2"), snapshot_id="snap-9"), [], scope)
    assert caught.value.code == "snapshot_mismatch"


def test_a_rules_version_that_moved_is_refused():
    with pytest.raises(ReportError) as caught:
        build_report(batch_of(("1",)), [], scope_of(("1",)), rules_version="v2")
    assert caught.value.code == "rules_version_mismatch"


def test_another_account_is_refused():
    with pytest.raises(ReportError) as caught:
        build_report(batch_of(("1",)), [], scope_of(("1",)), account_id="acct-2")
    assert caught.value.code == "account_mismatch"


def test_a_scope_that_names_another_account_is_refused():
    source = {"visibility": "show", "catalog_complete": True, "account_id": "acct-2"}
    scope = GroupScope(
        kind="group_download",
        snapshot_id="snap-1",
        group_pairs=(("show", "g"),),
        artist_ids=("1",),
        sides=("show",),
        blocks=(("show", ("1",)),),
        overlap={},
        source=source,
        completeness="complete",
        unknown_reason="",
    )
    with pytest.raises(ReportError) as caught:
        build_report(batch_of(("1",)), [], scope)
    assert caught.value.code == "account_scope_mismatch"


def test_an_unknown_batch_snapshot_is_not_a_contradiction():
    # A batch planned without a snapshot id says nothing; silence is not a
    # disagreement, and refusing here would make the normal offline flow
    # impossible.
    report = build_report(batch_of(("1",), snapshot_id=""), [], scope_of(("1",)))
    assert report["provenance"]["scope_snapshot_id"] == "snap-1"


# -- the command ---------------------------------------------------------------


def write(tmp_path, name, payload):
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def inputs(tmp_path, *, batch_ids=("1", "2"), scope_ids=("1", "2"), complete=True):
    batch = write(tmp_path, "b.json", batch_of(batch_ids).as_dict())
    scope = write(tmp_path, "s.json", scope_of(scope_ids, complete=complete).as_dict())
    decisions = write(tmp_path, "d.json", [decision(i) for i in batch_ids])
    return batch, decisions, scope


def run(batch, decisions, scope, *extra):
    return ["batch-report", "--batch", str(batch), "--decisions", str(decisions),
            "--scope", str(scope), *extra]


def test_the_command_exits_zero_only_when_everything_lines_up(tmp_path, capsys):
    batch, decisions, scope = inputs(tmp_path)
    code = main(run(batch, decisions, scope, "--json"))
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["report"]["scope"]["covers_batch"] is True
    assert payload["report"]["proof_limits"]["website_write_back"] == "not_proven_here"


def test_a_scope_that_excludes_the_batch_exits_three(tmp_path, capsys):
    batch, decisions, scope = inputs(tmp_path, batch_ids=("1",), scope_ids=("2",))
    code = main(run(batch, decisions, scope, "--json"))
    payload = json.loads(capsys.readouterr().out)
    assert code == 3
    assert payload["ok"] is False
    assert payload["code"].startswith("report_refused:scope_excludes_batch")


def test_a_wider_scope_exits_three_until_the_flag_is_given(tmp_path, capsys):
    batch, decisions, scope = inputs(tmp_path, batch_ids=("1",), scope_ids=("1", "2"))
    assert main(run(batch, decisions, scope)) == 3
    capsys.readouterr()

    code = main(run(batch, decisions, scope, "--superset-ok", "--json"))
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["report"]["scope"]["superset"]["extra_artists"] == ["2"]


def test_an_incomplete_scope_exits_five_and_never_zero(tmp_path, capsys):
    batch, decisions, scope = inputs(tmp_path, complete=False)
    code = main(run(batch, decisions, scope, "--json"))
    payload = json.loads(capsys.readouterr().out)
    assert code == 5
    assert payload["report"]["proof_limits"]["coverage"] != "complete"


def test_an_undecided_artist_exits_five(tmp_path, capsys):
    batch, decisions, scope = inputs(tmp_path)
    write(tmp_path, "d.json", [decision("1")])
    code = main(run(batch, decisions, scope, "--json"))
    payload = json.loads(capsys.readouterr().out)
    assert code == 5
    assert payload["report"]["unknowns"][0]["reason"] == "no_decision_for_artist"


def test_a_rules_version_flag_that_moved_exits_three(tmp_path, capsys):
    batch, decisions, scope = inputs(tmp_path)
    code = main(run(batch, decisions, scope, "--rules-version", "v9"))
    assert code == 3
    assert "rules_version_mismatch" in capsys.readouterr().err


def test_an_account_flag_that_moved_exits_three(tmp_path, capsys):
    batch, decisions, scope = inputs(tmp_path)
    code = main(run(batch, decisions, scope, "--account", "acct-9"))
    assert code == 3
    assert "account_mismatch" in capsys.readouterr().err


def test_a_contradictory_decision_in_a_file_exits_three(tmp_path, capsys):
    batch, decisions, scope = inputs(tmp_path)
    payload = json.loads(Path(decisions).read_text(encoding="utf-8"))
    payload[0]["action"] = "replan"
    write(tmp_path, "d.json", payload)
    code = main(run(batch, decisions, scope))
    assert code == 3
    assert "decision_action_mismatch" in capsys.readouterr().err


def test_no_path_prints_a_traceback(tmp_path, capsys):
    batch, decisions, scope = inputs(tmp_path, batch_ids=("1",), scope_ids=("2",))
    main(run(batch, decisions, scope))
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "Traceback" not in captured.out
