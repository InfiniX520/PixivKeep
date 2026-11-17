"""Read-only access to the crawl state store.

The writer class (`core.follow_crawl_state.FollowCrawlStateRepository`) cannot
be reused for reading: its connection step creates the directory, opens
read-write, switches `journal_mode=WAL` and runs the schema script.  A reporting
command that used it would quietly create or modify the very database it is
supposed to describe, so this module opens the file itself with SQLite's
`mode=ro` and sets `query_only`.

Rules this module keeps, and the tests check:

* no `mkdir`, no `PRAGMA journal_mode`, no DDL, no `commit()`;
* a database that cannot be read raises a typed error - it is never reported as
  "there are no runs", because a fault is not an empty result;
* rows come back as plain mappings with the same column names the writer's
  SELECTs use.  The reporting path only needs facts (run id, status, artists,
  works seen), so re-deriving the writer's dataclasses here would duplicate its
  decoding rules for no gain.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

#: The tables this reader depends on.
REQUIRED_TABLES = ("crawl_runs", "artist_progress")


class CrawlStateUnreadable(ValueError):
    """The store could not be read.  Never a conclusion about the run."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = str(code)
        self.detail = str(detail)
        super().__init__(self.code if not detail else "%s: %s" % (self.code, detail))


def _uri(database: Path) -> str:
    """A read-only URI.  The path is quoted so spaces and # survive."""
    return "file:%s?mode=ro" % quote(str(database))


class ReadOnlyCrawlState:
    """Three SELECTs over a crawl state store, opened strictly read-only."""

    def __init__(self, database: str | Path, *, connection=None) -> None:
        self.database_path = Path(database)
        if connection is None and not self.database_path.is_file():
            raise CrawlStateUnreadable("database_missing", str(self.database_path))
        if connection is not None:
            # A caller that already decided *how* to read (the coverage command
            # uses core.follow_facts_v2.read_only_connection, which may read a
            # private copy) hands the connection in.  Opening a second connection
            # here would undo that decision: on a quiescent WAL store SQLite would
            # create its -shm/-wal beside the user's file (measured).
            self._connection = connection
            self._validate_schema()
            return
        try:
            connection = sqlite3.connect(
                _uri(self.database_path), uri=True, timeout=5.0
            )
        except sqlite3.Error as error:
            raise CrawlStateUnreadable(
                "database_unreadable", "%s (%s)" % (self.database_path, error)
            ) from None
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only=1")
        except sqlite3.Error as error:
            connection.close()
            raise CrawlStateUnreadable("database_unreadable", str(error)) from None
        self._connection = connection
        self._validate_schema()

    def _validate_schema(self) -> None:
        """The two tables every read below needs, or a typed refusal."""
        try:
            tables = {
                str(row[0])
                for row in self._connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        except sqlite3.Error as error:
            self._connection.close()
            raise CrawlStateUnreadable("database_unreadable", str(error)) from None
        missing = [name for name in REQUIRED_TABLES if name not in tables]
        if missing:
            self._connection.close()
            raise CrawlStateUnreadable(
                "schema_missing",
                "%s (missing %s)" % (self.database_path, ", ".join(missing)),
            )

    # -- reads -------------------------------------------------------------

    def _rows(self, query: str, parameters: tuple) -> list[dict[str, Any]]:
        try:
            return [
                dict(row) for row in self._connection.execute(query, parameters).fetchall()
            ]
        except sqlite3.Error as error:
            raise CrawlStateUnreadable("database_unreadable", str(error)) from None

    def latest_run(self, account_id: str, config_key: str) -> Mapping[str, Any] | None:
        """The most recently updated run for one account and configuration."""
        rows = self._rows(
            "SELECT * FROM crawl_runs WHERE account_id=? AND config_key=?"
            " ORDER BY updated_at DESC, created_at DESC LIMIT 1",
            (str(account_id), str(config_key)),
        )
        return rows[0] if rows else None

    def get_run(self, run_id: str) -> Mapping[str, Any]:
        """One run by id; an unknown id is an error, not an empty run."""
        rows = self._rows("SELECT * FROM crawl_runs WHERE run_id=?", (str(run_id),))
        if not rows:
            raise CrawlStateUnreadable("run_unknown", str(run_id))
        return rows[0]

    def list_artists(self, run_id: str) -> list[dict[str, Any]]:
        """Every artist row of one run, in the run's own ordinal order."""
        return self._rows(
            "SELECT * FROM artist_progress WHERE run_id=? ORDER BY ordinal", (str(run_id),)
        )

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "ReadOnlyCrawlState":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
