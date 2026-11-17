"""RA-05: the account a scope came from travels with it, and gates the report.

The audit's counterexample: a scope derived from account B's snapshot was placed
under account A's batch.  The member sets matched, the snapshot labels matched,
and the report had nothing left to compare, so it produced a successful document
about one account's catalog using another account's evidence.

These tests pin the replacement: the scope names its account (or says it cannot),
the two offline commands surface that fact, and no flag can turn "the snapshot
never said" into "same account".
"""
from __future__ import annotations

import json

import pytest

from organizer.batches import plan_batch
from organizer.cli import main
from organizer.common import OrganizerError
from organizer.group_scope import GroupScope, build_scope, ensure_current
from organizer.report import ReportError, build_report

ACCOUNT = "acct-1"
OTHER = "acct-2"


def snapshot(ids=("1", "2"), *, complete=True, account=ACCOUNT, snapshot_id="snap-1",
             status="complete"):
    """One snapshot as the exporter writes it: id, account, memberships, rows."""
    data = {
        "id": snapshot_id,
        "status": status,
        "memberships_complete": complete,
        "artists": {
            artist_id: {"visibility": "0", "groups": ["g"], "is_following": True}
            for artist_id in ids
        },
        "memberships": {"show": {"g": {"ids": list(ids)}}, "hide": {}},
    }
    if account is not None:
        data["account_id"] = account
    return data


def batch_of(ids=("1", "2"), *, snapshot_id="snap-1", rules="v1", account=ACCOUNT):
    return plan_batch(
        list(ids),
        account_id=account,
        artists=10,
        requests=20,
        snapshot_id=snapshot_id,
        group="g",
        rules_version=rules,
    )


def decision(artist_id, kind="unchanged"):
    table = {
        "unchanged": ("keep", False),
        "unknown": ("needs_human", True),
        "moved": ("needs_human", True),
    }
    action, needs_human = table[kind]
    return {
        "artist_id": artist_id,
        "kind": kind,
        "action": action,
        "reason": "same_as_planned",
        "needs_human": needs_human,
        "evidence": {"before": ["g"], "after": ["g"], "observed_at": "2026-11-15"},
    }


def scope_of(ids=("1", "2"), *, complete=True, account=ACCOUNT):
    return build_scope(snapshot(ids, complete=complete, account=account), groups=["g"])


def code_of(call):
    with pytest.raises(OrganizerError) as caught:
        call()
    return caught.value.code


# -- the scope carries whose catalog it is ------------------------------------


def test_a_scope_names_the_account_its_snapshot_came_from():
    scope = scope_of()
    assert scope.account_id == ACCOUNT
    assert scope.account_id_state == "recorded"
    assert scope.source["account_id"] == ACCOUNT
    assert scope.source["account_id_state"] == "recorded"
    identity = scope.source["identity_fingerprint"]
    assert len(identity) == 64 and all(c in "0123456789abcdef" for c in identity)
    # The member-set digest is unchanged by any of this: it still describes the
    # members, not whose catalog they came from.
    assert len(scope.fingerprint) == 64
    assert scope.fingerprint != identity


def test_a_snapshot_without_an_account_is_unknown_and_never_the_batchs():
    scope = scope_of(account=None)
    assert scope.account_id == ""
    assert scope.account_id_state == "not_recorded"
    assert scope.source["account_id"] == ""
    assert scope.source["account_id_state"] == "not_recorded"
    # It still describes a member set honestly; what it cannot say is whose.
    assert scope.artist_ids == ("1", "2")
    assert scope.completeness == "complete"


def test_the_identity_digest_separates_two_accounts_with_identical_members():
    one = scope_of(account=ACCOUNT)
    other = scope_of(account=OTHER)
    # Same snapshot label, same groups, same members: the member-set digest is
    # identical by construction, which is exactly why it cannot be the gate.
    assert one.fingerprint == other.fingerprint
    assert one.source["identity_fingerprint"] != other.source["identity_fingerprint"]


