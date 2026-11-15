"""W1: the online snapshot writes only through a capability it holds.

Every case here is synthetic: a temporary library created by the shared test
helper, a temporary backup directory, and sentinel files inside the same
temporary root.  Nothing reads a real gallery, account or network.

What the module proves, in order of the write:

* a name replaced between claim() and lease() is refused before a byte is
  written, and the stranger keeps every byte (the P1 in the audit);
* a replacement attempted while the lease is held is refused by the system,
  not merely detected afterwards;
* the directory is pinned while a member is created, so a swapped directory
  gets no member and no leftover;
* only members this run created are cleaned up; a stranger beside them is
  kept and named, including when the target cannot be opened or closed;
* cancellation is a typed failure, never a silent success.
"""
import contextlib
import os
import sqlite3
import threading
from pathlib import Path

import pytest

from core import library_maintenance as m
from core.workspace_ownership import TemporaryWorkspace

import test_u1a_library_maintenance as helpers


def _inside(path, root):
    """The adversarial hooks only ever touch this test's own temporary root."""
    return Path(path).resolve().is_relative_to(Path(root).resolve())


def _sqlite(path, *statements):
    """Run statements and close the file handle again; a leaked one locks it."""
    connection = sqlite3.connect(str(path))
    try:
        for statement in statements:
            connection.execute(statement)
        connection.commit()
    finally:
        connection.close()
    return path


def _sentinel(tmp_path):
    victim = tmp_path / "sentinel.sqlite3"
    _sqlite(
        victim,
        "CREATE TABLE sentinel(value)",
        "INSERT INTO sentinel VALUES ('preserve me')",
    )
    return victim, victim.read_bytes()


def _copy_of(manifest):
    directory = Path(str(manifest["manifest_path"])).parent
    return directory, directory / str(manifest["library_name"])


def test_claim_then_replace_before_lease_is_refused_without_writing(tmp_path, monkeypatch):
    """EA-01: the claim is proven on the descriptor before SQLite touches it."""
    source = helpers.make_library(tmp_path / "source" / "library.sqlite3", jobs=1)
    victim, before = _sentinel(tmp_path)
    hits = []
    original = TemporaryWorkspace.claim

    def claim(self, name, **options):
        result = original(self, name, **options)
        if options.get("purpose") == "online_snapshot":
            assert _inside(result.path, tmp_path)
            result.unlink()
            os.link(victim, result)
            hits.append(str(result.path))
        return result

    monkeypatch.setattr(TemporaryWorkspace, "claim", claim)
    with pytest.raises(m.BackupError) as caught:
        m.backup_library(source, tmp_path / "backups", label="online", mode="online_backup")
    assert hits, "the takeover hook has to run, otherwise this proves nothing"
    assert caught.value.code == "snapshot_target_replaced"
    assert "claim_not_owned" in caught.value.detail
    assert victim.stat().st_size == len(before)
    assert victim.read_bytes() == before
    # The stranger was not adopted: the cleanup names it instead of deleting it.
    cleanup = caught.value.cleanup
    assert cleanup["state"] != "ok"
    assert {item["reason"] for item in cleanup["kept"]} == {"replaced"}


def test_replacement_is_refused_by_the_system_while_the_lease_is_held(tmp_path):
    """The second half of the closure: the held descriptor blocks the swap."""
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    workspace = TemporaryWorkspace.adopt(workspace_path, purpose="synthetic")
    claimed = workspace.claim("target.sqlite3")
    other = tmp_path / "other.bin"
    other.write_bytes(b"stranger")
    with claimed.lease() as lease:
        assert lease.verify()["state"] == "owned"
        with pytest.raises(OSError):
            os.unlink(claimed.path)
        with pytest.raises(OSError):
            os.replace(other, claimed.path)
        # SQLite still can open, journal and write the file by name.
        with sqlite3.connect(str(claimed.path)) as connection:
            connection.execute("CREATE TABLE t(v)")
            connection.execute("INSERT INTO t VALUES (1)")
    assert claimed.path.stat().st_size > 0
    assert other.read_bytes() == b"stranger"


def test_online_backup_verifies_and_records_the_directory_pin(tmp_path):
    source = helpers.make_library(tmp_path / "source" / "library.sqlite3", jobs=2)
    manifest = m.backup_library(source, tmp_path / "backups", label="online", mode="online_backup")
    assert manifest["mode"] == "online_backup"
    fact = manifest["files"][0]
    assert fact["parent_pin"] in {"held", "degraded"}
    assert fact["parent_identity"] in {"handle", "path", ""}
    directory, copy = _copy_of(manifest)
    report = m.verify_backup(directory)
    assert report["ok"] is True
    assert report.get("exit_code", 0) == 0
    assert m.logical_fingerprint(copy)["digest"] == m.logical_fingerprint(source)["digest"]


