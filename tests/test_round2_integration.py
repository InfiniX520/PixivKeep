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