def test_a_broken_account_field_is_a_snapshot_error_not_a_silent_unknown():
    data = snapshot()
    data["account_id"] = 12345
    assert code_of(lambda: build_scope(data, groups=["g"])) == "snapshot_account_invalid"
    blank = snapshot()
    blank["account_id"] = "   "
    # Blank is the same fact as absent: the export did not name an account.
    assert build_scope(blank, groups=["g"]).account_id == ""


# -- export and restore -------------------------------------------------------


def test_a_dump_carries_the_account_and_refuses_a_rewritten_identity():
    scope = scope_of()
    dump = scope.as_dict()
    assert dump["account_id"] == ACCOUNT
    assert dump["source"]["identity_fingerprint"] == scope.source["identity_fingerprint"]
    restored = GroupScope.from_dict(dump)
    assert restored.account_id == ACCOUNT
    assert restored.source["identity_fingerprint"] == scope.source["identity_fingerprint"]
    assert restored == scope

    dump["account_id"] = OTHER
    dump["source"]["account_id"] = OTHER
    assert code_of(lambda: GroupScope.from_dict(dump)) == "scope_identity_fingerprint_mismatch"


def test_a_dump_without_an_account_still_loads_and_cannot_claim_one():
    dump = scope_of().as_dict()
    dump.pop("account_id")
    dump["source"].pop("account_id")
    dump["source"].pop("account_id_state")
    dump["source"].pop("identity_fingerprint")
    restored = GroupScope.from_dict(dump)
    assert restored.account_id == ""
    assert restored.account_id_state == "not_recorded"
    # Re-derived, not trusted: the restored scope has its own consistency digest
    # over "no account", which is not the digest of a scope built with one.
    assert restored.source["identity_fingerprint"] != scope_of().source["identity_fingerprint"]


def test_the_constructor_names_a_bad_account_in_its_own_vocabulary():
    assert code_of(lambda: GroupScope(
        kind="group_download",
        snapshot_id="snap-1",
        account_id=12345,
        group_pairs=(("show", "g"),),
        artist_ids=("1",),
        sides=("show",),
        blocks=(("show", ("1",)),),
        overlap={},
        source={"visibility": "show"},
        completeness="complete",
        unknown_reason="",
    )) == "scope_account_invalid"


def test_the_constructor_refuses_two_different_accounts():
    source = {"visibility": "show", "account_id": OTHER}
    assert code_of(lambda: GroupScope(
        kind="group_download",
        snapshot_id="snap-1",
        account_id=ACCOUNT,
        group_pairs=(("show", "g"),),
        artist_ids=("1",),
        sides=("show",),
        blocks=(("show", ("1",)),),
        overlap={},
        source=source,
        completeness="complete",
        unknown_reason="",
    )) == "scope_account_inconsistent"


def test_a_hand_built_scope_adopts_the_account_its_evidence_names():
    scope = GroupScope(
        kind="group_download",
        snapshot_id="snap-1",
        group_pairs=(("show", "g"),),
        artist_ids=("1",),
        sides=("show",),
        blocks=(("show", ("1",)),),
        overlap={},
        source={"visibility": "show", "account_id": OTHER},
        completeness="complete",
        unknown_reason="",
    )
    # No field was given, so the evidence bag is the only place the account can
    # come from; adopting it keeps hand-built scopes (the dump path) working.
    assert scope.account_id == OTHER
    assert scope.source["account_id"] == OTHER


# -- the report gate ----------------------------------------------------------


def test_another_accounts_scope_is_refused_for_the_same_snapshot_label():
    scope = scope_of(account=OTHER)
    with pytest.raises(ReportError) as caught:
        build_report(batch_of(), [decision("1"), decision("2")], scope)
    assert caught.value.code == "account_scope_mismatch"
    assert caught.value.detail == (OTHER, ACCOUNT)


