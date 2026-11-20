"""Price check (step 2): the game's item text, parsed and priced from poe.ninja."""

from __future__ import annotations

from typing import Any

import pytest

from poefire import gameinput as gi
from poefire import pricecheck
from poefire.gameinput import GameInput
from poefire.pricing.book import PriceBook
from poefire.pricing.ninja import PriceLine
from poefire.web.runtime import price_line

REALITY = """Item Class: Map Fragments
Rarity: Normal
Reality Fragment
--------
The Absence of Mercy and Empathy is level 85
The Maven's full power is unleashed in The Absence of Mercy and Empathy
--------
You must rise from mere amusement to respected equal.
--------
Use four of these in a personal Map Device to open Portals to The Absence of Mercy and Empathy. Can only be used once.
"""  # noqa: E501
DIVINES = """Item Class: Stackable Currency
Rarity: Currency
Divine Orb
--------
Stack Size: 7/20
--------
Randomises the numeric values of the random modifiers on an item
--------
Right click this item then left click a magic, rare or unique item to apply it.
Shift click to unstack.
"""
TABULA = """Item Class: Body Armours
Rarity: Unique
Tabula Rasa
Simple Robe
--------
Requirements:
Level: 68
--------
Sockets: W-W-W-W-W-W
--------
Item Level: 84
--------
Item has no Attribute Requirements
--------
Corrupted
"""
GEM = """Item Class: Support Gems
Rarity: Gem
Enlighten Support
--------
Support
Level: 3
Quality: +20% (augmented)
--------
Experience: 1/1
"""
MAGIC_MAP = """Item Class: Maps
Rarity: Magic
Fecund Strand Map of Exposure
--------
Map Tier: 16
Item Quantity: +12% (augmented)
--------
Item Level: 83
--------
Area is inhabited by an additional Rogue Exile
"""
RARE = """Item Class: Rings
Rarity: Rare
Storm Loop
Two-Stone Ring
--------
Item Level: 86
--------
+12% to Fire and Cold Resistances (implicit)
--------
+45 to maximum Life
+40% to Lightning Resistance
"""


def book() -> PriceBook:
    return PriceBook([
        ("exchange", "Currency", [PriceLine("Divine Orb", 182.0)]),
        ("exchange", "Fragment", [PriceLine("Reality Fragment", 64.83)]),
        ("item", "UniqueArmour", [PriceLine("Tabula Rasa", 10.0, base_type="Simple Robe"),
                                  PriceLine("Tabula Rasa", 30.0, base_type="Simple Robe",
                                            links=6)]),
        ("item", "SkillGem", [PriceLine("Enlighten Support", 900.0, gem_level=3, gem_quality=20),
                              PriceLine("Enlighten Support", 2.0, gem_level=1)]),
        ("item", "Map", [PriceLine("Strand Map", 4.0, base_type="Strand Map", map_tier=16)]),
    ])  # fmt: skip


def test_parse() -> None:
    r = pricecheck.parse(REALITY)
    assert (r.item_class, r.rarity, r.name, r.base, r.stack) == (
        "Map Fragments", "Normal", "", "Reality Fragment", 1)  # fmt: skip
    d = pricecheck.parse(DIVINES)
    assert d.base == "Divine Orb" and d.stack == 7 and d.frame_type == 5
    t = pricecheck.parse(TABULA.replace("\n", "\r\n"))  # Windows line ends
    assert (t.name, t.base, t.links, t.corrupted, t.item_level) == (
        "Tabula Rasa", "Simple Robe", 6, True, 84)  # fmt: skip
    g = pricecheck.parse(GEM)
    assert g.int_property("Level") == 3 and g.int_property("Quality") == 20
    for not_item in ("", "hello", "/hideout", "Item Class: x"):
        with pytest.raises(pricecheck.NotAnItem):
            pricecheck.parse(not_item)


