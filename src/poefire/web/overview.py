"""Overview tab: value of the watched tabs + inventory over time, and the top items now."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from poefire.loot.recorder import TAB_NAMES_KEY
from poefire.pricing.book import PriceBook
from poefire.pricing.valuation import is_worth_showing, value_holdings
from poefire.store import Store

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
    for taken_at, chaos, cpd in store.value_history(league, since):
        rate = cpd or current_cpd  # divine at that moment's rate when known
        points.append({
            "t": taken_at.isoformat(),
            "chaos": round(chaos, 1),
            "divine": round(chaos / rate, 3) if rate else None,
        })  # fmt: skip
    return {"league": league, "range": range_ if range_ in RANGES else "7d", "points": points}


def _round(v: float | None, nd: int = 2) -> float | None:
    return None if v is None else round(v, nd)


def holdings(store: Store, book: PriceBook, tabs: Iterable[str] = (), limit: int = 500) -> Json:
    """Latest snapshot's items, valued at current prices. `tabs` filters by source id
    (a stash tab id, or "inventory"); empty = everything."""
    latest = store.latest_snapshot_items()
    names: dict[str, str] = store.get_json(TAB_NAMES_KEY) or {}
    cpd = book.chaos_per_divine
    if latest is None:
        return {"taken_at": None, "tabs": [], "items": [], "total_chaos": None,
                "total_divine": None, "chaos_per_divine": cpd}  # fmt: skip
    taken_at, entries = latest

    # per-source totals for the tab filter chips
    by_source: dict[str, list[Any]] = {}
    for e in entries:
        by_source.setdefault(e.source, []).append(e)
    tab_values = {
        source: sum(h.chaos_total or 0 for h in value_holdings(items, book))
        for source, items in by_source.items()
    }
    order = sorted(tab_values, key=lambda src: (src != "inventory", -tab_values[src]))
    tab_list = [
        {"id": src, "name": names.get(src, src), "chaos": round(tab_values[src], 1),
         "items": len(by_source[src])}
        for src in order
    ]  # fmt: skip

    wanted = {t for t in tabs if t}
    chosen = [e for e in entries if not wanted or e.source in wanted]
    held = value_holdings(chosen, book)
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
                "tabs": [names.get(s, s) for s in h.sources],
            }
            for h in shown[:limit]
        ],
    }