def test_the_account_gate_does_not_lean_on_the_snapshot_label():
    # A batch planned without a snapshot id carries no label at all; the account
    # comparison still has to happen, or the label becomes the gate.
    scope = scope_of(account=OTHER)
    with pytest.raises(ReportError) as caught:
        build_report(batch_of(snapshot_id=""), [decision("1"), decision("2")], scope)
    assert caught.value.code == "account_scope_mismatch"


def test_a_scope_that_cannot_name_its_account_is_reported_unknown():
    scope = scope_of(account=None)
    # Artist 2 has no decision, so there is one artist-level unknown as well: the
    # ordering claim below needs both kinds in the same list.
    report = build_report(batch_of(), [decision("1")], scope)
    assert report["scope"]["account_id"] == ""
    assert report["scope"]["account_id_state"] == "not_recorded"
    assert report["proof_limits"]["account"] == "unknown"
    assert report["proof_limits"]["complete"] is False
    assert "scope_account_unknown" in report["proof_limits"]["incomplete_reasons"]
    # The scope-level unknown is appended, so an artist-level reader that looks
    # at the head of the list still finds the artists it asked about.
    assert [row.get("scope") for row in report["unknowns"]] == [None, "account"]
    assert report["unknowns"][0]["reason"] == "no_decision_for_artist"
    assert report["unknowns"][-1] == {
        "scope": "account",
        "state": "unknown",
        "reason": "scope_account_unknown",
    }
    # Whatever it cannot prove, it still refuses to claim the website was written.
    assert report["proof_limits"]["website_write_back"] == "not_proven_here"


def test_a_matching_account_leaves_no_unknowns_at_all():
    report = build_report(batch_of(), [decision("1"), decision("2")], scope_of())
    assert report["unknowns"] == []
    assert report["proof_limits"]["account"] == "matched"
    assert report["proof_limits"]["complete"] is True
    assert report["proof_limits"]["incomplete_reasons"] == []
    assert report["provenance"]["scope_account_id"] == ACCOUNT
    assert report["provenance"]["scope_account_state"] == "recorded"
    assert report["provenance"]["scope_identity_fingerprint"] == scope_of().source["identity_fingerprint"]


def test_the_report_is_complete_only_when_all_three_facts_hold():
    decided = [decision("1"), decision("2")]
    whole = build_report(batch_of(), decided, scope_of())
    assert whole["proof_limits"]["complete"] is True

    undecided = build_report(batch_of(), [decision("1")], scope_of())
    assert undecided["proof_limits"]["complete"] is False
    assert "unknowns_present" in undecided["proof_limits"]["incomplete_reasons"]

    partial = build_report(batch_of(), decided, scope_of(complete=False))
    assert partial["proof_limits"]["complete"] is False
    assert "coverage_not_complete" in partial["proof_limits"]["incomplete_reasons"]

    unknown_account = build_report(batch_of(), decided, scope_of(account=None))
    assert unknown_account["proof_limits"]["complete"] is False
    assert unknown_account["proof_limits"]["incomplete_reasons"] == ["scope_account_unknown"]


def test_a_wider_scope_does_not_launder_an_unknown_account():
    scope = scope_of(("1", "2"), account=None)
    report = build_report(batch_of(("1",)), [decision("1")], scope, superset_ok=True)
    assert report["scope"]["superset"]["allowed"] is True
    assert report["proof_limits"]["account"] == "unknown"
    assert report["proof_limits"]["complete"] is False


# -- ensure_current: four identity cases, and two unknowns --------------------


def test_ensure_current_accepts_the_same_account_and_the_same_members():
    before = scope_of()
    fresh = ensure_current(before, snapshot())
    assert fresh.account_id == ACCOUNT
    assert fresh.fingerprint == before.fingerprint


def test_ensure_current_refuses_a_moved_account():
    before = scope_of()
    assert code_of(lambda: ensure_current(before, snapshot(account=OTHER))) == "scope_account_changed"


