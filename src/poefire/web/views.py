"""JSON views for the UI: runs with loot values, the current session, the live run."""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from poefire.loot.sales import SOLD_PREFIX
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


# time back in the hideout after a map counts towards that map, up to this (longer is a break)
HIDEOUT_CAP = timedelta(minutes=10)


def _hideout_after(rows: Sequence[RunRow]) -> dict[str, float]:
    """Seconds between each finished map's end and the next map's start (rows newest first),
    capped at HIDEOUT_CAP. The newest map has none yet."""
    out: dict[str, float] = {}
    for newer, row in itertools.pairwise(rows):
        end = row.run.ended_at
        if row.run.status is RunStatus.FINISHED and end is not None:
            gap = min(max(newer.run.started_at - end, timedelta(0)), HIDEOUT_CAP)
            out[row.run.id] = gap.total_seconds()
    return out


def per_hour(rows: Sequence[RunRow], chaos_per_divine: float | None) -> Json:
    """Loot per hour played: finished maps' loot over the time from the first map's start to
    the last map's end, hideout time included (breaks longer than HIDEOUT_CAP left out). Maps
    without loot data count with the session's average rate."""
    done = [r for r in rows if r.run.status is RunStatus.FINISHED]
    tracked = [r for r in done if r.tracked]
    active_tracked = sum(r.active_seconds for r in tracked)
    if not tracked or active_tracked <= 0:
        return {"chaos_per_hour": None, "divine_per_hour": None}
    gaps = _hideout_after(rows)
    played = sum(r.active_seconds + gaps.get(r.run.id, 0.0) for r in done)
    if played < 60:
        return {"chaos_per_hour": None, "divine_per_hour": None}
    chaos = sum(r.value.chaos for r in tracked)
    # the tracked maps' loot rate, applied to all the time played
    rate = chaos / active_tracked * sum(r.active_seconds for r in done) / played * 3600
    cpd = chaos_per_divine
    return {"chaos_per_hour": round(rate), "divine_per_hour": _round(rate / cpd) if cpd else None}


def map_stats(rows: Sequence[RunRow], chaos_per_divine: float | None) -> list[Json]:
    """Finished maps grouped by name: how many, average loot and time, and loot per hour
    (with the hideout time after each map). Most run first."""
    gaps = _hideout_after(rows)
    groups: dict[str, list[RunRow]] = {}
    for r in rows:
        if r.run.status is RunStatus.FINISHED:
            groups.setdefault(r.run.map_name, []).append(r)
    out: list[Json] = []
    for name, rs in groups.items():
        tracked = [r for r in rs if r.tracked]
        chaos = sum(r.value.chaos for r in tracked)
        played = sum(r.active_seconds + gaps.get(r.run.id, 0.0) for r in tracked)
        avg = chaos / len(tracked) if tracked else None
        rate = chaos / played * 3600 if tracked and played >= 60 else None
        cpd = chaos_per_divine
        out.append({
            "map_name": name,
            "maps": len(rs),
            "loot_tracked_maps": len(tracked),
            "avg_chaos": round(avg, 1) if avg is not None else None,
            "avg_divine": _round(avg / cpd) if avg is not None and cpd else None,
            "avg_seconds": round(sum(r.active_seconds for r in rs) / len(rs)),
            "deaths": sum(r.run.deaths for r in rs),
            "chaos_per_hour": round(rate) if rate is not None else None,
            "best_chaos": round(max(r.value.chaos for r in tracked), 1) if tracked else None,
        })  # fmt: skip
    out.sort(key=lambda m: (-int(m["maps"]), -float(m["chaos_per_hour"] or 0), m["map_name"]))
    return out


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
                "sold": i.key.startswith(SOLD_PREFIX),  # a trade sale's payment, taken out
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
