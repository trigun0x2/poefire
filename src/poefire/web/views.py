"""JSON views for the UI: runs with loot values, the current session, the live run."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from poefire.pricing.book import PriceBook, Quote
from poefire.pricing.valuation import Valuation, is_worth_showing, value_deltas
from poefire.store import Store
from poefire.tracker import Run, RunStatus
from poefire.web.cache import SETTLED_AFTER, BookCache

type Json = dict[str, Any]


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


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
            "unpriced": self.value.unpriced,
        }


def _round(v: float | None, nd: int = 2) -> float | None:
    return None if v is None else round(v, nd)


def _settled(run: Run, now: datetime) -> bool:
    """Finished long enough ago that its loot can't change any more."""
    end = run.ended_at or run.last_activity
    return run.status is RunStatus.FINISHED and now - end > SETTLED_AFTER


def run_rows(store: Store, book: PriceBook, runs: Sequence[Run], now: datetime,
             cache: BookCache | None = None) -> list[RunRow]:  # fmt: skip
    """Runs with their loot value. Loot is loaded for all runs at once; values of settled
    runs come from `cache` when given."""
    values: dict[str, tuple[Valuation, bool]] = {}
    if cache is not None:
        for r in runs:
            if (hit := cache.get(book, ("run", r.id))) is not None:
                values[r.id] = hit
    missing = [r for r in runs if r.id not in values]
    if missing:
        ids = [r.id for r in missing]
        tracked = store.runs_with_loot_data(ids)
        loot = store.runs_loot(ids)
        quotes: dict[str, Quote | None] = {}
        for r in missing:
            value = (value_deltas(loot.get(r.id, []), book, quotes), r.id in tracked)
            values[r.id] = value
            if cache is not None and _settled(r, now):
                cache.put(book, ("run", r.id), value)
    return [
        RunRow(r, *values[r.id], r.active_seconds(now if r.status is RunStatus.ACTIVE else None))
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
        "best_run": {"id": best.run.id, "map_name": best.run.map_name,
                     "chaos": round(best.value.chaos, 1)} if best else None,
    }  # fmt: skip


RECENT_MAPS = 10


def recent_maps(rows: Sequence[RunRow], chaos_per_divine: float | None,
                n: int = RECENT_MAPS) -> Json:  # fmt: skip
    """Averages over the last `n` finished maps that have loot data (any session). The map in
    progress is left out: its loot isn't known until you're back in the hideout."""
    done = [r for r in rows if r.run.status is RunStatus.FINISHED and r.tracked][:n]
    if not done:
        return {"maps": 0, "of": n}
    avg_chaos = sum(r.value.chaos for r in done) / len(done)
    return {
        "maps": len(done),
        "of": n,
        "avg_chaos": round(avg_chaos, 1),
        "avg_divine": _round(avg_chaos / chaos_per_divine) if chaos_per_divine else None,
        "avg_seconds": round(sum(r.active_seconds for r in done) / len(done)),
        "avg_deaths": round(sum(r.run.deaths for r in done) / len(done), 1),
    }


def last_map(rows: Sequence[RunRow], chaos_per_divine: float | None, top: int = 3) -> Json | None:
    """The most recent map you're not inside: time, deaths, loot and its top drops. A map you
    just returned from counts (it's paused until the next map starts), unless you go back in."""
    row = next((r for r in rows if r.run.status is not RunStatus.ACTIVE), None)
    if row is None:
        return None
    drops = [i for i in row.value.items
             if is_worth_showing(i.chaos_total) and (i.chaos_total or 0) > 0][:top]  # fmt: skip
    return {
        **row.to_json(chaos_per_divine),
        "top_items": [
            {"name": i.name, "quantity": i.quantity, "chaos_total": _round(i.chaos_total, 1),
             "icon": i.icon}
            for i in drops
        ],
    }  # fmt: skip


def valuation_json(v: Valuation, chaos_per_divine: float | None) -> Json:
    shown = [i for i in v.items if is_worth_showing(i.chaos_total)]
    return {
        "chaos": round(v.chaos, 1),
        "divine": _round(v.divine(chaos_per_divine)),
        "unpriced": v.unpriced,
        "hidden": len(v.items) - len(shown),  # no price, or worth ~0c
        "items": [
            {
                "name": i.name,
                "quantity": i.quantity,
                "chaos_each": _round(i.chaos_each),
                "chaos_total": _round(i.chaos_total, 1),
                "icon": i.icon,
                "source": i.source,
            }
            for i in shown
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
