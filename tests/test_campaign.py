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
    assert (first["active"], first["pb_ids"], first["runs"]) == (None, {}, [])
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
    assert d["active"] is None and d["pb_ids"] == {"Witch": run_id}
    assert d["runs"][0]["notes"] == "good RNG" and d["runs"][0]["status"] == "finished"
    detail = client.get(f"/api/campaign/runs/{run_id}").json()
    assert detail["acts"][0]["rta"] == 2 + 2 + 600 + 2

    # a second run of the same base class is compared with that class's PB, act and zones
    client.post("/api/campaign/start", json={"character": "Hexy"}, headers=H)
    log.lines.clear()
    log.t = T0
    log.wait(1)
    log.enter("1_1_1", "The Twilight Strand")
    log.level(2, "Witch", "Hexy")
    log.wait(500)
    log.enter("1_2_1", "The Southern Forest")
    with path.open("a") as f:
        f.write("\n".join(log.lines) + "\n")
    rt.service.poll()
    active = client.get("/api/campaign").json()["active"]
    assert active["pb_class"] == "Witch" and active["pb_id"] == run_id
    assert active["best_class"] == "Witch" and active["best_runs"] == 1
    act1 = active["acts"][0]
    assert act1["delta"] == (1 + 2 + 500 + 2) - (2 + 2 + 600 + 2)
    strand = active["zones"][0]
    assert strand["name"] == "The Twilight Strand" and strand["pb_rta"] == 2 + 2
    assert strand["delta"] == (1 + 2) - (2 + 2)
    assert strand["best_rta"] == 2 + 2 and strand["best_delta"] == strand["delta"]
    assert client.get("/api/campaign").json()["top_ids"] == [run_id]

    # the patch, noted in the run's notes section; editable
    r = client.post(f"/api/campaign/runs/{run_id}/patch", json={"patch": "3.27"}, headers=H)
    assert r.json() == {"ok": True, "patch": "3.27"}
    assert client.get(f"/api/campaign/runs/{run_id}").json()["patch"] == "3.27"
    r = client.post(f"/api/campaign/runs/{run_id}/patch", json={"patch": "soon"}, headers=H)
    assert r.status_code == 400
    assert (
        client.post("/api/campaign/runs/999/patch", json={"patch": ""}, headers=H).status_code
        == 404
    )

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


def test_pb_is_per_base_class(tmp_path: Path) -> None:
    from poefire.campaign import CampaignRun, best_per_class, personal_best

    def done(rid: int, cls: str, minutes: int) -> CampaignRun:
        return CampaignRun(started_at=T0, id=rid, status="finished", char_class=cls,
                           ended_at=T0 + timedelta(minutes=minutes))  # fmt: skip

    runs = [done(1, "Deadeye", 200), done(2, "Ranger", 150), done(3, "Necromancer", 120)]
    runs.append(CampaignRun(started_at=T0, id=4, status="abandoned", char_class="Warden"))
    assert personal_best(runs, "Ranger") is runs[1]  # Deadeye and Ranger runs count together
    assert personal_best(runs, "Witch") is runs[2]
    assert personal_best(runs, "Templar") is None
    assert personal_best(runs) is runs[2]
    assert best_per_class(runs) == {"Ranger": 2, "Witch": 3}


