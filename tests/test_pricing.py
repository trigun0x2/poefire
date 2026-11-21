from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from poefire.loot.diff import ItemDelta
from poefire.pricing.book import PriceBook
from poefire.pricing.ninja import NINJA_BASE, NinjaClient, NinjaError, PriceLine
from poefire.pricing.service import PriceService
from poefire.pricing.valuation import value_deltas
from poefire.store import Store

type Json = dict[str, Any]

RESPONSES: dict[tuple[str, str], Json] = {
    ("/exchange/current/overview", "Currency"): {
        "core": {"primary": "chaos", "secondary": "divine"},
        "items": [{"id": "divine", "name": "Divine Orb"},
                  {"id": "exalted", "name": "Exalted Orb"}],
        "lines": [{"id": "divine", "primaryValue": 180.0},
                  {"id": "exalted", "primaryValue": 11.0},
                  {"id": "unknown", "primaryValue": 5}],
    },
    ("/exchange/current/overview", "DivinationCard"): {  # moved off the stash overview
        "core": {"primary": "chaos", "secondary": "divine"},
        "items": [{"id": "the-apothecary", "name": "The Apothecary"}],
        "lines": [{"id": "the-apothecary", "primaryValue": 2900.0}],
    },
    # fragments: the stash table keeps a few; the rest (Maven's, ...) only on the exchange
    ("/stash/current/currency/overview", "Fragment"): {
        "lines": [{"currencyTypeName": "Sacred Blossom", "chaosEquivalent": 3.0}],
    },
    ("/exchange/current/overview", "Fragment"): {
        "core": {"primary": "chaos", "secondary": "divine"},
        "items": [{"id": "reality-fragment", "name": "Reality Fragment"}],
        "lines": [{"id": "reality-fragment", "primaryValue": 42.0}],
    },
    ("/stash/current/item/overview", "Vial"): {"lines": []},  # an empty table: moved
    ("/exchange/current/overview", "Vial"): {
        "core": {"primary": "chaos", "secondary": "divine"},
        "items": [{"id": "vial-of-fate", "name": "Vial of Fate"}],
        "lines": [{"id": "vial-of-fate", "primaryValue": 7.0}],
    },
    ("/stash/current/currency/overview", "Currency"): {
        "lines": [{"currencyTypeName": "Divine Orb", "chaosEquivalent": 170.0},
                  {"currencyTypeName": "Orb of Alchemy", "chaosEquivalent": 0.3},
                  {"currencyTypeName": "Broken", "chaosEquivalent": None}],
    },
    ("/stash/current/item/overview", "UniqueArmour"): {
        "lines": [
            {"name": "Tabula Rasa", "baseType": "Simple Robe", "chaosValue": 8.0},
            {"name": "Kaom's Heart", "baseType": "Glorious Plate", "chaosValue": 40.0},
            {"name": "Kaom's Heart", "baseType": "Glorious Plate", "chaosValue": 90.0, "links": 6},
        ],
    },
    ("/stash/current/item/overview", "Map"): {
        "lines": [
            {"name": "Strand Map", "baseType": "Strand Map", "mapTier": 16, "chaosValue": 4.0},
            {"name": "Strand Map", "baseType": "Strand Map", "mapTier": 3, "chaosValue": 1.0},
        ],
    },
    ("/stash/current/item/overview", "SkillGem"): {
        "lines": [
            {"name": "Enlighten Support", "gemLevel": 3, "gemQuality": 0, "chaosValue": 900.0},
            {"name": "Enlighten Support", "gemLevel": 1, "gemQuality": 0, "chaosValue": 2.0},
            {"name": "Enlighten Support", "gemLevel": 4, "gemQuality": 0, "corrupted": True,
             "chaosValue": 3000.0},
        ],
    },
}  # fmt: skip


class Ninja:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.fail = False

    def __call__(self, r: httpx.Request) -> httpx.Response:
        self.requests.append(r)
        if self.fail:
            return httpx.Response(503)
        path = r.url.path.removeprefix("/poe1/api/economy")
        body = RESPONSES.get((path, r.url.params["type"]))
        return httpx.Response(200, json=body) if body else httpx.Response(404)


