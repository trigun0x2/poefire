"""SQLite persistence."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from poefire.campaign import CampaignRun, Split
from poefire.events import LogEvent, event_kind, event_payload_json
from poefire.loot.diff import ItemDelta
from poefire.loot.items import ItemEntry, is_tracked
from poefire.loot.snapshot import Snapshot
from poefire.pricing.ninja import PriceLine
from poefire.tracker import Run, RunStatus, Segment

SCHEMA_VERSION = 5

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

_SCHEMA_V3 = """
-- poe.ninja responses, reduced to pricing fields. One row per league and price table.
CREATE TABLE price_cache (
    league     TEXT NOT NULL,
    source     TEXT NOT NULL,
    type       TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    lines      TEXT NOT NULL,
    PRIMARY KEY (league, source, type)
);
"""

_SCHEMA_V4 = """
-- Total value of each complete snapshot at the prices of that moment (the Overview graph).
CREATE TABLE snapshot_values (
    snapshot_id      INTEGER PRIMARY KEY REFERENCES snapshots(id) ON DELETE CASCADE,
    taken_at         TEXT NOT NULL,
    league           TEXT NOT NULL,
    chaos            REAL NOT NULL,
    chaos_per_divine REAL,
    unpriced         INTEGER NOT NULL
);
CREATE INDEX snapshot_values_league ON snapshot_values(league, taken_at);
"""

_SCHEMA_V5 = """
-- Campaign runs (poefire.campaign). Splits are a JSON list, read with the run.
CREATE TABLE campaign_runs (
    id         INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    ended_at   TEXT,
    status     TEXT NOT NULL,
    character  TEXT,
    class      TEXT,
    level      INTEGER,
    deaths     INTEGER NOT NULL DEFAULT 0,
    act        INTEGER,
    load_ms    INTEGER NOT NULL DEFAULT 0,
    notes      TEXT NOT NULL DEFAULT '',
    splits     TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX campaign_runs_started ON campaign_runs(started_at);
"""

_MIGRATIONS = (
    _SCHEMA_V1,
    _SCHEMA_V2,
    _SCHEMA_V3,
    _SCHEMA_V4,
    _SCHEMA_V5,
)  # index i upgrades schema version i -> i + 1


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

    def latest_snapshot_items(self) -> tuple[datetime, list[ItemEntry]] | None:
        latest = self.last_complete_snapshot()
        return None if latest is None else (latest[1], latest[2])

    def add_snapshot_value(
        self,
        snapshot_id: int,
        taken_at: datetime,
        league: str,
        chaos: float,
        chaos_per_divine: float | None,
        unpriced: int,
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO snapshot_values "
            "(snapshot_id, taken_at, league, chaos, chaos_per_divine, unpriced) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (snapshot_id, taken_at.isoformat(), league, chaos, chaos_per_divine, unpriced),
        )

    def value_history(
        self, league: str, since: datetime | None = None
    ) -> list[tuple[datetime, float, float | None]]:
        """(taken_at, chaos, chaos_per_divine) for the league's snapshots, oldest first."""
        sql = "SELECT taken_at, chaos, chaos_per_divine FROM snapshot_values WHERE league = ?"
        args: list[Any] = [league]
        if since is not None:
            sql += " AND taken_at >= ?"
            args.append(since.isoformat())
        rows = self.conn.execute(sql + " ORDER BY taken_at", args).fetchall()
        return [(datetime.fromisoformat(r[0]), r[1], r[2]) for r in rows]

    def latest_value(self, league: str) -> tuple[datetime, float, float | None] | None:
        row = self.conn.execute(
            "SELECT taken_at, chaos, chaos_per_divine FROM snapshot_values WHERE league = ? "
            "ORDER BY taken_at DESC LIMIT 1",
            (league,),
        ).fetchone()
        return None if row is None else (datetime.fromisoformat(row[0]), row[1], row[2])

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
        deltas = [
            ItemDelta(r["key"], r["name"], r["frame_type"], r["quantity"], json.loads(r["sample"]))
            for r in rows
        ]
        # Gear isn't counted (see is_tracked); also hides it in loot recorded before that rule.
        return [d for d in deltas if is_tracked(d.sample)]

    def runs_with_loot_data(self, run_ids: list[str]) -> set[str]:
        """Runs that have at least one complete return-to-hideout snapshot (so a loot value
        of 0 means "nothing dropped", not "not tracked")."""
        if not run_ids:
            return set()
        marks = ",".join("?" * len(run_ids))
        rows = self.conn.execute(
            f"SELECT DISTINCT run_id FROM snapshots WHERE complete = 1 "
            f"AND trigger = 'run_return' AND run_id IN ({marks})",
            run_ids,
        ).fetchall()
        return {r["run_id"] for r in rows}

    def list_snapshots(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM snapshots ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # --- price cache ---

    def save_prices(
        self, league: str, source: str, type_: str, fetched_at: float, lines: list[PriceLine]
    ) -> None:
        self.conn.execute(
            "INSERT INTO price_cache (league, source, type, fetched_at, lines) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(league, source, type) DO UPDATE SET "
            "fetched_at = excluded.fetched_at, lines = excluded.lines",
            (league, source, type_, fetched_at,
             json.dumps([line.to_json() for line in lines], ensure_ascii=False)),
        )  # fmt: skip

    def load_prices(self, league: str) -> list[tuple[str, str, float, list[PriceLine]]]:
        rows = self.conn.execute(
            "SELECT source, type, fetched_at, lines FROM price_cache WHERE league = ?", (league,)
        ).fetchall()
        return [
            (r["source"], r["type"], r["fetched_at"],
             [PriceLine.from_json(d) for d in json.loads(r["lines"])])
            for r in rows
        ]  # fmt: skip

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

    # --- campaign runs ---

    def save_campaign_run(self, run: CampaignRun) -> int:
        """Insert (no id yet) or update a campaign run; returns its id."""
        values = (
            _dt(run.started_at), _dt(run.ended_at), run.status, run.character, run.char_class,
            run.level, run.deaths, run.act, run.load_ms, run.notes,
            json.dumps([s.to_json() for s in run.splits]),
        )  # fmt: skip
        if run.id is None:
            cur = self.conn.execute(
                "INSERT INTO campaign_runs (started_at, ended_at, status, character, class, "
                "level, deaths, act, load_ms, notes, splits) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                values,
            )
            run.id = int(cur.lastrowid or 0)
        else:
            self.conn.execute(
                "UPDATE campaign_runs SET started_at=?, ended_at=?, status=?, character=?, "
                "class=?, level=?, deaths=?, act=?, load_ms=?, notes=?, splits=? WHERE id=?",
                (*values, run.id),
            )
        return run.id

    @staticmethod
    def _campaign_run(row: sqlite3.Row) -> CampaignRun:
        return CampaignRun(
            id=row["id"],
            started_at=datetime.fromisoformat(row["started_at"]),
            ended_at=datetime.fromisoformat(row["ended_at"]) if row["ended_at"] else None,
            status=row["status"], character=row["character"], char_class=row["class"],
            level=row["level"], deaths=row["deaths"], act=row["act"], load_ms=row["load_ms"],
            notes=row["notes"], splits=[Split.from_json(s) for s in json.loads(row["splits"])],
        )  # fmt: skip

    def list_campaign_runs(self, limit: int = 200) -> list[CampaignRun]:
        rows = self.conn.execute(
            "SELECT * FROM campaign_runs ORDER BY started_at DESC, id DESC LIMIT ?", (limit,)
        )
        return [self._campaign_run(r) for r in rows]

    def get_campaign_run(self, run_id: int) -> CampaignRun | None:
        row = self.conn.execute("SELECT * FROM campaign_runs WHERE id = ?", (run_id,)).fetchone()
        return self._campaign_run(row) if row else None

    def active_campaign_run(self) -> CampaignRun | None:
        row = self.conn.execute(
            "SELECT * FROM campaign_runs WHERE status = 'running' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return self._campaign_run(row) if row else None

    def set_campaign_notes(self, run_id: int, notes: str) -> bool:
        cur = self.conn.execute("UPDATE campaign_runs SET notes = ? WHERE id = ?", (notes, run_id))
        return cur.rowcount > 0

    def delete_campaign_run(self, run_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM campaign_runs WHERE id = ?", (run_id,))
        return cur.rowcount > 0
