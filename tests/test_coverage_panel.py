"""The coverage panel's four states are pure functions; no Tk root needed.

The Tk half (page mounting, widget text) lives in test_coverage_page.py, which
is kept to three cases on purpose: on this host the third Tk root in one process
is where the known Tcl initialisation flake starts to show.
"""
from __future__ import annotations

import ast
from pathlib import Path

from gui import coverage_panel as panel

PANEL_SOURCE = Path(panel.__file__)


def test_idle_says_nothing_was_read_yet():
    view = panel.idle_view()
    assert view["state"] == "idle"
    assert "尚未读取" in view["text"]
    assert view["lines"] == ()


def test_loading_replaces_the_old_body_instead_of_keeping_it():
    view = panel.loading_view()
    assert view["state"] == "loading"
    assert view["lines"] == ()
    assert view["text"] == panel.LOADING_TEXT


def test_error_keeps_a_reason_and_shows_no_body():
    view = panel.error_view("database_unreadable")
    assert view["state"] == "error"
    assert "database_unreadable" in view["text"]
    assert view["lines"] == ()


def test_an_error_without_a_reason_still_says_unknown():
    for empty in (None, "", "   "):
        assert "未知原因" in panel.error_view(empty)["text"]


def test_ready_renders_the_engines_lines_verbatim():
    lines = ["本轮已检查 3 位画师。", "保存: 保存与核验未得证明（saving_not_measured）"]
    view = panel.ready_view(lines, run_id="run-1")
    assert view["state"] == "ready"
    assert view["lines"] == tuple(lines)
    assert view["text"] == ""


def test_unknown_numbers_are_never_rewritten_by_the_panel():
    """The panel must not turn 未知 into 0: the words come from the engine."""
    view = panel.ready_view(["新增 未知 个作品。"])
    assert list(view["lines"]) == ["新增 未知 个作品。"]
    assert "0" not in view["lines"][0]


def test_ready_with_no_lines_says_so_instead_of_showing_nothing():
    view = panel.ready_view([], run_id="run-1")
    assert view["state"] == "ready"
    assert view["lines"] == ()
    assert view["text"] == panel.EMPTY_TEXT


def test_the_header_names_the_run_only_when_it_is_known():
    assert "run-9" in panel.ready_view(["x"], run_id="run-9")["header"]
    assert panel.ready_view(["x"])["header"] == panel.HEADER_PLAIN
    assert panel.idle_view()["header"] == panel.HEADER_PLAIN


def test_every_state_is_one_of_the_four():
    for view in (panel.idle_view(), panel.loading_view(), panel.error_view("x"),
                 panel.ready_view(["x"]), panel.ready_view([])):
        assert view["state"] in panel.LOAD_STATES


def test_the_panel_module_cannot_do_io_or_hash():
    """A panel that opened SQLite or hashed bytes would defeat the whole split."""
    tree = ast.parse(PANEL_SOURCE.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden = {"hashlib", "sqlite3", "socket", "subprocess", "requests", "threading"}
    assert not (imported & forbidden)
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called.add(node.func.id)
    assert "open" not in called
