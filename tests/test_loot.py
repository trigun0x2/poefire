from __future__ import annotations

import itertools
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from poefire.loot.diff import diff_items
from poefire.loot.items import ItemEntry, display_name, item_key
from poefire.loot.recorder import LootRecorder, LootResult, Trigger
from poefire.loot.snapshot import Snapshot, Snapshotter
from poefire.store import Store
from poefire.tracker import NoticeKind, Run, RunNotice, SafeZoneEntered
from poefire.zones import ZoneKind

type Json = dict[str, Any]

_ids = itertools.count(1)


def currency(name: str, n: int, item_id: str | None = None) -> Json:
    return {"id": item_id or f"c{next(_ids)}", "typeLine": name, "baseType": name,
            "frameType": 5, "stackSize": n, "maxStackSize": 20}  # fmt: skip


def frag(name: str, item_id: str, frame: int = 0) -> Json:
    """A non-stackable item that isn't gear (breachstone, logbook, beast...)."""
    return {"id": item_id, "typeLine": name, "baseType": name, "frameType": frame}


def gear(name: str, base: str, item_id: str, frame: int = 3) -> Json:
    return {
        "id": item_id,
        "name": name,
        "typeLine": base,
        "baseType": base,
        "frameType": frame,
        "requirements": [{"name": "Level", "values": [["60", 0]]}],
    }


def entries(items: list[Json], source: str = "inventory") -> list[ItemEntry]:
    return [ItemEntry.from_api(i, source) for i in items]


def summary(before: list[ItemEntry], after: list[ItemEntry]) -> dict[str, int]:
    return {d.name: d.quantity for d in diff_items(before, after)}


# --- items ---


def test_display_name_strips_markup() -> None:
    raw = gear("<<set:MS>><<set:M>><<set:S>>Doom Spiral", "Hubris Circlet", "x")
    assert display_name(raw) == "Doom Spiral Hubris Circlet"
    assert display_name(currency("Divine Orb", 1)) == "Divine Orb"


def test_item_key_distinguishes_tier_level_quality_corruption() -> None:
    def map_item(tier: str) -> Json:
        return {"typeLine": "Strand Map", "frameType": 0,
                "properties": [{"name": "Map Tier", "values": [[tier, 0]]}]}  # fmt: skip

    assert item_key(map_item("16")) != item_key(map_item("15"))
    gem = {"typeLine": "Enlighten Support", "frameType": 4,
           "properties": [{"name": "Level", "values": [["4 (Max)", 0]]}]}  # fmt: skip
    assert item_key(gem).endswith("Level=4")
    assert item_key({**gem, "corrupted": True}) != item_key(gem)


# --- diff ---


def test_stacks_are_compared_by_quantity() -> None:
    before = entries([currency("Chaos Orb", 10), currency("Divine Orb", 2)])
    after = entries(
        [currency("Chaos Orb", 20), currency("Chaos Orb", 3), currency("Divine Orb", 2)]
    )
    assert summary(before, after) == {"Chaos Orb": 13}


def test_stack_merge_with_new_ids_nets_correctly() -> None:
    before = entries([currency("Chaos Orb", 5, "a"), currency("Chaos Orb", 5, "b")])
    after = entries([currency("Chaos Orb", 12, "z")])
    assert summary(before, after) == {"Chaos Orb": 2}


def test_moving_loot_into_a_watched_tab_is_not_a_change() -> None:
    stone = frag("Xoph's Breachstone", "x1")
    before = entries([stone, currency("Chaos Orb", 4)], "inventory")
    after = entries([stone], "tab1") + entries([currency("Chaos Orb", 4)], "tab2")
    assert summary(before, after) == {}


def test_non_stackables_by_id() -> None:
    a = frag("Xoph's Breachstone", "x1")
    b = frag("Xoph's Breachstone", "x2")
    used = frag("Expedition Logbook", "l1", frame=2)
    deltas = diff_items(entries([a, used]), entries([a, b]))
    assert [(d.name, d.quantity) for d in deltas] == [
        ("Xoph's Breachstone", 1),
        ("Expedition Logbook", -1),
    ]


