"""Resume an approved, exact archive plan; maintenance only, never product.

This tool is not on the product start path and imports nothing from core, gui
or utils: it moves historical archive batches and records what it measured.
Windows junctions are never traversed. Only recorded synthetic pytest junction
entries may be detached. Files and hardlinks move on the same volume. Worktrees
move through Git. Existing files are never overwritten.

Identity is reported as three separate facts (AMD2 section 6):

    handle_identity   the opened file did not move while its bytes were read
    path_binding      the path still named that same file afterwards
    content           size and mtime did not move while the bytes were read

identity is their composition: any unknown component makes the whole record
unknown, and a known conflict is never hidden behind another component being
unknown.  On Windows a directory-enumeration cache (DirEntry.stat) reports
st_dev/st_ino/st_nlink as 0 while os.stat/os.fstat return real values, so a
zero is "no evidence" and two zeros are never an equality.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
REPO = Path(os.environ.get("PIXIVKEEP_REPO", BASE.parents[1])).resolve()
ROOT = Path(os.environ.get("PIXIVKEEP_WORKSPACE", REPO.parent)).resolve()
PLAN_DIR = Path(os.environ.get("PIXIVKEEP_PLAN_DIR", BASE)).resolve()
ARCHIVE = Path(
    os.environ.get("PIXIVKEEP_ARCHIVE_ROOT", ROOT / "_archives" / "jpgpng_development_20261114")
).resolve()
RUN = Path(os.environ.get("PIXIVKEEP_RUN_DIR", BASE / "run")).resolve()
REPARSE = stat.FILE_ATTRIBUTE_REPARSE_POINT

#: Components of one file identity (AMD2 section 6).
COMPONENT_STATES = ("ok", "unknown", "changed")


def canonical(value):
    value = os.fspath(value).replace("/", "\\")
    if value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(os.path.abspath(value))


def extended(value):
    return "\\\\?\\" + str(canonical(value))


def identity_of(info):
    """Identity of one stat result, with an explicit unknown state instead of a zero."""
    volume = int(getattr(info, "st_dev", 0) or 0)
    file_id = int(getattr(info, "st_ino", 0) or 0)
    links = int(getattr(info, "st_nlink", 0) or 0)
    missing = [name for name, value in (("volume", volume), ("file_id", file_id),
                                        ("hardlink_count", links)) if value <= 0]
    if missing:
        return {"state": "unknown",
                "reason": "identity_unavailable:" + ",".join(missing),
                "volume": None, "file_id": None, "hardlink_count": None}
    return {"state": "ok", "reason": None, "volume": volume, "file_id": file_id,
            "hardlink_count": links}


def mtime_ns(info):
    """Modification time in nanoseconds; not every stat result carries the field."""
    value = getattr(info, "st_mtime_ns", None)
    if value is not None:
        return int(value)
    return int(round(float(getattr(info, "st_mtime", 0.0)) * 1_000_000_000))


def identity_key(info):
    """The comparable triple; a zero anywhere means this platform said nothing."""
    return (int(getattr(info, "st_dev", 0) or 0), int(getattr(info, "st_ino", 0) or 0),
            int(getattr(info, "st_nlink", 0) or 0))


def is_reparse(info):
    """True for any reparse point, judged the same way the walker judges it."""
    if getattr(info, "st_file_attributes", 0) & REPARSE:
        return True
    return stat.S_ISLNK(getattr(info, "st_mode", 0))


def identity_components(before, after, path_info):
    """Three independent facts plus their composition (AMD2 section 6).

    before/after come from os.fstat on the opened handle, so they describe the
    very bytes that were hashed; path_info comes from a fresh os.lstat and is
    the only thing that can say the name still points at that file.

    The composition is deliberate: a handle that moved is changed even when the
    path could not be read, and a path that could not be read is unknown even
    when the handle looked perfect.  Nothing here returns the same value for
    "matched" and "no evidence".
    """
    size_ok = int(before.st_size) == int(after.st_size)
    time_ok = mtime_ns(before) == mtime_ns(after)
    if size_ok and time_ok:
        content, content_reason = "ok", None
    else:
        content = "changed"
        content_reason = "size_changed_during_hash" if not size_ok else "mtime_changed_during_hash"

    opened_state, again_state, named_state = (identity_of(before), identity_of(after),
                                             identity_of(path_info))
    opened, again, named = identity_key(before), identity_key(after), identity_key(path_info)

    if opened_state["state"] != "ok" or again_state["state"] != "ok":
        handle = "unknown"
        missing = set()
        for state in (opened_state, again_state):
            if state["state"] != "ok":
                missing.update(state["reason"].split(":", 1)[1].split(","))
        handle_reason = "identity_unavailable:" + ",".join(sorted(missing))
    elif opened != again:
        handle, handle_reason = "changed", "identity_changed_during_hash"
    else:
        handle, handle_reason = "ok", None

    if handle != "ok":
        # A path can never be bound to a handle whose own identity is unknown,
        # and a handle that moved cannot be bound to the name either.
        path, path_reason = "unknown", handle_reason
    elif named_state["state"] != "ok":
        path, path_reason = "unknown", "identity_unavailable:path_binding"
    elif named != again:
        path, path_reason = "changed", "path_replaced_during_hash"
    else:
        path, path_reason = "ok", None

    if content == "changed":
        identity, reason = "changed", content_reason
    elif handle == "changed":
        identity, reason = "changed", handle_reason
    elif handle == "unknown":
        identity, reason = "unknown", handle_reason
    elif path == "unknown":
        identity, reason = "unknown", path_reason
    elif path == "changed":
        identity, reason = "changed", path_reason
    else:
        identity, reason = "ok", None
    return {"content": content, "content_reason": content_reason,
            "handle_identity": handle, "handle_reason": handle_reason,
            "path_binding": path, "path_binding_reason": path_reason,
            "identity": identity, "identity_reason": reason}


def lstat_of(path):
    """lstat with operator-readable failures instead of a bare OSError."""
    try:
        return os.lstat(extended(path))
    except FileNotFoundError:
        raise RuntimeError(f"file_missing_during_inventory: {path}") from None
    except OSError as error:
        raise RuntimeError(f"file_unreadable: {path} ({error})") from None


def file_record(path, *, hashes):
    """One file content facts, bound to one opened handle and to its path.

    A file that moved while it was read produces a record with
    identity="changed" instead of an exception: the caller decides whether that
    is a mismatch (it is, for a receipt) and the reason stays in the row.
    """
    entry = lstat_of(path)
    if is_reparse(entry):
        raise RuntimeError(f"Reparse point rejected before hash: {path}")
    try:
        stream = open(extended(path), "rb")
    except OSError as error:
        raise RuntimeError(f"file_unreadable: {path} ({error})") from None
    with stream:
        before = os.fstat(stream.fileno())
        digest = hashlib.file_digest(stream, "sha256").hexdigest() if hashes else None
        after = os.fstat(stream.fileno())
    path_info = lstat_of(path)
    if is_reparse(path_info):
        raise RuntimeError(f"Reparse point rejected during hash: {path}")
    parts = identity_components(before, after, path_info)
    handle = identity_of(after)
    row = {"bytes": int(after.st_size),
           "file_id": handle["file_id"],
           "hardlink_count": handle["hardlink_count"],
           "volume": handle["volume"],
           "handle_identity": parts["handle_identity"],
           "path_binding": parts["path_binding"],
           "content": parts["content"],
           "identity": parts["identity"]}
    if parts["identity_reason"]:
        row["identity_reason"] = parts["identity_reason"]
    if hashes:
        row["sha256"] = digest
    return row


def compare_files(expected, actual):
    """Content must match on a recorded digest; identity only when both have it.

    Returns (problems, unproven).  Missing evidence is never a pass: a name
    without a digest on either side becomes a sha256_missing problem, and a
    name whose identity is unknown on either side is unproven rather than
    equal.  A receipt built from such a name may not claim that content or
    file identity was preserved.
    """
    problems, unproven = [], []
    problems += [f"missing:{name}" for name in sorted(set(expected) - set(actual))]
    problems += [f"extra:{name}" for name in sorted(set(actual) - set(expected))]
    for name in sorted(set(expected) & set(actual)):
        left, right = expected[name], actual[name]
        proven = True
        if left.get("bytes") != right.get("bytes"):
            problems.append(f"bytes:{name}")
        left_digest, right_digest = left.get("sha256"), right.get("sha256")
        if left_digest is None and right_digest is None:
            # Two absent digests are two unknowns, not an equality.
            problems.append(f"sha256_missing:{name}")
            proven = False
        elif left_digest != right_digest:
            problems.append(f"sha256:{name}")
        if left.get("identity") == "ok" and right.get("identity") == "ok":
            if (left.get("volume"), left.get("file_id"), left.get("hardlink_count")) != (
                    right.get("volume"), right.get("file_id"), right.get("hardlink_count")):
                problems.append(f"identity:{name}")
        else:
            proven = False
        if not proven:
            unproven.append(name)
    return problems, unproven


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8", newline="\n")


def load(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def no_link_ancestors(path, *, root=None):
    """Reject any reparse point between a named root and the path.

    root defaults to the workspace this tool was started for; inventory of an
    arbitrary tree (a synthetic case directory) passes its own root so the walk
    still refuses to cross a link without pretending the tree is the workspace.
    """
    path = canonical(path)
    limit = canonical(ROOT if root is None else root)
    if not path.is_relative_to(limit):
        raise RuntimeError("Path leaves the named root")
    current = limit
    for name in path.relative_to(limit).parts:
        current /= name
        try:
            info = os.lstat(extended(current))
        except FileNotFoundError:
            continue
        if info.st_file_attributes & REPARSE:
            raise RuntimeError(f"Reparse ancestor rejected: {current}")
    return path


def git(path, *args):
    result = subprocess.run(["git", "--no-optional-locks", "-C", extended(path), *args],
                            capture_output=True, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    if result.returncode:
        raise RuntimeError(f"Git command failed ({args[0]}): " + result.stderr.decode("utf-8", "replace"))
    return result.stdout.decode("utf-8")


def worktree_state(path):
    return {"head": git(path, "rev-parse", "HEAD").strip(),
            "branch": git(path, "symbolic-ref", "-q", "HEAD").strip()
            if git(path, "rev-parse", "--abbrev-ref", "HEAD").strip() != "HEAD" else None,
            "dirty_names": git(path, "status", "--porcelain=v1").splitlines()}


def entries(root):
    """Enumerate without following junctions, including dangling reparse entries.

    The DirEntry result is used for selection only.  Its st_ino/st_nlink are 0
    on this platform, so content and identity are read by file_record().
    """
    root = canonical(root)
    if not os.path.lexists(extended(root)):
        return
    no_link_ancestors(root, root=root)
    stack = [(root, "")]
    while stack:
        folder, rel = stack.pop()
        with os.scandir(extended(folder)) as stream:
            children = sorted(stream, key=lambda e: e.name)
        for child in children:
            name = f"{rel}/{child.name}".lstrip("/")
            info = child.stat(follow_symlinks=False)
            path = folder / child.name
            if info.st_file_attributes & REPARSE:
                yield name, path, info, "link"
            elif stat.S_ISDIR(info.st_mode):
                if child.name == ".git":
                    raise RuntimeError(f"Nested Git repository requires separate review: {path}")
                yield name, path, info, "directory"
                stack.append((path, name))
            elif stat.S_ISREG(info.st_mode):
                if child.name != ".git":
                    yield name, path, info, "file"
            else:
                raise RuntimeError(f"Unsupported filesystem object: {path}")


def inventory(root, *, hashes):
    """Content and the three identity components of one tree.

    The tree state is "changed" when any file moved while it was read,
    "unknown" when any file could not be measured, "ok" only when every file
    was measured and none moved, and "empty" for a tree holding no file at
    all: a missing destination is never reported as a proven identity.
    """
    files, directories, links = {}, [], []
    unknown, changed = [], []
    last = time.monotonic()
    for rel, path, info, kind in entries(root):
        if kind == "file":
            row = file_record(path, hashes=hashes)
            if row["identity"] == "changed":
                changed.append({"path": rel, "reason": row.get("identity_reason")})
            elif row["identity"] != "ok":
                unknown.append({"path": rel, "reason": row.get("identity_reason")})
            files[rel] = row
        elif kind == "directory":
            directories.append(rel)
        else:
            links.append({"path": str(path), "target": str(canonical(os.readlink(extended(path)))),
                          "tag": info.st_reparse_tag})
        if time.monotonic() - last > 25:
            print(json.dumps({"event": "inventory_progress", "batch": canonical(root).name,
                              "files": len(files), "hashes": hashes,
                              "identity_unknown": len(unknown),
                              "identity_changed": len(changed)}), flush=True)
            last = time.monotonic()
    if changed:
        state = "changed"
    elif unknown:
        state = "unknown"
    else:
        state = "ok" if files else "empty"
    return {"files": files, "directories": sorted(directories), "links": links,
            "identity": {"state": state,
                         "files": len(files), "unknown": len(unknown),
                         "changed": len(changed),
                         "reasons": sorted({str(item["reason"]) for item in unknown + changed}),
                         "examples": (unknown + changed)[:5],
                         "checked_with": "os.fstat+os.lstat"}}


def all_batches(plan_dir=None):
    """The approved plan, validated against the workspace before anything moves."""
    directory = Path(plan_dir or PLAN_DIR)
    plan_path = directory / "archive_plan.json"
    if not plan_path.exists():
        raise RuntimeError(f"Plan directory has no archive_plan.json: {directory}")
    rows = load(plan_path)
    for row in rows:
        src, dst = canonical(row["source"]), canonical(row["destination"])
        if src != ROOT / ".runtime" / row["name"] or dst != ARCHIVE / row["month"] / row["name"]:
            raise RuntimeError("Archive plan path identity mismatch")
        row["scope"] = "workspace_runtime"
    local_plan = directory / "project_local_plan.json"
    if local_plan.exists():
        for old in load(local_plan):
            rows.append({"name": old["Name"], "source": old["FullName"],
                         "destination": str(ARCHIVE / "project_local" / old["Name"]),
                         "worktrees": [], "scope": "project_local"})
    for row in rows:
        src, dst = canonical(row["source"]), canonical(row["destination"])
        allowed = ROOT / (".runtime" if row["scope"] == "workspace_runtime" else "JpgPng/.runtime")
        if src.parent != allowed or not dst.is_relative_to(ARCHIVE) or src.name != dst.name:
            raise RuntimeError("Source/destination out of approved scope")
        no_link_ancestors(src)
        no_link_ancestors(dst)
        for wt in row["worktrees"]:
            if canonical(wt["old"]) != src / wt["relative"] or canonical(wt["destination"]) != dst / wt["relative"]:
                raise RuntimeError("Worktree path identity mismatch")
    return rows


def is_known_fixture(link, source):
    path, target = canonical(link["path"]), canonical(link["target"])
    return (path.is_relative_to(source) and target.is_relative_to(source) and
            any(part.startswith("test_") for part in path.relative_to(source).parts) and
            path.is_junction() and target.exists() and not target.is_junction())


def survey():
    RUN.mkdir(exist_ok=False)
    rows = all_batches()
    result = []
    for row in rows:
        src = canonical(row["source"])
        if not src.exists():
            result.append({"name": row["name"], "scope": row["scope"], "state": "previously_moved"})
            continue
        inv = inventory(src, hashes=False)
        bad = [x for x in inv["links"] if not is_known_fixture(x, src)]
        record = {"name": row["name"], "scope": row["scope"], "state": "pending",
                  "source": str(src), "destination": row["destination"],
                  "files": len(inv["files"]), "bytes": sum(x["bytes"] for x in inv["files"].values()),
                  "identity": inv["identity"]["state"],
                  "links": inv["links"], "unapproved_links": bad}
        result.append(record)
        print(json.dumps({k: v for k, v in record.items() if k not in {"links", "unapproved_links"}} |
                         {"synthetic_junctions": len(inv["links"]), "unapproved_links": len(bad)}), flush=True)
    save(RUN / "survey.json", result)
    print(json.dumps({"survey_complete": True, "pending": sum(x["state"] == "pending" for x in result),
                      "unapproved_links": sum(len(x.get("unapproved_links", [])) for x in result)}), flush=True)


def merge_remaining(source, destination):
    """Move only into absent file targets; recurse solely through real dirs."""
    no_link_ancestors(source)
    no_link_ancestors(destination)
    os.makedirs(extended(destination), exist_ok=True)
    with os.scandir(extended(source)) as stream:
        children = list(stream)
    for item in children:
        src, dst = source / item.name, destination / item.name
        info = item.stat(follow_symlinks=False)
        if info.st_file_attributes & REPARSE:
            raise RuntimeError(f"Unexpected link during merge: {src}")
        if not os.path.lexists(extended(dst)):
            os.rename(extended(src), extended(dst))
        elif stat.S_ISDIR(info.st_mode) and dst.is_dir() and not dst.is_junction():
            merge_remaining(src, dst)
        else:
            raise RuntimeError(f"Refusing target overwrite: {dst}")
    os.rmdir(extended(source))  # Only an empty, checked, exact source directory.


def apply():
    rows = all_batches()
    surveyed = load(RUN / "survey.json")
    if any(x.get("unapproved_links") for x in surveyed):
        raise RuntimeError("Unapproved reparse entries require review")
    for row in rows:
        key = row["scope"] + "__" + row["name"]
        done = RUN / (key + "__receipt.json")
        if done.exists():
            continue
        source, destination = canonical(row["source"]), canonical(row["destination"])
        if not source.exists():
            continue
        before_path = RUN / (key + "__before.json")
        if before_path.exists():
            raise RuntimeError("Partial local receipt exists; inspect before resuming this batch")
        before_source = inventory(source, hashes=True)
        before_target = inventory(destination, hashes=True)
        overlap = set(before_source["files"]) & set(before_target["files"])
        if overlap or before_target["links"]:
            raise RuntimeError(f"File target conflicts or target links in {key}")
        if any(not is_known_fixture(link, source) for link in before_source["links"]):
            raise RuntimeError("Unknown fixture link")
        expected = {**before_target["files"], **before_source["files"]}
        expected_dirs = sorted(set(before_source["directories"]) | set(before_target["directories"]))
        states = []
        for wt in row["worktrees"]:
            old, new = canonical(wt["old"]), canonical(wt["destination"])
            present = old if old.exists() else new
            if old.exists() and new.exists():
                raise RuntimeError("Both worktree paths exist")
            state = worktree_state(present)
            if state["head"] != wt["head"] or state["branch"] != wt["branch"] or state["dirty_names"] != wt["dirty_names"]:
                raise RuntimeError(f"Worktree differs from approved plan: {present}")
            states.append({"old": str(old), "destination": str(new), "before": state})
        save(before_path, {"source": str(source), "destination": str(destination), "files": expected,
                           "directories": expected_dirs, "synthetic_junctions": before_source["links"],
                           "worktrees": states})
        for link in before_source["links"]:
            if not is_known_fixture(link, source):
                raise RuntimeError("Fixture identity changed before detach")
            os.rmdir(extended(link["path"]))  # Junction entry only; never recursive.
            with (RUN / "detached_junctions.jsonl").open("a", encoding="utf-8", newline="\n") as log:
                log.write(json.dumps(link | {"state": "entry_only_detached", "batch": key}, ensure_ascii=False) + "\n")
        os.makedirs(extended(destination), exist_ok=True)
        for state in states:
            old, new = canonical(state["old"]), canonical(state["destination"])
            if old.exists():
                no_link_ancestors(old)
                no_link_ancestors(new)
                os.makedirs(extended(new.parent), exist_ok=True)
                git(REPO, "worktree", "move", extended(old), extended(new))
            state["after"] = worktree_state(new)
            if state["before"] != state["after"]:
                raise RuntimeError("Git state changed after move")
        if source.exists():
            merge_remaining(source, destination)
        after = inventory(destination, hashes=True)
        problems, unproven = compare_files(expected, after["files"])
        if after["identity"]["state"] == "changed":
            problems.append("identity_changed_during_inventory")
        if problems or after["directories"] != expected_dirs or after["links"]:
            save(RUN / (key + "__mismatch.json"),
                 {"after": after, "problems": problems, "identity_unproven": len(unproven)})
            raise RuntimeError(f"Post-move inventory mismatch: {key}")
        receipt = {"source": str(source), "destination": str(destination), "status": "verified",
                   "files_sha256_matched": len(expected), "bytes": sum(x["bytes"] for x in expected.values()),
                   "directories_preserved": len(expected_dirs), "junction_entries_detached": len(before_source["links"]),
                   "identity_state": "proven" if not unproven else "unproven",
                   "identity_unproven": len(unproven),
                   "identity_before": {"source": before_source["identity"]["state"],
                                       "target": before_target["identity"]["state"]},
                   "identity_after": after["identity"]["state"],
                   "worktrees": states, "source_absent": not source.exists()}
        save(done, receipt)
        print(json.dumps({"event": "batch_verified", "batch": key, "files": len(expected),
                          "worktrees": len(states), "junctions": len(before_source["links"])}), flush=True)
    print(json.dumps({"archive_execution_complete": True}), flush=True)


def main(argv=None):
    """survey/apply against an explicit plan directory; no default real batch."""
    parser = argparse.ArgumentParser(
        prog="resume_archive.py",
        description="Move one approved archive plan and record what was measured (maintenance only).",
    )
    parser.add_argument("command", choices=("survey", "apply"))
    parser.add_argument("--plan", required=True, help="directory holding archive_plan.json")
    parser.add_argument("--run", required=True, help="empty working directory for records")
    parser.add_argument("--workspace", default=None, help="workspace root that owns the plan")
    parser.add_argument("--archive", default=None, help="archive root the plan moves into")
    parser.add_argument("--repo", default=None, help="product repository the worktrees belong to")
    arguments = parser.parse_args(argv)
    global ROOT, PLAN_DIR, RUN, ARCHIVE, REPO
    if arguments.workspace is not None:
        ROOT = Path(arguments.workspace).resolve()
    if arguments.archive is not None:
        ARCHIVE = Path(arguments.archive).resolve()
    if arguments.repo is not None:
        REPO = Path(arguments.repo).resolve()
    PLAN_DIR = Path(arguments.plan).resolve()
    RUN = Path(arguments.run).resolve()
    try:
        (survey if arguments.command == "survey" else apply)()
    except RuntimeError as error:
        # A refusal is an operator answer, not a traceback: the plan was not
        # approved, a link appeared, or the trees disagree with the receipt.
        print("refused: %s" % error, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
