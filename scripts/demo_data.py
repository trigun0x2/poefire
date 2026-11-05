"""Fill a fresh data folder with made-up demo data, for the README screenshots.

    uv run python scripts/demo_data.py <data folder>

Then run poefire against it (see scripts/screenshots.js). Nothing here touches your real data.
Item icons are the game's own, looked up on poe.ninja (needs internet; without it the items
have no icons). The prices are fixed, so the pictures don't change with the market.
"""

from __future__ import annotations

import json
import random
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from poefire.builds import BuildSnapshot
from poefire.campaign import CampaignRun, Split, SplitKind
from poefire.loot.diff import ItemDelta
from poefire.loot.items import ItemEntry
from poefire.loot.recorder import TAB_NAMES_KEY
from poefire.loot.snapshot import Snapshot
from poefire.pricing.ninja import PRICE_SOURCES, PriceLine
from poefire.store import Store
from poefire.tracker import Run, RunStatus, Segment

LEAGUE = "Keepers"
ICONS: dict[str, str] = {}  # item name -> its icon on the game's CDN, from poe.ninja
GEAR_ICONS: dict[str, str] = {}  # unique name or base type -> icon


def with_gear_icons(node: object) -> None:
    """Give the build fixture's items their real icons (it has placeholders)."""
    if isinstance(node, dict):
        if "icon" in node and ("baseType" in node or "typeLine" in node):
            icon = GEAR_ICONS.get(str(node.get("name") or "")) or GEAR_ICONS.get(
                str(node.get("baseType") or node.get("typeLine") or "")
            )
            if icon:
                node["icon"] = icon
        for v in node.values():
            with_gear_icons(v)
    elif isinstance(node, list):
        for v in node:
            with_gear_icons(v)


PRICES = {  # name -> chaos (fixed, so the pictures don't change with the market)
    "Divine Orb": 182.0,
    "Exalted Orb": 16.0,
    "Chaos Orb": 1.0,
    "Orb of Annulment": 8.5,
    "Vaal Orb": 1.2,
    "Orb of Alchemy": 0.35,
    "Orb of Fusing": 0.6,
    "Gemcutter's Prism": 1.4,
    "Ambush Scarab of Containment": 6.0,
    "Breach Scarab of the Dreamer": 11.0,
    "Deafening Essence of Contempt": 3.5,
    "The Apothecary": 2900.0,
    "The Wrath": 24.0,
    "Simulacrum Splinter": 2.1,
}
MAPS = [
    ("MapWorldsStrand", "Strand"), ("MapWorldsCrimsonTemple", "Crimson Temple"),
    ("MapWorldsDunes", "Dunes"), ("MapWorldsCemetery", "Cemetery"),
    ("MapWorldsJungleValley", "Jungle Valley"), ("MapWorldsTower", "Tower"),
    ("MapWorldsDefiledCathedral", "Defiled Cathedral"), ("MapWorldsGlacier", "Glacier"),
]  # fmt: skip
ZONES = [
    ("1_1_1", "The Twilight Strand"), ("1_1_town", "Lioneye's Watch"), ("1_1_2", "The Coast"),
    ("1_1_3", "The Mud Flats"), ("1_1_4_1", "The Submerged Passage"), ("1_1_5", "The Ledge"),
    ("1_1_6", "The Climb"), ("1_1_8", "The Lower Prison"), ("1_1_9", "The Upper Prison"),
    ("1_1_11_1", "Merveil's Caverns"),
]  # fmt: skip


def load_icons() -> None:
    """The game's own icons for the demo items and the build's gear, from poe.ninja."""
    import httpx

    base = "https://poe.ninja/poe1/api/economy"
    cdn = "https://web.poecdn.com"
    agent = "poefire-readme-screenshots (+https://github.com/trigun0x2/poefire)"
    http = httpx.Client(timeout=30, headers={"User-Agent": agent})

    def get(path: str, **params: str) -> Any:
        try:
            r = http.get(base + path, params=params)
            if r.status_code != 200:
                print(f"{path} {params}: HTTP {r.status_code}")
                return {}
            return r.json()
        except (httpx.HTTPError, ValueError) as e:
            print(f"{path} {params}: {e}")
            return {}

    def absolute(icon: object) -> str | None:
        if not isinstance(icon, str) or not icon:
            return None
        return icon if icon.startswith("http") else cdn + icon

    leagues_data = get("/leagues")
    listed = leagues_data if isinstance(leagues_data, list) else (
        leagues_data.get("leagues") or leagues_data.get("economyLeagues") or []
    )  # fmt: skip
    names = [x if isinstance(x, str) else (x.get("name") or "") for x in listed]
    plain = [n for n in names if n and not any(w in n for w in ("Hardcore", "Standard", "HC"))]
    if not plain:
        print("poe.ninja unreachable: the demo items get no icons")
        return
    league = plain[0]
    found: dict[str, int] = {}
    for type_ in ("Currency", "Fragment"):
        data = get("/stash/current/currency/overview", league=league, type=type_)
        for d in data.get("currencyDetails") or []:
            icon = absolute(d.get("icon"))
            if icon and d.get("name"):
                ICONS.setdefault(d["name"], icon)
        found[type_] = len(data.get("currencyDetails") or [])
    for type_ in ("Currency", "DivinationCard", "Scarab", "Essence", "Fragment"):
        data = get("/exchange/current/overview", league=league, type=type_)
        for d in data.get("items") or []:
            icon = absolute(d.get("image") or d.get("icon"))
            if icon and d.get("name"):
                ICONS.setdefault(d["name"], icon)
        found[f"exchange {type_}"] = len(data.get("items") or [])
    for type_ in ("DivinationCard", "Scarab", "Essence", "UniqueArmour", "UniqueWeapon",
                  "UniqueAccessory", "UniqueFlask", "UniqueJewel", "BaseType"):  # fmt: skip
        data = get("/stash/current/item/overview", league=league, type=type_)
        lines = data.get("lines") or []
        found[type_] = len(lines)
        for d in lines:
            icon = absolute(d.get("icon"))
            if not icon:
                continue
            if d.get("name"):
                ICONS.setdefault(d["name"], icon)
                GEAR_ICONS.setdefault(d["name"], icon)
            if d.get("baseType"):
                GEAR_ICONS.setdefault(d["baseType"], icon)
    missing = sorted(n for n in PRICES if n not in ICONS)
    print(f"poe.ninja ({league}) entries per type: {found}")
    print(f"demo item icons: {len(PRICES) - len(missing)}/{len(PRICES)}; missing: {missing}")