def test_best_times_count_every_run_past_act_1(tmp_path: Path) -> None:
    from poefire.campaign import CampaignRun, Split, SplitKind, best_splits, run_detail

    def run(rid: int, cls: str, status: str, act: int, times: dict[str, float]) -> CampaignRun:
        r = CampaignRun(started_at=T0, id=rid, status=status, char_class=cls, act=act,
                        ended_at=T0 + timedelta(hours=5))  # fmt: skip
        for name, rta in times.items():
            kind = SplitKind.ACT if name.startswith("Act ") else SplitKind.ZONE
            n = int(name.split()[1]) if kind == SplitKind.ACT else 1 if rta < 600 else 2
            r.splits.append(Split(kind, name, n, T0 + timedelta(seconds=rta), rta, rta))
        return r

    runs = [
        # finished, but slow in Act 2
        run(1, "Deadeye", "finished", 11, {"The Coast": 100, "Act 1": 500, "Act 2": 1500}),
        # abandoned in Act 3: still counts, it got past Act 1 (and had the fastest Act 2)
        run(2, "Ranger", "abandoned", 3, {"The Coast": 120, "Act 1": 550, "Act 2": 1300}),
        # never left Act 1: doesn't count, however fast
        run(3, "Ranger", "abandoned", 1, {"The Coast": 50}),
        run(4, "Witch", "abandoned", 2, {"The Coast": 10, "Act 1": 100}),  # another class
    ]
    bests = best_splits(runs, "Ranger")
    assert bests.runs == 2
    assert bests.zones[(1, "The Coast")].rta == 100
    assert {n: s.rta for n, s in bests.acts.items()} == {"Act 1": 500, "Act 2": 1300}
    assert bests.act_segments == {"Act 1": 500, "Act 2": 750}  # run 2's Act 2 took 750

    now_run = run(5, "Ranger", "running", 2, {"The Coast": 90, "Act 1": 520})
    d = run_detail(now_run, T0, runs[0], bests)  # vs the PB (run 1) and the best times
    assert d["best_runs"] == 2 and d["best_class"] == "Ranger"
    assert d["pb_id"] == 1 and d["pb_class"] == "Ranger"
    coast = d["zones"][0]
    assert (coast["pb_rta"], coast["delta"], coast["best_rta"], coast["best_delta"]) == (
        100, -10, 100, -10)  # fmt: skip
    act1 = d["acts"][0]
    assert (act1["pb_rta"], act1["delta"], act1["best_rta"], act1["best_delta"]) == (
        500, 20, 500, 20)  # fmt: skip
    assert [(a["name"], a["segment"]) for a in d["best_acts"]] == [("Act 1", 500), ("Act 2", 750)]
    assert [a["name"] for a in d["pb_acts"]] == ["Act 1", "Act 2"]
    assert run_detail(now_run, T0, None, best_splits(runs, "Templar"))["best_class"] is None


def test_top_runs() -> None:
    from poefire.campaign import CampaignRun, top_runs

    def done(rid: int, minutes: int, status: str = "finished") -> CampaignRun:
        return CampaignRun(started_at=T0, id=rid, status=status, char_class="Witch",
                           ended_at=T0 + timedelta(minutes=minutes))  # fmt: skip

    runs = [done(1, 300), done(2, 200), done(3, 100, "abandoned"), done(4, 250), done(5, 400)]
    assert top_runs(runs) == [2, 4, 1]  # finished only, fastest first
    assert top_runs(runs[:1]) == [1]