def test_snapshot_follows_committed_rows_only(tmp_path):
    """A writer holding a transaction is not part of the snapshot."""
    source = helpers.make_library(tmp_path / "source" / "library.sqlite3", jobs=1, wal=True)
    writer = sqlite3.connect(str(source), isolation_level=None, timeout=5.0)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO schema_meta VALUES ('uncommitted', '1')")
        first = m.backup_library(source, tmp_path / "backups", label="online", mode="online_backup")
        _, first_copy = _copy_of(first)
        with contextlib.closing(sqlite3.connect(str(first_copy))) as connection:
            keys = {row[0] for row in connection.execute("SELECT key FROM schema_meta")}
        assert "uncommitted" not in keys
        writer.execute("COMMIT")
    finally:
        writer.close()
    second = m.backup_library(source, tmp_path / "backups2", label="online", mode="online_backup")
    _, second_copy = _copy_of(second)
    with contextlib.closing(sqlite3.connect(str(second_copy))) as connection:
        keys = {row[0] for row in connection.execute("SELECT key FROM schema_meta")}
    assert "uncommitted" in keys


def test_parent_pin_holds_the_directory_and_still_allows_creating_inside(tmp_path):
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    workspace = TemporaryWorkspace.adopt(workspace_path, purpose="synthetic")
    with workspace.parent_pin() as pin:
        assert pin.state == "held"
        assert pin.identity in {"handle", "path"}
        assert pin.as_dict()["state"] == "held"
        (workspace_path / "child.bin").write_bytes(b"x")
    assert (workspace_path / "child.bin").read_bytes() == b"x"


def test_claim_after_the_directory_is_replaced_is_refused(tmp_path):
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    workspace = TemporaryWorkspace.adopt(workspace_path, purpose="synthetic")
    moved = tmp_path / "original-workspace"
    workspace_path.rename(moved)
    workspace_path.mkdir()
    with pytest.raises(Exception) as caught:
        workspace.claim("unexpected.bin")
    assert "parent_replaced" in str(caught.value)
    assert list(workspace_path.iterdir()) == []
    assert list(moved.iterdir()) == []
    # The replacement directory is not this run's, so the cleanup keeps it.
    cleanup = workspace.discard()
    assert cleanup["state"] == "kept"
    assert cleanup["kept"] == [{"name": ".", "reason": "directory_replaced"}]
    assert workspace_path.exists()


def test_replaced_directory_between_claim_and_lease_gets_no_member(tmp_path, monkeypatch):
    source = helpers.make_library(tmp_path / "source" / "library.sqlite3", jobs=1)
    original = TemporaryWorkspace.claim
    moved = []

    def claim(self, name, **options):
        result = original(self, name, **options)
        if options.get("purpose") == "online_snapshot":
            parent = self.path
            assert _inside(parent, tmp_path)
            os.rename(parent, parent.with_name(parent.name + "-moved"))
            parent.mkdir()
            moved.append(parent)
        return result

    monkeypatch.setattr(TemporaryWorkspace, "claim", claim)
    with pytest.raises(m.BackupError) as caught:
        m.backup_library(source, tmp_path / "backups", label="online", mode="online_backup")
    assert moved
    assert caught.value.code in {
        "snapshot_target_replaced",
        "backup_workspace_replaced",
        "backup_failed",
    }
    assert list(moved[0].iterdir()) == [], "nothing may be created in the replacement directory"


def test_foreign_sidecar_is_kept_and_named(tmp_path, monkeypatch):
    source = helpers.make_library(tmp_path / "source" / "library.sqlite3", jobs=1)
    original = TemporaryWorkspace.record
    created = []

    def record(self, name, **options):
        kept = original(self, name, **options)
        if str(name).endswith(".sqlite3"):
            sidecar = Path(str(self.path / name) + "-wal")
            sidecar.write_bytes(b"foreign frame")
            created.append(sidecar)
        return kept

    monkeypatch.setattr(TemporaryWorkspace, "record", record)
    with pytest.raises(m.BackupError) as caught:
        m.backup_library(source, tmp_path / "backups", label="online", mode="online_backup")
    assert created
    assert caught.value.code == "snapshot_not_self_contained"
    cleanup = caught.value.cleanup
    assert cleanup["state"] != "ok"
    assert any(str(item["name"]).endswith("-wal") for item in cleanup["kept"])
    assert all(item["reason"] == "unowned" for item in cleanup["kept"] if str(item["name"]).endswith("-wal"))
    assert created[0].read_bytes() == b"foreign frame"


