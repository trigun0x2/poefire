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
    first = client.get("/api/campaign").json()
    assert (first["active"], first["pb"], first["runs"]) == (None, None, [])
    assert first["hotkey"]["key"] == "Ctrl+Alt+R" and first["hotkey"]["active"] is False
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


def test_pause_resume_and_toggle(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    clock = [T0]
    tracker = CampaignTracker(store, lambda: clock[0])
    assert tracker.toggle() == "started"
    run = tracker.active
    assert run is not None
    clock[0] = T0 + timedelta(seconds=100)
    assert tracker.toggle() == "paused" and run.paused
    log = Log()
    log.t, log.tick = T0 + timedelta(seconds=110), 2_000_000
    log.enter("1_1_1", "The Twilight Strand", load=4)  # entered while paused
    feed(tracker, log)
    clock[0] = T0 + timedelta(seconds=400)
    assert run.rta(clock[0]) == 100  # frozen while paused
    assert run.load_ms == 0  # loads don't count while the timer is stopped
    assert run.splits[0].rta == 100  # a split while paused gets the frozen time
    assert tracker.toggle() == "resumed" and not run.paused
    clock[0] = T0 + timedelta(seconds=430)
    assert run.rta(clock[0]) == 130
    # saved: a restart keeps the pause bookkeeping
    again = CampaignTracker(store, lambda: clock[0])
    assert again.active is not None and again.active.rta(clock[0]) == 130
    # finishing while paused ends the run at the pause
    tracker.pause()
    clock[0] = T0 + timedelta(seconds=900)
    done = tracker.finish()
    assert done is not None and done.rta(clock[0]) == 130 and not done.paused
    store.close()


def test_toggle_api(env: tuple[TestClient, AppRuntime, Path]) -> None:
    client, rt, _ = env
    assert client.post("/api/campaign/pause", headers=H).status_code == 409
    assert client.post("/api/campaign/toggle", headers=H).json()["action"] == "started"
    d = client.post("/api/campaign/toggle", headers=H).json()
    assert d["action"] == "paused" and d["active"]["paused"] is True
    assert client.post("/api/campaign/pause", headers=H).status_code == 409
    assert client.post("/api/campaign/resume", headers=H).json()["active"]["paused"] is False
    assert rt.hotkey_json()["last_action"] == "paused"
    rt.stop()


def test_ranger_mule_is_tagged_and_the_timer_keeps_going(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    tracker = CampaignTracker(store, lambda: T0)
    run = tracker.start()
    log = Log()
    log.wait(1)
    log.enter("1_1_1", "The Twilight Strand")  # the mule
    log.wait(20)
    log.level(2, "Ranger", "MuleBoy")
    log.line(": MuleBoy has been slain.")
    log.enter("1_1_town", "Lioneye's Watch")
    log.wait(30)
    log.enter("1_1_1", "The Twilight Strand")  # the real character, a new one
    real_entry = log.t
    log.wait(25)
    log.level(2, "Witch", "RealDeal")
    log.wait(40)
    log.enter("1_1_town", "Lioneye's Watch")
    log.level(3, "Witch", "RealDeal")
    feed(tracker, log)
    assert tracker.active is run and run.status == "running"  # the timer kept going
    assert (run.character, run.char_class, run.level, run.mule) == (
        "RealDeal",
        "Witch",
        3,
        "MuleBoy",
    )
    assert run.deaths == 0  # the mule's death doesn't count
    zones = [(s.name, s.at) for s in run.splits if s.kind == "zone"]
    assert zones[0] == ("The Twilight Strand", real_entry)  # the real character's first visit
    assert [s.name for s in run.splits if s.kind == "zone"] == ["The Twilight Strand",
                                                                "Lioneye's Watch"]  # fmt: skip
    assert [s.level for s in run.splits if s.kind == "level"] == [2, 3]
    saved = store.get_campaign_run(run.id or 0)
    assert saved is not None and saved.mule == "MuleBoy"
    store.close()


def test_mule_before_a_named_character(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    tracker = CampaignTracker(store, lambda: T0)
    run = tracker.start("RealDeal")
    log = Log()
    log.wait(1)
    log.enter("1_1_1", "The Twilight Strand")
    log.level(2, "Ranger", "MuleBoy")
    log.wait(30)
    log.enter("1_1_1", "The Twilight Strand")
    log.level(2, "Witch", "RealDeal")
    feed(tracker, log)
    assert (run.character, run.mule, run.level) == ("RealDeal", "MuleBoy", 2)
    store.close()


def test_not_a_mule(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    tracker = CampaignTracker(store, lambda: T0)
    run = tracker.start()
    log = Log()
    log.enter("1_1_1", "The Twilight Strand")
    log.level(2, "Ranger", "Speedy")
    log.line(": Friend (Witch) is now level 2")  # a party member's fresh character
    log.level(3, "Ranger", "Speedy")  # the "mule" keeps levelling: it was the real one
    feed(tracker, log)
    assert (run.character, run.mule, run.level) == ("Speedy", None, 3)
    # a Ranger that went past Lioneye's Watch isn't a mule either
    run2 = tracker.start()
    log.enter("1_1_1", "The Twilight Strand")
    log.level(2, "Ranger", "Speedy")
    log.enter("1_1_2", "The Coast")
    log.line(": Other (Witch) is now level 2")
    feed(tracker, log)
    assert (run2.character, run2.mule) == ("Speedy", None)
    store.close()