def test_spent_and_gained_and_sample() -> None:
    before = entries([currency("Chaos Orb", 10), {"id": "m1", "typeLine": "Strand Map",
                                                  "frameType": 0}])  # fmt: skip
    after = entries([currency("Chaos Orb", 4), frag("Xoph's Breachstone", "xb")])
    deltas = diff_items(before, after)
    assert {d.name: d.quantity for d in deltas} == {
        "Xoph's Breachstone": 1,
        "Chaos Orb": -6,
        "Strand Map": -1,
    }
    assert deltas[0].sample["id"] == "xb"


def test_gear_is_not_loot() -> None:
    before = entries([currency("Chaos Orb", 1)])
    after = entries([
        currency("Chaos Orb", 1),
        gear("Headhunter", "Leather Belt", "hh", 3),  # unique
        gear("Ruin Loop", "Amethyst Ring", "r1", 2),  # rare
        gear("", "Hubris Circlet", "h1", 0),  # normal
        {"id": "u", "name": "Maelström of Chaos", "typeLine": "Atoll Map", "frameType": 3,
         "properties": [{"name": "Map Tier", "values": [["5", 0]]}]},  # unique map
    ])  # fmt: skip
    assert diff_items(before, after) == []


# --- snapshotter ---


class FakeApi:
    def __init__(self) -> None:
        self.characters: list[Json] = [
            {"name": "OldChar", "league": "Standard"},
            {"name": "Me", "league": "Settlers", "current": True},
        ]
        self.inventory: list[Json] = []
        self.tabs: list[Json] = [
            {"id": "t1", "name": "Dump", "type": "PremiumStash"},
            {"id": "t2", "name": "Currency", "type": "CurrencyStash"},
            {"id": "t3", "name": "Unwatched", "type": "PremiumStash"},
            {"id": "f1", "name": "Loot", "type": "Folder", "children": [
                {"id": "t4", "name": "Dump 2", "type": "PremiumStash", "folder": "f1"},
            ]},
            {"id": "m0", "name": "Maps", "type": "MapStash", "children": [
                {"id": "m16", "name": "Tier 16", "type": "MapStash", "parent": "m0"},
            ]},
        ]  # fmt: skip
        self.contents: dict[str, list[Json]] = {k: [] for k in ("t1", "t2", "t3", "t4", "m16")}
        self.calls: list[tuple[str, ...]] = []
        self.fail_on: str | None = None

    def list_characters(self) -> list[Json]:
        self.calls.append(("list_characters",))
        return self.characters

    def get_character(self, name: str) -> Json:
        self.calls.append(("get_character", name))
        return {"name": name, "league": "Settlers", "inventory": self.inventory,
                "equipment": [gear("", "Worn Item", "eq1")]}  # fmt: skip

    def list_stashes(self, league: str) -> list[Json]:
        self.calls.append(("list_stashes", league))
        return self.tabs

    def get_stash(self, league: str, stash_id: str, substash_id: str | None = None) -> Json:
        self.calls.append(("get_stash", stash_id, *([substash_id] if substash_id else [])))
        if self.fail_on == stash_id:
            raise RuntimeError("boom")
        if substash_id:  # wrapped in parent
            child = {"id": substash_id, "items": self.contents[substash_id]}
            return {"id": stash_id, "children": [child]}
        return {"id": stash_id, "items": self.contents[stash_id]}


def make_snapshotter(api: FakeApi, tabs: list[str], character: str | None = None) -> Snapshotter:
    return Snapshotter(api, "Settlers", tabs, character, clock=lambda: datetime(2024, 1, 1))


def test_resolves_tabs_folders_and_special_sections() -> None:
    api = FakeApi()
    s = make_snapshotter(api, ["dump", "t2", "Loot", "Maps", "missing"])
    refs = s.resolve_tabs()
    assert [(r.id, r.parent_id) for r in refs] == [
        ("t1", None),
        ("t2", None),
        ("t4", None),  # folder expanded to its tabs, fetched directly
        ("m16", "m0"),  # special tab sections fetched as substashes
    ]
    s.resolve_tabs()
    assert api.calls.count(("list_stashes", "Settlers")) == 1  # cached


