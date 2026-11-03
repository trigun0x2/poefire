from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from poefire.api.session import SessionStore
from poefire.config import Config
from poefire.loot.items import ItemEntry
from poefire.loot.recorder import TAB_NAMES_KEY, LootRecorder, Trigger
from poefire.loot.snapshot import Snapshot
from poefire.pricing.ninja import NinjaClient, NinjaError, PriceLine
from poefire.pricing.service import PriceService
from poefire.store import Store
from poefire.web.app import create_app
from poefire.web.runtime import AppRuntime
from poefire.web.window import WindowControls

NOW = datetime(2026, 11, 3, 12, 0)


class OfflineNinja(NinjaClient):
    def __init__(self) -> None:
        super().__init__("test")

    def fetch(self, source: str, league: str, type_: str) -> list[PriceLine]:
        raise NinjaError("offline")

    def leagues(self) -> list[str]:
        return ["Keepers"]


def stack(name: str, n: int, source: str) -> ItemEntry:
    return ItemEntry.from_api(
        {"id": f"{name}-{source}", "typeLine": name, "frameType": 5, "stackSize": n,
         "icon": f"https://example.invalid/{name}.png"},
        source,
    )  # fmt: skip


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, AppRuntime, Path]:
    monkeypatch.delenv(SessionStore.ENV, raising=False)
    db = tmp_path / "db.sqlite3"
    with Store(db) as s, s.transaction():
        s.save_prices("Keepers", "exchange", "Currency", 1e12,
                      [PriceLine("Divine Orb", 200.0), PriceLine("Exalted Orb", 10.0)])  # fmt: skip
    cfg = Config(db_path=db, session_path=tmp_path / "s.json", league="Keepers")
    rt = AppRuntime(cfg, PriceService(OfflineNinja(), lambda: Store(db)), recorder=None,
                    log_path=tmp_path / "none.txt", now=lambda: NOW)  # fmt: skip
    client = TestClient(create_app(rt))
    client.headers["host"] = "127.0.0.1:8460"
    return client, rt, db


def seed_history(db: Path) -> None:
    with Store(db) as s, s.transaction():
        for days, chaos, cpd in [(40, 1000.0, 100.0), (6, 2000.0, 200.0), (2, 3000.0, None)]:
            sid = s.add_snapshot(Snapshot(NOW - timedelta(days=days)), "run_return", None)
            s.add_snapshot_value(sid, NOW - timedelta(days=days), "Keepers", chaos, cpd, 0)
        sid = s.add_snapshot(Snapshot(NOW), "manual", None)
        s.add_snapshot_value(sid, NOW, "Standard", 5.0, 200.0, 0)  # other league: excluded


def test_history_ranges_and_divine_conversion(env: tuple[TestClient, AppRuntime, Path]) -> None:
    client, _, db = env
    seed_history(db)
    week = client.get("/api/overview/history?range=7d").json()
    assert week["league"] == "Keepers"
    assert [(p["chaos"], p["divine"]) for p in week["points"]] == [
        (2000.0, 10.0),  # divine at the rate when it was recorded
        (3000.0, 15.0),  # no rate recorded: today's rate (200)
    ]
    assert len(client.get("/api/overview/history?range=all").json()["points"]) == 3
    assert client.get("/api/overview/history?range=24h").json()["points"] == []
    assert client.get("/api/overview/history?range=bogus").json()["range"] == "7d"


def seed_holdings(db: Path) -> None:
    items = [
        stack("Divine Orb", 2, "tabA"),
        stack("Divine Orb", 1, "inventory"),
        stack("Exalted Orb", 5, "tabB"),
        stack("Mystery Orb", 9, "tabB"),
    ]
    snap = Snapshot(NOW, items)
    with Store(db) as s, s.transaction():
        s.add_snapshot(snap, "manual", None)
        s.set_json(TAB_NAMES_KEY, {"inventory": "Inventory", "tabA": "Currency", "tabB": "Dump"})