def test_ensure_current_upgrades_a_legacy_scope_instead_of_stranding_it():
    # A dump written before the account was exported cannot name one; the next
    # snapshot can.  The evidence got stronger, so the task continues and the
    # returned scope is the one that now carries the account.
    before = scope_of(account=None)
    assert before.account_id == ""
    fresh = ensure_current(before, snapshot())
    assert fresh.account_id == ACCOUNT
    assert fresh.account_id_state == "recorded"
    assert fresh.fingerprint == before.fingerprint


def test_ensure_current_refuses_a_snapshot_that_lost_the_account():
    # The scope named an account; a snapshot that cannot name one has proven
    # nothing about it, so silence is not allowed to stand in for agreement.
    before = scope_of()
    assert code_of(lambda: ensure_current(before, snapshot(account=None))) == "scope_account_unproven"


def test_ensure_current_leaves_two_unknown_accounts_to_the_report():
    before = scope_of(account=None)
    fresh = ensure_current(before, snapshot(account=None))
    assert fresh.account_id == ""
    assert fresh.fingerprint == before.fingerprint


# -- the two commands ---------------------------------------------------------


def write(tmp_path, name, payload):
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def plan_inputs(tmp_path, *, scope_account=ACCOUNT, batch_account=ACCOUNT, snapshot_id="snap-1"):
    snap = write(tmp_path, "snap.json", snapshot(account=scope_account))
    batch = write(tmp_path, "batch.json", batch_of(["1", "2"], snapshot_id=snapshot_id,
                                                  account=batch_account).as_dict())
    decisions = write(tmp_path, "d.json", [decision("1"), decision("2")])
    return snap, batch, decisions


def scope_plan(tmp_path, snap):
    return ["scope-plan", "--snapshot", str(snap), "--group", "g", "--json"]


def report_args(tmp_path, batch, decisions, scope_path, *extra):
    return ["batch-report", "--batch", str(batch), "--decisions", str(decisions),
            "--scope", str(scope_path), *extra]


def test_scope_plan_prints_the_account_and_exits_zero_when_it_is_recorded(tmp_path, capsys):
    snap, _batch, _decisions = plan_inputs(tmp_path)
    assert main(["scope-plan", "--snapshot", str(snap), "--group", "g"]) == 0
    out = capsys.readouterr().out
    assert "账号: %s" % (ACCOUNT,) in out
    assert "完整性: 完整" in out


def test_scope_plan_without_an_account_exits_five_and_says_so(tmp_path, capsys):
    snap, _batch, _decisions = plan_inputs(tmp_path, scope_account=None)
    code = main(["scope-plan", "--snapshot", str(snap), "--group", "g"])
    out = capsys.readouterr().out
    assert code == 5
    assert "账号: 未记录" in out
    assert "无法核对账号来源" in out


def test_the_two_commands_round_trip_through_json(tmp_path, capsys):
    snap, batch, decisions = plan_inputs(tmp_path)
    assert main(scope_plan(tmp_path, snap)) == 0
    payload = json.loads(capsys.readouterr().out)
    scope_path = write(tmp_path, "scope.json", payload["scope"])
    code = main(report_args(tmp_path, batch, decisions, scope_path, "--json"))
    data = json.loads(capsys.readouterr().out)
    assert code == 0
    assert data["report"]["scope"]["account_id"] == ACCOUNT
    assert data["report"]["proof_limits"]["account"] == "matched"
    assert data["report"]["unknowns"] == []


def test_a_scope_written_from_another_account_is_refused_end_to_end(tmp_path, capsys):
    snap, batch, decisions = plan_inputs(tmp_path, scope_account=OTHER)
    assert main(scope_plan(tmp_path, snap)) == 0
    payload = json.loads(capsys.readouterr().out)
    scope_path = write(tmp_path, "scope.json", payload["scope"])
    code = main(report_args(tmp_path, batch, decisions, scope_path))
    captured = capsys.readouterr()
    assert code == 3
    assert "account_scope_mismatch" in captured.err
    assert "Traceback" not in captured.err