def test_snapshot_collects_inventory_and_tabs_but_not_equipment() -> None:
    api = FakeApi()
    api.inventory = [currency("Chaos Orb", 3)]
    api.contents["t1"] = [gear("", "Hubris Circlet", "h1", 0), frag("Xoph's Breachstone", "xb")]
    api.contents["m16"] = [{"id": "map1", "typeLine": "Strand Map", "frameType": 0}]
    api.contents["t3"] = [currency("Mirror of Kalandra", 1)]
    snap = make_snapshotter(api, ["Dump", "Maps"]).take()
    assert snap.complete
    assert sorted(e.name for e in snap.items) == ["Chaos Orb", "Strand Map", "Xoph's Breachstone"]
    assert ("get_character", "Me") in api.calls  # picked the current character in the league
    assert {e.source for e in snap.items} == {"inventory", "t1", "m16"}


def test_snapshot_failure_marks_incomplete() -> None:
    api = FakeApi()
    api.fail_on = "t2"
    snap = make_snapshotter(api, ["Dump", "Currency"], character="Me").take()
    assert not snap.complete and snap.error == "boom"
    assert ("list_characters",) not in api.calls


def test_character_in_other_league_is_an_error() -> None:
    api = FakeApi()
    s = Snapshotter(api, "Hardcore Settlers", [], "Me")
    snap = s.take()
    assert not snap.complete and "Settlers" in (snap.error or "")


# --- recorder ---


class ScriptedSnapshotter:
    """Returns pre-baked snapshots in order."""

    def __init__(self, snaps: list[Snapshot]) -> None:
        self.snaps = snaps

    def take(self) -> Snapshot:
        return self.snaps.pop(0)

    def tab_names(self) -> dict[str, str]:
        return {"inventory": "Inventory"}


T = datetime(2024, 7, 27, 20, 0)


def snap(minute: int, items: list[Json], complete: bool = True) -> Snapshot:
    return Snapshot(
        T + timedelta(minutes=minute), entries(items), complete, None if complete else "x"
    )


def make_recorder(tmp_path: Path, snaps: list[Snapshot], **kw: Any) -> tuple[LootRecorder, Store]:
    db = tmp_path / "db.sqlite3"
    rec = LootRecorder(ScriptedSnapshotter(snaps), lambda: Store(db),  # type: ignore[arg-type]
                       now=lambda: T, **kw)  # fmt: skip
    return rec, Store(db)  # separate connection for assertions, like the real wiring


def test_chain_attributes_hideout_and_run_deltas(tmp_path: Path) -> None:
    c = "Chaos Orb"
    rec, store = make_recorder(
        tmp_path,
        [
            snap(0, [currency(c, 10), {"id": "map", "typeLine": "Strand Map", "frameType": 0}]),
            snap(1, [currency(c, 10)]),  # entered map: the map item was used
            snap(5, [currency(c, 25), currency("Divine Orb", 1, "d")]),  # back in hideout
            snap(6, [currency(c, 40), currency("Divine Orb", 1, "d")]),  # mid-map dump trip
        ],
    )
    assert rec.record(Trigger.MANUAL).baseline
    r1 = rec.record(Trigger.RUN_START, "run1")
    assert r1.kind == "between" and {d.name: d.quantity for d in r1.deltas} == {"Strand Map": -1}
    rec.record(Trigger.RUN_RETURN, "run1")
    rec.record(Trigger.RUN_RETURN, "run1")
    loot = {d.name: d.quantity for d in store.run_loot("run1")}
    assert loot == {"Chaos Orb": 30, "Divine Orb": 1}
    assert {d.name: d.quantity for d in store.run_loot("run1", "between")} == {"Strand Map": -1}


def test_incomplete_snapshot_is_skipped_in_chain(tmp_path: Path) -> None:
    c = "Chaos Orb"
    rec, store = make_recorder(
        tmp_path,
        [snap(0, [currency(c, 1)]), snap(1, [], complete=False), snap(2, [currency(c, 5)])],
    )
    rec.record(Trigger.MANUAL)
    failed = rec.record(Trigger.RUN_RETURN, "run1")
    assert not failed.complete
    ok = rec.record(Trigger.RUN_RETURN, "run1")
    assert {d.name: d.quantity for d in ok.deltas} == {c: 4}
    assert [s["complete"] for s in store.list_snapshots()] == [1, 0, 1]


