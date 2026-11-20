"""Price check on the trade site: mod lines -> trade filters, the query, the listings."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from poefire import pricecheck, trade
from poefire.pricing.book import PriceBook
from poefire.pricing.ninja import PriceLine
from poefire.web.window import popup_position

# GGG's /api/trade/data/stats shape (a few entries)
STATS = {"result": [
    {"label": "Explicit", "entries": [
        {"id": "explicit.stat_life", "text": "+# to maximum Life", "type": "explicit"},
        {"id": "explicit.stat_light", "text": "+#% to Lightning Resistance", "type": "explicit"},
        {"id": "explicit.stat_adds", "text": "Adds # to # Lightning Damage to Attacks",
         "type": "explicit"},
        {"id": "explicit.stat_mana", "text": "#% increased Mana Cost of Skills",
         "type": "explicit"},
        {"id": "explicit.stat_ias", "text": "#% increased Attack Speed", "type": "explicit"},
        {"id": "explicit.stat_ias_local", "text": "#% increased Attack Speed (Local)",
         "type": "explicit"},
        {"id": "explicit.stat_opt", "text": "Allocates #", "type": "explicit",
         "option": {"options": []}},
    ]},
    {"label": "Implicit", "entries": [
        {"id": "implicit.stat_fc", "text": "+#% to Fire and Cold Resistances",
         "type": "implicit"}]},
    {"label": "Crafted", "entries": [
        {"id": "crafted.stat_cold", "text": "+#% to Cold Resistance", "type": "crafted"}]},
]}  # fmt: skip
ITEMS = {"result": [{"id": "accessory", "entries": [
    {"type": "Two-Stone Ring"}, {"type": "Ring"},
    {"name": "Berek's Grip", "type": "Two-Stone Ring", "flags": {"unique": True}}]}]}  # fmt: skip
STATIC = {"result": [{"id": "Currency", "entries": [
    {"id": "chaos", "text": "Chaos Orb"}, {"id": "divine", "text": "Divine Orb"}]}]}  # fmt: skip

RARE = """Item Class: Rings
Rarity: Rare
Storm Loop
Two-Stone Ring
--------
Requirements:
Level: 52
--------
Item Level: 86
--------
+12% to Fire and Cold Resistances (implicit)
--------
+45 to maximum Life
+40% to Lightning Resistance
Adds 3 to 41 Lightning Damage to Attacks
10% reduced Mana Cost of Skills
+25% to Cold Resistance (crafted)
Grants Level 20 Mystery Skill
--------
Note: ~price 5 chaos
"""
MAGIC = """Item Class: Rings
Rarity: Magic
Hale Two-Stone Ring of the Storm
--------
Item Level: 70
--------
+12% to Fire and Cold Resistances (implicit)
--------
+20 to maximum Life
"""
SWORD = """Item Class: One Hand Swords
Rarity: Rare
Grim Edge
Jewelled Foil
--------
Attacks per Second: 1.89 (augmented)
--------
Item Level: 84
--------
18% increased Attack Speed
"""


def data() -> trade.TradeData:
    return trade.TradeData(STATS, ITEMS, STATIC)


def book() -> PriceBook:
    return PriceBook([("exchange", "Currency", [PriceLine("Divine Orb", 180.0)])])


def test_mod_lines_become_filters() -> None:
    check = trade.build_check(pricecheck.parse(RARE), data())
    rows = {f.label: f for f in check.filters}
    life = rows["+45 to maximum Life"]
    assert (life.stat_id, life.enabled, life.value, life.min, life.max) == (
        "explicit.stat_life",
        True,
        45,
        40,
        None,
    )  # 90% of the roll, rounded down
    assert rows["Adds 3 to 41 Lightning Damage to Attacks"].value == 22  # the average
    mana = rows["10% reduced Mana Cost of Skills"]  # reduced = negative increased
    assert (mana.stat_id, mana.value, mana.min, mana.max) == ("explicit.stat_mana", -10, None, -9)
    assert rows["+25% to Cold Resistance"].stat_id == "crafted.stat_cold"
    imp = rows["+12% to Fire and Cold Resistances"]
    assert imp.group == "implicit" and not imp.enabled  # implicits start unticked
    assert not rows["Item level 86+"].enabled and rows["Not corrupted"].enabled
    assert check.unmatched == ["Grants Level 20 Mystery Skill"]  # a mod line we can't search
    assert "Note: ~price 5 chaos" not in check.unmatched  # properties aren't mods
    assert (check.name, check.base) == (None, "Two-Stone Ring")  # a rare's name isn't searched


def test_local_mods_and_magic_bases() -> None:
    sword = trade.build_check(pricecheck.parse(SWORD), data())
    assert [f.stat_id for f in sword.filters if f.stat_id] == ["explicit.stat_ias_local"]
    magic = trade.build_check(pricecheck.parse(MAGIC), data())
    assert magic.base == "Two-Stone Ring"  # not "Ring", not the affixed line


def test_query_and_popup_choices() -> None:
    check = trade.build_check(pricecheck.parse(RARE), data())
    trade.apply_choices(check, [
        {"key": "stat:explicit.stat_light", "enabled": False},
        {"key": "stat:explicit.stat_life", "enabled": True, "min": 50, "max": None},
        {"key": "ilvl", "enabled": True, "min": 84},
    ])  # fmt: skip
    q = trade.query(check)["query"]
    stats = {f["id"]: f["value"] for f in q["stats"][0]["filters"]}
    assert "explicit.stat_light" not in stats and stats["explicit.stat_life"] == {"min": 50}
    assert q["type"] == "Two-Stone Ring" and "name" not in q
    misc = q["filters"]["misc_filters"]["filters"]
    assert misc == {"ilvl": {"min": 84}, "corrupted": {"option": "false"}}
    assert q["filters"]["type_filters"]["filters"]["rarity"] == {"option": "nonunique"}
    assert q["status"] == {"option": "available"}  # Instant Buyout, not In Person
    gem = trade.build_check(pricecheck.parse(GEM), data())
    gq = trade.query(gem)["query"]
    assert gq["type"] == "Enlighten Support"
    assert gq["filters"]["misc_filters"]["filters"]["gem_level"] == {"min": 3}


GEM = """Item Class: Support Gems
Rarity: Gem
Enlighten Support
--------
Support
Level: 3
Quality: +20% (augmented)
--------
Requirements:
Level: 70
--------
Experience: 1/1
"""

PSEUDO = {"label": "Pseudo", "entries": [
    {"id": trade.PSEUDO_ELE_RES, "text": "+#% total Elemental Resistance", "type": "pseudo"},
    {"id": trade.PSEUDO_RES, "text": "+#% total Resistance", "type": "pseudo"},
    {"id": trade.PSEUDO_LIFE, "text": "+# total maximum Life", "type": "pseudo"},
]}  # fmt: skip
INFLUENCED = """Item Class: Rings
Rarity: Rare
Doom Band
Two-Stone Ring
--------
Item Level: 84
--------
+12% to Fire and Cold Resistances (implicit)
--------
+45 to maximum Life
+40% to Lightning Resistance
+30 to Strength
--------
Shaper Item
Hunter Item
"""


def with_pseudo() -> trade.TradeData:
    return trade.TradeData({"result": [*STATS["result"], PSEUDO]}, ITEMS, STATIC)


def test_pseudo_totals_stand_in_for_the_mods_they_sum() -> None:
    check = trade.build_check(pricecheck.parse(INFLUENCED), with_pseudo())
    rows = {f.label: f for f in check.filters}
    ele = rows["+64% total Elemental Resistance"]  # 12 fire + 12 cold (implicit) + 40 lightning
    assert (ele.group, ele.enabled, ele.min) == ("pseudo", True, 57)
    life = rows["+60 total maximum Life"]  # 45 + 30 strength / 2
    assert (life.enabled, life.min) == (True, 54)
    assert not any("total Resistance" in k for k in rows)  # no chaos resistance: not offered
    # the totals are searched instead of the mods they sum
    assert not rows["+45 to maximum Life"].enabled
    assert not rows["+40% to Lightning Resistance"].enabled
    # trade data without pseudo stats offers no totals, and the mods stay ticked
    plain = trade.build_check(pricecheck.parse(INFLUENCED), data())
    assert not [f for f in plain.filters if f.group == "pseudo"]
    assert {f.label: f for f in plain.filters}["+45 to maximum Life"].enabled


def test_chaos_resistance_makes_one_total_for_all_four() -> None:
    text = INFLUENCED.replace("+30 to Strength", "+20% to Chaos Resistance")
    check = trade.build_check(pricecheck.parse(text), with_pseudo())
    rows = {f.label: f for f in check.filters}
    assert rows["+84% total Resistance"].enabled
    assert not rows["+64% total Elemental Resistance"].enabled
    # a mod the trade data has no stat for still counts toward the total
    assert check.unmatched == ["+20% to Chaos Resistance"]
    assert not rows["+40% to Lightning Resistance"].enabled


def test_influence_filters() -> None:
    item = pricecheck.parse(INFLUENCED)
    assert item.influences == ["shaper", "hunter"]
    check = trade.build_check(item, with_pseudo())
    inf = [f for f in check.filters if f.key.startswith("influence:")]
    assert [(f.label, f.enabled) for f in inf] == [("Shaper influence", True),
                                                   ("Hunter influence", True)]  # fmt: skip
    misc = trade.query(check)["query"]["filters"]["misc_filters"]["filters"]
    assert misc["shaper_item"] == misc["hunter_item"] == {"option": "true"}
    trade.apply_choices(check, [{"key": "influence:hunter", "enabled": False}])
    misc = trade.query(check)["query"]["filters"]["misc_filters"]["filters"]
    assert "hunter_item" not in misc and "Shaper Item" not in check.unmatched


def test_trimmed_average() -> None:
    assert trade.trimmed_average([]) is None
    assert trade.trimmed_average([1, 2, 3]) == 2
    prices = [1.0] + [10.0] * 18 + [5000.0]  # one bait listing, one absurd one
    assert trade.trimmed_average(prices) == 10.0


def hit(amount: float, currency: str) -> dict[str, Any]:
    return {"id": "x", "listing": {"price": {"amount": amount, "currency": currency},
                                   "indexed": "2026-11-20T10:00:00Z"}}  # fmt: skip


class Site:
    """The trade site: search, fetch (10 ids at most), data; rate-limit headers."""

    def __init__(self, n: int = 25) -> None:
        self.n = n
        self.requests: list[httpx.Request] = []
        self.limit_next = False

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if self.limit_next:
            return httpx.Response(429, headers={"Retry-After": "30"}, json={})
        h = {"X-Rate-Limit-Policy": "trade-search-request-limit",
             "X-Rate-Limit-Rules": "Ip", "X-Rate-Limit-Ip": "8:10:60",
             "X-Rate-Limit-Ip-State": "1:10:0"}  # fmt: skip
        if req.url.path.startswith("/api/trade/search/"):
            body = json.loads(req.content)
            assert body["query"]["type"] == "Two-Stone Ring"
            return httpx.Response(
                200,
                headers=h,
                json={"id": "Q1", "total": self.n, "result": [f"i{k}" for k in range(self.n)]},
            )
        if req.url.path.startswith("/api/trade/fetch/"):
            ids = req.url.path.rsplit("/", 1)[1].split(",")
            assert len(ids) <= 10 and req.url.params["query"] == "Q1"
            out = [hit(1, "divine") if i == "i0" else hit(10 + int(i[1:]), "chaos") for i in ids]
            return httpx.Response(200, headers=h, json={"result": out})
        if req.url.path == "/api/trade/data/stats":
            return httpx.Response(200, json=STATS)
        return httpx.Response(404, json={"error": {"code": 2, "message": "Resource not found"}})


def client(site: Site) -> trade.TradeClient:
    return trade.TradeClient("poefire-test", cookie=lambda: "c00kie",
                             http=httpx.Client(transport=httpx.MockTransport(site)))  # fmt: skip


def test_search_returns_the_5_cheapest_and_the_average() -> None:
    site = Site()
    check = trade.build_check(pricecheck.parse(RARE), data())
    r = trade.search(client(site), data(), book(), "Keepers", check)
    assert r["total"] == 25 and r["averaged"] == 20  # 20 fetched, in 2 requests
    assert len([q for q in site.requests if "/fetch/" in q.url.path]) == 2
    assert [x["chaos"] for x in r["cheapest"]] == [11, 12, 13, 14, 15]
    # i0 at 1 divine = 180c is the dearest; the average drops it
    assert r["average_chaos"] == pytest.approx(sum(range(13, 29)) / 16)
    assert r["url"] == "https://www.pathofexile.com/trade/search/Keepers/Q1"
    assert site.requests[0].headers["Cookie"] == "POESESSID=c00kie"
    assert "poefire" in site.requests[0].headers["User-Agent"]


def test_rate_limits_are_never_waited_out() -> None:
    site = Site()
    c = client(site)
    site.limit_next = True
    check = trade.build_check(pricecheck.parse(RARE), data())
    with pytest.raises(trade.TradeRateLimited, match="30s"):
        trade.search(c, data(), book(), "Keepers", check)
    site.limit_next = False
    sent = len(site.requests)
    with pytest.raises(trade.TradeRateLimited):  # still limited: nothing is sent
        trade.search(c, data(), book(), "Keepers", check)
    assert len(site.requests) == sent
    with pytest.raises(trade.TradeError, match="Resource not found"):
        client(Site()).data("nope")


def test_trade_data_is_kept_for_a_day(tmp_path: Path) -> None:
    path = tmp_path / "trade-data.json"
    calls: list[str] = []

    def fetch(kind: str) -> dict[str, Any]:
        calls.append(kind)
        tables: dict[str, dict[str, Any]] = {"stats": STATS, "items": ITEMS, "static": STATIC}
        return tables[kind]

    d = trade.load_trade_data(path, fetch, now=1000.0)
    assert d.stats["explicit"]["+# to maximum Life"] == "explicit.stat_life"
    assert d.currency["divine"] == "Divine Orb" and "Two-Stone Ring" in d.bases
    assert "+#" not in json.dumps(d.stats.get("explicit", {}).get("Allocates #"))  # option stats
    assert calls == ["stats", "items", "static"]
    again = trade.load_trade_data(path, fetch, now=2000.0)
    assert len(calls) == 3 and again.stats == d.stats  # from the saved copy

    def down(kind: str) -> dict[str, Any]:
        raise trade.TradeError("offline")

    stale = trade.load_trade_data(path, down, now=1000.0 + 2 * trade.DATA_MAX_AGE)
    assert stale.stats == d.stats  # an old copy beats nothing
    with pytest.raises(trade.TradeError):
        trade.load_trade_data(tmp_path / "none.json", down)


def test_popup_beside_the_cursor_inside_the_screen() -> None:
    area = (0, 0, 1920, 1040)
    assert popup_position((500, 400), (440, 600), area) == (524, 200)
    assert popup_position((1800, 400), (440, 600), area) == (1336, 200)  # flips left
    assert popup_position((500, 1000), (440, 600), area) == (524, 440)  # kept above taskbar
    assert popup_position((10, 5), (440, 600), None) == (34, 0)


def test_price_check_api_searches_the_trade_site(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient
    from test_web import OfflineNinja

    from poefire.config import Config
    from poefire.pricing.service import PriceService
    from poefire.store import Store
    from poefire.web.app import create_app
    from poefire.web.runtime import AppRuntime, trade_line

    db = tmp_path / "db"
    rt = AppRuntime(Config(db_path=db, league="Keepers", session_path=tmp_path / "s.json"),
                    PriceService(OfflineNinja(), lambda: Store(db)), recorder=None)  # fmt: skip
    site = Site()
    rt._trade_client = client(site)
    (tmp_path / "trade-data.json").write_text(json.dumps(
        {**data().to_json(), "fetched_at": 4e12}))  # fmt: skip
    api = TestClient(create_app(rt))
    api.headers["host"] = "127.0.0.1:8460"
    h = {"X-Poefire": "1"}
    st = api.post("/api/pricecheck", headers=h, json={"text": RARE}).json()["check"]
    assert st["kind"] == "trade" and st["result"] is None  # the popup searches it
    keys = [f["key"] for f in st["check"]["filters"]]
    assert "stat:explicit.stat_life" in keys
    body = {"id": st["id"], "filters": [{"key": "stat:explicit.stat_light", "enabled": False}]}
    done = api.post("/api/pricecheck/search", headers=h, json=body).json()["check"]
    assert done["result"]["cheapest"][0]["chaos"] == 11 and done["error"] is None
    sent = json.loads(next(r for r in site.requests if "/search/" in r.url.path).content)
    assert "explicit.stat_light" not in json.dumps(sent)  # unticked in the popup
    assert trade_line(done).startswith("Storm Loop Two-Stone Ring: from 11c, avg")
    stale = {"id": st["id"] - 1, "filters": []}
    assert api.post("/api/pricecheck/search", headers=h, json=stale).status_code == 409
    assert api.get("/price-check").status_code == 200
    rt.stop()
