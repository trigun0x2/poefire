"""Overview tab: value of the watched tabs + inventory over time, and the top items now."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from poefire.loot.recorder import TAB_NAMES_KEY
from poefire.pricing.book import PriceBook
from poefire.pricing.valuation import is_worth_showing, value_holdings
from poefire.store import Store
from poefire.web.cache import BookCache

type Json = dict[str, Any]

RANGES: dict[str, timedelta | None] = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "all": None,
}


def history(store: Store, league: str, range_: str, now: datetime,
            current_cpd: float | None) -> Json:  # fmt: skip
    span = RANGES.get(range_, RANGES["7d"])
    since = now - span if span else None
    points = []
    for taken_at, chaos, cpd in thin(store.value_history(league, since)):
        rate = cpd or current_cpd  # divine at that moment's rate when known
        points.append({
            "t": taken_at.isoformat(),
            "chaos": round(chaos, 1),
            "divine": round(chaos / rate, 3) if rate else None,
        })  # fmt: skip
    return {"league": league, "range": range_ if range_ in RANGES else "7d", "points": points}


MAX_POINTS = 800  # about the chart's width in pixels; more points can't be told apart


def thin[T: tuple[datetime, float, float | None]](
    points: list[T], max_points: int = MAX_POINTS
) -> list[T]:
    """At most `max_points` points: the lowest and highest of each time slice, so peaks
    and dips stay visible, plus the first and last points."""
    if len(points) <= max_points:
        return points
    slices = max_points // 2
    t0, t1 = points[0][0], points[-1][0]
    width = (t1 - t0) / slices
    buckets: dict[int, list[T]] = {}
    for p in points:
        i = min(int((p[0] - t0) / width), slices - 1) if width else 0
        buckets.setdefault(i, []).append(p)
    out: list[T] = []
    for i in sorted(buckets):
        b = buckets[i]
        lo, hi = min(b, key=lambda p: p[1]), max(b, key=lambda p: p[1])
        out.extend(sorted({id(lo): lo, id(hi): hi}.values(), key=lambda p: p[0]))
    if out[0] is not points[0]:
        out.insert(0, points[0])
    if out[-1] is not points[-1]:
        out.append(points[-1])
    return out


def _round(v: float | None, nd: int = 2) -> float | None:
    return None if v is None else round(v, nd)


def _latest(store: Store, book: PriceBook, cache: BookCache | None) -> Any:
    """(taken_at, entries, {source: (entries, chaos)}, all held items) of the latest complete
    snapshot, valued; kept in `cache` until there's a new snapshot or new prices."""
    snap_id = store.last_complete_snapshot_id()
    if snap_id is None:
        return None
    if cache is not None and (hit := cache.get(book, ("holdings", snap_id))) is not None:
        return hit
    latest = store.latest_snapshot_items()
    if latest is None:
        return None
    taken_at, entries = latest
    by_source: dict[str, list[Any]] = {}
    for e in entries:
        by_source.setdefault(e.source, []).append(e)
    tab_values = {
        source: sum(h.chaos_total or 0 for h in value_holdings(items, book))
        for source, items in by_source.items()
    }
    result = (taken_at, entries, by_source, tab_values, value_holdings(entries, book))
    if cache is not None:
        cache.put(book, ("holdings", snap_id), result)
    return result


def holdings(store: Store, book: PriceBook, tabs: Iterable[str] = (), limit: int = 500,
             cache: BookCache | None = None) -> Json:  # fmt: skip
    """Latest snapshot's items, valued at current prices. `tabs` filters by source id
    (a stash tab id, or "inventory"); empty = everything."""
    latest = _latest(store, book, cache)
    saved: dict[str, str] = store.get_json(TAB_NAMES_KEY) or {}

    def name_of(src: str) -> str:
        return saved.get(src) or ("Inventory" if src == "inventory" else "Unnamed tab")

    cpd = book.chaos_per_divine
    if latest is None:
        return {"taken_at": None, "tabs": [], "items": [], "total_chaos": None,
                "total_divine": None, "chaos_per_divine": cpd}  # fmt: skip
    taken_at, entries, by_source, tab_values, held_all = latest

    # per-source totals for the tab filter chips
    order = sorted(tab_values, key=lambda src: (src != "inventory", -tab_values[src]))
    tab_list = [
        {"id": src, "name": name_of(src), "chaos": round(tab_values[src], 1),
         "items": len(by_source[src])}
        for src in order
    ]  # fmt: skip

    wanted = {t for t in tabs if t}
    held = (value_holdings([e for e in entries if e.source in wanted], book) if wanted
            else held_all)  # fmt: skip
    total = sum(h.chaos_total or 0 for h in held)
    shown = [h for h in held if is_worth_showing(h.chaos_total)]
    return {
        "taken_at": taken_at.isoformat(),
        "tabs": tab_list,
        "total_chaos": round(total, 1),
        "total_divine": _round(total / cpd) if cpd else None,
        "chaos_per_divine": cpd,
        "unpriced": sum(1 for h in held if h.chaos_each is None),
        "hidden": len(held) - len(shown),  # no price, or worth ~0c
        "items": [
            {
                "name": h.name,
                "quantity": h.quantity,
                "chaos_each": _round(h.chaos_each),
                "chaos_total": _round(h.chaos_total, 1),
                "divine_total": _round(h.chaos_total / cpd, 3) if cpd and h.chaos_total else None,
                "share": round(h.chaos_total / total, 4) if total and h.chaos_total else 0,
                "icon": h.icon,
                "tabs": [name_of(s) for s in h.sources],
            }
            for h in shown[:limit]
        ],
    }