def test_large_gap_starts_new_baseline(tmp_path: Path) -> None:
    rec, store = make_recorder(
        tmp_path, [snap(0, [currency("Chaos Orb", 1)]), snap(600, [currency("Chaos Orb", 99)])]
    )
    rec.record(Trigger.MANUAL)
    assert rec.record(Trigger.RUN_RETURN, "run1").baseline
    assert store.run_loot("run1") == []


def test_only_latest_snapshot_items_are_kept(tmp_path: Path) -> None:
    rec, store = make_recorder(
        tmp_path, [snap(i, [currency("Chaos Orb", i + 1)]) for i in range(5)]
    )
    for _ in range(5):
        rec.record(Trigger.MANUAL)
    (n,) = store.conn.execute("SELECT COUNT(DISTINCT snapshot_id) FROM snapshot_items").fetchone()
    assert n == 2


def run(run_id: str = "run1", started: datetime = T) -> Run:
    return Run(run_id, "MapWorldsStrand", "Strand", 83, 1, started)


@pytest.mark.parametrize(
    ("notice", "expected"),
    [
        (RunNotice(NoticeKind.STARTED, run()), [(Trigger.RUN_START, "run1")]),
        (RunNotice(NoticeKind.RESUMED, run()), []),
        (SafeZoneEntered(T, "Hideout", ZoneKind.HIDEOUT, "run1", True),
         [(Trigger.RUN_RETURN, "run1")]),
        (SafeZoneEntered(T, "Hideout", ZoneKind.HIDEOUT, "run1", False), []),  # from town
        (SafeZoneEntered(T, "Hideout", ZoneKind.HIDEOUT, None, False), []),
        # catching up on old log lines after a restart: the API can't show the past
        (SafeZoneEntered(T - timedelta(hours=1), "Hideout", ZoneKind.HIDEOUT, "run1", True), []),
        (RunNotice(NoticeKind.STARTED, run(started=T - timedelta(hours=1))), []),
    ],
)  # fmt: skip
def test_notices_to_requests(
    tmp_path: Path, notice: Any, expected: list[tuple[Trigger, str]]
) -> None:
    rec, _ = make_recorder(tmp_path, [])
    rec.on_notice(notice)
    assert [(r.trigger, r.run_id) for r in rec._pending] == expected


def test_run_start_snapshot_can_be_disabled(tmp_path: Path) -> None:
    rec, _ = make_recorder(tmp_path, [], snapshot_on_run_start=False)
    rec.on_notice(RunNotice(NoticeKind.STARTED, run()))
    assert rec._pending == []


def test_worker_processes_and_coalesces(tmp_path: Path) -> None:
    rec, store = make_recorder(
        tmp_path, [snap(0, [currency("Chaos Orb", 1)]), snap(1, [currency("Chaos Orb", 3)])]
    )
    results: list[LootResult] = []
    done = threading.Event()

    def handler(r: LootResult) -> None:
        results.append(r)
        if len(results) == 2:
            done.set()

    rec.handlers.append(handler)
    rec.request(Trigger.MANUAL, None)
    rec.request(Trigger.RUN_RETURN, "run1", delay=0.05)
    rec.request(Trigger.RUN_RETURN, "run1", delay=0.1)  # supersedes the previous one
    rec.start()
    assert done.wait(5)
    rec.stop()
    assert [r.trigger for r in results] == [Trigger.MANUAL, Trigger.RUN_RETURN]
    assert {d.name: d.quantity for d in store.run_loot("run1")} == {"Chaos Orb": 2}