def test_backup_failure_inside_the_lease_cleans_only_its_own_members(tmp_path, monkeypatch):
    """The SQLite copy itself fails: the stranger stays, our member does not."""
    source = helpers.make_library(tmp_path / "source" / "library.sqlite3", jobs=1)
    original = TemporaryWorkspace.claim
    stranger = []

    def claim(self, name, **options):
        result = original(self, name, **options)
        if options.get("purpose") == "online_snapshot":
            outsider = self.path / "stranger.bin"
            outsider.write_bytes(b"not ours")
            stranger.append(outsider)
        return result

    class Origin:
        """A real source connection whose copy step fails, inside the lease."""

        def __init__(self, connection):
            self._connection = connection

        def __getattr__(self, name):
            return getattr(self._connection, name)

        def close(self):
            self._connection.close()

        def backup(self, destination, pages=-1, progress=None):
            raise sqlite3.OperationalError("synthetic backup failure")

    real = m._connect_readonly

    def connect(source_path, snapshot=True):
        return Origin(real(source_path, snapshot=snapshot))

    monkeypatch.setattr(TemporaryWorkspace, "claim", claim)
    monkeypatch.setattr(m, "_connect_readonly", connect)
    with pytest.raises(m.BackupError) as caught:
        m.backup_library(source, tmp_path / "backups", label="online", mode="online_backup")
    assert caught.value.code == "backup_failed"
    assert "synthetic backup failure" in caught.value.detail
    cleanup = caught.value.cleanup
    assert cleanup["state"] != "ok"
    assert {"name": "stranger.bin", "reason": "unowned"} in cleanup["kept"]
    assert stranger[0].read_bytes() == b"not ours"
    assert not (stranger[0].parent / "library.sqlite3").exists(), "our own member must be gone"


class _TargetFails:
    """The sqlite3 module, except that the backup target cannot be opened.

    The source keeps the real connection: the failure has to happen on the
    target side, after the claim and the lease, which is where the audit's
    "connect fails" case lives.
    """

    def __init__(self, source):
        self.source = Path(source).resolve()

    def __getattr__(self, name):
        return getattr(sqlite3, name)

    def _is_source(self, path):
        text = str(path)
        if text.startswith("file:"):  # the read-only URI _connect_readonly builds
            return True
        return Path(text).resolve() == self.source

    def connect(self, path, *args, **options):
        if self._is_source(path):
            return sqlite3.connect(path, *args, **options)
        raise sqlite3.OperationalError("synthetic target connect failure")


class _CloseFails(sqlite3.Connection):
    """A real connection whose close() reports a failure after releasing."""

    def close(self):
        super().close()
        raise OSError("synthetic close failure")


class _TargetCloseFails(_TargetFails):
    def connect(self, path, *args, **options):
        if self._is_source(path):
            return sqlite3.connect(path, *args, **options)
        options.pop("factory", None)
        return sqlite3.connect(path, *args, factory=_CloseFails, **options)


def _claiming_with_a_stranger(tmp_path):
    """A claim hook that plants one foreign file beside the target."""
    original = TemporaryWorkspace.claim
    stranger = []

    def claim(self, name, **options):
        result = original(self, name, **options)
        if options.get("purpose") == "online_snapshot":
            assert _inside(result.path, tmp_path)
            outsider = self.path / "stranger.bin"
            outsider.write_bytes(b"not ours")
            stranger.append(outsider)
        return result

    return claim, stranger


def test_target_connect_failure_is_typed_and_publishes_nothing(tmp_path, monkeypatch):
    source = helpers.make_library(tmp_path / "source" / "library.sqlite3", jobs=1)
    claim, stranger = _claiming_with_a_stranger(tmp_path)
    monkeypatch.setattr(TemporaryWorkspace, "claim", claim)
    monkeypatch.setattr(m, "sqlite3", _TargetFails(source))
    with pytest.raises(m.BackupError) as caught:
        m.backup_library(source, tmp_path / "backups", label="online", mode="online_backup")
    assert isinstance(caught.value, m.LibraryMaintenanceError)
    assert caught.value.code == "backup_failed"
    assert "synthetic target connect failure" in caught.value.detail
    cleanup = caught.value.cleanup
    assert cleanup["state"] != "ok"
    assert {"name": "stranger.bin", "reason": "unowned"} in cleanup["kept"]
    assert stranger[0].read_bytes() == b"not ours"
    assert not (stranger[0].parent / "library.sqlite3").exists(), "our own member must be gone"
    assert list((tmp_path / "backups").rglob("manifest.json")) == []