def test_patch_is_noted_on_new_runs(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    tracker = CampaignTracker(store, lambda: T0, lambda when: "3.27")
    run = tracker.start()
    assert run.patch == "3.27" and store.get_campaign_run(run.id or 0).patch == "3.27"  # type: ignore[union-attr]
    old = CampaignTracker(store, lambda: T0)  # e.g. no patch list
    old.start()
    with store.transaction():
        store.conn.execute("UPDATE campaign_runs SET patch = NULL")
    assert store.fill_campaign_patches(lambda when: "3.26") == 2
    assert {r.patch for r in store.list_campaign_runs(splits=False)} == {"3.26"}


def test_kitavas_minus_60_finishes_the_run(tmp_path: Path) -> None:
    from poefire.events import KitavaAffliction

    line = (": You have been permanently weakened by Kitava's merciless affliction. "
            "You now have a total of {}% to all Resistances.")  # fmt: skip
    assert parse_line(f"2026/11/08 10:00:00 1 a [INFO Client 1] {line.format(-60)}") == \
        KitavaAffliction(datetime(2026, 11, 8, 10), -60)  # fmt: skip
    store = Store(tmp_path / "db.sqlite3")
    tracker = CampaignTracker(store, lambda: T0)
    run = tracker.start()
    log = Log()
    log.wait(1)
    log.enter("2_10_9", "The Feeding Trough")  # Act 10
    log.wait(60)
    log.line(line.format(-30))  # (Act 5's penalty: not the end)
    feed(tracker, log)
    assert tracker.active is run and run.status == "running"
    log.wait(30)
    log.line(line.format(-60))
    feed(tracker, log)
    assert tracker.active is None and run.status == "finished" and run.act == 11
    assert run.ended_at == T0 + timedelta(seconds=1 + 2 + 60 + 30)
    assert run.splits[-1].name == "Act 10" and run.splits[-1].rta == 1 + 2 + 60 + 30
    log.wait(120)
    log.enter("2_11_1", "Karui Shores")  # the Epilogue afterwards: nothing more happens
    feed(tracker, log)
    saved = store.get_campaign_run(run.id or 0)
    assert saved is not None and saved.ended_at == run.ended_at


def test_town_time_per_act(tmp_path: Path) -> None:
    from poefire.campaign import run_detail

    store = Store(tmp_path / "db.sqlite3")
    clock = [T0]
    tracker = CampaignTracker(store, lambda: clock[0])
    run = tracker.start()
    log = Log()
    log.wait(1)
    log.enter("1_1_1", "The Twilight Strand")
    log.wait(30)
    log.enter("1_1_town", "Lioneye's Watch")  # in town 40 s
    log.wait(40)
    log.enter("1_1_2", "The Coast")
    log.wait(60)
    log.enter("HideoutSpace", "Celestial Hideout")  # a hideout counts too: 20 s
    log.wait(20)
    log.enter("1_1_2", "The Coast")
    log.wait(30)
    log.enter("1_2_1", "The Southern Forest")  # Act 2
    log.wait(10)
    log.enter("1_2_town", "The Forest Encampment")
    feed(tracker, log)
    assert run.town == {1: 60.0}  # 40 + 20: from arriving until the next loading screen
    clock[0] = log.t + timedelta(seconds=15)
    assert run.town_seconds(clock[0]) == {1: 60.0, 2: 15.0}  # in town right now
    d = run_detail(run, clock[0], None)
    assert d["town"] == {"1": 60.0, "2": 15.0} and d["best_town"] == {}
    tracker.pause()  # paused in town: the timer stops, so does town time
    clock[0] += timedelta(seconds=100)
    tracker.resume()
    log.t = clock[0] + timedelta(seconds=5)
    log.enter("1_2_2", "The Old Fields")
    feed(tracker, log)
    assert run.town[2] == 15 + 5  # (the load into The Old Fields started at +5 s)
    saved = store.get_campaign_run(run.id or 0)
    assert saved is not None and saved.town == run.town


def test_town_time_is_filled_in_from_saved_events(tmp_path: Path) -> None:
    import dataclasses

    from poefire.campaign import CampaignRun, town_from_events
    from poefire.events import AreaGenerated, ZoneEntered

    store = Store(tmp_path / "db.sqlite3")
    run = CampaignRun(started_at=T0, status="finished", ended_at=T0 + timedelta(minutes=10))
    run.id = store.save_campaign_run(run)
    with store.transaction():
        store.conn.execute("UPDATE campaign_runs SET town = NULL")  # recorded before town time
        events: list[tuple[int, AreaGenerated | ZoneEntered]] = [
            (5, AreaGenerated(T0, 1, "1_1_1", 1)), (7, ZoneEntered(T0, "The Twilight Strand")),
            (60, AreaGenerated(T0, 1, "1_1_town", 2)), (62, ZoneEntered(T0, "Lioneye's Watch")),
            (92, AreaGenerated(T0, 1, "1_1_2", 3)), (94, ZoneEntered(T0, "The Coast")),
            (200, AreaGenerated(T0, 1, "1_2_town", 4)), (203, ZoneEntered(T0, "Forest Camp")),
        ]  # fmt: skip
        for sec, ev in events:
            store.add_event(dataclasses.replace(ev, ts=T0 + timedelta(seconds=sec)), None)
    assert store.fill_campaign_town(town_from_events) == 1
    filled = store.get_campaign_run(run.id)
    assert filled is not None and filled.town == {1: 30.0, 2: 600 - 203}
    assert store.fill_campaign_town(town_from_events) == 0  # done once
