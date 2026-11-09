"""Farming sessions: "New session" starts one; it lasts until the next one starts.

Runs belong to the session they started in. Runs from before the first explicit session form
an implicit session (id 0), so nothing recorded earlier is lost from the history.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from poefire.pricing.book import PriceBook
from poefire.store import Store
from poefire.tracker import Run, RunStatus
from poefire.web.cache import SETTLED_AFTER, BookCache
from poefire.web.views import Json, RunRow, _iso, _round, per_hour, run_rows

INIT_KEY = "sessions_initialized"


@dataclass(frozen=True)
class Span:
    id: int
    start: datetime | None  # None: from the beginning (the implicit first session)
    end: datetime | None  # the next session's start; None for the current one

    @property
    def current(self) -> bool:
        return self.end is None


def ensure_session(store: Store, gap: timedelta) -> None:
    """First start with sessions: open one where the current play session began (runs no
    more than `gap` apart, as before). With no runs yet, the implicit first session (from the
    beginning) stays the current one."""
    if store.get_json(INIT_KEY) or store.list_sessions():
        return
    start: datetime | None = None
    newer: Run | None = None
    for run in store.list_runs(limit=500):  # newest first
        if newer is not None and newer.started_at - run.last_activity > gap:
            break
        start, newer = run.started_at, run
    with store.transaction():
        if start is not None:
            store.add_session(start)
        store.set_json(INIT_KEY, True)


def spans(store: Store) -> list[Span]:
    """Oldest first; the last one is the current session."""
    rows = store.list_sessions()
    if not rows:
        return [Span(0, None, None)]
    out = [Span(0, None, rows[0][1])]
    for i, (sid, start) in enumerate(rows):
        out.append(Span(sid, start, rows[i + 1][1] if i + 1 < len(rows) else None))
    return out


def find(store: Store, session_id: int | None) -> Span | None:
    all_spans = spans(store)
    if session_id is None:
        return all_spans[-1]
    return next((s for s in all_spans if s.id == session_id), None)


def runs_of(store: Store, span: Span) -> list[Run]:
    return store.list_runs_between(span.start, span.end)


def summary(span: Span, rows: list[RunRow], chaos_per_divine: float | None, now: datetime) -> Json:
    """Totals and averages of one session. `rows` newest first."""
    first = span.start or (rows[-1].run.started_at if rows else None)
    last_activity = max((r.run.last_activity for r in rows), default=None)
    ended = None if span.current else (last_activity or span.end)
    active = sum(r.active_seconds for r in rows)
    tracked = [r for r in rows if r.tracked]
    chaos = sum(r.value.chaos for r in tracked)
    # averages over finished maps, like the Session tiles (a map in progress has no loot yet)
    done = [r for r in rows if r.run.status is RunStatus.FINISHED]
    done_tracked = [r for r in done if r.tracked]
    avg = sum(r.value.chaos for r in done_tracked) / len(done_tracked) if done_tracked else None
    cpd = chaos_per_divine
    return {
        "id": span.id,
        "current": span.current,
        "started_at": _iso(first),
        "ended_at": _iso(ended),
        "wall_seconds": round(((ended or now) - first).total_seconds()) if first else 0,
        "maps": len(rows),
        "active_seconds": round(active),
        "avg_map_seconds": round(sum(r.active_seconds for r in done) / len(done)) if done else None,
        "deaths": sum(r.run.deaths for r in rows),
        "loot_tracked_maps": len(tracked),
        "chaos": round(chaos, 1) if tracked else None,
        "divine": _round(chaos / cpd) if tracked and cpd else None,
        "avg_chaos": round(avg, 1) if avg is not None else None,
        "avg_divine": _round(avg / cpd) if avg is not None and cpd else None,
        "in_progress": any(r.run.status is not RunStatus.FINISHED for r in rows),
        **per_hour(rows, cpd),
    }


def history(store: Store, book: PriceBook, now: datetime,
            cache: BookCache | None = None) -> list[Json]:  # fmt: skip
    """Every session with at least one map (and the current one), newest first. A past
    session's summary is kept in `cache` once all its maps have settled: it only changes
    with the prices."""
    out = []
    for span in reversed(spans(store)):
        key = ("session", span.id, span.start, span.end)
        if cache is not None and not span.current:
            hit = cache.get(book, key)
            if hit is not None:
                if hit["maps"]:
                    out.append(hit)
                continue
        runs = runs_of(store, span)
        if not runs and not span.current:
            if cache is not None:
                cache.put(book, key, {"maps": 0})
            continue
        rows = run_rows(store, book, runs, now, cache)
        result = summary(span, rows, book.chaos_per_divine, now)
        if (
            cache is not None
            and not span.current
            and all(
                now - (r.run.ended_at or r.run.last_activity) > SETTLED_AFTER
                and r.run.status is RunStatus.FINISHED
                for r in rows
            )
        ):
            cache.put(book, key, result)
        out.append(result)
    return out
