from __future__ import annotations

import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from poefire.builds import BuildSnapshot, BuildWatcher, summarize, wants_snapshot
from poefire.config import Config
from poefire.events import LevelUp, ZoneEntered
from poefire.pricing.ninja import NinjaClient, NinjaError, PriceLine
from poefire.pricing.service import PriceService
from poefire.store import Store
from poefire.web.app import create_app
from poefire.web.runtime import AppRuntime

NOW = datetime(2026, 11, 4, 20, 0)


def gem(name: str, socket: int, level: str = "20", support: bool = False) -> dict[str, Any]:
    return {"typeLine": name, "frameType": 4, "support": support, "socket": socket,
            "properties": [{"name": "Level", "values": [[level, 0]]},
                           {"name": "Quality", "values": [["+20%", 1]]}]}  # fmt: skip


WEBSITE_ITEMS: dict[str, Any] = {
    "character": {"name": "Speedy", "class": "Deadeye", "classId": 2, "ascendancyClass": 1,
                  "league": "Keepers", "level": 42},
    "items": [
        {"inventoryId": "Helm", "name": "Doom Visor", "typeLine": "Hubris Circlet",
         "frameType": 2, "icon": "helm.png",
         "sockets": [{"group": 0, "sColour": "B"}, {"group": 0, "sColour": "B"},
                     {"group": 1, "sColour": "R"}],
         "socketedItems": [gem("Arc", 0),
                           gem("Added Lightning Damage Support", 1, "18 (Max)", True),
                           gem("Flame Dash", 2, "5")]},
        {"inventoryId": "Weapon", "name": "", "typeLine": "Driftwood Wand", "frameType": 0},
        {"inventoryId": "Ring2", "name": "Ventor's Gamble", "typeLine": "Gold Ring",
         "frameType": 3},
        {"inventoryId": "Flask", "x": 1, "typeLine": "Quicksilver Flask", "frameType": 1},
        {"inventoryId": "Flask", "x": 0, "typeLine": "Divine Life Flask", "frameType": 0},
        {"inventoryId": "MainInventory", "typeLine": "Chaos Orb", "frameType": 5},
    ],
}  # fmt: skip
WEBSITE_PASSIVES: dict[str, Any] = {
    "hashes": [1, 2, 3, 4], "hashes_ex": [70000], "mastery_effects": {"5": "6"},
    "items": [{"typeLine": "Cobalt Jewel", "x": 3}], "jewel_data": {}, "bandit_choice": "Alira",
}  # fmt: skip
BUILD: dict[str, Any] = {"source": "website", "items": WEBSITE_ITEMS, "passives": WEBSITE_PASSIVES}


class FakeApi:
    def __init__(self, fail: int = 0) -> None:
        self.calls: list[str] = []
        self.fail = fail

    def list_characters(self) -> list[dict[str, Any]]:
        return [{"name": "Speedy", "league": "Keepers"}, {"name": "Alt", "league": "Standard"}]

    def get_build(self, name: str) -> dict[str, Any]:
        self.calls.append(name)
        if self.fail:
            self.fail -= 1
            raise RuntimeError("rate limited")
        return BUILD


def test_schedule() -> None:
    assert [lv for lv in range(1, 21) if wants_snapshot(lv)] == [5, 10, 15, 16, 17, 18, 19, 20]


def test_summary_from_website_data() -> None:
    s = summarize(BuildSnapshot("Speedy", 42, NOW, data=BUILD, id=7))
    assert (s["class"], s["league"], s["ok"]) == ("Deadeye", "Keepers", True)
    assert [g["slot"] for g in s["gear"]] == ["Weapon", "Helmet", "Ring 2", "Flask", "Flask"]
    assert [g["base"] for g in s["gear"] if g["slot"] == "Flask"] == [
        "Divine Life Flask",
        "Quicksilver Flask",
    ]
    helm = s["gear"][1]
    assert helm["links"] == "B-B R" and helm["rarity"] == "rare"
    assert [[g["name"] for g in grp] for grp in helm["gems"]] == [
        ["Arc", "Added Lightning Damage Support"],
        ["Flame Dash"],
    ]
    assert helm["gems"][0][1] == {
        "name": "Added Lightning Damage Support",
        "support": True,
        "level": 18,
        "quality": 20,
        "icon": None,
    }
    assert s["passives"] == {"allocated": 5, "masteries": 1, "jewels": 1, "bandit": "Alira"}


