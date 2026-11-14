"""The two read-only report commands: shapes, exit codes and the no-write rule.

Every fixture is a synthetic database in tmp_path and a two-line JSON file.
Nothing here touches a real library, a real gallery or the network.
"""
from __future__ import annotations

import ast
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from core.follow_crawl_state import FollowArtistSnapshot, FollowCrawlStateRepository
from core.library_maintenance import main
from core.report_commands import (
    EXIT_ATTENTION,
    EXIT_INCOMPLETE,
    EXIT_OK,
    ReportError,
    coverage_exit_code,
    coverage_lines,
    coverage_report,
    filter_explain_report,
)

ROOT = Path(__file__).resolve().parent.parent
MODULE = ROOT / "core" / "report_commands.py"
READER = ROOT / "core" / "crawl_state_reader.py"


def _seal_wal(database: Path) -> None:
    """Leave a rollback-journal file behind, so a read-only open needs no -shm."""
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.execute("PRAGMA journal_mode=DELETE")
    connection.close()


def _crawl_db(tmp_path: Path, *, states=("completed", "completed"), status="completed"):
    database = tmp_path / "crawl.sqlite3"
    repository = FollowCrawlStateRepository(database)
    run = repository.create_run(
        account_id="acct-synthetic",
        config_key="follow-default",
        artists=[FollowArtistSnapshot(artist_id=str(1000 + index)) for index in range(len(states))],
        start_mode="history",
    )
    connection = sqlite3.connect(database)
    for index, state in enumerate(states, start=1):
        connection.execute(
            "UPDATE artist_progress SET state=?, works_seen=? WHERE run_id=? AND ordinal=?",
            (state, 3, run.run_id, index),
        )
    connection.execute("UPDATE crawl_runs SET status=? WHERE run_id=?", (status, run.run_id))
    connection.commit()
    connection.close()
    repository.close()
    _seal_wal(database)
    return database, run.run_id


def _candidates(path: Path, rows) -> Path:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


def _conditions(path: Path, **data) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_the_coverage_exit_code_is_the_conclusion_not_the_run():
    assert coverage_exit_code("complete") == EXIT_OK
    assert coverage_exit_code("partial") == EXIT_ATTENTION
    assert coverage_exit_code("unknown") == EXIT_INCOMPLETE
    assert coverage_exit_code("half-done") == EXIT_INCOMPLETE
    assert coverage_exit_code(None) == EXIT_INCOMPLETE


def test_a_run_with_no_page_evidence_reports_unknown_and_exits_five(tmp_path, capsys):
    database, run_id = _crawl_db(tmp_path)
    code = main(["--json", "coverage", "--database", str(database), "--run", run_id])
    payload = json.loads(capsys.readouterr().out)
    assert code == 5
    assert payload["coverage"]["overall"] == "unknown"
    assert payload["coverage"]["delivery"]["state"] == "unknown"
    assert payload["facts_notes"]["page_evidence"] == "missing"


def test_a_run_with_an_unfinished_artist_exits_two(tmp_path, capsys):
    database, run_id = _crawl_db(tmp_path, states=("completed", "pending"))
    code = main(["--json", "coverage", "--database", str(database), "--run", run_id])
    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["coverage"]["overall"] == "partial"
    assert payload["coverage"]["enumeration"]["state"] == "partial"


def test_complete_cannot_be_reached_from_the_crawl_store_alone(tmp_path):
    """A documented engine gap, not a bug hidden in the wiring.

    The crawl store persists no filter counters (so decision is skipped) and no
    page evidence (so delivery is unknown), therefore overall=complete needs a
    save-side evidence source that does not exist yet.  The command reports
    unknown rather than filling the gap with zeros.
    """
    database, run_id = _crawl_db(tmp_path)
    report = coverage_report(database, run_id)
    assert report["coverage"]["decision"]["state"] == "skipped"
    assert report["coverage"]["counts"]["pages_saved"] is None
    assert report["exit_code"] == EXIT_INCOMPLETE


def test_the_text_never_prints_an_unmeasured_number_as_zero(tmp_path, capsys):
    database, run_id = _crawl_db(tmp_path)
    code = main(["coverage", "--database", str(database), "--run", run_id])
    printed = capsys.readouterr().out
    assert code == 5
    # Works seen is provable from the artist rows (3 + 3), so that number is
    # printed; the save side has no source at all, so its clause is absent
    # instead of being filled with zeros.
    assert "新增 6 个作品。" in printed
    for forbidden in ("保存 0 页", "排除 0 件", "字段未知 0 件"):
        assert forbidden not in printed
    assert "筛选排除" not in printed
    assert "页级证据: 无" in printed