def client(ninja: Ninja) -> NinjaClient:
    return NinjaClient("poefire/test", httpx.Client(transport=httpx.MockTransport(ninja)),
                       min_interval=0)  # fmt: skip


def test_fetch_parses_each_source() -> None:
    ninja = Ninja()
    c = client(ninja)
    assert c.fetch("exchange", "Keepers", "Currency") == [
        PriceLine("Divine Orb", 180.0), PriceLine("Exalted Orb", 11.0)]  # fmt: skip
    assert c.fetch("currency", "Keepers", "Currency") == [
        PriceLine("Divine Orb", 170.0), PriceLine("Orb of Alchemy", 0.3)]  # fmt: skip
    maps = c.fetch("item", "Keepers", "Map")
    assert maps[0] == PriceLine("Strand Map", 4.0, base_type="Strand Map", map_tier=16)
    assert c.fetch("item", "Keepers", "Tattoo") == []  # 404 on both: retired type
    # 404 on the stash overview, but on the currency exchange: priced from there
    assert c.fetch("item", "Keepers", "DivinationCard") == [PriceLine("The Apothecary", 2900.0)]
    r = ninja.requests[0]
    assert str(r.url).startswith(NINJA_BASE) and r.url.params["league"] == "Keepers"
    assert r.headers["User-Agent"] == "poefire/test"


def test_fetch_errors() -> None:
    ninja = Ninja()
    ninja.fail = True
    with pytest.raises(NinjaError, match="503"):
        client(ninja).fetch("item", "Keepers", "Map")


def book() -> PriceBook:
    c = client(Ninja())
    return PriceBook(
        (src, t, c.fetch(src, "L", t))
        for src, t in [("currency", "Currency"), ("exchange", "Currency"),
                       ("item", "UniqueArmour"), ("item", "Map"), ("item", "SkillGem")]
    )  # fmt: skip


def test_exchange_beats_stash_and_chaos_is_fixed() -> None:
    b = book()
    assert b.chaos_per_divine == 180.0
    assert b.price({"typeLine": "Chaos Orb", "frameType": 5}).chaos_each == 1.0  # type: ignore[union-attr]
    assert b.price({"typeLine": "Orb of Alchemy", "frameType": 5}).chaos_each == 0.3  # type: ignore[union-attr]


def sockets(n: int) -> list[Json]:
    return [{"group": 0, "attr": "S"} for _ in range(n)]


@pytest.mark.parametrize(
    ("raw", "chaos"),
    [
        ({"name": "Tabula Rasa", "typeLine": "Simple Robe", "frameType": 3}, 8.0),
        ({"name": "<<set:MS>><<set:M>><<set:S>>Kaom's Heart", "typeLine": "Glorious Plate",
          "baseType": "Glorious Plate", "frameType": 3}, 40.0),
        ({"name": "Kaom's Heart", "typeLine": "Glorious Plate", "frameType": 3,
          "sockets": sockets(6)}, 90.0),
        ({"name": "Tabula Rasa", "typeLine": "Wrong Base", "frameType": 3}, None),
        ({"typeLine": "Strand Map", "frameType": 0,
          "properties": [{"name": "Map Tier", "values": [["16", 0]]}]}, 4.0),
        ({"typeLine": "Strand Map", "frameType": 2,
          "properties": [{"name": "Map Tier", "values": [["3", 0]]}]}, 1.0),
        ({"typeLine": "Enlighten Support", "frameType": 4,
          "properties": [{"name": "Level", "values": [["3", 0]]}]}, 900.0),
        ({"typeLine": "Enlighten Support", "frameType": 4, "corrupted": True,
          "properties": [{"name": "Level", "values": [["4 (Max)", 0]]}]}, 3000.0),
        ({"typeLine": "Hubris Circlet", "frameType": 2}, None),
    ],
)  # fmt: skip
def test_price_matching(raw: Json, chaos: float | None) -> None:
    q = book().price(raw)
    assert (q.chaos_each if q else None) == chaos


