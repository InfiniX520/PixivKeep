"""RA-01: the backup target is a file *family*, authorised before SQLite runs.

Every case here is synthetic: a temporary library built by the shared test
helper and a temporary destination inside this test's own root.  Nothing reads a
real gallery, account, backup or network.

What is proven, in the order the guarantee is built:

* the artifact is written inside a private staging directory, so SQLite never
  opens a database whose family names are the ones a reader will see;
* a foreign -journal/-wal/-shm beside the staging database is refused *before*
  the connection is opened, and keeps every byte, its time and its other links;
* publication is one directory rename: a name this run did not create blocks it,
  an existing final name is never replaced, and a failure publishes nothing;
* the ACL restriction is recorded as a fact, the published directory stays
  usable for this account, and a finished backup still verifies.
"""
import hashlib
import os
import sqlite3
import threading
from pathlib import Path

import pytest

from core import library_maintenance as m
from core.workspace_ownership import TemporaryWorkspace

import test_u1a_library_maintenance as helpers


def _library(tmp_path, **options):
    return helpers.make_library(tmp_path / "source" / "library.sqlite3", **options)


def _sentinel(tmp_path, name="sentinel.bin"):
    """A file this run must never open, journal over or remove."""
    path = tmp_path / name
    payload = b"foreign sentinel, do not touch\n" * 64
    path.write_bytes(payload)
    return path, payload, path.stat().st_mtime_ns


class _RecordingSqlite:
    """The sqlite3 module, remembering which paths were opened."""

    def __init__(self):
        self.paths = []

    def __getattr__(self, name):
        return getattr(sqlite3, name)

    def connect(self, path, *args, **options):
        self.paths.append(str(path))
        return sqlite3.connect(path, *args, **options)


def _staging_leftovers(destination_root):
    return [
        path
        for path in destination_root.iterdir()
        if path.name.startswith(m._STAGING_PREFIX)
    ]


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
def test_a_foreign_family_member_is_refused_before_sqlite_opens_the_target(
    tmp_path, monkeypatch, suffix
):
    """The audit's P1: SQLite removed a name it did not create.

    Measured on this host, SQLite unlinks a pre-existing -journal and -wal by
    itself while opening and closing the database, so the only place to refuse
    is before the connection.  The sentinel is planted as a hard link, exactly
    like the audit's probe: removing the name would not save the bytes, so the
    bytes and the time are compared as well.
    """
    source = _library(tmp_path)
    victim, payload, before_mtime = _sentinel(tmp_path)
    planted = []
    original_claim = TemporaryWorkspace.claim

    def claim(self, name, **options):
        result = original_claim(self, name, **options)
        if options.get("purpose") == "online_snapshot":
            sidecar = Path(str(result.path) + suffix)
            os.link(victim, sidecar)
            planted.append(sidecar)
        return result

    recorder = _RecordingSqlite()
    monkeypatch.setattr(TemporaryWorkspace, "claim", claim)
    monkeypatch.setattr(m, "sqlite3", recorder)
    with pytest.raises(m.BackupError) as caught:
        m.backup_library(
            source, tmp_path / "backups", label="family", mode="online_backup"
        )

    assert caught.value.code == "backup_target_family_occupied", caught.value.detail
    assert planted, "the hook has to run, otherwise this proves nothing"
    sidecar = planted[0]
    print(
        "RA01_FAMILY",
        suffix,
        "bytes",
        len(victim.read_bytes()),
        "mtime_equal",
        victim.stat().st_mtime_ns == before_mtime,
        "connects",
        len(recorder.paths),
    )
    # The sentinel and its second link are untouched, byte for byte and in time.
    assert victim.read_bytes() == payload
    assert victim.stat().st_mtime_ns == before_mtime
    assert sidecar.exists() and sidecar.read_bytes() == payload
    # SQLite was used for the source (so the module really ran), but it never
    # opened anything inside the staging directory: the target family stayed
    # closed, which is the whole point of checking before the connection.
    assert recorder.paths, "the source inspection must still have read the library"
    assert [path for path in recorder.paths if ".staging-" in path] == []
    # Nothing is published, and the member that could not be owned is named.
    assert not (tmp_path / "backups" / "family").exists()
    assert sidecar.name in caught.value.detail


def test_the_refusal_names_every_foreign_member_and_keeps_it(tmp_path, monkeypatch):
    source = _library(tmp_path)
    first, payload_one, _ = _sentinel(tmp_path, "one.bin")
    second, payload_two, _ = _sentinel(tmp_path, "two.bin")
    original_claim = TemporaryWorkspace.claim

    def claim(self, name, **options):
        result = original_claim(self, name, **options)
        if options.get("purpose") == "online_snapshot":
            for suffix, victim in (("-journal", first), ("-shm", second)):
                os.link(victim, Path(str(result.path) + suffix))
        return result

    monkeypatch.setattr(TemporaryWorkspace, "claim", claim)
    with pytest.raises(m.BackupError) as caught:
        m.backup_library(source, tmp_path / "backups", label="two")
    detail = caught.value.detail
    assert caught.value.code == "backup_target_family_occupied"
    assert "library.sqlite3-journal" in detail and "library.sqlite3-shm" in detail
    assert first.read_bytes() == payload_one and second.read_bytes() == payload_two