def test_the_coverage_text_is_the_engine_sentences(tmp_path):
    database, run_id = _crawl_db(tmp_path)
    lines = coverage_lines(coverage_report(database, run_id))
    assert lines[0].startswith("本轮已检查 ")
    assert any(line.startswith("查找:") for line in lines)
    assert any(line.startswith("判定:") for line in lines)
    assert any(line.startswith("保存:") for line in lines)
    assert lines[-1].startswith("退出码: ")


def test_a_missing_database_is_a_refusal_not_an_empty_run(tmp_path, capsys):
    code = main(["--json", "coverage", "--database", str(tmp_path / "nope.sqlite3"), "--run", "x"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 3
    assert payload["status"] == "error"
    assert payload["code"] == "database_missing"


def test_an_unknown_run_is_a_refusal(tmp_path, capsys):
    database, _ = _crawl_db(tmp_path)
    code = main(["--json", "coverage", "--database", str(database), "--run", "no-such-run"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 3
    assert payload["code"] == "run_unknown"


def test_a_file_that_is_not_a_database_is_a_refusal(tmp_path, capsys):
    broken = tmp_path / "broken.sqlite3"
    broken.write_bytes(b"not a database at all")
    code = main(["--json", "coverage", "--database", str(broken), "--run", "x"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 3
    assert payload["code"] in {"database_unreadable", "run_unknown"}


def test_a_refusal_in_text_mode_goes_to_stderr_without_a_traceback(tmp_path, capsys):
    code = main(["coverage", "--database", str(tmp_path / "nope.sqlite3"), "--run", "x"])
    captured = capsys.readouterr()
    assert code == 3
    assert captured.out == ""
    assert "database_missing" in captured.err
    assert "Traceback" not in captured.err


def test_every_candidate_is_explained_and_a_clean_batch_exits_zero(tmp_path, capsys):
    candidates = _candidates(tmp_path / "c.jsonl", [
        {"illust_id": "1", "kind": "illust", "bookmarks": 500, "ai_flag": False, "available": True},
        {"illust_id": "2", "kind": "illust", "bookmarks": 5, "ai_flag": False, "available": True},
    ])
    conditions = _conditions(tmp_path / "k.json", min_bookmarks=100, exclude_ai=True)
    code = main(["--json", "filter-explain", "--candidates", str(candidates),
                 "--conditions", str(conditions)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["summary"] == {"total": 2, "match": 1, "exclude": 1, "unknown": 0}
    assert payload["candidates"][1]["reasons"][0]["code"] == "favorites_below"
    assert payload["conditions"] == {"min_bookmarks": 100, "exclude_ai": True}


def test_an_unknown_candidate_makes_the_whole_batch_five(tmp_path, capsys):
    candidates = _candidates(tmp_path / "c.jsonl", [
        {"illust_id": "1", "kind": "illust", "bookmarks": 500, "ai_flag": False, "available": True},
        {"illust_id": "2", "kind": "illust", "bookmarks": None,
         "ai_flag": False, "available": True},
    ])
    conditions = _conditions(tmp_path / "k.json", min_bookmarks=100, exclude_ai=False)
    code = main(["--json", "filter-explain", "--candidates", str(candidates),
                 "--conditions", str(conditions)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 5
    assert payload["candidates"][1]["verdict"] == "unknown"
    assert payload["summary"]["unknown"] == 1


def test_the_explainer_text_carries_the_counts_it_actually_saw(tmp_path, capsys):
    candidates = _candidates(tmp_path / "c.jsonl", [
        {"illust_id": "1", "kind": "ugoira", "bookmarks": 900, "ai_flag": False, "available": True},
        {"illust_id": "2", "kind": "illust", "bookmarks": 900, "ai_flag": False, "available": True},
        {"illust_id": "3", "kind": "illust", "bookmarks": 900, "ai_flag": None, "available": True},
    ])
    conditions = _conditions(tmp_path / "k.json", min_bookmarks=100, exclude_ai=True)
    code = main(["filter-explain", "--candidates", str(candidates),
                 "--conditions", str(conditions)])
    printed = capsys.readouterr().out
    assert code == 5
    assert "候选 3 个: match 1、exclude 1、unknown 1" in printed
    assert "候选 1: exclude（type_unsupported/type）" in printed


def test_a_missing_candidates_file_is_a_refusal(tmp_path, capsys):
    conditions = _conditions(tmp_path / "k.json", min_bookmarks=1, exclude_ai=False)
    code = main(["--json", "filter-explain", "--candidates", str(tmp_path / "nope.jsonl"),
                 "--conditions", str(conditions)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 3
    assert payload["code"] == "candidates_unreadable"


def test_a_broken_candidate_line_names_the_line(tmp_path, capsys):
    path = tmp_path / "c.jsonl"
    path.write_text("{\"illust_id\": \"1\", \"kind\": \"illust\"}\n{not json\n", encoding="utf-8")
    conditions = _conditions(tmp_path / "k.json", min_bookmarks=1, exclude_ai=False)
    code = main(["--json", "filter-explain", "--candidates", str(path),
                 "--conditions", str(conditions)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 3
    assert payload["code"] == "candidate_not_json"
    assert "line 2" in payload["detail"]


def test_an_unknown_condition_key_is_refused_instead_of_guessed(tmp_path, capsys):
    candidates = _candidates(tmp_path / "c.jsonl", [
        {"illust_id": "1", "kind": "illust", "bookmarks": 1, "ai_flag": False, "available": True},
    ])
    conditions = _conditions(tmp_path / "k.json", min_bookmarks=1, exclude_ai=False, max_pages=3)
    code = main(["--json", "filter-explain", "--candidates", str(candidates),
                 "--conditions", str(conditions)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 3
    assert payload["code"] == "conditions_invalid"
    assert "max_pages" in payload["detail"]


def test_a_conditions_file_that_is_not_json_is_refused(tmp_path, capsys):
    candidates = _candidates(tmp_path / "c.jsonl", [
        {"illust_id": "1", "kind": "illust", "bookmarks": 1, "ai_flag": False, "available": True},
    ])
    conditions = tmp_path / "k.json"
    conditions.write_text("{min_bookmarks: 1}", encoding="utf-8")
    code = main(["--json", "filter-explain", "--candidates", str(candidates),
                 "--conditions", str(conditions)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 3
    assert payload["code"] == "conditions_not_json"


def test_an_empty_candidate_file_is_refused_rather_than_reported_as_clean(tmp_path):
    candidates = tmp_path / "c.jsonl"
    candidates.write_text("\n\n", encoding="utf-8")
    conditions = _conditions(tmp_path / "k.json", min_bookmarks=1, exclude_ai=False)
    with pytest.raises(ReportError) as caught:
        filter_explain_report(candidates, conditions)
    assert caught.value.code == "candidates_empty"


def test_a_candidate_that_cannot_be_judged_is_refused_with_its_id(tmp_path):
    candidates = _candidates(tmp_path / "c.jsonl", [
        {"illust_id": "7", "kind": "illust", "bookmarks": 1, "available": "yes"},
    ])
    conditions = _conditions(tmp_path / "k.json", min_bookmarks=1, exclude_ai=False)
    with pytest.raises(ReportError) as caught:
        filter_explain_report(candidates, conditions)
    assert caught.value.code == "candidate_refused"
    assert "7" in caught.value.detail


def test_the_module_imports_no_network_or_process_helpers():
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    for forbidden in ("requests", "urllib", "socket", "subprocess", "http", "shutil", "os"):
        assert forbidden not in imported, forbidden


def test_the_module_contains_no_write_statement():
    code = _code_only(MODULE)
    for verb in ("INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER",
                 "executescript", "commit"):
        assert verb not in code, verb
    for mode in ("r+", "wb", "w+", "a+"):
        assert mode not in code, mode


def test_the_database_is_opened_read_only():
    source = READER.read_text(encoding="utf-8")
    assert "mode=ro" in source
    assert "query_only=1" in source


def _code_only(path: Path) -> str:
    """The module without its docstrings, so prose cannot trip a word scan."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def test_the_reader_never_creates_or_reconfigures_anything():
    code = _code_only(READER)
    for verb in ("mkdir", "journal_mode", "CREATE", "INSERT", "UPDATE", "DELETE",
                 "DROP", "ALTER", "executescript", "commit"):
        assert verb not in code, verb


def test_the_reporting_source_is_not_the_writing_repository():
    source = MODULE.read_text(encoding="utf-8")
    assert "follow_crawl_state" not in source


def _fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_the_coverage_command_does_not_touch_the_database(tmp_path, capsys):
    """The falsifiable read-only proof: same bytes, same files, same mtime."""
    database, run_id = _crawl_db(tmp_path)
    before_hash = _fingerprint(database)
    before_stat = database.stat()
    before_files = sorted(item.name for item in database.parent.iterdir())
    json_code = main(["--json", "coverage", "--database", str(database), "--run", run_id])
    capsys.readouterr()
    text_code = main(["coverage", "--database", str(database), "--run", run_id])
    capsys.readouterr()
    after_stat = database.stat()
    assert json_code == text_code == 5
    assert _fingerprint(database) == before_hash
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
    assert sorted(item.name for item in database.parent.iterdir()) == before_files
    assert not (database.parent / (database.name + "-wal")).exists()
    assert not (database.parent / (database.name + "-shm")).exists()


def test_a_missing_database_is_not_created_on_the_way_out(tmp_path, capsys):
    target = tmp_path / "nested" / "nope.sqlite3"
    code = main(["--json", "coverage", "--database", str(target), "--run", "x"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 3
    assert payload["code"] == "database_missing"
    assert not target.exists()
    assert not target.parent.exists()
    assert list(tmp_path.iterdir()) == []


def test_the_reader_refuses_a_database_without_the_crawl_tables(tmp_path):
    from core.crawl_state_reader import CrawlStateUnreadable, ReadOnlyCrawlState

    other = tmp_path / "other.sqlite3"
    connection = sqlite3.connect(other)
    connection.execute("CREATE TABLE unrelated (x INTEGER)")
    connection.commit()
    connection.close()
    with pytest.raises(CrawlStateUnreadable) as caught:
        ReadOnlyCrawlState(other)
    assert caught.value.code == "schema_missing"


def test_the_reader_answers_the_three_questions_it_promises(tmp_path):
    from core.crawl_state_reader import CrawlStateUnreadable, ReadOnlyCrawlState

    database, run_id = _crawl_db(tmp_path)
    reader = ReadOnlyCrawlState(database)
    try:
        latest = reader.latest_run("acct-synthetic", "follow-default")
        assert latest is not None and latest["run_id"] == run_id
        assert reader.get_run(run_id)["status"] == "completed"
        assert [row["artist_id"] for row in reader.list_artists(run_id)] == ["1000", "1001"]
        assert reader.latest_run("acct-synthetic", "other-config") is None
        with pytest.raises(CrawlStateUnreadable) as caught:
            reader.get_run("no-such-run")
        assert caught.value.code == "run_unknown"
    finally:
        reader.close()


class _CompleteSummary:
    """A stand-in for a summary the crawl store cannot produce yet."""

    def __init__(self, payload):
        self._payload = payload

    def as_dict(self):
        return dict(self._payload)


def test_a_complete_summary_reaches_exit_zero_on_both_outputs(tmp_path, capsys, monkeypatch):
    """The engine tops out at unknown from this source, so the 0 branch of the
    command was never executed.  This pins the plumbing: given a complete
    summary, both outputs say the conclusion holds and the code is 0.
    """
    import core.report_commands as module
    from core.follow_coverage_counts import COUNT_KEYS

    counts = {key: 0 for key in COUNT_KEYS}
    counts["artists_checked"] = counts["artists_confirmed"] = 1
    payload = {
        "scope": {"kind": "follow_run"},
        "overall": "complete",
        "overall_reason": "",
        "enumeration": {"state": "complete", "reason": ""},
        "decision": {"state": "applied", "reason": ""},
        "delivery": {"state": "complete", "reason": ""},
        "page_end_evidence": {"state": "complete", "reason": ""},
        "counts": counts,
        "denominators": {"artists_total": 1, "artworks_total": None},
        "consistent": True,
        "reasons": [],
        "inconsistent_reasons": [],
        "stop_reason": "",
        "notes": [],
    }
    monkeypatch.setattr(module, "build_coverage", lambda *a, **k: _CompleteSummary(payload))
    database, run_id = _crawl_db(tmp_path)

    code = main(["--json", "coverage", "--database", str(database), "--run", run_id])
    body_json = json.loads(capsys.readouterr().out)
    assert code == body_json["exit_code"] == 0
    assert body_json["coverage"]["overall"] == "complete"

    text_code = main(["coverage", "--database", str(database), "--run", run_id])
    printed = capsys.readouterr().out
    assert text_code == 0
    assert "结论成立" in printed
