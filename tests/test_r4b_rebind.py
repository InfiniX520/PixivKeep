"""R4-B: a legacy scope cannot acquire an account from a later snapshot.

The audit's R4-02 counterexample moved a scope from "no account recorded" to
"account B" by refreshing it against account B's snapshot, and the report went
from unknown/incomplete to matched/complete.  The member set and the snapshot
label were the same in both snapshots, which is exactly why neither of them can
carry an identity (RA-05's original counterexample was the same label pointing at
another account).

This file pins the replacement rule (S0_CONTRACTS_R4 section 2):
a refresh adopts members and evidence, never an account; a caller that needs the
account has to build a fresh scope from the snapshot it trusts.
"""
from __future__ import annotations

import json

import pytest

from organizer.cli import main
from organizer.common import OrganizerError
from organizer.group_scope import GroupScope, build_scope, ensure_current
from organizer.report import ReportError, build_report

from test_ra05_scope_account import (
    ACCOUNT,
    OTHER,
    batch_of,
    code_of,
    decision,
    scope_of,
    snapshot,
    write,
)


def decisions(*artist_ids):
    return [decision(artist_id) for artist_id in artist_ids]


# -- the refresh adopts evidence, not identity ---------------------------------


def test_a_later_snapshot_cannot_give_a_legacy_scope_its_account():
    legacy = build_scope(snapshot(account=None, snapshot_id="legacy-without-account"), groups=["g"])
    fresh = ensure_current(legacy, snapshot(account=OTHER, snapshot_id="later"))
    control = build_scope(snapshot(account=OTHER, snapshot_id="later"), groups=["g"])

    # The members and their fingerprint are the refreshed ones ...
    assert fresh.fingerprint == legacy.fingerprint == control.fingerprint
    assert fresh.snapshot_id == "later"
    # ... and the identity is not: the returned scope is indistinguishable from a
    # scope whose snapshot never named an account.
    assert fresh.account_id == ""
    assert fresh.account_id_state == "not_recorded"
    assert fresh.account_id != control.account_id
    assert fresh.source["identity_fingerprint"] != control.source["identity_fingerprint"]


def test_the_same_label_and_the_same_members_do_not_make_an_identity_chain():
    # Both snapshots carry the same id, the same members and the same groups; only
    # the account differs.  A label is not a key, so the legacy scope still cannot
    # learn whose catalog it is.
    same_label = "snapshot-label-shared-by-two-accounts"
    legacy = build_scope(snapshot(account=None, snapshot_id=same_label), groups=["g"])
    fresh = ensure_current(legacy, snapshot(account=OTHER, snapshot_id=same_label))
    assert fresh.account_id == ""
    assert fresh.account_id_state == "not_recorded"
    report = build_report(batch_of(snapshot_id=same_label), decisions("1", "2"), fresh)
    assert report["proof_limits"]["account"] == "unknown"
    assert report["proof_limits"]["complete"] is False


def test_a_batch_without_a_snapshot_id_cannot_become_complete_after_a_refresh():
    legacy = scope_of(account=None)
    fresh = ensure_current(legacy, snapshot())
    accountless = build_report(batch_of(snapshot_id=""), decisions("1", "2"), fresh)
    assert accountless["proof_limits"]["account"] == "unknown"
    assert accountless["proof_limits"]["complete"] is False

    # The control matters: the same batch against a scope that really does carry
    # the account is complete, so the refusal above is about the identity and not
    # about the batch being an unusual shape.
    control = build_report(batch_of(snapshot_id=""), decisions("1", "2"), build_scope(snapshot(), groups=["g"]))
    assert control["proof_limits"]["account"] == "matched"
    assert control["proof_limits"]["complete"] is True


def test_a_batch_pinned_to_the_old_snapshot_is_refused_after_the_refresh():
    # The refresh says "my evidence is the newer snapshot"; a task that still pins
    # the old one has to be re-planned, not silently resumed on mixed evidence.
    legacy = build_scope(snapshot(account=None, snapshot_id="legacy-without-account"), groups=["g"])
    fresh = ensure_current(legacy, snapshot(account=OTHER, snapshot_id="later"))
    with pytest.raises(ReportError) as caught:
        build_report(batch_of(snapshot_id="legacy-without-account"), decisions("1", "2"), fresh)
    assert caught.value.code == "snapshot_mismatch"


