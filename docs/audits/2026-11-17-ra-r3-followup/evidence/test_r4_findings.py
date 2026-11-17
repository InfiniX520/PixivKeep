"""Curated R4 findings for 7e657cb. Synthetic files only; no network or real data."""
import contextlib
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(os.environ.get("PIXIVKEEP_AUDIT_REPO", Path(__file__).resolve().parents[4]))
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from core import follow_facts_v2 as facts
from organizer.batches import plan_batch
from organizer.group_scope import build_scope, ensure_current
from organizer.report import build_report


def snapshot(*, account, snapshot_id):
    return {
        "id": snapshot_id,
        "account_id": account,
        "status": "complete",
        "memberships_complete": True,
        "artists": {"1": {"artist_id": "1", "visibility": "0", "groups": ["g"], "is_following": True}},
        "memberships": {"show": {"g": {"ids": ["1"], "complete": True}}, "hide": {}},
    }


def test_r4_01_copy_retry_must_not_drop_committed_wal_rows(tmp_path, monkeypatch):
    database = tmp_path / "source.sqlite3"
    with contextlib.closing(sqlite3.connect(database)) as connection:
        with connection:
            assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
            connection.execute("CREATE TABLE sample(v)")
            connection.execute("INSERT INTO sample VALUES (1)")
    original = facts._copy_hashed
    writer = None
    calls = []

    def inject_commit(source, destination, **kwargs):
        nonlocal writer
        result = original(source, destination, **kwargs)
        calls.append(source.name)
        if len(calls) == 1:
            writer = sqlite3.connect(database)
            writer.execute("INSERT INTO sample VALUES (2)")
            writer.commit()
        return result

    monkeypatch.setattr(facts, "_copy_hashed", inject_commit)
    try:
        connection = facts.read_only_connection(database, private_root=tmp_path)
        try:
            copied_rows = [row[0] for row in connection.execute("SELECT v FROM sample ORDER BY v")]
            copy_fact = dict(connection.plan.copy or {})
        finally:
            connection.close()
    finally:
        if writer is not None:
            writer.close()
    print({"copy_calls": calls, "copied_rows": copied_rows, "copy_fact": copy_fact})
    assert 2 in copied_rows
    assert any(name.endswith("-wal") for name in copy_fact["members"])


def test_r4_02_legacy_scope_cannot_acquire_account_from_another_snapshot():
    legacy = build_scope(snapshot(account=None, snapshot_id="legacy-without-account"), groups=["g"])
    upgraded = ensure_current(legacy, snapshot(account="account-B", snapshot_id="later"))
    batch = plan_batch(["1"], account_id="account-B", artists=1, requests=1, snapshot_id="")
    decisions = [{"artist_id": "1", "kind": "unchanged", "action": "keep",
                  "reason": "same", "needs_human": False,
                  "evidence": {"before": ["g"], "after": ["g"]}}]
    before = build_report(batch, decisions, legacy)
    after = build_report(batch, decisions, upgraded)
    print({"before": before["proof_limits"], "after": after["proof_limits"]})
    assert before["proof_limits"]["account"] == "unknown"
    assert upgraded.account_id == ""
    assert after["proof_limits"]["account"] == "unknown"
    assert after["proof_limits"]["complete"] is False


def test_control_cross_job_pending_delivery_is_not_a_save(tmp_path):
    import test_ra03_relations as h
    from core.library_repository import LibraryRepository
    path = tmp_path / "library.sqlite3"
    with LibraryRepository(path) as repo:
        first = h._submit(repo, request_id="first")
        ids = h._accept(repo, first, artwork="702")
        assert repo.claim_delivery(ids[0], first["batch_id"])
        repo.release_delivery(ids[0], first["batch_id"], state="suspended")
        repo.request_stop(first["job_id"], "pause")
        assert repo.settle_stop(first["batch_id"])
        second = h._submit(repo, request_id="second", output_root="other")
        h._accept(repo, second, artwork="702", root="other")
        with sqlite3.connect(path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM batch_item_pages WHERE batch_id=?",
                                     (second["batch_id"],)).fetchone()
            connection.execute("UPDATE batch_item_pages SET delivery_id=?, artifact_id=NULL "
                               "WHERE batch_id=? AND illust_id=? AND page_index=?",
                               (ids[0], second["batch_id"], row["illust_id"], row["page_index"]))
        result = h._facts(path, second["job_id"])
    assert result["pages"]["observed"][0]["state"] not in {"saved_new", "already_verified"}


def test_control_same_content_generations_fold_under_the_r3_contract(tmp_path):
    import test_ra03_relations as h
    from core.library_repository import LibraryRepository
    path = tmp_path / "library.sqlite3"
    with LibraryRepository(path) as repo:
        job = h._submit(repo, request_id="dedup")
        ids = h._accept(repo, job)
        h.run_page(repo, job, ids[0], h.executor(repo, tmp_path, h.Transport(h.Response())))
        before = h._facts(path, job["job_id"])
        digest = before["pages"]["observed"][0]["content_sha256"]
        resumed = h._resume(repo, job)
        h._accept(repo, job, batch_id=resumed["batch_id"])
        h._second_generation(path, job["job_id"], digest=digest, batch_id=resumed["batch_id"])
        result = h._facts(path, job["job_id"])
    assert len(result["pages"]["observed"]) == 1