def test_a_name_this_run_did_not_create_blocks_publication(tmp_path, monkeypatch):
    """A stranger inside the staging directory survives, and nothing is published."""
    source = _library(tmp_path)
    original_claim = TemporaryWorkspace.claim
    planted = []

    def claim(self, name, **options):
        result = original_claim(self, name, **options)
        if options.get("purpose") == "online_snapshot":
            stranger = self.path / "stranger.bin"
            stranger.write_bytes(b"planted by nobody")
            planted.append(stranger)
        return result

    monkeypatch.setattr(TemporaryWorkspace, "claim", claim)
    with pytest.raises(m.BackupError) as caught:
        m.backup_library(source, tmp_path / "backups", label="foreign")

    assert caught.value.code == "backup_target_family_foreign", caught.value.detail
    assert "stranger.bin" in caught.value.detail
    assert not (tmp_path / "backups" / "foreign").exists()
    assert planted[0].read_bytes() == b"planted by nobody"
    staging = _staging_leftovers(tmp_path / "backups")
    assert len(staging) == 1, caught.value.detail
    assert str(staging[0]) in caught.value.detail
    # Only the stranger is left: everything this run created was removed again.
    assert [item.name for item in staging[0].iterdir()] == ["stranger.bin"]


def test_a_leftover_sidecar_after_the_close_blocks_publication(tmp_path, monkeypatch):
    """Even an empty sidecar is a name this run did not create."""
    source = _library(tmp_path)
    original = m._own_manifest

    def plant(workspace, directory):
        original(workspace, directory)
        Path(str(directory / "library.sqlite3") + "-wal").write_bytes(b"")

    monkeypatch.setattr(m, "_own_manifest", plant)
    with pytest.raises(m.BackupError) as caught:
        m.backup_library(source, tmp_path / "backups", label="leftover")

    assert caught.value.code == "backup_target_family_foreign", caught.value.detail
    assert "library.sqlite3-wal" in caught.value.detail
    assert not (tmp_path / "backups" / "leftover").exists()


def _tree_digest(root):
    """Names and bytes of one directory, as a fact this run can compare."""
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.iterdir())
    }


def test_a_target_that_appears_before_publication_is_never_replaced(
    tmp_path, monkeypatch
):
    """The final name belongs to whoever got there first; we only rename onto nothing.

    The occupied directory is populated before the rename is attempted, and its
    names and digests are compared afterwards: a publish that cannot happen must
    leave the other run's artifact exactly as it was, and must account for the
    staging directory it does not publish.
    """
    source = _library(tmp_path)
    original = TemporaryWorkspace.record
    hits = []

    def record(self, name, **options):
        kept = original(self, name, **options)
        if not hits:
            hits.append(str(name))
            occupied = tmp_path / "backups" / "race"
            occupied.mkdir(parents=True, exist_ok=True)
            (occupied / "their-file.bin").write_bytes(b"the other run's bytes")
        return kept

    monkeypatch.setattr(TemporaryWorkspace, "record", record)
    occupied = tmp_path / "backups" / "race"
    with pytest.raises(m.BackupError) as caught:
        m.backup_library(source, tmp_path / "backups", label="race")

    assert caught.value.code == "backup_target_exists", caught.value.detail
    assert hits, "the hook has to run"
    # 1. The other run's directory is untouched: same names, same bytes.
    assert _tree_digest(occupied) == {
        "their-file.bin": hashlib.sha256(b"the other run's bytes").hexdigest()
    }
    # 2. Our staging directory accounts for itself: either it is gone, or it is
    #    still there and the failure names it.  Both are acceptable, silence is
    #    not -- and here nothing foreign was planted, so it must be gone.
    staging = _staging_leftovers(tmp_path / "backups")
    if staging:
        for path in staging:
            assert str(path) in caught.value.detail, caught.value.detail
    else:
        assert "kept=" not in caught.value.detail, caught.value.detail
        assert caught.value.cleanup["state"] == "ok"
    assert staging == [], caught.value.detail
    # 3. The failure names what happened, including where the staging run lived.
    assert "residual=" not in caught.value.detail or staging != []


def test_a_clean_failure_leaves_no_staging_shell(tmp_path):
    """A failure that owns everything it touched removes everything it touched."""
    source = _library(tmp_path)
    stop = threading.Event()
    stop.set()
    with pytest.raises(m.BackupError) as caught:
        m.backup_library(source, tmp_path / "backups", label="cancelled", stop=stop)
    assert caught.value.code == "backup_cancelled"
    assert caught.value.cleanup["state"] == "ok"
    assert _staging_leftovers(tmp_path / "backups") == []
    assert list((tmp_path / "backups").iterdir()) == []
    assert not (tmp_path / "backups" / "cancelled").exists()


