"""What the in-game overlay shows (GET /api/overlay).

- During a campaign run: the timer, the act, and the last 3 areas with your time, your PB's
  time there (best finished run of the same base class) and the difference.
- Otherwise (mapping): the session's average chaos per map (finished maps with loot data,
  since the session started), its total loot and number of maps, and the map you're in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from poefire import campaign
from poefire.store import Store
from poefire.tracker import RunStatus
from poefire.web import sessions, views

if TYPE_CHECKING:
    from poefire.web.runtime import AppRuntime

type Json = dict[str, Any]

LAST_AREAS = 3


def overlay_state(rt: AppRuntime, store: Store) -> Json:
    now = rt.now()
    with rt._lock:
        active = rt.campaign.active
        if active is not None:
            runs = rt._store.list_campaign_runs(1000, splits=False)
            pb = rt._personal_best(runs, active)
            detail = campaign.run_detail(active, now, pb)
    if active is not None:
        areas = detail["zones"][-LAST_AREAS:]
        return {
            "mode": "campaign",
            "rta": detail["rta"],
            "igt": detail["igt"],
            "paused": detail["paused"],
            "act": detail["act"],
            "character": detail["character"],
            "class": detail["class"],
            "base_class": detail["base_class"],
            "pb_class": detail["pb_class"] if detail["pb_id"] else None,
            "areas": [  # newest first
                {k: a[k] for k in ("name", "act", "rta", "pb_rta", "delta")}
                for a in reversed(areas)
            ],
        }

    book = rt.prices.book(rt.price_league)
    cpd = book.chaos_per_divine
    span = sessions.find(store, None)
    rows = (
        views.run_rows(store, book, sessions.runs_of(store, span), now, rt.values) if span else []
    )
    summary = sessions.summary(span, rows, cpd, now) if span else {}
    live = rt.state().run
    current = None
    if live is not None and live.status is not RunStatus.FINISHED:
        current = {
            "name": live.map_name,
            "status": live.status.value,  # active (in the map) or paused (back in hideout)
            "seconds": round(
                live.active_seconds(now if live.status is RunStatus.ACTIVE else None), 1
            ),
        }
    return {
        "mode": "mapping",
        "maps": summary.get("maps", 0),
        "avg_chaos": summary.get("avg_chaos"),
        "avg_divine": summary.get("avg_divine"),
        "chaos": summary.get("chaos"),
        "divine": summary.get("divine"),
        "since": summary.get("started_at"),
        "current": current,
    }