def raw(name: str, qty: int) -> dict[str, object]:
    frame = 6 if name.startswith("The ") else 5
    out: dict[str, object] = {"typeLine": name, "baseType": name, "frameType": frame,
                              "stackSize": qty}  # fmt: skip
    if name in ICONS:
        out["icon"] = ICONS[name]
    return out


def delta(name: str, qty: int) -> ItemDelta:
    r = raw(name, qty)
    return ItemDelta(f"{r['frameType']}||{name}", name, int(r["frameType"]), qty, r)  # type: ignore[call-overload]


def seed(folder: Path) -> None:
    rnd = random.Random(7)
    folder.mkdir(parents=True, exist_ok=True)
    s = Store(folder / "poefire.sqlite3")
    now = datetime.now().replace(microsecond=0)

    with s.transaction():
        for source, type_ in PRICE_SOURCES:  # all fresh, so nothing is fetched
            lines = [PriceLine(n, c) for n, c in PRICES.items()] if type_ == "Currency" \
                else [PriceLine(n, c) for n, c in PRICES.items() if n.startswith("The ")] \
                if type_ == "DivinationCard" \
                else [PriceLine(n, c) for n, c in PRICES.items() if "Scarab" in n] \
                if type_ == "Scarab" \
                else [PriceLine(n, c) for n, c in PRICES.items() if "Essence" in n] \
                if type_ == "Essence" else []  # fmt: skip
            s.save_prices(LEAGUE, source, type_, time.time(), lines)

        # maps: three earlier sittings and today's, ~6 minutes each, loot from the hideout trips
        sittings = [(now - timedelta(days=d, hours=h), n) for d, h, n in
                    [(3, 4, 18), (2, 3, 22), (1, 5, 15), (0, 2.4, 21)]]  # fmt: skip
        n_run = 0
        for start, count in sittings:
            t = start
            for _ in range(count):
                n_run += 1
                area, name = MAPS[(n_run * 5) % len(MAPS)]
                length = rnd.uniform(4.2, 8.5)
                mid = t + timedelta(minutes=length * 0.6)
                end = t + timedelta(minutes=length)
                run = Run(f"run{n_run}", area, name, 83, n_run, t, RunStatus.FINISHED,
                          deaths=1 if rnd.random() < 0.12 else 0,
                          segments=[Segment(t, mid), Segment(mid + timedelta(seconds=25), end)],
                          ended_at=end)  # fmt: skip
                s.upsert_run(run)
                loot = [delta("Chaos Orb", rnd.randint(14, 48)),
                        delta("Orb of Alchemy", rnd.randint(6, 22)),
                        delta("Orb of Fusing", rnd.randint(3, 14))]  # fmt: skip
                if rnd.random() < 0.55:
                    loot.append(delta(rnd.choice(["Ambush Scarab of Containment",
                                                  "Breach Scarab of the Dreamer"]), 1))  # fmt: skip
                if rnd.random() < 0.35:
                    loot.append(delta("Deafening Essence of Contempt", rnd.randint(1, 3)))
                if rnd.random() < 0.25:
                    loot.append(delta("Simulacrum Splinter", rnd.randint(2, 9)))
                if rnd.random() < 0.18:
                    loot.append(delta("Exalted Orb", 1))
                if rnd.random() < 0.08:
                    loot.append(delta("Divine Orb", 1))
                if rnd.random() < 0.12:
                    loot.append(delta("The Wrath", 1))
                a = s.add_snapshot(Snapshot(t), "run_start", run.id)
                b = s.add_snapshot(Snapshot(end + timedelta(seconds=3)), "run_return", run.id)
                s.add_loot(run.id, "run", a, b, loot)
                t = end + timedelta(minutes=rnd.uniform(0.8, 2.5))
        # the last map dropped something nice
        s.add_loot(f"run{n_run}", "run", a, b, [delta("The Apothecary", 1)])
        for start, _ in sittings:
            s.add_session(start - timedelta(minutes=1))

        # the stash, and its value over the last 10 days
        tabs = {"t-dump": "Dump", "t-cur": "Currency", "t-frag": "Fragments"}
        s.set_json(TAB_NAMES_KEY, tabs)
        held = [("Divine Orb", 61, "t-cur"), ("Exalted Orb", 48, "t-cur"),
                ("Chaos Orb", 2410, "t-cur"), ("Orb of Annulment", 37, "t-cur"),
                ("Gemcutter's Prism", 120, "t-cur"), ("Orb of Alchemy", 960, "t-cur"),
                ("Vaal Orb", 210, "t-cur"), ("Simulacrum Splinter", 140, "t-frag"),
                ("Breach Scarab of the Dreamer", 22, "t-frag"),
                ("Ambush Scarab of Containment", 31, "t-frag"),
                ("Deafening Essence of Contempt", 26, "t-dump"),
                ("The Apothecary", 1, "t-dump"), ("The Wrath", 4, "t-dump")]  # fmt: skip
        items = [ItemEntry(None, f"5||{n}", n, 5, q, True, src, raw(n, q)) for n, q, src in held]
        cpd = PRICES["Divine Orb"]
        for k in range(0, 240, 3):  # every 3 hours
            at = now - timedelta(hours=240 - k)
            value = (52 + k * 0.105 + rnd.uniform(-1.6, 1.6) + (14 if k > 226 else 0)) * cpd
            sid = s.add_snapshot(Snapshot(at), "run_return", None)
            s.add_snapshot_value(sid, at, LEAGUE, value, cpd, 0)
        sid = s.add_snapshot(Snapshot(now - timedelta(minutes=4), items=items), "manual", None)
        s.add_snapshot_value(sid, now - timedelta(minutes=4), LEAGUE,
                             sum(PRICES[e.name] * e.quantity for e in items),
                             cpd, 0)  # fmt: skip

        # campaign runs: a few Deadeye/Witch runs, one in progress
        def campaign(days: float, cls: str, char: str, minutes: float, status: str,
                     act: int, notes: str = "") -> CampaignRun:  # fmt: skip
            st = now - timedelta(days=days)
            r = CampaignRun(started_at=st, status=status, character=char, char_class=cls,
                            act=act, level=68 if act > 10 else 12 + act * 6, notes=notes,
                            patch="3.27", deaths=rnd.randint(0, 3))  # fmt: skip
            pace = minutes / 300
            for i, (_, zone) in enumerate(ZONES):
                sec = (i + 1) * 95 * pace + rnd.uniform(-8, 8)
                r.splits.append(Split(SplitKind.ZONE, zone, 1, st + timedelta(seconds=sec),
                                      sec, sec * 0.93))  # fmt: skip
            for n in range(1, min(act, 11)):
                sec = n * 1740 * pace * rnd.uniform(0.94, 1.06)
                r.splits.append(Split(SplitKind.ACT, f"Act {n}", n,
                                      st + timedelta(seconds=sec), sec, sec * 0.93))  # fmt: skip
            if status != "running":
                r.ended_at = st + timedelta(minutes=minutes)
            return r

        for r in [
            campaign(9, "Deadeye", "QuickShot", 318, "finished", 11, "Tornado Shot league start"),
            campaign(7, "Necromancer", "Bonelord", 301, "finished", 11, "SRS, skipped Act 6 lab"),
            campaign(5, "Deadeye", "QuickShotII", 287, "finished", 11, "Better act 4 route"),
            campaign(4, "Juggernaut", "Tankard", 344, "finished", 11),
            campaign(2, "Ranger", "QuickShotIII", 120, "abandoned", 4, "Died to Kaom"),
        ]:
            s.save_campaign_run(r)

        # a build at a few levels, from the test fixture
        fixture = json.loads(
            (Path(__file__).parent.parent / "tests" / "data" / "build_website.json").read_text()
        )
        with_gear_icons(fixture)
        for i, level in enumerate([5, 10, 15, 22, 38, 61, 74, 88]):
            s.add_build_snapshot(BuildSnapshot("QuickShot", level, now - timedelta(days=9 - i),
                                               LEAGUE, "Deadeye", fixture))  # fmt: skip

    (folder / "config.toml").write_text(
        f'league = "{LEAGUE}"\naccount_name = "Exile#1234"\ncharacter = "QuickShot"\n'
        'stash_tabs = ["t-dump", "t-cur", "t-frag"]\ncheck_updates = false\n'
    )
    s.close()


if __name__ == "__main__":
    load_icons()
    seed(Path(sys.argv[1]))