def test_end_to_end_log_to_run_loot(tmp_path: Path) -> None:
    from poefire.service import TrackerService

    log = tmp_path / "Client.txt"
    log.write_text("")
    db = tmp_path / "db.sqlite3"
    c = "Chaos Orb"
    snaps = [
        snap(0, [currency(c, 5), {"id": "m", "typeLine": "Strand Map", "frameType": 0}]),
        snap(1, [currency(c, 5)]),  # map entry
        snap(
            4,
            [
                currency(c, 9),
                gear("Headhunter", "Leather Belt", "hh"),  # back home
                frag("Xoph's Breachstone", "xb"),
            ],
        ),
    ]
    rec = LootRecorder(ScriptedSnapshotter(snaps), lambda: Store(db),  # type: ignore[arg-type]
                       return_delay=0, now=lambda: T)  # fmt: skip
    results: list[LootResult] = []
    got = threading.Semaphore(0)

    def on_result(r: LootResult) -> None:
        results.append(r)
        got.release()

    rec.handlers.append(on_result)
    store = Store(db)
    svc = TrackerService(store, log)
    svc.handlers.append(rec.on_notice)
    svc.start()
    rec.start()
    try:
        rec.request(Trigger.MANUAL, None)  # baseline
        assert got.acquire(timeout=5)
        prefix = "2024/07/27 20:{} 1 abc [{} Client 1] "
        with open(log, "a") as f:
            f.write(
                prefix.format("00:30", "DEBUG")
                + 'Generating level 83 area "MapWorldsStrand" with seed 7\n'
            )
            f.write(prefix.format("00:32", "INFO") + ": You have entered Strand.\n")
        svc.poll()
        assert got.acquire(timeout=5)
        with open(log, "a") as f:
            f.write(
                prefix.format("03:00", "DEBUG")
                + 'Generating level 1 area "HideoutFelled" with seed 1\n'
            )
            f.write(prefix.format("03:02", "INFO") + ": You have entered Felled Hideout.\n")
        svc.poll()
        assert got.acquire(timeout=5)
    finally:
        rec.stop()
    (run_row,) = store.list_runs()
    assert [r.trigger for r in results] == [Trigger.MANUAL, Trigger.RUN_START, Trigger.RUN_RETURN]
    assert {d.name: d.quantity for d in store.run_loot(run_row.id)} == {
        "Chaos Orb": 4,
        "Xoph's Breachstone": 1,  # the Headhunter is gear: not counted
    }
    assert {d.name: d.quantity for d in store.run_loot(run_row.id, "between")} == {"Strand Map": -1}


ICON_BASE = "https://web.poecdn.com/gen/image/"


def icon(path: str) -> str:
    import base64
    import json

    seg = base64.urlsafe_b64encode(json.dumps([1, 1, {"f": path, "scale": 1}]).encode())
    return ICON_BASE + seg.decode().rstrip("=") + "/abc123/x.png"


@pytest.mark.parametrize(
    ("raw", "tracked"),
    [
        ({"typeLine": "Vaal Regalia", "frameType": 2,
          "icon": icon("2DItems/Armours/BodyArmours/BodyInt3")}, False),
        ({"typeLine": "Opal Ring", "frameType": 1, "icon": icon("2DItems/Rings/Ring9")}, False),
        ({"typeLine": "Divine Life Flask", "frameType": 0,
          "icon": icon("2DItems/Flasks/lifeflask11")}, False),
        ({"typeLine": "Cobalt Jewel", "frameType": 2,
          "icon": icon("2DItems/Jewels/basicint")}, False),
        ({"typeLine": "Imbued Wand", "frameType": 0,
          "icon": "https://web.poecdn.com/image/Art/2DItems/Weapons/OneHandWeapons/Wands/Wand7.png"},
         False),
        ({"typeLine": "Xoph's Breachstone", "frameType": 0,
          "icon": icon("2DItems/Maps/Breachstones/XophBreachstone")}, True),
        ({"typeLine": "Expedition Logbook", "frameType": 2,
          "icon": icon("2DItems/Maps/ExpeditionLogbook")}, True),
        ({"typeLine": "Chaos Orb", "frameType": 5, "stackSize": 3,
          "icon": icon("2DItems/Currency/CurrencyRerollRare")}, True),
        ({"typeLine": "Ambush Scarab", "frameType": 0, "stackSize": 2}, True),
        ({"typeLine": "The Doctor", "frameType": 6, "stackSize": 1}, True),
        ({"typeLine": "Enlighten Support", "frameType": 4}, True),
        ({"typeLine": "Strand Map", "frameType": 2,
          "properties": [{"name": "Map Tier", "values": [["16", 0]]}]}, True),
        ({"name": "Tabula Rasa", "typeLine": "Simple Robe", "frameType": 3}, False),
    ],
)  # fmt: skip
def test_is_tracked(raw: Json, tracked: bool) -> None:
    from poefire.loot.items import is_tracked

    assert is_tracked(raw) is tracked