def test_target_close_failure_propagates_and_publishes_nothing(tmp_path, monkeypatch):
    source = helpers.make_library(tmp_path / "source" / "library.sqlite3", jobs=1)
    claim, stranger = _claiming_with_a_stranger(tmp_path)
    monkeypatch.setattr(TemporaryWorkspace, "claim", claim)
    monkeypatch.setattr(m, "sqlite3", _TargetCloseFails(source))
    with pytest.raises(m.BackupError) as caught:
        m.backup_library(source, tmp_path / "backups", label="online", mode="online_backup")
    assert caught.value.code == "backup_failed"
    assert "synthetic close failure" in caught.value.detail
    cleanup = caught.value.cleanup
    assert cleanup["state"] != "ok"
    assert {"name": "stranger.bin", "reason": "unowned"} in cleanup["kept"]
    assert stranger[0].read_bytes() == b"not ours"
    assert not (stranger[0].parent / "library.sqlite3").exists(), "our own member must be gone"
    assert list((tmp_path / "backups").rglob("manifest.json")) == []


class _FlipStop:
    """False for the first "after" looks, true afterwards.

    The two stage checks run before the copy starts (copy, online_backup); the
    third look is the SQLite progress callback inside the copy.  Setting the
    flag there proves the abort happens mid-snapshot, not merely at a boundary.
    """

    def __init__(self, after=2):
        self.calls = 0
        self.after = after

    def is_set(self):
        self.calls += 1
        return self.calls > self.after


def test_stop_during_the_snapshot_aborts_through_the_progress_callback(tmp_path, monkeypatch):
    source = helpers.make_library(tmp_path / "source" / "library.sqlite3", jobs=1)
    seen = {}

    class Origin:
        """A real connection that reports how the copy was asked to run."""

        def __init__(self, connection):
            self._connection = connection

        def __getattr__(self, name):
            return getattr(self._connection, name)

        def close(self):
            self._connection.close()

        def backup(self, destination, pages=-1, progress=None):
            seen["pages"] = pages
            seen["progress"] = progress is not None
            assert progress is not None, "the copy must be interruptible"
            progress("main", 1, 1)

    real = m._connect_readonly

    def connect(source_path, snapshot=True):
        return Origin(real(source_path, snapshot=snapshot))

    monkeypatch.setattr(m, "_connect_readonly", connect)
    with pytest.raises(m.BackupError) as caught:
        m.backup_library(
            source, tmp_path / "backups", label="online", mode="online_backup", stop=_FlipStop()
        )
    assert seen["pages"] == 256
    assert caught.value.code == "backup_cancelled"
    assert "during the online snapshot" in caught.value.detail
    cleanup = caught.value.cleanup
    assert cleanup["state"] == "ok"
    assert cleanup["removed"] == ["library.sqlite3"]


def test_stop_before_the_copy_is_a_typed_failure_with_a_clean_directory(tmp_path):
    source = helpers.make_library(tmp_path / "source" / "library.sqlite3", jobs=1)
    stop = threading.Event()
    stop.set()
    with pytest.raises(m.BackupError) as caught:
        m.backup_library(source, tmp_path / "backups", label="online", mode="online_backup", stop=stop)
    assert caught.value.code == "backup_cancelled"
    assert caught.value.cleanup["state"] == "ok"
    assert list((tmp_path / "backups").iterdir()) == []


def test_real_copy_aborts_when_the_progress_callback_refuses_to_continue(tmp_path):
    """The wiring is real: SQLite itself stops, and nothing is published."""
    source = helpers.make_library(tmp_path / "source" / "library.sqlite3", jobs=1)
    filler = sqlite3.connect(str(source))
    try:
        filler.execute("CREATE TABLE filler(v)")
        filler.executemany(
            "INSERT INTO filler VALUES (?)", [(b"x" * 2000,) for _ in range(1200)]
        )
        filler.commit()
        pages = filler.execute("PRAGMA page_count").fetchone()[0]
    finally:
        filler.close()
    # 256 pages per step is what the snapshot asks SQLite for, so this fixture
    # has to be bigger than one step for the callback to run at all.
    assert pages >= 512, "the fixture must be larger than one copy step"

    with pytest.raises(m.BackupError) as caught:
        m.backup_library(
            source,
            tmp_path / "backups",
            label="online",
            mode="online_backup",
            stop=_FlipStop(after=2),
        )
    assert caught.value.code == "backup_cancelled"
    assert "during the online snapshot" in caught.value.detail
    assert caught.value.cleanup["state"] == "ok"
    assert list((tmp_path / "backups").iterdir()) == []


def test_tampering_after_the_lease_closed_is_reported_by_verify(tmp_path):
    """The guarantee ends at the last stat; verify() is what names the end."""
    source = helpers.make_library(tmp_path / "source" / "library.sqlite3", jobs=1)
    manifest = m.backup_library(source, tmp_path / "backups", label="online", mode="online_backup")
    directory, copy = _copy_of(manifest)
    assert m.verify_backup(directory)["ok"] is True
    stranger = tmp_path / "stranger.sqlite3"
    _sqlite(stranger, "CREATE TABLE sentinel(value)")
    os.replace(stranger, copy)
    report = m.verify_backup(directory)
    assert report["ok"] is False
    assert report["sealed"]["state"] != "ok"

