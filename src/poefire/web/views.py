"""JSON views for the UI: runs with loot values, the current session, the live run."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from poefire.pricing.book import PriceBook
from poefire.pricing.valuation import Valuation, value_deltas
from poefire.store import Store
from poefire.tracker import Run, RunStatus

type Json = dict[str, Any]


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _per_hour(value: float, seconds: float) -> float | None:
    return value * 3600 / seconds if seconds >= 60 else None


@dataclass
class RunRow:
    run: Run
    value: Valuation
    tracked: bool
    active_seconds: float

    def to_json(self, chaos_per_divine: float | None) -> Json:
        r = self.run
        return {
            "id": r.id,
            "map_name": r.map_name,
            "area_id": r.area_id,
            "area_level": r.area_level,
            "started_at": _iso(r.started_at),
            "ended_at": _iso(r.ended_at),
            "status": r.status.value,
            "active_seconds": round(self.active_seconds, 1),
            "deaths": r.deaths,
            "loot_tracked": self.tracked,
            "chaos": round(self.value.chaos, 1) if self.tracked else None,
            "divine": _round(self.value.divine(chaos_per_divine)) if self.tracked else None,
            "chaos_per_hour": _round(_per_hour(self.value.chaos, self.active_seconds), 0)
            if self.tracked
            else None,
            "unpriced": self.value.unpriced,
        }


def _round(v: float | None, nd: int = 2) -> float | None:
    return None if v is None else round(v, nd)


def run_rows(store: Store, book: PriceBook, runs: Sequence[Run], now: datetime) -> list[RunRow]:
    tracked = store.runs_with_loot_data([r.id for r in runs])
    return [
        RunRow(
            r,
            value_deltas(store.run_loot(r.id), book),
            r.id in tracked,
            r.active_seconds(now if r.status is RunStatus.ACTIVE else None),
        )
        for r in runs
    ]


def current_session(rows: Sequence[RunRow], gap: timedelta) -> list[RunRow]:
    """The latest run plus every earlier run that started within `gap` of the previous run's
    last activity. `rows` newest first."""
    session: list[RunRow] = []
    for row in rows:
        if session:
            newer = session[-1].run
            if newer.started_at - row.run.last_activity > gap:
                break
        session.append(row)
    return session


def session_summary(rows: Sequence[RunRow], chaos_per_divine: float | None, now: datetime) -> Json:
    if not rows:
        return {"runs": 0}
    active = sum(r.active_seconds for r in rows)
    tracked = [r for r in rows if r.tracked]
    chaos = sum(r.value.chaos for r in tracked)
    tracked_seconds = sum(r.active_seconds for r in tracked)
    first = min(r.run.started_at for r in rows)
    newest = rows[0].run
    end = now if newest.status is not RunStatus.FINISHED else (newest.ended_at or now)
    wall = max((end - first).total_seconds(), 0.0)
    best = max(tracked, key=lambda r: r.value.chaos, default=None)
    return {
        "runs": len(rows),
        "started_at": _iso(first),
        "active_seconds": round(active),
        "wall_seconds": round(wall),
        "avg_run_seconds": round(active / len(rows)),
        "deaths": sum(r.run.deaths for r in rows),
        "loot_tracked_runs": len(tracked),
        "chaos": round(chaos, 1) if tracked else None,
        "divine": _round(chaos / chaos_per_divine) if tracked and chaos_per_divine else None,
        # per hour of map time (tracked runs), and per hour played incl. hideout time
        "chaos_per_hour": _round(_per_hour(chaos, tracked_seconds), 0) if tracked else None,
        "chaos_per_hour_wall": _round(_per_hour(chaos, wall), 0) if tracked else None,
        "divine_per_hour": _round(_per_hour(chaos / chaos_per_divine, tracked_seconds))
        if tracked and chaos_per_divine
        else None,
        "best_run": {"id": best.run.id, "map_name": best.run.map_name,
                     "chaos": round(best.value.chaos, 1)} if best else None,
    }  # fmt: skip


def valuation_json(v: Valuation, chaos_per_divine: float | None) -> Json:
    return {
        "chaos": round(v.chaos, 1),
        "divine": _round(v.divine(chaos_per_divine)),
        "unpriced": v.unpriced,
        "items": [
            {
                "name": i.name,
                "quantity": i.quantity,
                "chaos_each": _round(i.chaos_each),
                "chaos_total": _round(i.chaos_total, 1),
                "icon": i.icon,
                "source": i.source,
            }
            for i in v.items
        ],
    }


def run_detail(store: Store, book: PriceBook, run: Run, now: datetime) -> Json:
    (row,) = run_rows(store, book, [run], now)
    cpd = book.chaos_per_divine
    return {
        **row.to_json(cpd),
        "segments": [[_iso(s.start), _iso(s.end)] for s in run.segments],
        "loot": valuation_json(row.value, cpd),
        "before": valuation_json(value_deltas(store.run_loot(run.id, "between"), book), cpd),
    }