# -- the other four states are untouched ---------------------------------------


def test_a_known_account_that_moved_is_still_refused():
    assert code_of(lambda: ensure_current(scope_of(account=ACCOUNT), snapshot(account=OTHER))) == "scope_account_changed"


def test_a_snapshot_that_lost_the_account_is_still_unproven():
    assert code_of(lambda: ensure_current(scope_of(account=ACCOUNT), snapshot(account=None))) == "scope_account_unproven"


def test_two_unknown_accounts_still_pass_and_stay_empty():
    before = scope_of(account=None)
    fresh = ensure_current(before, snapshot(account=None))
    assert fresh.account_id == ""
    assert fresh.account_id_state == "not_recorded"
    assert fresh.fingerprint == before.fingerprint


def test_same_account_and_same_members_still_pass():
    before = scope_of()
    fresh = ensure_current(before, snapshot())
    assert fresh.account_id == ACCOUNT
    assert fresh.account_id_state == "recorded"


# -- adjacency: the new branch must not swallow the older refusals -------------


def test_a_member_change_is_still_refused_when_the_snapshot_names_an_account():
    legacy = build_scope(snapshot(("1",), account=None), groups=["g"])
    assert code_of(lambda: ensure_current(legacy, snapshot(("1", "2"), account=OTHER))) == "scope_changed"


def test_a_member_change_is_still_refused_when_both_accounts_are_unknown():
    legacy = build_scope(snapshot(("1",), account=None), groups=["g"])
    assert code_of(lambda: ensure_current(legacy, snapshot(("1", "2"), account=None))) == "scope_changed"


def test_weaker_evidence_is_still_refused_after_a_refresh(): 
    legacy = build_scope(snapshot(("1", "2"), account=None), groups=["g"])
    partial = snapshot(("1", "2"), account=OTHER, complete=False)
    assert code_of(lambda: ensure_current(legacy, partial)) == "scope_evidence_degraded"


def test_a_group_that_vanished_is_still_reported_as_missing():
    legacy = build_scope(snapshot(account=None), groups=["g"])
    without = snapshot(account=OTHER)
    without["memberships"] = {"show": {}, "hide": {}}
    without["artists"] = {}
    assert code_of(lambda: ensure_current(legacy, without)) == "group_not_found"


# -- the facts survive a round trip, the account does not ----------------------


def test_a_serialised_scope_cannot_be_given_an_account_by_a_round_trip():
    legacy = build_scope(snapshot(account=None, snapshot_id="legacy-without-account"), groups=["g"])
    fresh = ensure_current(legacy, snapshot(account=OTHER, snapshot_id="later"))
    again = GroupScope.from_dict(fresh.as_dict())
    assert again.account_id == ""
    assert again.account_id_state == "not_recorded"
    assert again.source["identity_fingerprint"] == fresh.source["identity_fingerprint"]
    assert again.fingerprint == fresh.fingerprint
    # Refreshing the round-tripped scope against the same snapshot still refuses
    # to invent one: the rule is a property of the scope, not of the call order.
    third = ensure_current(again, snapshot(account=OTHER, snapshot_id="later"))
    assert third.account_id == ""


def test_the_command_still_exits_five_for_a_legacy_scope(tmp_path, capsys):
    legacy = build_scope(snapshot(account=None), groups=["g"])
    batch = write(tmp_path, "batch.json", batch_of(snapshot_id="").as_dict())
    rows = write(tmp_path, "decisions.json", decisions("1", "2"))
    scope_path = write(tmp_path, "scope.json", legacy.as_dict())
    code = main(["batch-report", "--batch", str(batch), "--decisions", str(rows),
                 "--scope", str(scope_path)])
    out = capsys.readouterr().out
    assert code == 5
    assert "未记录" in out