def test_holdings_and_tab_filter(env: tuple[TestClient, AppRuntime, Path]) -> None:
    client, _, db = env
    seed_holdings(db)
    d = client.get("/api/overview/items").json()
    assert d["total_chaos"] == 3 * 200 + 5 * 10
    assert d["total_divine"] == round(650 / 200, 2)
    assert [t["name"] for t in d["tabs"]] == ["Inventory", "Currency", "Dump"]
    tab_values = [(t["name"], t["chaos"]) for t in d["tabs"]][1:]
    assert tab_values == [("Currency", 400.0), ("Dump", 50.0)]
    first = d["items"][0]
    assert (first["name"], first["quantity"], first["chaos_total"]) == ("Divine Orb", 3, 600.0)
    assert first["tabs"] == ["Inventory", "Currency"] and first["share"] == round(600 / 650, 4)
    # unpriced items are hidden from the list but counted
    assert "Mystery Orb" not in [i["name"] for i in d["items"]]
    assert d["unpriced"] == 1 and d["hidden"] == 1

    only = client.get("/api/overview/items?tabs=tabB").json()
    assert only["total_chaos"] == 50.0 and [i["name"] for i in only["items"]] == [
        "Exalted Orb"]  # fmt: skip
    both = client.get("/api/overview/items?tabs=tabA,inventory").json()
    assert both["total_chaos"] == 600.0


def test_holdings_without_snapshot(env: tuple[TestClient, AppRuntime, Path]) -> None:
    client, _, _ = env
    d = client.get("/api/overview/items").json()
    assert d["taken_at"] is None and d["items"] == [] and d["loot_enabled"] is False


class OneSnapshot:
    def __init__(self, items: list[ItemEntry]) -> None:
        self.items = items

    def take(self) -> Snapshot:
        return Snapshot(NOW, list(self.items))

    def tab_names(self) -> dict[str, str]:
        return {"inventory": "Inventory", "tabA": "Currency"}


def test_recorder_values_each_snapshot(env: tuple[TestClient, AppRuntime, Path]) -> None:
    _, rt, db = env
    rec = LootRecorder(OneSnapshot([stack("Divine Orb", 2, "tabA")]),  # type: ignore[arg-type]
                       lambda: Store(db), now=lambda: NOW)  # fmt: skip
    rec.valuer = rt.value_snapshot
    rec.record(Trigger.MANUAL)
    rec.stop()
    with Store(db) as s:
        assert s.value_history("Keepers") == [(NOW, 400.0, 200.0)]
        assert s.get_json(TAB_NAMES_KEY) == {"inventory": "Inventory", "tabA": "Currency"}


class FakeWindow:
    def __init__(self) -> None:
        self.x, self.y, self.width, self.height = 100, 50, 1360, 860
        self.calls: list[Any] = []

    def minimize(self) -> None:
        self.calls.append("minimize")

    def maximize(self) -> None:
        self.calls.append("maximize")

    def resize(self, width: int, height: int) -> None:
        self.calls.append(("resize", width, height))

    def move(self, x: int, y: int) -> None:
        self.calls.append(("move", x, y))

    def destroy(self) -> None:
        self.calls.append("destroy")


def test_window_controls(monkeypatch: pytest.MonkeyPatch) -> None:
    w = FakeWindow()
    c = WindowControls()
    c._attach(w)
    monkeypatch.setattr("poefire.web.window.work_area", lambda *a: (0, 0, 1920, 1040))
    assert c.toggle_maximize() is True and c.is_maximized()
    assert w.calls[-2:] == [("move", 0, 0), ("resize", 1920, 1040)]
    assert c.toggle_maximize() is False
    assert w.calls[-2:] == [("resize", 1360, 860), ("move", 100, 50)]
    c.resize(300, 200)  # clamped to the minimum size
    assert w.calls[-1] == ("resize", 900, 600)
    c.minimize()
    c.close()
    assert w.calls[-2:] == ["minimize", "destroy"]
    # no work area (not Windows): native maximize
    monkeypatch.setattr("poefire.web.window.work_area", lambda *a: None)
    assert c.toggle_maximize() is True and w.calls[-1] == "maximize"


def test_window_api_exposes_only_controls() -> None:
    public = {n for n in dir(WindowControls) if not n.startswith("_")}
    assert public == {"minimize", "is_maximized", "toggle_maximize", "resize", "close"}
