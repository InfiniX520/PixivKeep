"""A backup must name every difference that appears beside the user's library.

Opening a WAL database read-only can make SQLite create its own bookkeeping
beside it when none exists yet.  No library byte moves, but the listing can
change, and a maintenance command must not leave that for an operator to
discover (RA-01 adjacent; S0_CONTRACTS_R3 section 1).

What is asserted is "absent before, present after" - the honest name for the
field's meaning (audit R4-04).  The causal attribution is not exclusive: another
process could create the same names in the same window, so the report says the
names appeared during the run, not that this read created them.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from core import library_maintenance as m
from core.library_repository import LibraryRepository


def _digest(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_a_backup_names_the_sidecars_that_appeared_during_the_run(tmp_path):
    library = tmp_path / "library" / "library.sqlite3"
    library.parent.mkdir()
    with LibraryRepository(library):
        pass
    # Clean close: this is the shape a user has when the application is closed.
    assert not Path(str(library) + "-wal").exists()
    assert not Path(str(library) + "-shm").exists()
    before = _digest(library)

    manifest = m.backup_library(library, tmp_path / "backups", label="named")

    assert manifest["status"] == "ok"
    assert manifest["source_sidecars_created"] == ["-wal", "-shm"]
    # The fact is a report about the directory, not an excuse for changing the
    # library: the bytes are the ones we started with.
    assert _digest(library) == before
    # And the same library opens and inspects cleanly afterwards.
    assert m.inspect_library(library)["status"] == "compatible"


def test_a_second_backup_names_nothing_because_nothing_new_appeared(tmp_path):
    library = tmp_path / "library" / "library.sqlite3"
    library.parent.mkdir()
    with LibraryRepository(library):
        pass
    first = m.backup_library(library, tmp_path / "backups", label="first")
    assert first["source_sidecars_created"] == ["-wal", "-shm"]
    second = m.backup_library(library, tmp_path / "backups", label="second")
    assert second["source_sidecars_created"] == []


def test_a_live_wal_library_reports_nothing_new_during_the_run(tmp_path):
    # While a writer is open the sidecars already exist, so the read creates
    # nothing: the fact is measured, not assumed.
    library = tmp_path / "library" / "library.sqlite3"
    library.parent.mkdir()
    with LibraryRepository(library):
        assert Path(str(library) + "-wal").exists()
        assert Path(str(library) + "-shm").exists()
        manifest = m.backup_library(library, tmp_path / "backups", label="live")
    assert manifest["status"] == "ok"
    assert manifest["source_sidecars_created"] == []
