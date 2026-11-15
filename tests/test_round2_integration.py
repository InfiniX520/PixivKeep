"""Round-2 integration: the maintenance CLI still produces a backup that verifies.

The online-backup write path changed in EA-01 (claim, lease, then hand the name
to SQLite).  The lane tests prove the workspace behaviour; this test walks the
public command end to end - argparse, backup, the manifest on disk, verify - and
checks that what the CLI says matches what the files say.

Synthetic library, temporary directory, no real data.
"""
import json
from pathlib import Path

import test_u1a_library_maintenance as fixture

from core import library_maintenance as m


def _backup(tmp_path, capsys):
    source = fixture.make_library(tmp_path / "source" / "library.sqlite3", jobs=1)
    destination = tmp_path / "backups"
    code = m.main(
        [
            "backup",
            str(source),
            str(destination),
            "--label",
            "round2",
            "--mode",
            "online_backup",
            "--json",
        ]
    )
    printed = capsys.readouterr().out
    assert code == 0
    manifest = json.loads(printed)
    return source, Path(manifest["manifest_path"]).parent, manifest


def test_the_backup_command_writes_a_manifest_that_verify_accepts(tmp_path, capsys):
    _, target, manifest = _backup(tmp_path, capsys)
    assert (target / "manifest.json").is_file()
    assert manifest["verified"] is True
    assert manifest["objects_match_source"] is True
    assert manifest["counts_match_source"] is True
    # The directory pin is a fact about the write, and it travels in the result:
    # "held" when the platform gave us a handle, "degraded" when it did not.
    assert manifest["files"][0]["parent_pin"] in {"held", "degraded"}
    assert manifest["files"][0]["parent_pin"] != ""
    code = m.main(["verify", str(target), "--json"])
    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert report["ok"] is True


def test_verify_reports_a_tampered_copy_as_a_failed_seal(tmp_path, capsys):
    _, target, manifest = _backup(tmp_path, capsys)
    copy = target / manifest["files"][0]["name"]
    with copy.open("ab") as stream:
        stream.write(b"tampered")
    code = m.main(["verify", str(target), "--json"])
    report = json.loads(capsys.readouterr().out)
    assert code != 0
    assert report["ok"] is False
    # A failed seal is a statement about the copy, not about the source: the
    # report has to name the member that no longer matches.
    assert report.get("reasons")

def test_the_coverage_command_reads_a_v2_job_and_says_so(tmp_path, capsys):
    """--job reads library.sqlite3; the report names the provider it used."""
    from core.library_repository import LibraryRepository
    from delivery_fixtures import Response, Transport, executor, make_job, run

    logs = tmp_path / "download_logs"
    logs.mkdir()
    with LibraryRepository(logs / "library.sqlite3") as repository:
        job, deliveries, _ = make_job(repository, artwork="950", count=1)
        assert run(repository, job, deliveries[0], executor(
            repository, logs, Transport(Response()))).state == "completed"
    code = m.main([
        "coverage", "--database", str(logs / "library.sqlite3"),
        "--job", job["job_id"], "--json",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert payload["provider"] == "v2"
    assert payload["coverage"]["scope"]["job_id"] == job["job_id"]
    # The job is still open in this fixture (no batch was ever closed), so the
    # enumeration dimension is partial and the command says 2 - a dimension is
    # known unfinished.  Delivery is not complete either, because V2 records no
    # expected page version; both facts have to be visible, not merged into one
    # number.
    assert payload["coverage"]["overall"] == "partial"
    assert payload["coverage"]["delivery"]["state"] != "complete"
    assert code == 2
    assert payload["exit_code"] == 2


def test_the_coverage_command_leaves_the_library_byte_identical(tmp_path, capsys):
    """A read-only report must be provably read-only: bytes, time, directory."""
    import hashlib

    from core.library_repository import LibraryRepository
    from delivery_fixtures import Response, Transport, executor, make_job, run

    logs = tmp_path / "download_logs"
    logs.mkdir()
    library = logs / "library.sqlite3"
    with LibraryRepository(library) as repository:
        job, deliveries, _ = make_job(repository, artwork="951", count=1)
        assert run(repository, job, deliveries[0], executor(
            repository, logs, Transport(Response()))).state == "completed"
    before = (
        hashlib.sha256(library.read_bytes()).hexdigest(),
        library.stat().st_mtime_ns,
        sorted(path.name for path in logs.iterdir()),
    )
    code = m.main([
        "coverage", "--database", str(library), "--job", job["job_id"], "--json",
    ])
    capsys.readouterr()
    after = (
        hashlib.sha256(library.read_bytes()).hexdigest(),
        library.stat().st_mtime_ns,
        sorted(path.name for path in logs.iterdir()),
    )
    assert after == before
    assert code != 0  # the conclusion is "unproven", not "fine"


def test_the_coverage_command_refuses_to_guess_which_store_to_read(capsys):
    code = m.main(["coverage", "--database", "nowhere.sqlite3", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 3
    assert payload["code"] == "coverage_scope_required"
def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    import struct
    import zlib

    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def test_the_decode_text_output_names_one_file_budget_not_a_command_total(tmp_path, capsys):
    """The human block must not read like a total for the whole invocation."""
    import zlib

    image = tmp_path / "one.png"
    image.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", bytes([0, 0, 0, 1, 0, 0, 0, 1, 8, 0, 0, 0, 0]))
        + _png_chunk(b"IDAT", zlib.compress(b"\x00\x00"))
        + _png_chunk(b"IEND", b"")
    )
    code = m.main(["decode-check", str(image), "--structure-only"])
    out = capsys.readouterr().out
    assert "预算:" in out
    assert "file_call" in out
    assert "command" not in out
    assert code in (0, 2, 4, 5)