def test_summary_from_oauth_data_and_missed() -> None:
    oauth = {
        "source": "oauth",
        "character": {
            "name": "Speedy",
            "class": "Deadeye",
            "league": "Keepers",
            "level": 42,
            "equipment": [WEBSITE_ITEMS["items"][0]],
            "jewels": [{}],
            "passives": WEBSITE_PASSIVES,
        },
    }
    s = summarize(BuildSnapshot("Speedy", 42, NOW, data=oauth))
    assert s["gear"][0]["slot"] == "Helmet" and s["passives"]["jewels"] == 1
    missed = summarize(BuildSnapshot("Speedy", 15, NOW, error="rate limited"))
    assert missed["ok"] is False and "gear" not in missed


def watcher(tmp_path: Path, api: FakeApi | None) -> BuildWatcher:
    return BuildWatcher(lambda: api, lambda: Store(tmp_path / "db"), lambda: NOW, retry_after=0.01)


def test_levels_are_captured_at_the_next_zone_change(tmp_path: Path) -> None:
    api = FakeApi()
    w = watcher(tmp_path, api)
    got = threading.Event()
    w.on_snapshot = lambda snap: got.set() if snap.level == 16 else None
    t = NOW - timedelta(minutes=1)
    w.handle(LevelUp(t, "Speedy", "Ranger", 14), "")  # not a snapshot level
    assert w.pending == {}
    w.handle(LevelUp(t, "Speedy", "Ranger", 15), "")
    w.handle(LevelUp(t, "Speedy", "Ranger", 16), "")  # same zone: one snapshot, level 16
    assert api.calls == []  # waits for the game to save on a zone change
    w.handle(ZoneEntered(t, "The Coast"), "")
    assert got.wait(5)
    w.stop()
    with Store(tmp_path / "db") as s:
        (snap,) = s.list_build_snapshots("Speedy")
        assert snap.level == 16 and snap.ok and snap.league == "Keepers"
        full = s.get_build_snapshot(snap.id or 0)
        assert full is not None and full.data == BUILD  # stored compressed, read back whole
        assert s.build_characters()[0]["snapshots"] == 1


def test_old_lines_others_and_no_connection_are_ignored(tmp_path: Path) -> None:
    api = FakeApi()
    w = watcher(tmp_path, api)
    old = NOW - timedelta(hours=3)  # catching up on the log after a restart
    w.handle(LevelUp(old, "Speedy", "Ranger", 20), "")
    assert w.pending == {}
    assert w.capture("Stranger", 30) is None  # a party member, not on this account
    offline = watcher(tmp_path, None)
    offline.handle(LevelUp(NOW, "Speedy", "Ranger", 20), "")
    offline.handle(ZoneEntered(NOW, "Town"), "")
    assert offline.pending == {} and "account" in (offline.last_error or "")
    with Store(tmp_path / "db") as s:
        assert s.build_characters() == []


def test_failure_is_retried_once_then_recorded(tmp_path: Path) -> None:
    w = watcher(tmp_path, FakeApi(fail=1))
    done = threading.Event()
    w.on_snapshot = lambda snap: done.set()
    w.request("Speedy", 20)
    assert done.wait(5)  # first try failed; the retry worked
    w2 = watcher(tmp_path, FakeApi(fail=5))
    snap = w2.capture("Speedy", 21, final=True)
    assert snap is not None and not snap.ok and snap.error == "rate limited"
    w.stop()
    with Store(tmp_path / "db") as s:
        assert [(b.level, b.ok) for b in s.list_build_snapshots("Speedy")] == [
            (20, True),
            (21, False),
        ]


# --- API ---


class Offline(NinjaClient):
    def __init__(self) -> None:
        super().__init__("t")

    def fetch(self, source: str, league: str, type_: str) -> list[PriceLine]:
        raise NinjaError("offline")

    def leagues(self) -> list[str]:
        return []


H = {"X-Poefire": "1"}


@pytest.fixture
def client(tmp_path: Path) -> tuple[TestClient, AppRuntime]:
    db = tmp_path / "db.sqlite3"
    prices = PriceService(Offline(), lambda: Store(db))
    rt = AppRuntime(Config(db_path=db, league="Keepers"), prices, recorder=None,
                    log_path=tmp_path / "none.txt", now=lambda: NOW)  # fmt: skip
    c = TestClient(create_app(rt))
    c.headers["host"] = "127.0.0.1:8460"
    return c, rt


def test_builds_api(client: tuple[TestClient, AppRuntime], monkeypatch: pytest.MonkeyPatch) -> None:
    c, rt = client
    d = c.get("/api/builds").json()
    assert d["characters"] == [] and d["connected"] is False and d["enabled"] is True
    assert c.post("/api/builds/character/Speedy/snapshot", headers=H).status_code == 409
    api = FakeApi()
    monkeypatch.setattr(rt, "_build_api", lambda: api)
    rt.builds.api_getter = lambda: api
    snap = rt.builds.capture("Speedy", 0)  # "snapshot now": level from the website
    assert snap is not None and snap.level == 42
    chars = c.get("/api/builds").json()["characters"]
    assert chars[0]["character"] == "Speedy" and chars[0]["class"] == "Deadeye"
    levels = c.get("/api/builds/character/Speedy").json()["snapshots"]
    assert [lv["level"] for lv in levels] == [42]
    detail = c.get(f"/api/builds/snapshot/{levels[0]['id']}").json()
    assert detail["gear"][1]["links"] == "B-B R"
    code = c.get(f"/api/builds/snapshot/{levels[0]['id']}/pob").json()
    assert code["tree_version"] == "3_29" and code["code"].startswith("eN")
    assert c.post(f"/api/builds/snapshot/{levels[0]['id']}/delete", headers=H).json() == {
        "ok": True
    }
    assert c.get(f"/api/builds/snapshot/{levels[0]['id']}").status_code == 404
    off = c.post("/api/settings", headers=H, json={"build_snapshots": False}).json()
    assert off["build_snapshots"] is False and rt.builds.enabled is False
    rt.stop()


def test_characters_are_listed_by_level(tmp_path: Path) -> None:
    t = datetime(2026, 11, 7, 12)
    with Store(tmp_path / "db") as s:
        for char, level, age in [("Fresh", 12, 0), ("Main", 92, 5), ("Alt", 60, 2)]:
            s.add_build_snapshot(BuildSnapshot(char, level, t - timedelta(days=age), "L", "Witch",
                                               {"items": [], "passives": {}}))  # fmt: skip
        assert [c["character"] for c in s.build_characters()] == ["Main", "Alt", "Fresh"]


class HighLevelApi(FakeApi):
    def __init__(self, level: int = 92) -> None:
        super().__init__()
        self.level = level
        self.listed = 0

    def list_characters(self) -> list[dict[str, Any]]:
        self.listed += 1
        return [{"name": "Speedy", "league": "Keepers", "level": self.level, "class": "Deadeye",
                 "current": True}, {"name": "Alt", "league": "Standard", "level": 95}]  # fmt: skip


class ChangingApi(HighLevelApi):
    """The build changes when `gear` does."""

    def __init__(self, level: int = 92) -> None:
        super().__init__(level)
        self.gear = "helm-1"

    def get_build(self, name: str) -> dict[str, Any]:
        self.calls.append(name)
        if self.fail:
            self.fail -= 1
            raise RuntimeError("rate limited")
        items = [{**WEBSITE_ITEMS["items"][0], "id": self.gear}]
        return {**BUILD, "items": {**BUILD["items"], "items": items}}


def test_stale_level_snapshots(tmp_path: Path) -> None:
    """From level 80: after `stale_hours` without a snapshot, one is taken at a zone change
    (an hour into the day's play), and kept only if the build changed."""
    api = ChangingApi()
    clock = [NOW]
    w = BuildWatcher(lambda: api, lambda: Store(tmp_path / "db"), lambda: clock[0])
    w.stale_hours = 6
    w._ensure_worker = lambda: None  # type: ignore[method-assign]  # jobs run here instead

    def zone(minutes: int) -> None:
        clock[0] = NOW + timedelta(minutes=minutes)
        w.handle(ZoneEntered(clock[0], "Strand"), "")
        while not w._queue.empty():
            job = w._queue.get()
            if job is not None and job[1] == -1:
                w.daily_snapshot()

    def stored() -> list[BuildSnapshot]:
        with Store(tmp_path / "db") as s:
            return s.list_build_snapshots("Speedy")

    zone(0)
    zone(30)
    assert api.calls == []  # not an hour of play yet
    zone(61)
    (snap,) = stored()
    assert snap.level == 92 and snap.ok and snap.char_class == "Deadeye"
    zone(130)
    zone(300)
    assert api.calls == ["Speedy"]  # 6 hours not up yet
    zone(61 + 6 * 60)
    assert api.calls == ["Speedy", "Speedy"] and len(stored()) == 1  # same build: not kept
    zone(61 + 7 * 60)
    assert len(api.calls) == 2  # checked an hour ago: the next check in 6 hours
    api.gear = "helm-2"  # a new helmet
    zone(61 + 12 * 60 + 5)
    assert len(stored()) == 2  # changed: kept
    w.stale_hours = 0  # off
    zone(61 + 30 * 60)
    assert len(api.calls) == 3


def test_no_stale_snapshot_below_80_or_too_soon(tmp_path: Path) -> None:
    api = HighLevelApi()
    with Store(tmp_path / "db") as s:
        s.add_build_snapshot(BuildSnapshot("Speedy", 91, NOW - timedelta(hours=2), data=BUILD))
    w = BuildWatcher(lambda: api, lambda: Store(tmp_path / "db"), lambda: NOW)
    assert w.daily_snapshot() is None and api.calls == []  # a level-up snapshot 2 hours ago
    low = HighLevelApi(level=79)
    w2 = BuildWatcher(lambda: low, lambda: Store(tmp_path / "other"), lambda: NOW)
    assert w2.daily_snapshot() is None and low.calls == []
    failing = HighLevelApi()
    failing.fail = 1
    w3 = BuildWatcher(lambda: failing, lambda: Store(tmp_path / "third"), lambda: NOW)
    with pytest.raises(Exception):  # noqa: B017 (retried at a zone change an hour later)
        w3.daily_snapshot()
    with Store(tmp_path / "third") as s:
        assert s.build_characters() == []  # a failed try isn't stored as a missed level


def test_changes_between_snapshots(tmp_path: Path) -> None:
    from poefire import builds

    def build(helm: str, gems: list[str], hashes: list[int]) -> dict[str, Any]:
        item = {**WEBSITE_ITEMS["items"][0], "id": helm,
                "socketedItems": [{"typeLine": g, "socket": 0} for g in gems]}  # fmt: skip
        passives = {**BUILD["passives"], "hashes": hashes}
        return {**BUILD, "items": {**BUILD["items"], "items": [item]}, "passives": passives}

    a = BuildSnapshot("Speedy", 90, NOW, data=build("h1", ["Fireball"], [1, 2]), id=1)
    b = BuildSnapshot("Speedy", 90, NOW, data=build("h1", ["Fireball"], [1, 2]), id=2)
    c = BuildSnapshot("Speedy", 91, NOW, data=build("h2", ["Fireball", "Added Fire Damage Support"],
                                                    [1, 2, 3]), id=3)  # fmt: skip
    assert builds.fingerprint(a.data or {}) == builds.fingerprint(b.data or {})
    assert builds.fingerprint(a.data or {}) != builds.fingerprint(c.data or {})
    d = builds.changes(a, c)
    key = builds.summarize(c)["gear"][0]["key"]
    assert d["slots"] == [key] and d["gems"] == ["Added Fire Damage Support"]
    assert d["passives"] == 1 and d["tree_changed"] and d["previous"]["id"] == 1
    assert builds.changes(a, b)["slots"] == []
    with Store(tmp_path / "db") as s:
        for snap in (a, c):
            snap.id = s.add_build_snapshot(snap)
        prev = s.previous_build_snapshot(c)
        assert prev is not None and prev.id == a.id
        assert s.previous_build_snapshot(a) is None