def test_a_mid_copy_failure_leaves_no_staging_shell(tmp_path, monkeypatch):
    source = _library(tmp_path, wal=True)

    def explode(source_path, destination):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(m, "_copy_file", explode)
    with pytest.raises(m.BackupError) as caught:
        m.backup_library(source, tmp_path / "backups", label="enospc", mode="file_group")
    assert caught.value.code == "backup_failed"
    cleanup = caught.value.cleanup
    # The half-written member is ours, so it goes; nothing foreign was planted,
    # so the staging directory goes with it and no empty shell stays behind.
    assert cleanup["state"] == "ok"
    assert "library.sqlite3" in cleanup["removed"]
    assert _staging_leftovers(tmp_path / "backups") == []
    assert list((tmp_path / "backups").iterdir()) == []


def test_a_file_group_backup_also_refuses_a_foreign_family_member(
    tmp_path, monkeypatch
):
    """The group mode checks the same three names before it copies a byte."""
    source = _library(tmp_path, wal=True)
    victim, payload, before_mtime = _sentinel(tmp_path)
    planted = []
    original = TemporaryWorkspace.restrict

    def restrict(self):
        fact = original(self)
        sidecar = self.path / (source.name + "-journal")
        os.link(victim, sidecar)
        planted.append(sidecar)
        return fact

    monkeypatch.setattr(TemporaryWorkspace, "restrict", restrict)
    with pytest.raises(m.BackupError) as caught:
        m.backup_library(source, tmp_path / "backups", label="group", mode="file_group")

    assert caught.value.code == "backup_target_family_occupied", caught.value.detail
    assert planted and planted[0].read_bytes() == payload
    assert victim.stat().st_mtime_ns == before_mtime
    assert not (tmp_path / "backups" / "group").exists()


def test_the_manifest_records_the_family_and_the_copy_stays_usable(tmp_path):
    """The published artifact is one rename away from a private staging run."""
    source = _library(tmp_path, jobs=2)
    manifest = m.backup_library(
        source, tmp_path / "backups", label="acl", mode="online_backup"
    )
    family = manifest["family"]
    print("RA01_FAMILY_FACT", family)
    assert family["precheck"] == "pass"
    assert family["present"] == []
    assert family["published"] is True
    assert family["publish"] == "atomic_directory_rename"
    assert family["acl"]["state"] in {"restricted", "degraded"}
    if family["acl"]["state"] == "restricted":
        assert family["acl"]["account"]
    staging = Path(str(family["root"]))
    assert staging.name.startswith(m._STAGING_PREFIX)
    assert not staging.exists(), "the staging directory is gone once it is published"

    directory = Path(manifest["manifest_path"]).parent
    assert directory.name == "acl"
    assert _staging_leftovers(directory.parent) == []
    # The ACL follows the rename, and it still leaves this account full control.
    probe = directory / "probe.tmp"
    probe.write_bytes(b"probe")
    assert probe.read_bytes() == b"probe"
    probe.unlink()
    report = m.verify_backup(directory)
    assert report["ok"] is True
    assert report.get("exit_code", 0) == 0
    assert m.logical_fingerprint(directory / source.name)["digest"] == (
        m.logical_fingerprint(source)["digest"]
    )


def test_a_staging_directory_is_never_taken_for_a_backup(tmp_path, monkeypatch):
    """No manifest means no backup: a half-built directory is refused, not listed.

    Nothing in the project enumerates a backup root (checked by reading the
    callers: verify/restore take an explicit path), so the only thing that could
    mistake a staging directory for a backup is someone pointing a command at
    it.  That path has to be a typed refusal, never a partial success.
    """
    source = _library(tmp_path)
    original = m._own_manifest

    def plant(workspace, directory):
        original(workspace, directory)
        Path(str(directory / "library.sqlite3") + "-wal").write_bytes(b"")

    monkeypatch.setattr(m, "_own_manifest", plant)
    with pytest.raises(m.BackupError) as caught:
        m.backup_library(source, tmp_path / "backups", label="half")
    assert caught.value.code == "backup_target_family_foreign"

    staging = _staging_leftovers(tmp_path / "backups")
    assert len(staging) == 1
    with pytest.raises(m.BackupError) as refused:
        m.verify_backup(staging[0])
    assert refused.value.code == "manifest_missing"


def test_a_published_backup_of_a_wal_library_is_self_contained(tmp_path):
    source = _library(tmp_path, jobs=1, wal=True)
    manifest = m.backup_library(source, tmp_path / "backups", label="wal")
    directory = Path(manifest["manifest_path"]).parent
    assert [item["name"] for item in manifest["files"]] == ["library.sqlite3"]
    assert sorted(item.name for item in directory.iterdir()) == [
        "library.sqlite3",
        "manifest.json",
    ]
    assert m.verify_backup(directory)["ok"] is True