@pytest.mark.parametrize(
    ("text", "each", "total"),
    [(REALITY, 64.83, 64.83), (DIVINES, 182.0, 1274.0), (TABULA, 30.0, 30.0),
     (GEM, 900.0, 900.0), (MAGIC_MAP, 4.0, 4.0)],
)  # fmt: skip
def test_prices_from_poe_ninja(text: str, each: float, total: float) -> None:
    r = pricecheck.price(pricecheck.parse(text), book())
    assert r.chaos_each == each and r.chaos_total == pytest.approx(total)
    j = r.to_json(182.0)
    assert j["chaos_total"] == round(total, 2) and j["note"] is None


def test_rares_wait_for_the_trade_search() -> None:
    r = pricecheck.price(pricecheck.parse(RARE), book())
    assert r.chaos_each is None and "trade search" in (r.note or "")
    unknown = pricecheck.parse(REALITY.replace("Reality Fragment", "Mystery Fragment"))
    assert pricecheck.price(unknown, book()).note == "poe.ninja has no price for it"


def test_overlay_line() -> None:
    j = pricecheck.price(pricecheck.parse(DIVINES), book()).to_json(182.0)
    assert price_line(j, 182.0) == "Divine Orb x7: 7.00d (182c each)"
    j = pricecheck.price(pricecheck.parse(REALITY), book()).to_json(182.0)
    assert price_line(j, 182.0) == "Reality Fragment: 64.83c"
    j = pricecheck.price(pricecheck.parse(RARE), book()).to_json(182.0)
    assert price_line(j, 182.0).startswith("Storm Loop Two-Stone Ring: needs a trade search")


class CopyingGame:
    """The game copies the hovered item's text on Ctrl+C."""

    def __init__(self, item: str | None) -> None:
        self.item, self.clipboard, self.events = item, "my own text", []  # type: ignore[var-annotated]

    def game_focused(self) -> bool:
        return True

    def key_down(self, vk: int) -> bool:
        return False

    def send_key(self, vk: int, up: bool) -> None:
        self.events.append((vk, up))
        if vk == gi.VK_C and not up and self.item is not None:
            self.clipboard = self.item

    def get_clipboard(self) -> str | None:
        return self.clipboard

    def set_clipboard(self, text: str) -> bool:
        self.clipboard = text
        return True


def test_copy_item_sends_ctrl_c_once_and_restores_the_clipboard() -> None:
    game = CopyingGame(REALITY)
    assert GameInput(game, sleep=lambda s: None).copy_item() == REALITY
    assert game.events == [(gi.VK_CONTROL, False), (gi.VK_C, False), (gi.VK_C, True),
                           (gi.VK_CONTROL, True)]  # fmt: skip
    assert game.clipboard == "my own text"
    nothing = CopyingGame(None)  # no item under the cursor
    assert GameInput(nothing, sleep=lambda s: None).copy_item() is None
    assert nothing.clipboard == "my own text"


def test_price_check_api(tmp_path: Any) -> None:
    from fastapi.testclient import TestClient
    from test_web import OfflineNinja

    from poefire.config import Config
    from poefire.pricing.service import PriceService
    from poefire.store import Store
    from poefire.web.app import create_app
    from poefire.web.runtime import AppRuntime

    db = tmp_path / "db"
    with Store(db) as s, s.transaction():
        s.save_prices(
            "Keepers", "exchange", "Fragment", 1e12, [PriceLine("Reality Fragment", 65.0)]
        )
    rt = AppRuntime(Config(db_path=db, league="Keepers"),
                    PriceService(OfflineNinja(), lambda: Store(db)), recorder=None)  # fmt: skip
    client = TestClient(create_app(rt))
    client.headers["host"] = "127.0.0.1:8460"
    h = {"X-Poefire": "1"}
    assert client.get("/api/pricecheck/last").json() == {"result": None}
    r = client.post("/api/pricecheck", headers=h, json={"text": REALITY}).json()["result"]
    assert r["name"] == "Reality Fragment" and r["chaos_each"] == 65.0
    assert client.get("/api/pricecheck/last").json()["result"]["name"] == "Reality Fragment"
    assert client.post("/api/pricecheck", headers=h, json={"text": "hi"}).status_code == 400
    assert client.get("/api/settings").json()["price_check_hotkey"] == "Ctrl+D"
    clash = {"price_check_hotkey": "F5"}  # F5 is /hideout
    assert client.post("/api/settings", headers=h, json=clash).status_code == 400
    rt.stop()