def test_value_deltas() -> None:
    deltas = [
        ItemDelta("a", "Divine Orb", 5, 2, {"typeLine": "Divine Orb", "frameType": 5}),
        ItemDelta("b", "Strand Map", 0, -1, {"typeLine": "Strand Map", "frameType": 0,
                  "properties": [{"name": "Map Tier", "values": [["16", 0]]}]}),
        ItemDelta("c", "Hubris Circlet", 2, 1, {"typeLine": "Hubris Circlet", "frameType": 2}),
    ]  # fmt: skip
    v = value_deltas(deltas, book())
    assert v.chaos == 2 * 180 and v.spent == 4  # the map used isn't taken off the loot
    assert v.unpriced == 1
    assert v.divine(180.0) == pytest.approx(360 / 180)
    assert v.items[0].name == "Divine Orb"


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


SOURCES = (("exchange", "Currency"), ("item", "Map"))


def test_service_caches_in_sqlite_and_refreshes_when_stale(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite3"
    ninja, clock = Ninja(), Clock()
    svc = PriceService(client(ninja), lambda: Store(db), refresh_seconds=1800, sources=SOURCES,
                       clock=clock)  # fmt: skip
    assert svc.is_stale("Keepers")
    st = svc.refresh("Keepers")
    assert st.entries > 0 and st.chaos_per_divine == 180.0 and not st.failed_types
    assert len(ninja.requests) == 2

    # a new process loads the cache from SQLite without any request
    ninja2 = Ninja()
    svc2 = PriceService(client(ninja2), lambda: Store(db), refresh_seconds=1800,
                        sources=SOURCES, clock=clock)  # fmt: skip
    assert svc2.book("Keepers").chaos_per_divine == 180.0
    assert not svc2.is_stale("Keepers") and ninja2.requests == []

    clock.t += 1801
    assert svc2.is_stale("Keepers")
    svc2.refresh("Keepers")
    assert len(ninja2.requests) == 2


def test_failed_refresh_keeps_cache_and_backs_off(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite3"
    ninja, clock = Ninja(), Clock()
    svc = PriceService(client(ninja), lambda: Store(db), sources=SOURCES, clock=clock)
    svc.refresh("Keepers")
    ninja.fail = True
    clock.t += 3600
    st = svc.refresh("Keepers")
    assert st.failed_types == ["exchange:Currency", "item:Map"]
    assert st.chaos_per_divine == 180.0  # old prices kept
    assert not svc.is_stale("Keepers")  # no hammering right after a failure
    clock.t += 301
    assert svc.is_stale("Keepers")


def test_refresh_interval_has_a_floor(tmp_path: Path) -> None:
    svc = PriceService(client(Ninja()), lambda: Store(tmp_path / "db"), refresh_seconds=10)
    assert svc.refresh_seconds == 15 * 60


def test_refresh_stops_early_when_ninja_is_down(tmp_path: Path) -> None:
    ninja = Ninja()
    ninja.fail = True
    sources = tuple(("item", f"T{i}") for i in range(10))
    svc = PriceService(client(ninja), lambda: Store(tmp_path / "db"), sources=sources)
    st = svc.refresh("Keepers")
    assert len(ninja.requests) == 3 and len(st.failed_types) == 3


def test_refresh_loop_survives_an_error(tmp_path: Path) -> None:
    import threading

    svc = PriceService(client(Ninja()), lambda: Store(tmp_path / "db"))
    calls: list[str] = []
    stop = threading.Event()

    def broken(league: str, force: bool = False) -> None:
        calls.append(league)
        stop.set()
        raise RuntimeError("database went away")

    svc.refresh = broken  # type: ignore[method-assign,assignment]
    svc.run_forever(lambda: "Keepers", stop)  # returns once stopped, didn't raise
    assert calls == ["Keepers"]


def test_price_lines_keep_zero_values(tmp_path: Path) -> None:
    line = PriceLine("Scroll Fragment", 0.0, gem_quality=0)
    assert PriceLine.from_json(line.to_json()) == line
    # what older versions saved: no chaos when it was 0, and keys they didn't know
    assert PriceLine.from_json({"name": "Old"}) == PriceLine("Old", 0.0)
    assert PriceLine.from_json({"name": "X", "chaos": 2, "future": 1}) == PriceLine("X", 2)
    store = Store(tmp_path / "db")
    with store.transaction():
        store.save_prices("L", "item", "Card", 1.0, [line])
        store.conn.execute("INSERT INTO price_cache (league, source, type, fetched_at, lines) "
                           "VALUES ('L', 'item', 'Broken', 1.0, 'not json')")  # fmt: skip
    tables = store.load_prices("L")  # the unreadable table is skipped, not an error
    assert [(t[1], t[3]) for t in tables] == [("Card", [line])]


def test_fragments_and_emptied_tables_come_from_the_exchange() -> None:
    from poefire.pricing.ninja import PRICE_SOURCES

    assert ("exchange", "Fragment") in PRICE_SOURCES
    c = client(Ninja())
    assert c.fetch("item", "L", "Vial") == [PriceLine("Vial of Fate", 7.0)]
    b = PriceBook((src, t, c.fetch(src, "L", t))
                  for src, t in [("currency", "Fragment"), ("exchange", "Fragment")])  # fmt: skip
    reality = {"typeLine": "Reality Fragment", "baseType": "Reality Fragment", "frameType": 0}
    quote = b.price(reality)
    assert quote is not None and quote.chaos_each == 42.0
    blossom = b.price({"typeLine": "Sacred Blossom", "frameType": 0})
    assert blossom is not None and blossom.chaos_each == 3.0


def test_blighted_maps_are_priced_per_tier() -> None:
    b = PriceBook([
        ("item", "Map", [PriceLine("Strand Map", 4.0, base_type="Strand Map", map_tier=16)]),
        ("item", "BlightedMap", [PriceLine("Blighted Map (Tier 16)", 9.0),
                                 PriceLine("Blighted Map (Tier 16)", 7.0),  # variants: lowest
                                 PriceLine("Blighted Map (Tier 3)", 2.0)]),
        ("item", "BlightRavagedMap", [PriceLine("Blight-ravaged Map (Tier 16)", 30.0)]),
    ])  # fmt: skip

    def m(type_line: str, tier: int) -> float | None:
        q = b.price({"typeLine": type_line, "baseType": "Strand Map", "frameType": 0,
                     "properties": [{"name": "Map Tier", "values": [[str(tier), 0]]}]})  # fmt: skip
        return q.chaos_each if q else None

    assert m("Strand Map", 16) == 4.0
    assert m("Blighted Strand Map", 16) == 7.0  # not the plain map's price
    assert m("Blighted Strand Map", 3) == 2.0
    assert m("Blight-ravaged Strand Map", 16) == 30.0
    assert m("Blighted Strand Map", 10) is None  # no price for that tier


def test_blighted_maps_per_map_and_thin_map_prices_left_out() -> None:
    b = PriceBook([
        ("item", "BlightedMap", [
            PriceLine("Blighted Strand Map", 12.0, map_tier=16, listings=40),
            PriceLine("Blighted Beach Map", 5.0, map_tier=16, listings=80),
            PriceLine("Blighted Ashen Wood Map", 30056.0, map_tier=16, listings=7),  # 1 seller
        ]),
        ("item", "UniqueMap", [PriceLine("Mobilised Mire", 542152.0, map_tier=17, listings=2,
                                         base_type="Burial Chambers Map")]),
    ])  # fmt: skip

    def m(type_line: str, frame: int = 0, name: str = "") -> float | None:
        q = b.price({"name": name, "typeLine": type_line, "baseType": type_line, "frameType": frame,
                     "properties": [{"name": "Map Tier", "values": [["16", 0]]}]})  # fmt: skip
        return q.chaos_each if q else None

    assert m("Blighted Strand Map") == 12.0  # its own price
    assert m("Blighted Ashen Wood Map") == 5.0  # thin: the tier's cheapest instead
    assert m("Burial Chambers Map", 3, "Mobilised Mire") is None


def test_price_sources() -> None:
    from poefire.pricing.ninja import PRICE_SOURCES

    types = {t for _, t in PRICE_SOURCES}
    assert "Scarab" not in types  # gone from the game
    for t in ("AllflameEmber", "Runegraft", "Astrolabe", "Thread", "Wombgift", "BlightedMap",
              "BlightRavagedMap", "UniqueTincture", "UniqueRelic"):  # fmt: skip
        assert t in types
    assert ("exchange", "DivinationCard") in PRICE_SOURCES  # straight to where it's priced


def test_spending_is_not_counted_but_sales_are_taken_out() -> None:
    div = {"typeLine": "Divine Orb", "frameType": 5}
    v = value_deltas([
        ItemDelta("a", "Divine Orb", 5, 3, div),
        ItemDelta("sold|x", "Sold on trade · Ring", 5, -1, div),  # a sale's payment: out
        ItemDelta("b", "Strand Map", 0, -2, {"typeLine": "Strand Map", "frameType": 0,
                  "properties": [{"name": "Map Tier", "values": [["16", 0]]}]}),
    ], book())  # fmt: skip
    assert v.chaos == 2 * 180 and v.spent == 8
    only_spent = value_deltas([ItemDelta("a", "Divine Orb", 5, -1, div)], book())
    assert only_spent.chaos == 0 and only_spent.spent == 180  # never negative


def test_maps_are_priced_by_name_not_mods() -> None:
    """Current leagues: no "Map Tier" property, the tier is in the base ("Map (Tier 16)");
    poe.ninja prices "Map (Tier 16)", "Nightmare Map"... without a tier."""
    b = PriceBook([("item", "Map", [PriceLine("Map (Tier 16)", 2.0),
                                    PriceLine("Nightmare Map", 70.0),
                                    PriceLine("Shaper Guardian Map", 5.0)]),
                   ("item", "BlightedMap", [PriceLine("Blighted Map (Tier 16)", 3.0)]),
                   ("item", "UniqueMap", [PriceLine("Acton's Nightmare", 9.0,
                                                    base_type="Map (Tier 16)")])])  # fmt: skip

    def chaos(**raw: Any) -> float | None:
        q = b.price(raw)
        return q.chaos_each if q else None

    assert chaos(typeLine="Oppressive Map of Rust (Tier 16)", baseType="Map (Tier 16)",
                 frameType=1) == 2.0  # fmt: skip
    assert chaos(name="Gloom Hustle", typeLine="Map (Tier 16)", baseType="Map (Tier 16)",
                 frameType=2, corrupted=True) == 2.0  # fmt: skip
    assert chaos(name="Oblivion Artifice", typeLine="Nightmare Map", baseType="Nightmare Map",
                 frameType=2) == 70.0  # fmt: skip
    assert chaos(name="Hidden Direction", typeLine="Shaper Guardian Map",
                 baseType="Shaper Guardian Map", frameType=2) == 5.0  # fmt: skip
    assert chaos(typeLine="Blighted Map (Tier 16)", baseType="Blighted Map (Tier 16)",
                 frameType=0) == 3.0  # fmt: skip
    assert chaos(name="Acton's Nightmare", typeLine="Map (Tier 16)", baseType="Map (Tier 16)",
                 frameType=3) == 9.0  # fmt: skip
    assert chaos(typeLine="Map (Tier 15)", baseType="Map (Tier 15)", frameType=0) is None
    # blighted on a plain base: the blighted price, not the plain map's
    assert chaos(typeLine="Blighted Map (Tier 16)", baseType="Map (Tier 16)", frameType=0) == 3.0
