"""Identity capability of the archive collector, measured on synthetic trees.

Run either way:

    python test_identity_capability.py        (each case prints one JSON line)
    python -m pytest test_identity_capability.py -q

Every case builds its own tree under the system temporary directory and removes
it again.  No real archive, worktree, backup or business file is read, moved or
touched, and every injected path is asserted to live inside that tree first.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import resume_archive as archive

REPARSE = 0x400


@contextlib.contextmanager
def synthetic(name):
    root = Path(tempfile.mkdtemp(prefix="identity-" + name + "-"))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def inside(root, *paths):
    """Every injected path lives in this case tree; nothing touches user data."""
    for path in paths:
        assert Path(path).resolve().is_relative_to(Path(root).resolve()), path


def three_measurements(path):
    """The same file measured three ways: which one carries a real identity."""
    with os.scandir(path.parent) as stream:
        entry = [item for item in stream if item.name == path.name][0]
        cached = entry.stat(follow_symlinks=False)
    named = os.stat(path)
    with open(path, "rb") as stream:
        opened = os.fstat(stream.fileno())

    def pick(info):
        return {"st_dev": info.st_dev, "st_ino": info.st_ino, "st_nlink": info.st_nlink,
                "st_size": info.st_size}

    return {"dir_entry": pick(cached), "os_stat": pick(named), "os_fstat": pick(opened)}


def variant(info, **overrides):
    """A stat result with explicit fields replaced; everything else is real."""
    fields = dict(st_mode=info.st_mode, st_ino=info.st_ino, st_dev=info.st_dev,
                  st_nlink=info.st_nlink, st_uid=info.st_uid, st_gid=info.st_gid,
                  st_size=info.st_size, st_mtime=info.st_mtime,
                  st_mtime_ns=info.st_mtime_ns,
                  st_file_attributes=getattr(info, "st_file_attributes", 0))
    fields.update(overrides)
    return SimpleNamespace(**fields)


def without_identity(info):
    """The same file with the platform refusing to name it (all zeros)."""
    return variant(info, st_ino=0, st_dev=0, st_nlink=0)


def test_plain_file_identity_comes_from_the_opened_file():
    with synthetic("plain") as root:
        target = root / "plain.bin"
        payload = b"payload" * 64
        target.write_bytes(payload)
        inside(root, target)
        measured = three_measurements(target)
        record = archive.file_record(target, hashes=True)
        print(json.dumps({"case": "plain", "measurements": measured,
                          "record": {key: record[key] for key in
                                     ("bytes", "volume", "file_id", "hardlink_count",
                                      "identity", "handle_identity", "path_binding",
                                      "content", "sha256")}}, ensure_ascii=False))
        assert archive.identity_of(os.stat(target))["state"] == "ok"
        assert record["identity"] == "ok"
        assert record["handle_identity"] == "ok"
        assert record["path_binding"] == "ok"
        assert record["content"] == "ok"
        assert record["file_id"] == measured["os_stat"]["st_ino"]
        assert record["file_id"] == measured["os_fstat"]["st_ino"]
        assert record["file_id"] > 0
        assert record["volume"] == measured["os_fstat"]["st_dev"] > 0
        assert record["hardlink_count"] == 1
        assert record["bytes"] == len(payload)
        assert record["sha256"] == hashlib.sha256(payload).hexdigest()
        if measured["dir_entry"]["st_ino"] == 0:
            # This platform enumeration cache carries no file id at all; the
            # record must not have copied that zero into the manifest.
            assert archive.identity_of(_cached_info(target))["state"] == "unknown"


def _cached_info(path):
    with os.scandir(path.parent) as stream:
        return [item for item in stream if item.name == path.name][0].stat(follow_symlinks=False)


def test_two_hardlinks_share_one_identity_and_the_link_count():
    with synthetic("hardlink") as root:
        first = root / "first.bin"
        first.write_bytes(b"shared bytes")
        second = root / "second.bin"
        inside(root, first, second)
        try:
            os.link(first, second)
        except OSError as error:
            raise AssertionError(f"hardlinks unavailable on this volume: {error}") from None
        left = archive.file_record(first, hashes=True)
        right = archive.file_record(second, hashes=True)
        print(json.dumps({"case": "hardlink", "left": left, "right": right}, ensure_ascii=False))
        assert left["identity"] == right["identity"] == "ok"
        assert left["file_id"] == right["file_id"] == os.stat(first).st_ino
        assert left["hardlink_count"] == right["hardlink_count"] >= 2
        assert left["sha256"] == right["sha256"]


def test_independent_copy_has_its_own_identity():
    with synthetic("copy") as root:
        original = root / "original.bin"
        original.write_bytes(b"same bytes")
        copy = root / "copy.bin"
        shutil.copyfile(original, copy)
        left = archive.file_record(original, hashes=True)
        right = archive.file_record(copy, hashes=True)
        print(json.dumps({"case": "copy", "original": left, "copy": right}, ensure_ascii=False))
        assert left["sha256"] == right["sha256"]
        assert left["identity"] == right["identity"] == "ok"
        assert left["file_id"] != right["file_id"]
        problems, unproven = archive.compare_files({"page.bin": left}, {"page.bin": right})
        assert problems == ["identity:page.bin"], problems
        assert unproven == []


def test_path_replacement_is_detected_even_with_identical_content():
    with synthetic("replace") as root:
        target = root / "page.bin"
        target.write_bytes(b"contents")
        before = archive.file_record(target, hashes=True)
        replacement = root / "replacement.bin"
        replacement.write_bytes(b"contents")
        inside(root, target, replacement)
        os.replace(replacement, target)
        after = archive.file_record(target, hashes=True)
        problems, unproven = archive.compare_files({"page.bin": before}, {"page.bin": after})
        print(json.dumps({"case": "replace", "before": before, "after": after,
                          "problems": problems, "unproven": unproven}, ensure_ascii=False))
        assert before["sha256"] == after["sha256"]
        assert before["file_id"] != after["file_id"]
        assert problems == ["identity:page.bin"], problems
        assert unproven == []
        # The same seam refuses a path that stopped naming the opened file while
        # the bytes were being read (an open file cannot be replaced on Windows,
        # so the decision function is exercised with real stat results).
        opened = os.stat(target)
        elsewhere = root / "elsewhere.bin"
        elsewhere.write_bytes(b"contents")
        inside(root, elsewhere)
        parts = archive.identity_components(opened, opened, os.stat(elsewhere))
        assert parts["path_binding"] == "changed", parts
        assert parts["identity"] == "changed", parts


def test_content_change_is_detected_by_content_as_well():
    with synthetic("content") as root:
        target = root / "page.bin"
        target.write_bytes(b"before")
        before = archive.file_record(target, hashes=True)
        target.write_bytes(b"after")
        after = archive.file_record(target, hashes=True)
        problems, unproven = archive.compare_files({"page.bin": before}, {"page.bin": after})
        assert "sha256:page.bin" in problems, problems
        assert "bytes:page.bin" in problems, problems
        assert unproven == []


def test_junction_is_never_traversed_or_hashed():
    with synthetic("junction") as root:
        real = root / "real"
        real.mkdir()
        (real / "inside.bin").write_bytes(b"inside")
        junction = root / "junction"
        inside(root, real, junction)
        created = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(real)],
                                 capture_output=True)
        try:
            assert created.returncode == 0, created.stderr.decode("utf-8", "replace")
            assert os.path.isjunction(junction)
            names = [(name, kind) for name, _path, _info, kind in archive.entries(root)]
            print(json.dumps({"case": "junction", "entries": names}, ensure_ascii=False))
            assert ("junction", "link") in names
            assert not any(name.startswith("junction/") for name, _kind in names)
            try:
                archive.no_link_ancestors(junction / "inside.bin", root=root)
            except RuntimeError as error:
                assert "Reparse ancestor" in str(error), error
            else:
                raise AssertionError("a junction ancestor must be rejected")
            # The default root is the workspace; a tree outside it is refused
            # before any stat is taken, which is a different refusal.
            try:
                archive.no_link_ancestors(junction / "inside.bin")
            except RuntimeError as error:
                assert "leaves the named root" in str(error), error
            else:
                raise AssertionError("a path outside the workspace must be refused")
            try:
                archive.file_record(junction, hashes=True)
            except (RuntimeError, OSError) as error:
                assert "Reparse point rejected" in str(error), error
            else:
                raise AssertionError("a junction must never be hashed as a file")
        finally:
            if os.path.lexists(junction):
                os.rmdir(junction)


def test_unavailable_identity_is_unknown_and_never_proof():
    with synthetic("unknown") as root:
        target = root / "file.bin"
        target.write_bytes(b"x" * 32)
        real_fstat = os.fstat
        inside(root, target)

        def zeroed(descriptor):
            return without_identity(real_fstat(descriptor))

        with mock.patch.object(archive.os, "fstat", zeroed):
            record = archive.file_record(target, hashes=True)
        print(json.dumps({"case": "unknown", "record": record}, ensure_ascii=False))
        assert record["identity"] == "unknown"
        assert record["handle_identity"] == "unknown"
        assert record["path_binding"] == "unknown"
        assert record["file_id"] is None
        assert record["hardlink_count"] is None
        assert record["volume"] is None
        assert record["identity_reason"].startswith("identity_unavailable:")
        assert record["bytes"] == 32
        assert record["sha256"] == hashlib.sha256(b"x" * 32).hexdigest()
        problems, unproven = archive.compare_files({"file.bin": record},
                                                  {"file.bin": dict(record)})
        assert problems == []
        assert unproven == ["file.bin"]


def _counting_fstat(real, *, first=None, second=None):
    """fstat replacement that changes only the numbered call."""
    calls = {"count": 0}

    def fstat(descriptor):
        info = real(descriptor)
        calls["count"] += 1
        if calls["count"] == 1 and first is not None:
            return first(info)
        if calls["count"] == 2 and second is not None:
            return second(info)
        return info

    return fstat, calls


def test_only_the_first_fstat_unknown_is_not_proof():
    with synthetic("first_unknown") as root:
        target = root / "file.bin"
        target.write_bytes(b"y" * 16)
        real_fstat = os.fstat
        fstat, calls = _counting_fstat(real_fstat, first=without_identity)
        with mock.patch.object(archive.os, "fstat", fstat):
            record = archive.file_record(target, hashes=True)
        print(json.dumps({"case": "first_unknown", "calls": calls["count"],
                          "record": record}, ensure_ascii=False))
        assert calls["count"] == 2, "both measurements must really have run"
        assert record["handle_identity"] == "unknown"
        assert record["path_binding"] == "unknown"
        assert record["identity"] == "unknown"
        assert record["identity_reason"].startswith("identity_unavailable:")
        assert record["sha256"] == hashlib.sha256(b"y" * 16).hexdigest()


def test_only_the_second_fstat_unknown_is_not_proof():
    with synthetic("second_unknown") as root:
        target = root / "file.bin"
        target.write_bytes(b"z" * 16)
        real_fstat = os.fstat
        fstat, calls = _counting_fstat(real_fstat, second=without_identity)
        with mock.patch.object(archive.os, "fstat", fstat):
            record = archive.file_record(target, hashes=True)
        print(json.dumps({"case": "second_unknown", "calls": calls["count"],
                          "record": record}, ensure_ascii=False))
        assert calls["count"] == 2
        assert record["handle_identity"] == "unknown"
        assert record["path_binding"] == "unknown"
        assert record["identity"] == "unknown"
        assert record["file_id"] is None


def _counting_lstat(*, second):
    real = archive.lstat_of
    calls = {"count": 0}

    def lstat(path):
        info = real(path)
        calls["count"] += 1
        if calls["count"] == 2:
            return second(info)
        return info

    return lstat, calls


def test_final_path_identity_unknown_is_not_proof():
    """AMD2 6 rule 1: a good handle never makes an unread path "proven"."""
    with synthetic("path_unknown") as root:
        target = root / "page.bin"
        target.write_bytes(b"same length bytes")
        lstat, calls = _counting_lstat(second=without_identity)
        with mock.patch.object(archive, "lstat_of", lstat):
            record = archive.file_record(target, hashes=True)
        print(json.dumps({"case": "path_unknown", "calls": calls["count"],
                          "record": record}, ensure_ascii=False))
        assert calls["count"] == 2, "the final path measurement must really have run"
        assert record["handle_identity"] == "ok"
        assert record["path_binding"] == "unknown"
        assert record["identity"] == "unknown", record
        assert record["identity_reason"] == "identity_unavailable:path_binding"
        assert record["file_id"] > 0


def test_a_known_handle_change_beats_an_unknown_path():
    """AMD2 6 rule 2: a conflict is never hidden behind another unknown."""
    with synthetic("handle_changed") as root:
        target = root / "page.bin"
        target.write_bytes(b"moving bytes")
        real_fstat = os.fstat
        fstat, calls = _counting_fstat(
            real_fstat,
            second=lambda info: variant(info, st_ino=info.st_ino + 1),
        )
        lstat, _lstat_calls = _counting_lstat(second=without_identity)
        with mock.patch.object(archive.os, "fstat", fstat), \
                mock.patch.object(archive, "lstat_of", lstat):
            record = archive.file_record(target, hashes=True)
        print(json.dumps({"case": "handle_changed", "calls": calls["count"],
                          "record": record}, ensure_ascii=False))
        assert record["handle_identity"] == "changed"
        assert record["path_binding"] == "unknown"
        assert record["identity"] == "changed", record
        assert record["identity_reason"] == "identity_changed_during_hash"


def test_a_known_path_conflict_survives_an_unknown_handle():
    """A conflict is evidence: it outranks the other side being unmeasurable."""
    with synthetic("conflict_over_unknown") as root:
        target = root / "page.bin"
        target.write_bytes(b"conflict bytes")
        real_fstat = os.fstat
        fstat, calls = _counting_fstat(real_fstat, first=without_identity)
        lstat, _lstat_calls = _counting_lstat(
            second=lambda info: variant(info, st_ino=info.st_ino + 1)
        )
        with mock.patch.object(archive.os, "fstat", fstat), \
                mock.patch.object(archive, "lstat_of", lstat):
            record = archive.file_record(target, hashes=True)
        print(json.dumps({"case": "conflict_over_unknown", "calls": calls["count"],
                          "record": record}, ensure_ascii=False))
        assert calls["count"] == 2
        assert record["handle_identity"] == "unknown"
        assert record["path_binding"] == "changed", record
        assert record["identity"] == "changed", record
        assert record["identity_reason"] == "path_replaced_during_hash"


def test_an_unknown_final_handle_leaves_the_path_unknown():
    """With no measurable handle after the read there is nothing to compare."""
    with synthetic("no_comparable_pair") as root:
        target = root / "page.bin"
        target.write_bytes(b"no comparable bytes")
        real_fstat = os.fstat
        fstat, calls = _counting_fstat(real_fstat, second=without_identity)
        with mock.patch.object(archive.os, "fstat", fstat):
            record = archive.file_record(target, hashes=True)
        print(json.dumps({"case": "no_comparable_pair", "calls": calls["count"],
                          "record": record}, ensure_ascii=False))
        assert calls["count"] == 2
        assert record["handle_identity"] == "unknown"
        assert record["path_binding"] == "unknown", record
        assert record["identity"] == "unknown", record
        assert record["identity_reason"].startswith("identity_unavailable:")


def test_cross_volume_identity_change_is_changed():
    """A volume change is a changed identity; a real cross-volume move is untested."""
    with synthetic("cross_volume") as root:
        target = root / "page.bin"
        target.write_bytes(b"cross volume bytes")
        real_fstat = os.fstat
        fstat, calls = _counting_fstat(
            real_fstat,
            second=lambda info: variant(info, st_dev=info.st_dev + 4096),
        )
        with mock.patch.object(archive.os, "fstat", fstat):
            record = archive.file_record(target, hashes=True)
        print(json.dumps({"case": "cross_volume", "calls": calls["count"],
                          "record": record}, ensure_ascii=False))
        assert record["handle_identity"] == "changed"
        assert record["identity"] == "changed"


def test_a_reparse_point_appearing_after_the_hash_is_refused():
    with synthetic("late_reparse") as root:
        target = root / "page.bin"
        target.write_bytes(b"reparse bytes")
        lstat, calls = _counting_lstat(
            second=lambda info: variant(
                info, st_file_attributes=getattr(info, "st_file_attributes", 0) | REPARSE)
        )
        with mock.patch.object(archive, "lstat_of", lstat):
            try:
                archive.file_record(target, hashes=True)
            except RuntimeError as error:
                assert "Reparse point rejected during hash" in str(error), error
            else:
                raise AssertionError("a reparse point must never be hashed as a file")
        assert calls["count"] == 2


def test_inventory_reports_unknown_instead_of_a_zero_identity():
    with synthetic("inventory_unknown") as root:
        (root / "one.bin").write_bytes(b"1")
        (root / "two.bin").write_bytes(b"22")
        real_fstat = os.fstat

        def zeroed(descriptor):
            return without_identity(real_fstat(descriptor))

        with mock.patch.object(archive.os, "fstat", zeroed):
            report = archive.inventory(root, hashes=True)
        print(json.dumps({"case": "inventory_unknown", "identity": report["identity"]},
                         ensure_ascii=False))
        assert report["identity"]["state"] == "unknown"
        assert report["identity"]["files"] == 2
        assert report["identity"]["unknown"] == 2
        assert report["identity"]["reasons"]
        assert all(row["identity"] == "unknown" for row in report["files"].values())
        assert [row["bytes"] for row in report["files"].values()] == [1, 2]


def test_whole_tree_rename_keeps_content_and_identity():
    with synthetic("rename") as root:
        source = root / "before"
        (source / "nested").mkdir(parents=True)
        (source / "a.bin").write_bytes(b"a" * 10)
        (source / "nested" / "b.bin").write_bytes(b"b" * 20)
        before = archive.inventory(source, hashes=True)
        moved = root / "after"
        inside(root, source, moved)
        os.rename(source, moved)
        after = archive.inventory(moved, hashes=True)
        problems, unproven = archive.compare_files(before["files"], after["files"])
        print(json.dumps({"case": "rename", "problems": problems, "unproven": unproven,
                          "before": before["identity"]["state"],
                          "after": after["identity"]["state"]}, ensure_ascii=False))
        assert problems == [], problems
        assert unproven == []
        assert before["identity"]["state"] == after["identity"]["state"] == "ok"


def test_unhashed_inventories_never_compare_as_equal():
    with synthetic("no_digest") as root:
        (root / "a.bin").write_bytes(b"a" * 10)
        (root / "b.bin").write_bytes(b"b" * 20)
        first = archive.inventory(root, hashes=False)
        second = archive.inventory(root, hashes=False)
        problems, unproven = archive.compare_files(first["files"], second["files"])
        print(json.dumps({"case": "no_digest", "problems": problems, "unproven": unproven},
                         ensure_ascii=False))
        assert problems == ["sha256_missing:a.bin", "sha256_missing:b.bin"], problems
        assert unproven == ["a.bin", "b.bin"]
        # One recorded digest against an absent one is a mismatch, not a pass.
        hashed = archive.inventory(root, hashes=True)
        mixed, mixed_unproven = archive.compare_files(first["files"], hashed["files"])
        assert mixed == ["sha256:a.bin", "sha256:b.bin"], mixed
        assert mixed_unproven == []
        # An explicit null digest is missing evidence too, not an equality.
        blank = {name: dict(row, sha256=None) for name, row in first["files"].items()}
        also_blank = {name: dict(row, sha256=None) for name, row in second["files"].items()}
        blanks, _ = archive.compare_files(blank, also_blank)
        assert blanks == ["sha256_missing:a.bin", "sha256_missing:b.bin"], blanks


def test_missing_file_becomes_a_readable_failure():
    with synthetic("missing") as root:
        gone = root / "gone.bin"
        try:
            archive.file_record(gone, hashes=True)
        except RuntimeError as error:
            assert str(error).startswith("file_missing_during_inventory:"), error
        else:
            raise AssertionError("a vanished file must not produce a record")
        keeper = root / "keeper.bin"
        keeper.write_bytes(b"k")
        inventory_source = root / "tree"
        inventory_source.mkdir()
        (inventory_source / "here.bin").write_bytes(b"here")
        report = archive.inventory(inventory_source, hashes=True)
        assert report["identity"]["state"] == "ok"
        assert report["files"]["here.bin"]["file_id"] > 0


def test_inventory_without_hashes_still_records_identity():
    with synthetic("survey") as root:
        (root / "empty.bin").write_bytes(b"")
        (root / "full.bin").write_bytes(b"z" * 5)
        report = archive.inventory(root, hashes=False)
        print(json.dumps({"case": "survey", "identity": report["identity"],
                          "files": report["files"]}, ensure_ascii=False))
        assert report["identity"]["state"] == "ok"
        assert sorted(report["files"]) == ["empty.bin", "full.bin"]
        assert report["files"]["empty.bin"]["bytes"] == 0
        assert report["files"]["full.bin"]["bytes"] == 5
        assert all("sha256" not in row for row in report["files"].values())
        assert all(row["identity"] == "ok" and row["file_id"] > 0
                   for row in report["files"].values())


def _apply_synthetic_batch(root, *, zero_before_identity):
    """Run the collector own apply() on a synthetic batch inside the case tree."""
    source = root / "batch_in" / "demo"
    (source / "nested").mkdir(parents=True)
    (source / "a.bin").write_bytes(b"a" * 10)
    (source / "nested" / "b.bin").write_bytes(b"b" * 20)
    destination = root / "batch_out" / "demo"
    run = root / "run"
    run.mkdir()
    inside(root, source, destination, run)
    (run / "survey.json").write_text(json.dumps([{"name": "demo", "unapproved_links": []}]),
                                     encoding="utf-8", newline="\n")
    rows = [{"name": "demo", "scope": "workspace_runtime", "source": str(source),
             "destination": str(destination), "worktrees": []}]
    real_inventory = archive.inventory
    calls = {"count": 0}

    def inventory(path, *, hashes):
        calls["count"] += 1
        report = real_inventory(path, hashes=hashes)
        if zero_before_identity and calls["count"] == 1:
            # A pre-move inventory recorded by the old collector: content only.
            for row in report["files"].values():
                row.update({"file_id": None, "hardlink_count": None, "volume": None,
                            "handle_identity": "unknown", "path_binding": "unknown",
                            "identity": "unknown",
                            "identity_reason": "identity_unavailable:volume,file_id,hardlink_count"})
            report["identity"] = {"state": "unknown", "files": len(report["files"]),
                                  "unknown": len(report["files"]), "changed": 0,
                                  "reasons": ["identity_unavailable:volume,file_id,hardlink_count"],
                                  "examples": [], "checked_with": "synthetic-old-manifest"}
        return report

    with mock.patch.multiple(archive, ROOT=root, RUN=run, all_batches=lambda: rows), \
            mock.patch.object(archive, "inventory", inventory):
        archive.apply()
    receipt = json.loads((run / "workspace_runtime__demo__receipt.json").read_text("utf-8"))
    return destination, receipt


def test_apply_receipt_states_identity_proven_on_a_synthetic_batch():
    with synthetic("apply") as root:
        destination, receipt = _apply_synthetic_batch(root, zero_before_identity=False)
        assert receipt["status"] == "verified"
        assert receipt["files_sha256_matched"] == 2
        assert receipt["identity_state"] == "proven"
        assert receipt["identity_unproven"] == 0
        assert receipt["identity_before"]["source"] == "ok"
        assert receipt["identity_after"] == "ok"
        assert receipt["source_absent"] is True
        assert (destination / "a.bin").read_bytes() == b"a" * 10
        assert (destination / "nested" / "b.bin").read_bytes() == b"b" * 20


def test_apply_receipt_says_unproven_when_only_content_is_recorded():
    with synthetic("apply_old") as root:
        destination, receipt = _apply_synthetic_batch(root, zero_before_identity=True)
        assert receipt["status"] == "verified"
        assert receipt["files_sha256_matched"] == 2
        assert receipt["identity_state"] == "unproven"
        assert receipt["identity_unproven"] == 2
        assert receipt["identity_before"]["source"] == "unknown"
        assert receipt["identity_after"] == "ok"
        assert (destination / "a.bin").read_bytes() == b"a" * 10


def test_the_plan_directory_is_required_and_never_defaults_to_a_real_batch():
    with synthetic("plan") as root:
        with pytest.raises(RuntimeError) as caught:
            archive.all_batches(root)
        assert "archive_plan.json" in str(caught.value)


if __name__ == "__main__":
    failures = 0
    cases = [(name, value) for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for name, case in cases:
        try:
            case()
        except BaseException as error:
            failures += 1
            print(json.dumps({"test": name, "state": "failed", "detail": repr(error)},
                             ensure_ascii=False))
        else:
            print(json.dumps({"test": name, "state": "ok"}, ensure_ascii=False))
    print(json.dumps({"tests": len(cases), "failed": failures}, ensure_ascii=False))
    raise SystemExit(1 if failures else 0)
