"""SQLite persistence."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from poefire.events import LogEvent, event_kind, event_payload_json
from poefire.tracker import Run, RunStatus, Segment

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE runs (
    id             TEXT PRIMARY KEY,
    area_id        TEXT NOT NULL,
    map_name       TEXT NOT NULL,
    area_level     INTEGER NOT NULL,
    seed           INTEGER NOT NULL,
    started_at     TEXT NOT NULL,
    ended_at       TEXT,
    status         TEXT NOT NULL,
    active_seconds REAL NOT NULL,
    deaths         INTEGER NOT NULL
);
CREATE INDEX runs_started_at ON runs(started_at);

CREATE TABLE run_segments (
    run_id     TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    idx        INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    ended_at   TEXT,
    PRIMARY KEY (run_id, idx)
);

CREATE TABLE events (
    id      INTEGER PRIMARY KEY,
    ts      TEXT NOT NULL,
    kind    TEXT NOT NULL,
    run_id  TEXT,
    payload TEXT NOT NULL
);
CREATE INDEX events_ts ON events(ts);
CREATE INDEX events_run ON events(run_id);

CREATE TABLE kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _dt(v: datetime | None) -> str | None:
    return v.isoformat() if v else None


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = path
        if isinstance(path, Path):
            path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if str(path) != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def _migrate(self) -> None:
        (version,) = self.conn.execute("PRAGMA user_version").fetchone()
        if version == 0:
            # executescript() would commit implicitly; run statements inside one transaction.
            with self.transaction():
                for stmt in _SCHEMA.split(";"):
                    if stmt.strip():
                        self.conn.execute(stmt)
                self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        elif version > SCHEMA_VERSION:
            raise RuntimeError(f"database schema v{version} is newer than this app")

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.conn.execute("BEGIN")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- runs ---

    def upsert_run(self, run: Run) -> None:
        self.conn.execute(
            """
            INSERT INTO runs (id, area_id, map_name, area_level, seed, started_at, ended_at,
                              status, active_seconds, deaths)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                ended_at = excluded.ended_at, status = excluded.status,
                active_seconds = excluded.active_seconds, deaths = excluded.deaths
            """,
            (
                run.id, run.area_id, run.map_name, run.area_level, run.seed,
                _dt(run.started_at), _dt(run.ended_at), run.status.value,
                run.active_seconds(), run.deaths,
            ),
        )  # fmt: skip
        self.conn.execute("DELETE FROM run_segments WHERE run_id = ?", (run.id,))
        self.conn.executemany(
            "INSERT INTO run_segments (run_id, idx, started_at, ended_at) VALUES (?, ?, ?, ?)",
            [(run.id, i, _dt(s.start), _dt(s.end)) for i, s in enumerate(run.segments)],
        )

    def get_run(self, run_id: str) -> Run | None:
        row = self.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return self._row_to_run(row) if row else None

    def list_runs(self, limit: int = 100, offset: int = 0) -> list[Run]:
        rows = self.conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC, rowid DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [self._row_to_run(r) for r in rows]

    def _row_to_run(self, row: sqlite3.Row) -> Run:
        segs = self.conn.execute(
            "SELECT started_at, ended_at FROM run_segments WHERE run_id = ? ORDER BY idx",
            (row["id"],),
        ).fetchall()
        return Run(
            id=row["id"],
            area_id=row["area_id"],
            map_name=row["map_name"],
            area_level=row["area_level"],
            seed=row["seed"],
            started_at=datetime.fromisoformat(row["started_at"]),
            status=RunStatus(row["status"]),
            deaths=row["deaths"],
            segments=[
                Segment(
                    datetime.fromisoformat(s["started_at"]),
                    datetime.fromisoformat(s["ended_at"]) if s["ended_at"] else None,
                )
                for s in segs
            ],
            ended_at=datetime.fromisoformat(row["ended_at"]) if row["ended_at"] else None,
        )

    # --- events ---

    def add_event(self, event: LogEvent, run_id: str | None) -> None:
        self.conn.execute(
            "INSERT INTO events (ts, kind, run_id, payload) VALUES (?, ?, ?, ?)",
            (event.ts.isoformat(), event_kind(event), run_id, event_payload_json(event)),
        )

    def list_events(self, kind: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        sql = "SELECT ts, kind, run_id, payload FROM events"
        args: tuple[Any, ...] = ()
        if kind:
            sql += " WHERE kind = ?"
            args = (kind,)
        sql += " ORDER BY id DESC LIMIT ?"
        rows = self.conn.execute(sql, (*args, limit)).fetchall()
        return [
            {"ts": r["ts"], "kind": r["kind"], "run_id": r["run_id"], **json.loads(r["payload"])}
            for r in rows
        ]

    # --- key/value (log cursor, tracker state) ---

    def get_json(self, key: str) -> Any:
        row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else None

    def set_json(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )
