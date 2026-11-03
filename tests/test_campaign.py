from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from poefire.campaign import CampaignTracker, act_of, base_class, log_tick
from poefire.config import Config
from poefire.parser import parse_line
from poefire.pricing.ninja import NinjaClient, NinjaError, PriceLine
from poefire.pricing.service import PriceService
from poefire.service import TrackerService
from poefire.store import Store
from poefire.web.app import create_app
from poefire.web.runtime import AppRuntime

T0 = datetime(2026, 11, 3, 20, 0, 0)


class Log:
    """Writes Client.txt lines with a clock and the client's ms tick counter."""

    def __init__(self) -> None:
        self.t = T0
        self.tick = 1_000_000
        self.lines: list[str] = []

    def wait(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)
        self.tick += int(seconds * 1000)

    def line(self, msg: str, level: str = "INFO") -> str:
        text = f"{self.t:%Y/%m/%d %H:%M:%S} {self.tick} cffb065b [{level} Client 1] {msg}"
        self.lines.append(text)
        return text

    def enter(self, area_id: str, zone: str, load: float = 2.0) -> None:
        self.line("Got Instance Details from login server", "DEBUG")
        self.wait(0.05)
        self.line(f'Generating level 1 area "{area_id}" with seed 1', "DEBUG")
        self.wait(load - 0.05)
        self.line(f": You have entered {zone}.")

    def level(self, n: int, cls: str = "Ranger", name: str = "Speedy") -> None:
        self.line(f": {name} ({cls}) is now level {n}")


def feed(tracker: CampaignTracker, log: Log) -> None:
    for text in log.lines:
        tracker.handle(parse_line(text), text)
    log.lines.clear()


def test_helpers() -> None:
    assert act_of("1_1_1") == 1 and act_of("2_10_town") == 10 and act_of("2_6_3a") == 6
    assert act_of("HideoutSpace") is None and act_of("MapWorldsCells") is None
    assert log_tick("2026/11/01 15:52:33 842736578 cffb065b [INFO Client 1] : x") == 842736578
    assert base_class("Deadeye") == "Ranger" and base_class("Witch") == "Witch"
    assert base_class(None) is None


def test_run_splits_timing_and_class(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    clock = [T0]
    tracker = CampaignTracker(store, lambda: clock[0])
    log = Log()
    log.enter("HideoutSpace", "Celestial Hideout")  # before the start: ignored
    feed(tracker, log)
    run = tracker.start()
    log.wait(1)
    log.enter("1_1_1", "The Twilight Strand", load=3)
    log.wait(30)
    log.level(2)
    log.line(": Speedy has been slain.")
    log.line(": Someone (Witch) is now level 40")  # other characters don't count
    log.wait(60)
    log.enter("1_1_town", "Lioneye's Watch")
    log.wait(5)
    log.enter("1_1_1", "The Twilight Strand")  # revisit: no new split
    log.wait(100)
    log.enter("1_2_1", "The Southern Forest")  # act 1 done
    log.wait(20)
    log.enter("Labyrinth_1", "Aspirants' Plaza")  # side area: no act change
    log.level(33, "Deadeye")
    feed(tracker, log)

    assert run.character == "Speedy" and run.char_class == "Deadeye" and run.level == 33
    assert run.deaths == 1 and run.act == 2
    zones = [(s.act, s.name) for s in run.splits if s.kind == "zone"]
    assert zones == [(1, "The Twilight Strand"), (1, "Lioneye's Watch"), (2, "The Southern Forest")]
    (act1,) = run.act_splits()
    assert (
        act1.name == "Act 1" and act1.rta == 2 + 1 + 3 + 30 + 60 + 2 + 5 + 2 + 100 + 2
    )  # 2: pre-start load
    assert run.load_ms == 3000 + 2000 * 4  # five loads, measured by the tick counter
    assert act1.igt == act1.rta - (3 + 2 + 2 + 2)  # loads up to that split
    assert [s.level for s in run.splits if s.kind == "level"] == [2, 33]

    # saved after every change; a restart picks the run up again
    again = CampaignTracker(store, lambda: clock[0])
    assert again.active is not None and again.active.id == run.id
    assert again.active.splits[-1].name == "Level 33"
    store.close()


def test_epilogue_finishes_and_new_start_abandons(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    tracker = CampaignTracker(store, lambda: T0)
    first = tracker.start()
    second = tracker.start("Named")
    assert store.get_campaign_run(first.id or 0).status == "abandoned"  # type: ignore[union-attr]
    assert second.character == "Named"
    log = Log()
    log.wait(5)
    log.enter("2_10_1", "The Cathedral Rooftop")
    log.level(5, name="SomeoneElse")  # bound to "Named": ignored
    log.wait(50)
    log.enter("2_11_town", "Karui Shores")
    feed(tracker, log)
    assert tracker.active is None
    done = store.get_campaign_run(second.id or 0)
    assert done is not None and done.status == "finished" and done.level is None
    assert [s.name for s in done.act_splits()] == ["Act 10"]
    assert done.ended_at == T0 + timedelta(seconds=59)
    store.close()


# --- API ---


class OfflineNinja(NinjaClient):
    def __init__(self) -> None:
        super().__init__("test")

    def fetch(self, source: str, league: str, type_: str) -> list[PriceLine]:
        raise NinjaError("offline")

    def leagues(self) -> list[str]:
        return []


H = {"X-Poefire": "1"}


@pytest.fixture
def env(tmp_path: Path) -> tuple[TestClient, AppRuntime, Path]:
    db = tmp_path / "db.sqlite3"
    log = tmp_path / "Client.txt"
    log.write_text("")
    clock = [T0]
    rt = AppRuntime(Config(db_path=db, league="Keepers"),
                    PriceService(OfflineNinja(), lambda: Store(db)),
                    recorder=None, log_path=log, now=lambda: clock[0])  # fmt: skip
    assert rt.service is not None
    rt.service.start(from_beginning=True)
    client = TestClient(create_app(rt))
    client.headers["host"] = "127.0.0.1:8460"
    return client, rt, log


def test_campaign_api_flow(env: tuple[TestClient, AppRuntime, Path]) -> None:
    client, rt, path = env
    assert client.get("/api/campaign").json() == {"active": None, "pb": None, "runs": []}
    assert client.post("/api/campaign/finish", headers=H).status_code == 409
    d = client.post("/api/campaign/start", json={}, headers=H).json()
    run_id = d["active"]["id"]

    log = Log()
    log.wait(2)
    log.enter("1_1_1", "The Twilight Strand")
    log.level(2, "Witch", "Hexy")
    log.wait(600)
    log.enter("1_2_1", "The Southern Forest")
    path.write_text("\n".join(log.lines) + "\n")
    assert isinstance(rt.service, TrackerService)
    rt.service.poll()

    active = client.get("/api/campaign").json()["active"]
    assert (active["character"], active["class"], active["act"]) == ("Hexy", "Witch", 2)
    assert [a["name"] for a in active["acts"]] == ["Act 1"] and active["acts"][0]["delta"] is None
    assert len(active["zones"]) == 2

    r = client.post(f"/api/campaign/runs/{run_id}/notes", json={"notes": "good RNG"}, headers=H)
    assert r.json() == {"ok": True}
    client.post("/api/campaign/finish", headers=H)
    d = client.get("/api/campaign").json()
    assert d["active"] is None and d["pb"]["id"] == run_id
    assert d["runs"][0]["notes"] == "good RNG" and d["runs"][0]["status"] == "finished"
    detail = client.get(f"/api/campaign/runs/{run_id}").json()
    assert detail["acts"][0]["rta"] == 2 + 2 + 600 + 2

    # a second run is compared with the PB
    client.post("/api/campaign/start", json={"character": "Hexy"}, headers=H)
    log.lines.clear()
    log.t = T0
    log.wait(1)
    log.enter("1_1_1", "The Twilight Strand")
    log.wait(500)
    log.enter("1_2_1", "The Southern Forest")
    with path.open("a") as f:
        f.write("\n".join(log.lines) + "\n")
    rt.service.poll()
    act1 = client.get("/api/campaign").json()["active"]["acts"][0]
    assert act1["delta"] == (1 + 2 + 500 + 2) - (2 + 2 + 600 + 2)

    assert client.post(f"/api/campaign/runs/{run_id}/delete", headers=H).json() == {"ok": True}
    assert client.get(f"/api/campaign/runs/{run_id}").status_code == 404
    rt.stop()
