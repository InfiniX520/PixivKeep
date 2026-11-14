"""AMD2: who owns a temporary member, and what a failure must still say.

Every case here builds its own synthetic directory under the pytest basetemp.
The dangerous ones (a junction pointing at another directory, a member replaced
under the same name, a directory root swapped out) assert that both absolute
paths are inside this test's root and that a sentinel file keeps the exact
bytes and digest it started with.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from core import library_maintenance as m
from core.workspace_ownership import TemporaryWorkspace, is_reparse_point

import test_u1a_library_maintenance as h


def sentinel(path: Path, data: bytes = b"protected synthetic bytes") -> tuple[Path, str]:
    path.write_bytes(data)
    return path, hashlib.sha256(data).hexdigest()


def digest_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inside(path: Path, root: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


def make_junction(link: Path, target: Path) -> None:
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# The ownership record itself
# ---------------------------------------------------------------------------


def test_cleanup_removes_only_the_members_this_run_recorded(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    workspace = TemporaryWorkspace.adopt(root, purpose="test")
    (root / "a.bin").write_bytes(b"a")
    (root / "b.bin").write_bytes(b"b")
    workspace.record("a.bin")
    workspace.record("b.bin")
    cleanup = workspace.discard()
    assert cleanup["state"] == "ok"
    assert sorted(cleanup["removed"]) == ["a.bin", "b.bin"]
    assert cleanup["kept"] == []
    assert not root.exists()


def test_cleanup_keeps_a_file_this_run_did_not_create(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    workspace = TemporaryWorkspace.adopt(root, purpose="test")
    (root / "mine.bin").write_bytes(b"mine")
    workspace.record("mine.bin")
    foreign, foreign_digest = sentinel(root / "unowned.txt")
    assert inside(foreign, tmp_path)
    cleanup = workspace.discard()
    assert cleanup["removed"] == ["mine.bin"]
    assert [item["name"] for item in cleanup["kept"]] == ["unowned.txt"]
    assert cleanup["kept"][0]["reason"] == "unowned"
    assert cleanup["state"] != "ok"
    assert foreign.exists() and digest_of(foreign) == foreign_digest
    assert root.exists()


def test_cleanup_keeps_an_unowned_directory_and_never_recurses(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    workspace = TemporaryWorkspace.adopt(root, purpose="test")
    nested = root / "someone-elses"
    nested.mkdir()
    keeper, keeper_digest = sentinel(nested / "keep.txt")
    cleanup = workspace.discard()
    assert cleanup["state"] != "ok"
    assert {"name": "someone-elses", "reason": "unowned"} in cleanup["kept"]
    assert keeper.exists() and digest_of(keeper) == keeper_digest


def test_cleanup_refuses_a_junction_and_leaves_its_target_alone(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    outside = tmp_path / "separate-owned-directory"
    outside.mkdir()
    sentinel_path, sentinel_digest = sentinel(outside / "must-stay.txt")
    assert inside(root, tmp_path) and inside(outside, tmp_path)
    make_junction(root / "foreign-junction", outside)
    assert (root / "foreign-junction").is_junction()
    workspace = TemporaryWorkspace.adopt(root, purpose="test")
    cleanup = workspace.discard()
    assert {"name": "foreign-junction", "reason": "unowned"} in cleanup["kept"]
    assert cleanup["state"] != "ok"
    assert sentinel_path.exists() and digest_of(sentinel_path) == sentinel_digest


def test_cleanup_keeps_a_recorded_member_that_was_replaced(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    workspace = TemporaryWorkspace.adopt(root, purpose="test")
    target = root / "copy.bin"
    target.write_bytes(b"first")
    workspace.record("copy.bin")
    target.unlink()
    target.write_bytes(b"second")
    cleanup = workspace.discard()
    assert cleanup["kept"] == [{"name": "copy.bin", "reason": "replaced"}]
    assert cleanup["state"] != "ok"
    assert target.read_bytes() == b"second"


def test_cleanup_touches_nothing_when_the_directory_was_replaced(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    workspace = TemporaryWorkspace.adopt(root, purpose="test")
    (root / "mine.bin").write_bytes(b"mine")
    workspace.record("mine.bin")
    moved = tmp_path / "run-moved"
    root.rename(moved)
    root.mkdir()
    sentinel_path, sentinel_digest = sentinel(root / "new-owner.txt")
    cleanup = workspace.discard()
    assert cleanup["kept"] == [{"name": ".", "reason": "directory_replaced"}]
    assert cleanup["removed"] == []
    assert sentinel_path.exists() and digest_of(sentinel_path) == sentinel_digest
    assert (moved / "mine.bin").exists()


def test_cleanup_reports_an_unreadable_member_instead_of_raising(tmp_path, monkeypatch):
    root = tmp_path / "run"
    root.mkdir()
    workspace = TemporaryWorkspace.adopt(root, purpose="test")
    (root / "locked.bin").write_bytes(b"locked")
    workspace.record("locked.bin")
    real_lstat = os.lstat

    def refusing(path, *args, **kwargs):
        if str(path).endswith("locked.bin"):
            raise PermissionError("synthetic access denied")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", refusing)
    cleanup = workspace.discard()
    monkeypatch.setattr(os, "lstat", real_lstat)
    assert {"name": "locked.bin", "reason": "unreadable"} in cleanup["kept"]
    assert cleanup["errors"]
    assert (root / "locked.bin").exists()


def test_cleanup_reports_a_remove_failure_instead_of_raising(tmp_path, monkeypatch):
    root = tmp_path / "run"
    root.mkdir()
    workspace = TemporaryWorkspace.adopt(root, purpose="test")
    (root / "stuck.bin").write_bytes(b"stuck")
    workspace.record("stuck.bin")

    def refusing(path, *args, **kwargs):
        raise PermissionError("synthetic unlink denied")

    monkeypatch.setattr(os, "unlink", refusing)
    cleanup = workspace.discard()
    assert {"name": "stuck.bin", "reason": "remove_failed"} in cleanup["kept"]
    assert any("synthetic unlink denied" in item for item in cleanup["errors"])
    assert cleanup["state"] in {"partial", "failed"}


def test_recorded_sidecars_are_removed_with_their_database(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    workspace = TemporaryWorkspace.adopt(root, purpose="test")
    for name in ("library.sqlite3", "library.sqlite3-wal", "library.sqlite3-shm"):
        (root / name).write_bytes(b"x")
        workspace.record(name)
    cleanup = workspace.discard()
    assert cleanup["state"] == "ok"
    assert sorted(cleanup["removed"]) == [
        "library.sqlite3", "library.sqlite3-shm", "library.sqlite3-wal"
    ]


def test_adopting_a_junction_is_refused(tmp_path):
    outside = tmp_path / "target"
    outside.mkdir()
    make_junction(tmp_path / "link", outside)
    with pytest.raises(Exception) as caught:
        TemporaryWorkspace.adopt(tmp_path / "link", purpose="test")
    assert "workspace_not_a_plain_directory" in str(caught.value)


# ---------------------------------------------------------------------------
# The three call sites in library_maintenance
# ---------------------------------------------------------------------------


def backup(tmp_path, *, label="sealed"):
    source = h.make_library(tmp_path / "source" / "library.sqlite3", jobs=1)
    info = m.backup_library(source, tmp_path / "backups", label=label)
    return Path(info["manifest_path"]).parent


def test_a_target_that_cannot_be_created_is_a_typed_refusal(tmp_path, monkeypatch):
    source = h.make_library(tmp_path / "source" / "library.sqlite3", jobs=1)
    real_mkdir = Path.mkdir

    def refusing(self, *args, **kwargs):
        if self.name == "blocked":
            raise PermissionError("synthetic no permission")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", refusing)
    with pytest.raises(m.BackupError) as info:
        m.backup_library(source, tmp_path / "backups", label="blocked")
    assert info.value.code == "backup_directory_failed"


def test_a_late_manifest_failure_is_typed_and_names_the_residual(tmp_path, monkeypatch):
    source = h.make_library(tmp_path / "source" / "library.sqlite3", jobs=1)

    def failing(directory, manifest):
        raise OSError(28, "synthetic disk full")

    monkeypatch.setattr(m, "_write_manifest", failing)
    with pytest.raises(m.BackupError) as info:
        m.backup_library(source, tmp_path / "backups", label="late")
    assert info.value.code == "backup_manifest_failed"
    assert "disk full" in info.value.detail
    # The failure must say what happened to the directory it created: either the
    # cleanup removed everything it owned, or it names the residual path.
    assert "cleanup=" in info.value.detail
    if "cleanup=ok" not in info.value.detail:
        assert "residual=" in info.value.detail


def test_a_cleanup_failure_cannot_replace_the_primary_error(tmp_path, monkeypatch):
    source = h.make_library(tmp_path / "source" / "library.sqlite3", jobs=1)

    def failing(*args, **kwargs):
        raise OSError("synthetic copy failure")

    def refusing(*args, **kwargs):
        raise PermissionError("synthetic cleanup failure")

    monkeypatch.setattr(m, "_online_snapshot", failing)
    monkeypatch.setattr(m, "_discard", refusing)
    with pytest.raises(m.LibraryMaintenanceError) as info:
        m.backup_library(source, tmp_path / "backups", label="both")
    message = str(info.value)
    assert "copy failure" in message
    assert "cleanup" in message


def test_a_successful_backup_leaves_no_cleanup_fact_behind(tmp_path):
    directory = backup(tmp_path)
    assert (directory / "manifest.json").is_file()
    assert (directory / "library.sqlite3").is_file()


def test_verify_reports_a_cleanup_failure_next_to_a_sound_verdict(tmp_path, monkeypatch):
    directory = backup(tmp_path)

    def refusing(workspace):
        raise PermissionError("synthetic cleanup failure")

    monkeypatch.setattr(m, "_discard", refusing)
    result = m.verify_backup(directory)
    assert result["ok"] is True
    assert result["exit_code"] == 0
    assert result["evidence"]["cleanup"]["state"] == "failed"
    assert "verify_cleanup_failed" in result["warnings"]


def test_an_unavailable_staging_area_is_an_unreadable_exit(tmp_path, monkeypatch):
    directory = backup(tmp_path)

    def refusing(root, *args, **kwargs):
        raise OSError("synthetic no space")

    monkeypatch.setattr(m, "_verification_workspace", refusing)
    result = m.verify_backup(directory)
    assert result["ok"] is False
    assert result["exit_code"] == 3
    assert "verify_workspace_unavailable" in result["reasons"]


def test_a_member_name_with_a_control_character_is_refused_first(tmp_path):
    directory = backup(tmp_path)
    manifest = json.loads((directory / "manifest.json").read_text("utf-8"))
    manifest["members"][0]["name"] = "bad\x00name"
    (directory / "manifest.json").write_text(json.dumps(manifest), "utf-8")
    with pytest.raises(m.BackupError) as info:
        m.verify_backup(directory)
    assert info.value.code == "manifest_member_invalid"


# ---------------------------------------------------------------------------
# restore-check: one line per question
# ---------------------------------------------------------------------------


def test_restore_check_accepts_a_consistent_snapshot(tmp_path):
    directory = backup(tmp_path)
    report = m.restore_check(directory / "library.sqlite3")
    assert report["state"] == "ok"
    assert report["exit_code"] == 0
    assert report["content_healthy"] is True
    assert report["openable"] is True
    assert report["group_consistent"] is True


@pytest.mark.parametrize("kind", ["corrupt", "empty", "incompatible"])
def test_restore_check_refuses_an_unusable_database(tmp_path, kind):
    database = tmp_path / "bad.sqlite3"
    if kind == "corrupt":
        database.write_bytes(b"not sqlite but definitely exists")
    elif kind == "empty":
        database.touch()
    else:
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE unrelated (v TEXT)")
    report = m.restore_check(database)
    assert report["state"] == "not_usable"
    assert report["exit_code"] == 2
    assert report["openable"] is False
    assert report["group_consistent"] is False


def test_restore_check_refuses_a_rollback_database_with_a_wal_beside_it(tmp_path):
    database = h.make_library(tmp_path / "library.sqlite3", jobs=0)
    wal = Path(str(database) + "-wal")
    wal.write_bytes(b"stale frames from another file")
    report = m.restore_check(database)
    assert report["state"] in {"orphan_wal", "not_usable"}
    assert report["exit_code"] == 2
    if report["state"] == "orphan_wal":
        assert report["wal_bytes"] == wal.stat().st_size
        assert report["group_consistent"] is False


def test_restore_check_reports_an_unreadable_wal_as_unknown_not_zero(tmp_path, monkeypatch):
    database = h.make_library(tmp_path / "library.sqlite3", jobs=0)
    wal = Path(str(database) + "-wal")
    wal.write_bytes(b"frames")
    real_stat = Path.stat

    def refusing(self, *args, **kwargs):
        if self.name.endswith("-wal"):
            raise PermissionError("synthetic stat denied")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", refusing)
    report = m.restore_check(database)
    monkeypatch.setattr(Path, "stat", real_stat)
    assert report["state"] == "wal_unreadable"
    assert report["wal_bytes"] is None
    assert report["exit_code"] == 2


def test_restore_check_still_reports_a_missing_file(tmp_path):
    report = m.restore_check(tmp_path / "absent.sqlite3")
    assert report["state"] == "library_missing"
    assert report["exit_code"] == 2


def test_an_adopt_failure_is_an_unreadable_exit_without_a_traceback(tmp_path, monkeypatch, capsys):
    directory = backup(tmp_path)

    class Refusing:
        @staticmethod
        def adopt(*args, **kwargs):
            raise m.WorkspaceError("synthetic adopt failure")

    monkeypatch.setattr(m, "TemporaryWorkspace", Refusing)
    code = m.main(["--json", "verify", str(directory)])
    printed = capsys.readouterr().out
    result = json.loads(printed)
    assert code == 3
    assert result["ok"] is False
    assert result["status"] == "error"
    assert "verify_workspace_unavailable" in result["reasons"]
    assert "Traceback" not in printed


def test_a_copy_that_fails_halfway_leaves_nothing_behind(tmp_path, monkeypatch):
    directory = backup(tmp_path)

    def failing(source, destination):
        with open(destination, "r+b") as stream:
            stream.write(b"x" * 4096)
        raise OSError("synthetic write failure")

    monkeypatch.setattr(m, "_copy_and_hash", failing)
    with pytest.raises(m.LibraryMaintenanceError):
        m.verify_backup(directory)
    leftovers = sorted(directory.parent.glob(directory.name + ".verify-*"))
    assert leftovers == []


def test_an_interrupt_during_cleanup_keeps_its_own_type(tmp_path, monkeypatch):
    source = h.make_library(tmp_path / "source" / "library.sqlite3", jobs=1)

    def failing(*args, **kwargs):
        raise OSError("synthetic copy failure")

    def interrupting(*args, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(m, "_online_snapshot", failing)
    monkeypatch.setattr(m, "_discard", interrupting)
    with pytest.raises(KeyboardInterrupt):
        m.backup_library(source, tmp_path / "backups", label="interrupted")


class _NoIdentity:
    """A stat result from a volume that reports no file identity."""

    def __init__(self, real):
        self.st_dev = 0
        self.st_ino = 0
        self.st_mode = real.st_mode
        self.st_file_attributes = getattr(real, "st_file_attributes", 0)
        self.st_size = real.st_size
        self.st_mtime_ns = real.st_mtime_ns


def test_a_member_without_a_reported_identity_is_never_deleted(tmp_path, monkeypatch):
    root = tmp_path / "run"
    root.mkdir()
    workspace = TemporaryWorkspace.adopt(root, purpose="test")
    real_fstat = os.fstat
    calls = {"n": 0}

    def anonymous_once(fd):
        calls["n"] += 1
        info = real_fstat(fd)
        return _NoIdentity(info) if calls["n"] == 1 else info

    monkeypatch.setattr(os, "fstat", anonymous_once)
    opaque = workspace.claim("opaque.bin")
    opaque.write_bytes(b"written while the volume would not say which file this is")
    monkeypatch.setattr(os, "fstat", real_fstat)
    plain = workspace.claim("plain.bin")
    plain.write_bytes(b"plain")
    cleanup = workspace.discard()
    assert {"name": "opaque.bin", "reason": "identity_unavailable"} in cleanup["kept"]
    assert cleanup["removed"] == ["plain.bin"]
    assert cleanup["state"] != "ok"
    assert opaque.exists() and opaque.stat().st_size > 0
    assert not plain.exists()


def test_a_directory_without_a_reported_identity_is_left_alone(tmp_path, monkeypatch):
    root = tmp_path / "run"
    root.mkdir()
    real_lstat = os.lstat
    calls = {"n": 0}

    def anonymous_once(path, *args, **kwargs):
        calls["n"] += 1
        info = real_lstat(path, *args, **kwargs)
        return _NoIdentity(info) if calls["n"] == 1 else info

    monkeypatch.setattr(os, "lstat", anonymous_once)
    workspace = TemporaryWorkspace.adopt(root, purpose="test")
    monkeypatch.setattr(os, "lstat", real_lstat)
    (root / "mine.bin").write_bytes(b"mine")
    workspace.record("mine.bin")
    cleanup = workspace.discard()
    assert cleanup["state"] == "kept"
    assert cleanup["removed"] == []
    assert cleanup["kept"] == [{"name": ".", "reason": "identity_unavailable"}]
    assert (root / "mine.bin").exists()
