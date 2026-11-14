"""The coverage page mounts; one Tk root for the whole module on purpose.

Measured on this host: the second tk.Tk() in one process already fails
intermittently with a Tcl initialisation error ("invalid command name
tcl_findLibrary", or "Can't find a usable tk.tcl"), while a single root used by
every case is stable.  So the root is module-scoped and each case only drives
widgets; the acceptance driver still runs each Tk module in its own process.
"""
from __future__ import annotations

import tkinter as tk

import pytest

from gui.collection_view import PAGE_KEYS, PAGE_LABELS, CollectionView


@pytest.fixture(scope="module")
def screen():
    root = tk.Tk()
    root.withdraw()
    view = CollectionView(root, lambda action, payload: None)
    root.update_idletasks()
    try:
        yield root, view
    finally:
        root.destroy()


def test_the_coverage_page_sits_between_verify_and_settings(screen):
    root, view = screen
    assert "coverage" in PAGE_KEYS
    assert PAGE_KEYS.index("coverage") == PAGE_KEYS.index("verify") + 1
    assert PAGE_LABELS["coverage"] == "覆盖"
    assert "coverage" in view._pages
    assert view.coverage_state() == "idle"


def test_the_panel_renders_a_finished_read_and_an_error(screen):
    root, view = screen
    view.set_coverage_result(
        ["本轮已检查 3 位画师。", "保存: 保存与核验未得证明（saving_not_measured）"],
        run_id="run-matrix",
    )
    root.update_idletasks()
    panel = view._coverage_panel
    assert panel.state == "ready"
    assert "保存与核验未得证明" in panel._body.get("1.0", "end")
    assert "run-matrix" in panel._header_label.cget("text")

    view.set_coverage_error("database_unreadable")
    root.update_idletasks()
    assert panel.state == "error"
    assert "database_unreadable" in panel._body.get("1.0", "end")


def test_adding_the_page_did_not_break_the_other_pages(screen):
    root, view = screen
    for key in ("follow", "tasks", "sources", "records", "verify", "settings"):
        assert key in view._pages
        assert view._pages[key].winfo_exists()
    for key in PAGE_KEYS:
        view._navigate(key)
        root.update_idletasks()
    assert view._active_page == "settings"
