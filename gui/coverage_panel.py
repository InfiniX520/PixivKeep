#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only coverage panel: what one follow crawl actually covered.

The panel is presentation only.  It never opens SQLite, never touches the file
system, never hashes bytes, never starts a thread and never talks to the
network: one action leaves through the callback, the controller reads on the
query owner, and the finished result comes back through set_result.

The body is the engine's own sentences (coverage_lines).  The panel does not
assemble a second set of words, so "未知" can only come from the engine: a
widget that decided on its own to print 0 would be inventing a number nobody
measured (see docs/optimization/2026-11-15/FIX_CONTRACTS.md §2/§3).

Every method must be called on the Tk owner thread.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from gui.collection_theme import scale_px

# The four states of one read, copied from the verification panel on purpose:
# a failed read is its own state, not an empty result.
LOAD_STATES = ("idle", "loading", "ready", "error")

IDLE_TEXT = "尚未读取：还没有可显示的关注运行。"
LOADING_TEXT = "正在读取覆盖摘要…"
EMPTY_TEXT = "这次运行没有可显示的摘要行。"
ERROR_TEXT = "读取没有完成；原因：%s"
HEADER_TEXT = "运行 %s 的覆盖摘要"
HEADER_PLAIN = "覆盖摘要"


# -- pure view states (tested without a Tk root) ---------------------------


def idle_view() -> dict:
    return {"state": "idle", "header": HEADER_PLAIN, "text": IDLE_TEXT, "lines": ()}


def loading_view() -> dict:
    return {"state": "loading", "header": HEADER_PLAIN, "text": LOADING_TEXT, "lines": ()}


def error_view(reason: object) -> dict:
    detail = str(reason or "").strip() or "未知原因"
    return {
        "state": "error",
        "header": HEADER_PLAIN,
        "text": ERROR_TEXT % (detail,),
        "lines": (),
    }


def ready_view(lines, *, run_id: object = "") -> dict:
    """A finished read: the engine's lines, or an explicit "nothing here"."""
    body = tuple(str(line) for line in (lines or ()))
    header = HEADER_TEXT % (str(run_id),) if str(run_id or "") else HEADER_PLAIN
    if not body:
        return {"state": "ready", "header": header, "text": EMPTY_TEXT, "lines": ()}
    return {"state": "ready", "header": header, "text": "", "lines": body}


class CoveragePanel(ttk.Frame):
    """The panel: one run id, one read, one read-only text body."""

    def __init__(self, master: tk.Misc, on_action=None) -> None:
        super().__init__(master, style="CollectionPage.TFrame")
        self._on_action = on_action if callable(on_action) else None
        self._view = idle_view()
        self._disposed = False
        self._build()

    # -- construction ----------------------------------------------------

    def _build(self) -> None:
        s = lambda value: scale_px(self, value)  # noqa: E731
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        bar = ttk.Frame(self, style="CollectionPage.TFrame")
        bar.grid(row=0, column=0, sticky="ew", pady=(s(8), s(6)))
        bar.columnconfigure(1, weight=1)
        self._run_label = ttk.Label(bar, text="运行 ID", style="CollectionMuted.TLabel")
        self._run_label.grid(row=0, column=0, sticky="w")
        self._run_entry = ttk.Entry(bar)
        self._run_entry.grid(row=0, column=1, sticky="ew", padx=(s(8), s(8)))
        self._load_button = ttk.Button(bar, text="读取", command=self._request_load)
        self._load_button.grid(row=0, column=2, sticky="e")
        self._header_label = ttk.Label(self, text=HEADER_PLAIN, style="CollectionMuted.TLabel")
        self._header_label.grid(row=1, column=0, sticky="w", pady=(s(4), s(2)))
        self._body = tk.Text(
            self,
            height=18,
            wrap="word",
            state="disabled",
            relief="flat",
            borderwidth=0,
            background=self._palette_colour(),
        )
        self._body.grid(row=2, column=0, sticky="nsew")
        self._render()

    def _palette_colour(self) -> str:
        try:
            return self.winfo_toplevel()._palette["page"]
        except (AttributeError, KeyError, tk.TclError):  # pragma: no cover - headless
            return "white"

    # -- actions ---------------------------------------------------------

    def _request_load(self) -> None:
        """Ask the controller to read one run; never read here."""
        if self._disposed:
            return
        run_id = ""
        try:
            run_id = self._run_entry.get().strip()
        except tk.TclError:  # pragma: no cover - widget going away
            return
        if not run_id:
            self.set_error("还没有填运行 ID")
            return
        if self._on_action is not None:
            self._on_action("coverage_load", {"run_id": run_id})

    # -- state setters ---------------------------------------------------

    @property
    def state(self) -> str:
        return str(self._view["state"])

    @property
    def view(self) -> dict:
        """The current view state, for tests and for the page builder."""
        return dict(self._view)

    def set_idle(self) -> None:
        self._apply(idle_view())

    def set_loading(self) -> None:
        """A new read starts: the old body goes before the request does."""
        self._apply(loading_view())

    def set_error(self, reason: object) -> None:
        self._apply(error_view(reason))

    def set_result(self, lines, *, run_id: object = "") -> None:
        self._apply(ready_view(lines, run_id=run_id))

    def dispose(self) -> None:
        """Release a half-built panel (the page may be rebuilt)."""
        if self._disposed:
            return
        self._disposed = True
        self._on_action = None
        for name in ("_body", "_run_label", "_run_entry", "_load_button", "_header_label"):
            widget = getattr(self, name, None)
            if widget is not None:
                try:
                    widget.destroy()
                except tk.TclError:  # pragma: no cover - already gone
                    pass

    # -- rendering -------------------------------------------------------

    def _apply(self, view: dict) -> None:
        self._view = view
        if self._disposed:
            return
        self._render()

    def _render(self) -> None:
        try:
            self._header_label.configure(text=self._view["header"])
            body = self._body
            body.configure(state="normal")
            body.delete("1.0", "end")
            text = self._view["text"]
            lines = self._view["lines"]
            if text:
                body.insert("1.0", text)
            elif lines:
                body.insert("1.0", "\n".join(lines))
            body.configure(state="disabled")
        except tk.TclError:  # pragma: no cover - widget going away
            pass
