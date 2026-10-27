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
from poefire.loot.diff import ItemDelta
from poefire.loot.items import ItemEntry
from poefire.loot.snapshot import Snapshot
from poefire.tracker import Run, RunStatus, Segment

SCHEMA_VERSION = 2

_SCHEMA_V1 = """
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

_SCHEMA_V2 = """
CREATE TABLE snapshots (
    id         INTEGER PRIMARY KEY,
    taken_at   TEXT NOT NULL,
    trigger    TEXT NOT NULL,
    run_id     TEXT,
    complete   INTEGER NOT NULL,
    item_count INTEGER NOT NULL,
    error      TEXT
);

-- Only the latest complete snapshots keep their items (needed for the next diff).
CREATE TABLE snapshot_items (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    item_id     TEXT,
    key         TEXT NOT NULL,
    name        TEXT NOT NULL,
    frame_type  INTEGER NOT NULL,
    quantity    INTEGER NOT NULL,
    stackable   INTEGER NOT NULL,
    source      TEXT NOT NULL,
    raw         TEXT NOT NULL
);
CREATE INDEX snapshot_items_snapshot ON snapshot_items(snapshot_id);

-- kind: 'run' (loot of run_id), 'between' (hideout activity before run_id), 'manual'
CREATE TABLE loot (
    id            INTEGER PRIMARY KEY,
    run_id        TEXT,
    kind          TEXT NOT NULL,
    from_snapshot INTEGER NOT NULL REFERENCES snapshots(id),
    to_snapshot   INTEGER NOT NULL REFERENCES snapshots(id),
    key           TEXT NOT NULL,
    name          TEXT NOT NULL,
    frame_type    INTEGER NOT NULL,
    quantity      INTEGER NOT NULL,
    sample        TEXT NOT NULL
);
CREATE INDEX loot_run ON loot(run_id);
"""

_MIGRATIONS = (_SCHEMA_V1, _SCHEMA_V2)  # index i upgrades schema version i -> i + 1


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
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"database schema v{version} is newer than this app")
        for v in range(version, SCHEMA_VERSION):
            # executescript() would commit implicitly; run statements inside one transaction.
            with self.transaction():
                for stmt in _MIGRATIONS[v].split(";"):
                    if stmt.strip():
                        self.conn.execute(stmt)
                self.conn.execute(f"PRAGMA user_version = {v + 1}")

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

    # --- snapshots and loot ---

    def add_snapshot(self, snap: Snapshot, trigger: str, run_id: str | None) -> int:
        cur = self.conn.execute(
            "INSERT INTO snapshots (taken_at, trigger, run_id, complete, item_count, error) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (snap.taken_at.isoformat(), trigger, run_id, int(snap.complete),
             len(snap.items), snap.error),
        )  # fmt: skip
        snapshot_id = cur.lastrowid
        assert snapshot_id is not None
        if snap.complete:
            self.conn.executemany(
                "INSERT INTO snapshot_items (snapshot_id, item_id, key, name, frame_type, "
                "quantity, stackable, source, raw) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (snapshot_id, e.item_id, e.key, e.name, e.frame_type, e.quantity,
                     int(e.stackable), e.source, json.dumps(e.raw, ensure_ascii=False))
                    for e in snap.items
                ],
            )  # fmt: skip
        return snapshot_id

    def last_complete_snapshot(self) -> tuple[int, datetime, list[ItemEntry]] | None:
        row = self.conn.execute(
            "SELECT id, taken_at FROM snapshots WHERE complete = 1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        items = [
            ItemEntry(
                item_id=r["item_id"], key=r["key"], name=r["name"], frame_type=r["frame_type"],
                quantity=r["quantity"], stackable=bool(r["stackable"]), source=r["source"],
                raw=json.loads(r["raw"]),
            )
            for r in self.conn.execute(
                "SELECT * FROM snapshot_items WHERE snapshot_id = ?", (row["id"],)
            )
        ]  # fmt: skip
        return row["id"], datetime.fromisoformat(row["taken_at"]), items

    def prune_snapshot_items(self, keep: int = 2) -> None:
        self.conn.execute(
            "DELETE FROM snapshot_items WHERE snapshot_id NOT IN "
            "(SELECT id FROM snapshots WHERE complete = 1 ORDER BY id DESC LIMIT ?)",
            (keep,),
        )

    def add_loot(
        self,
        run_id: str | None,
        kind: str,
        from_snapshot: int,
        to_snapshot: int,
        deltas: list[ItemDelta],
    ) -> None:
        self.conn.executemany(
            "INSERT INTO loot (run_id, kind, from_snapshot, to_snapshot, key, name, frame_type, "
            "quantity, sample) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (run_id, kind, from_snapshot, to_snapshot, d.key, d.name, d.frame_type,
                 d.quantity, json.dumps(d.sample, ensure_ascii=False))
                for d in deltas
            ],
        )  # fmt: skip

    def run_loot(self, run_id: str, kind: str = "run") -> list[ItemDelta]:
        """Net loot of a run, summed over all its hideout returns."""
        rows = self.conn.execute(
            "SELECT key, name, frame_type, SUM(quantity) AS quantity, "
            "(SELECT l2.sample FROM loot l2 WHERE l2.run_id = loot.run_id AND l2.kind = loot.kind "
            " AND l2.key = loot.key ORDER BY l2.id DESC LIMIT 1) AS sample "
            "FROM loot WHERE run_id = ? AND kind = ? GROUP BY key HAVING SUM(quantity) != 0 "
            "ORDER BY SUM(quantity) DESC, name",
            (run_id, kind),
        ).fetchall()
        return [
            ItemDelta(r["key"], r["name"], r["frame_type"], r["quantity"], json.loads(r["sample"]))
            for r in rows
        ]

    def list_snapshots(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM snapshots ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

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
