"""S1: ownership has to cover create -> write -> SQLite -> cleanup.

The frozen audit baseline proves that a *replaced* destination cannot
receive another file's bytes.  These tests prove the same property through
the paths the baseline does not reach: the claim-carrying writer, an
unprovable identity, a parent directory that moved, a stranger sidecar next
to a backup copy, and the split between a primary failure and a cleanup
failure.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from core import library_maintenance as m
from core.workspace_ownership import (
    OWNED,
    TemporaryWorkspace,
    WorkspaceError,
    verify_owned,
)

from test_u1a_library_maintenance import insert_job, make_library


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sealed(tmp_path: Path) -> Path:
    database = make_library(tmp_path / "source" / "library.sqlite3", jobs=1)
    manifest = m.backup_library(database, tmp_path / "backups", label="baseline")
    return Path(manifest["manifest_path"]).parent


def test_a_claim_carries_the_identity_the_writer_checks(tmp_path):
    workspace = TemporaryWorkspace.adopt(tmp_path, purpose="test")
    claimed = workspace.claim("member.bin")
    assert claimed.claim.provable is True
    assert verify_owned(claimed.claim)["state"] == OWNED
    with claimed.writer() as stream:
        stream.write(b"first")
    assert Path(claimed).read_bytes() == b"first"
    workspace.discard()


def test_a_replaced_name_is_refused_before_a_byte_moves(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    workspace = TemporaryWorkspace.adopt(root, purpose="test")
    claimed = workspace.claim("member.bin")
    sentinel = tmp_path / "sentinel.bin"
    sentinel.write_bytes(b"not this run's bytes")
    before = sentinel.read_bytes()
    # The name is taken over between creation and the write.
    Path(claimed).unlink()
    os.link(sentinel, Path(claimed))
    with pytest.raises(WorkspaceError) as caught:
        claimed.writer()
    assert "claim_not_owned" in str(caught.value)
    assert sentinel.read_bytes() == before
    # And the cleanup must not delete the stranger that took the name.
    cleanup = workspace.discard()
    assert {"name": "member.bin", "reason": "replaced"} in cleanup["kept"]
    assert sentinel.read_bytes() == before


def test_a_claim_without_an_identity_is_never_written(tmp_path, monkeypatch):
    root = tmp_path / "run"
    root.mkdir()
    workspace = TemporaryWorkspace.adopt(root, purpose="test")
    real_fstat = os.fstat

    class _NoIdentity:
        def __init__(self, info):
            self._info = info

        def __getattr__(self, name):
            if name in ("st_dev", "st_ino"):
                return 0
            return getattr(self._info, name)

    monkeypatch.setattr(os, "fstat", lambda fd: _NoIdentity(real_fstat(fd)))
    claimed = workspace.claim("anonymous.bin")
    monkeypatch.setattr(os, "fstat", real_fstat)
    assert claimed.claim.provable is False
    with pytest.raises(WorkspaceError) as caught:
        claimed.writer()
    assert "claim_unprovable" in str(caught.value)
    assert Path(claimed).stat().st_size == 0


def test_a_record_that_meets_another_object_keeps_the_first_claim(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    workspace = TemporaryWorkspace.adopt(root, purpose="test")
    claimed = workspace.claim("member.bin")
    keep = tmp_path / "other.bin"
    keep.write_bytes(b"another object")
    Path(claimed).unlink()
    os.link(keep, Path(claimed))
    # Refreshing must not launder the replacement into an owned member.
    assert workspace.record("member.bin") is False
    assert workspace.claim_of("member.bin").identity == claimed.claim.identity
    cleanup = workspace.discard()
    assert cleanup.get("conflicts")
    assert keep.read_bytes() == b"another object"


def test_a_moved_parent_makes_the_claim_name_missing(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    workspace = TemporaryWorkspace.adopt(root, purpose="test")
    claimed = workspace.claim("member.bin")
    root.rename(tmp_path / "moved")
    with pytest.raises(WorkspaceError) as caught:
        claimed.writer()
    assert "claim_name_missing" in str(caught.value)


def test_a_stranger_inside_the_backup_directory_is_named_and_untouched(tmp_path):
    root = _sealed(tmp_path)
    library = root / "library.sqlite3"
    stranger = Path(str(library) + "-shm")
    stranger.write_bytes(b"planted, not created by SQLite in this run")
    before = _sha(stranger)
    report = m.verify_backup(root)
    # A verifier never owns what happens to sit beside the copy: the file is
    # reported as unexpected and left exactly as it was.
    assert stranger.exists()
    assert _sha(stranger) == before
    assert "library.sqlite3-shm" in [
        str(name) for name in report["evidence"]["unexpected_members"]
    ]
    assert "unexpected_members" in report["warnings"]


def test_a_sidecar_identity_failure_is_reported_not_raised(tmp_path, monkeypatch):
    root = _sealed(tmp_path)
    real_lstat = os.lstat
    hits = []

    def unavailable(path, *args, **kwargs):
        if ".verify-" in str(path) and str(path).endswith("-wal"):
            hits.append(str(path))
            raise PermissionError("synthetic")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", unavailable)
    report = m.verify_backup(root)
    assert hits
    assert "workspace_sidecar_unavailable" in report["reasons"]
    assert report["exit_code"] == 3
    assert report["status"] == "error"
    assert report["evidence"]["sidecars"]["errors"]


def test_a_copy_that_cannot_prove_its_destination_never_writes(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    workspace = TemporaryWorkspace.adopt(root, purpose="test")
    source = tmp_path / "source.bin"
    source.write_bytes(b"x" * 4096)
    claimed = workspace.claim("copy.bin")
    sentinel = tmp_path / "sentinel.bin"
    sentinel.write_bytes(b"untouched")
    Path(claimed).unlink()
    os.link(sentinel, Path(claimed))
    with pytest.raises(WorkspaceError):
        m._copy_and_hash(source, claimed)
    assert sentinel.read_bytes() == b"untouched"


def test_a_backup_that_fails_still_reports_its_cleanup(tmp_path, monkeypatch):
    library = make_library(tmp_path / "lib" / "library.sqlite3", jobs=2, wal=True)
    original = m._copy_file

    def moving(source, destination):
        if source.name == library.name:
            connection = sqlite3_connect(library)
            insert_job(connection, "late")
            connection.close()
        return original(source, destination)

    monkeypatch.setattr(m, "_copy_file", moving)
    with pytest.raises(m.BackupError) as caught:
        m.backup_library(library, tmp_path / "backups", label="moving", mode="file_group")
    detail = caught.value.detail
    assert "library_changed_during_copy" in detail
    assert "cleanup=" in detail
    assert not (tmp_path / "backups" / "moving" / m.MANIFEST_NAME).exists()


def sqlite3_connect(path):
    import sqlite3

    return sqlite3.connect(path, isolation_level=None)