def test_a_legacy_scope_reaches_the_report_as_unknown(tmp_path, capsys):
    snap, batch, decisions = plan_inputs(tmp_path, scope_account=None)
    assert main(scope_plan(tmp_path, snap)) == 5
    payload = json.loads(capsys.readouterr().out)
    scope_path = write(tmp_path, "scope.json", payload["scope"])
    code = main(report_args(tmp_path, batch, decisions, scope_path))
    out = capsys.readouterr().out
    assert code == 5
    assert "范围账号: 未记录（无法核对账号来源）" in out
    assert "范围account: scope_account_unknown" in out


def test_the_account_flag_checks_the_calling_context_and_nothing_else(tmp_path, capsys):
    # The flag agrees with the batch, which is all it can prove; the scope still
    # cannot name an account, so the answer stays unproven.
    snap, batch, decisions = plan_inputs(tmp_path, scope_account=None)
    main(scope_plan(tmp_path, snap))
    payload = json.loads(capsys.readouterr().out)
    scope_path = write(tmp_path, "scope.json", payload["scope"])
    code = main(report_args(tmp_path, batch, decisions, scope_path, "--account", ACCOUNT))
    capsys.readouterr()
    assert code == 5

    # And it still refuses when it disagrees with the batch it is checking.
    code = main(report_args(tmp_path, batch, decisions, scope_path, "--account", "acct-9"))
    assert code == 3
    assert "account_mismatch" in capsys.readouterr().err


@pytest.mark.parametrize(
    "case,expected,cause",
    [
        ("scope_complete_with_account", 0, "member set whole and account recorded"),
        ("scope_complete_without_account", 5, "account unknown"),
        ("scope_incomplete_with_account", 5, "coverage partial"),
        ("scope_missing_file", 3, "input missing"),
        ("report_aligned", 0, "batch, decisions and scope all line up"),
        ("report_scope_other_account", 3, "account_scope_mismatch"),
        ("report_scope_without_account", 5, "account unknown"),
        ("report_undecided_artist", 5, "unknown decision"),
        ("report_broken_decision", 3, "decision truth table"),
    ],
)
def test_each_exit_code_has_exactly_one_cause(tmp_path, capsys, case, expected, cause):
    """The 0 / 3 / 5 table, one row per cause.

    Every row asserts the code *and* the fact that caused it, so a row that
    starts failing for another reason is visible as itself and not as "the
    numbers moved".
    """
    assert cause  # the table is documentation as much as a test
    if case.startswith("scope_"):
        account = None if case.endswith("without_account") else ACCOUNT
        complete = not case.startswith("scope_incomplete")
        snap = write(tmp_path, "snap.json", snapshot(account=account, complete=complete))
        if case == "scope_missing_file":
            snap = tmp_path / "absent.json"
        code = main(["scope-plan", "--snapshot", str(snap), "--group", "g"])
        capsys.readouterr()
        assert code == expected
        return

    snap, batch, decisions = plan_inputs(
        tmp_path, scope_account=None if case.endswith("without_account") else ACCOUNT
    )
    if case == "report_scope_other_account":
        snap = write(tmp_path, "snap.json", snapshot(account=OTHER))
    main(["scope-plan", "--snapshot", str(snap), "--group", "g", "--json"])
    payload = json.loads(capsys.readouterr().out)
    scope_path = write(tmp_path, "scope.json", payload["scope"])
    if case == "report_undecided_artist":
        write(tmp_path, "d.json", [decision("1")])
    if case == "report_broken_decision":
        broken = [decision("1"), decision("2")]
        broken[0]["action"] = "replan"
        write(tmp_path, "d.json", broken)
    code = main(report_args(tmp_path, batch, decisions, scope_path))
    capsys.readouterr()
    assert code == expected
