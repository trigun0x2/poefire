from __future__ import annotations

import copy
import itertools
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from poefire.events import AreaGenerated
from poefire.loot import recorder
from poefire.loot.diff import diff_items
from poefire.loot.items import ItemEntry, display_name, item_key
from poefire.loot.recorder import LootRecorder, LootResult, Trigger
from poefire.loot.snapshot import Snapshot, Snapshotter
from poefire.store import Store
from poefire.tracker import NoticeKind, Run, RunNotice, RunStatus, SafeZoneEntered, Segment
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
    ])  # fmt: skip
    assert diff_items(before, after) == []


def test_unique_maps_are_loot() -> None:
    unique_map = {
        "id": "u",
        "name": "Maelström of Chaos",
        "typeLine": "Atoll Map",
        "frameType": 3,
        "properties": [{"name": "Map Tier", "values": [["5", 0]]}],
    }
    assert {d.name: d.quantity for d in diff_items([], entries([unique_map]))} == {
        "Maelström of Chaos Atoll Map": 1}  # fmt: skip


def test_maps_are_one_kind_whatever_their_mods() -> None:
    def t16(item_id: str, frame: int, name: str = "", type_line: str = "Map (Tier 16)",
            **flags: bool) -> Json:  # fmt: skip
        return {"id": item_id, "name": name, "typeLine": type_line, "baseType": "Map (Tier 16)",
                "frameType": frame, **flags}  # fmt: skip

    maps = [t16("a", 0), t16("b", 1, type_line="Oppressive Map of Rust (Tier 16)"),
            t16("c", 2, "Gloom Hustle"), t16("d", 2, "Woe Nexus", corrupted=True),
            t16("e", 1, mirrored=True),
            {"id": "n", "name": "Oblivion Artifice", "typeLine": "Nightmare Map",
             "baseType": "Nightmare Map", "frameType": 2}]  # fmt: skip
    got = {d.name: d.quantity for d in diff_items([], entries(maps))}
    assert got == {"Map (Tier 16)": 3, "Map (Tier 16) (corrupted)": 1,
                   "Map (Tier 16) (mirrored)": 1, "Nightmare Map": 1}  # fmt: skip


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
                {"id": "m15", "name": "", "type": "MapStash",
                 "metadata": {"map": {"section": "tier15"}}},  # not watched
                {"id": "m17", "name": "", "type": "MapStash",
                 "metadata": {"map": {"section": "tier17"}}},
                {"id": "mu", "name": "", "type": "MapStash",
                 "metadata": {"map": {"section": "unique"}}},
                {"id": "ms", "name": "", "type": "MapStash",
                 "metadata": {"map": {"section": "special"}}},
            ]},
        ]  # fmt: skip
        self.contents: dict[str, list[Json]] = {
            k: [] for k in ("t1", "t2", "t3", "t4", "m16", "m17", "mu", "ms")
        }
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
        ("mu", "m0"),  # map tab: Tier 16, Unique and Special only (not 15 or 17)
        ("ms", "m0"),
    ]
    s.resolve_tabs()
    assert api.calls.count(("list_stashes", "Settlers")) == 1  # cached


def test_tab_names_never_fall_back_to_the_tab_id() -> None:
    api = FakeApi()
    t16, uniq = {"map": {"section": "tier16"}}, {"map": {"section": "unique"}}
    api.tabs = [
        {"id": "a1b2c3", "name": "", "type": "PremiumStash", "index": 4},
        {"id": "d4e5f6", "name": "", "type": "CurrencyStash", "index": 0},
        {"id": "m0", "name": "Maps", "type": "MapStash", "children": [
            {"id": "s1", "name": "1", "type": "MapStash", "metadata": t16},
            {"id": "s2", "name": "", "type": "MapStash", "metadata": uniq},
        ]},
        {"id": "o9", "name": "Other", "type": "PremiumStash"},
    ]  # fmt: skip
    api.contents.update({k: [] for k in ("a1b2c3", "d4e5f6", "s1", "s2")})
    s = make_snapshotter(api, ["a1b2c3", "d4e5f6", "Maps"], character="Me")
    assert s.take().complete
    assert s.tab_names() == {
        "inventory": "Inventory", "a1b2c3": "Tab 5", "d4e5f6": "Currency",
        "m0": "Maps", "s1": "Maps / Tier 16", "s2": "Maps / Unique",
        "o9": "Other",  # unwatched tabs are named too
    }  # fmt: skip


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


def test_unique_tabs_are_never_read() -> None:
    """Of uniques only unique maps count: the map tab's Unique section is read, a unique tab
    isn't."""
    api = FakeApi()
    api.tabs.append({"id": "u0", "name": "Uniques", "type": "UniqueStash", "children": [
        {"id": "u1", "name": "Flasks", "type": "UniqueStash"}]})  # fmt: skip
    s = make_snapshotter(api, ["Maps", "Uniques"])
    assert [r.id for r in s.resolve_tabs()] == ["m16", "mu", "ms"]
    assert s.tab_requests() == 3


def test_reading_tabs_stops_when_told() -> None:
    api = FakeApi()
    s = make_snapshotter(api, ["Dump", "Currency"], character="Me")
    asked: list[int] = []

    def keep_going() -> bool:
        asked.append(1)
        return len(asked) < 3  # yes before the tab list and the first tab, no before the next

    assert s.read_tabs(keep_going) is None
    assert [c for c in api.calls if c[0] == "get_stash"] == [("get_stash", "t1")]


def test_the_tab_list_is_read_again_after_a_while() -> None:
    from poefire.loot import snapshot as sn

    api = FakeApi()
    clock = [0.0]
    s = Snapshotter(api, "Settlers", ["Dump", "Maps"], "Me", monotonic=lambda: clock[0])
    s.resolve_tabs()
    assert s.tab_requests() == 4  # Dump and Maps' 3 sections (Tier 16, Unique, Special)
    clock[0] = sn.TAB_LIST_EVERY
    assert s.tab_requests() == 5  # and the tab list
    s.resolve_tabs()
    assert api.calls.count(("list_stashes", "Settlers")) == 2


def test_retryable() -> None:
    from poefire.api.client import ApiError
    from poefire.api.session import LeagueError, SessionError, SessionExpiredError
    from poefire.loot.snapshot import SnapshotError, retryable

    assert retryable(SessionError("rate limited; retry after 30s"))
    assert retryable(RuntimeError("boom")) and retryable(ApiError(503, "busy"))
    assert retryable(ApiError(429, "slow down"))
    assert not retryable(SessionExpiredError("expired")) and not retryable(ApiError(404, "no"))
    assert not retryable(LeagueError("X", "no")) and not retryable(SnapshotError("other league"))


# --- recorder ---


class ScriptedSnapshotter:
    """Returns pre-baked snapshots in order (their items, for an inventory read)."""

    def __init__(self, snaps: list[Snapshot]) -> None:
        self.snaps = snaps
        self.wanted: list[str] = []

    def clock(self) -> datetime:
        return self.snaps[0].taken_at if self.snaps else T

    def take(self, scope: str = "full") -> Snapshot:
        return self.snaps.pop(0)

    def read_inventory(self) -> list[ItemEntry]:
        s = self.snaps.pop(0)
        if not s.complete:
            raise RuntimeError(s.error or "failed")
        return s.items

    def read_tabs(self, keep_going: Callable[[], bool] | None = None) -> list[ItemEntry]:
        return []

    def tab_requests(self) -> int:
        return 0

    def tab_names(self) -> dict[str, str]:
        return {"inventory": "Inventory"}


T = datetime(2024, 7, 27, 20, 0)


def snap(minute: int, items: list[Json], complete: bool = True, scope: str = "full") -> Snapshot:
    return Snapshot(T + timedelta(minutes=minute), entries(items), complete,
                    None if complete else "x", scope)  # fmt: skip


def make_recorder(tmp_path: Path, snaps: list[Snapshot], **kw: Any) -> tuple[LootRecorder, Store]:
    db = tmp_path / "db.sqlite3"
    rec = LootRecorder(ScriptedSnapshotter(snaps), lambda: Store(db),  # type: ignore[arg-type]
                       now=lambda: T, **kw)  # fmt: skip
    return rec, Store(db)  # separate connection for assertions, like the real wiring


def run(run_id: str = "run1", started: datetime = T) -> Run:
    return Run(run_id, "MapWorldsStrand", "Strand", 83, 1, started)


class Clock:
    """The app's clocks, moved by hand: wall time (as in the log) and monotonic seconds."""

    def __init__(self) -> None:
        self.t = T

    def now(self) -> datetime:
        return self.t

    def mono(self) -> float:
        return (self.t - T).total_seconds()

    def tick(self, seconds: float) -> datetime:
        self.t += timedelta(seconds=seconds)
        return self.t


class Limits:
    """The website's rate limits as the recorder asks about them."""

    def __init__(self) -> None:
        self.wait = 0.0
        self.inventory_wait: float | None = None  # None: the same as `wait`
        self.room = True
        self.asked: list[tuple[int, int]] = []

    def rate_limited_for(self, inventory: bool = False) -> float:
        if inventory and self.inventory_wait is not None:
            return self.inventory_wait
        return self.wait

    def fits(self, n: int, reserve: int = 0) -> bool:
        self.asked.append((n, reserve))
        return self.room


class Account:
    """A fake account as the website shows it: the inventory (as the game last saved it) and
    the watched tabs. `reads` lists what was read; hooks run during a read."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock.now  # what a snapshot's time is read from
        self.api = Limits()
        self.inventory: list[Json] = []
        self.tabs: dict[str, list[Json]] = {"dump": []}
        self.wanted = ["Dump"]
        self.reads: list[str] = []
        self.fail: list[Exception] = []  # raised by the next inventory reads
        self.during_read: Callable[[], None] | None = None
        self.during_tabs: Callable[[], None] | None = None

    def read_inventory(self) -> list[ItemEntry]:
        self.reads.append("inventory")
        if self.during_read is not None:
            self.during_read()
        if self.fail:
            raise self.fail.pop(0)
        return entries(self.inventory)

    def read_tabs(self, keep_going: Callable[[], bool] | None = None) -> list[ItemEntry] | None:
        out: list[ItemEntry] = []
        for tab, items in self.tabs.items():
            if keep_going is not None and not keep_going():
                return None
            self.reads.append(f"tab {tab}")
            out += entries(items, tab)
            if self.during_tabs is not None:
                self.during_tabs()
        return out

    def take(self, scope: str = "full") -> Snapshot:
        items = self.read_inventory() + (self.read_tabs() or [] if scope == "full" else [])
        return Snapshot(self.clock(), items, scope=scope)

    def tab_requests(self) -> int:
        return len(self.tabs)

    def tab_names(self) -> dict[str, str]:
        return {"inventory": "Inventory", **{t: t.title() for t in self.tabs}}


class Play:
    """Drives a recorder like the game and the log would."""

    def __init__(self, tmp_path: Path, **kw: Any) -> None:
        self.clock = Clock()
        self.account = Account(self.clock)
        db = tmp_path / "db.sqlite3"
        self.rec = LootRecorder(self.account, lambda: Store(db), now=self.clock.now,  # type: ignore[arg-type]
                                monotonic=self.clock.mono, **kw)  # fmt: skip
        self.store = Store(db)  # separate connection for assertions, like the real wiring
        self.results: list[LootResult] = []
        self.rec.handlers.append(self.results.append)
        self.run: Run | None = None
        self.seeds = itertools.count(1)

    def load(self) -> None:
        """An area starts loading: the game saves the character."""
        self.clock.tick(1)
        self.rec.on_event(AreaGenerated(self.clock.now(), 83, "MapWorldsStrand", 0))
        self.clock.tick(2)

    def enter_map(self, run_id: str = "run1") -> None:
        """Into a new map (a new run), or back into the same one."""
        self.load()
        now = self.clock.now()
        if self.run is not None and self.run.id == run_id:
            self.run.segments.append(Segment(now))
            self.rec.on_notice(RunNotice(NoticeKind.RESUMED, copy.deepcopy(self.run)))
        else:
            if self.run is not None:
                self.finish()
            self.run = Run(run_id, "MapWorldsStrand", "Strand", 83, next(self.seeds), now,
                           segments=[Segment(now)])  # fmt: skip
            self.rec.on_notice(RunNotice(NoticeKind.STARTED, copy.deepcopy(self.run)))

    def back_home(self) -> None:
        assert self.run is not None
        self.load()
        now = self.clock.now()
        start = self.run.segments[-1].start
        self.run.segments[-1].end = now
        self.rec.on_notice(SafeZoneEntered(now, "Hideout", ZoneKind.HIDEOUT, self.run.id, True,
                                           start))  # fmt: skip

    def finish(self) -> None:
        assert self.run is not None
        self.run.status, self.run.ended_at = RunStatus.FINISHED, self.run.last_activity
        self.rec.on_notice(RunNotice(NoticeKind.FINISHED, copy.deepcopy(self.run)))
        self.run = None

    def settle(self, seconds: float = 5) -> None:
        """Time passes; the recorder does what's due by then (as its worker thread would)."""
        self.clock.tick(seconds)
        while True:
            with self.rec._cond:
                due = [r for r in self.rec._pending if r.due <= self.rec.monotonic()]
                if not due:
                    return
                req = min(due, key=lambda r: r.due)
                self.rec._pending.remove(req)
                if self.rec._superseded(req):
                    continue
            self.rec._handle(req)

    def loot(self, run_id: str = "run1") -> dict[str, int]:
        return {d.name: d.quantity for d in self.store.run_loot(run_id)}

    def snapshots(self) -> list[tuple[str, int, str]]:
        return [(s["trigger"], s["complete"], s["scope"]) for s in reversed(
            self.store.list_snapshots(100))]  # fmt: skip


def test_a_map_is_measured_with_two_inventory_reads(tmp_path: Path) -> None:
    p = Play(tmp_path)
    c = "Chaos Orb"
    p.account.inventory = [currency(c, 10, "c")]
    p.account.tabs["dump"] = [currency("Divine Orb", 3, "d")]
    p.enter_map("run1")
    p.settle()  # the inventory as you go in; the tabs too: never read yet
    p.account.inventory = [currency(c, 25, "c"), frag("Xoph's Breachstone", "x")]  # picked up
    p.clock.tick(120)
    p.back_home()
    p.settle()
    assert p.loot("run1") == {c: 15, "Xoph's Breachstone": 1}
    assert p.account.reads == ["inventory", "tab dump", "inventory"]

    # in the hideout: everything into the stash, then the next map
    p.account.tabs["dump"] = [currency("Divine Orb", 3, "d"), currency(c, 25, "c2"),
                              frag("Xoph's Breachstone", "x")]  # fmt: skip
    p.account.inventory = []
    p.enter_map("run2")
    p.settle()  # the tabs were read minutes ago: the inventory only
    p.account.inventory = [currency(c, 5, "c3")]
    p.clock.tick(120)
    p.back_home()
    p.settle()
    assert p.loot("run2") == {c: 5}  # what went into the stash doesn't hide it
    assert p.account.reads[3:] == ["inventory", "inventory"]
    assert p.store.runs_with_loot_data(["run1", "run2"]) == {"run1", "run2"}
    assert p.snapshots() == [
        ("run_start", 1, "full"),
        ("run_return", 1, "inventory"),
        ("run_start", 1, "inventory"),
        ("run_return", 1, "inventory"),
    ]
    assert p.store.run_loot("run2", "between") == []  # hideout activity isn't recorded


def test_hideout_trips_add_up(tmp_path: Path) -> None:
    p = Play(tmp_path)
    c = "Chaos Orb"
    p.enter_map("run1")
    p.settle()
    p.account.inventory = [currency(c, 10, "c")]
    p.back_home()
    p.settle()
    p.account.tabs["dump"] = [currency(c, 10, "c")]  # stashed, and a scroll taken out
    p.account.inventory = [currency("Portal Scroll", 20, "p")]
    p.enter_map("run1")  # back into the same map
    p.settle()
    p.account.inventory = [currency("Portal Scroll", 19, "p"), currency(c, 4, "c2")]
    p.back_home()
    p.settle()
    assert p.loot("run1") == {c: 14, "Portal Scroll": -1}  # the scroll taken out isn't loot
    assert [s[0] for s in p.snapshots()] == ["run_start", "run_return", "run_resume", "run_return"]


def test_a_read_after_you_moved_on_is_thrown_away(tmp_path: Path) -> None:
    """Into a side area before the start read: the inventory may already hold loot, so
    the read isn't made, and the map has no loot data instead of a wrong number."""
    p = Play(tmp_path)
    p.enter_map("run1")
    p.load()  # a side area, before the read was due
    p.settle()
    assert p.account.reads == []
    p.account.inventory = [currency("Chaos Orb", 50, "c")]
    p.back_home()
    p.settle()
    assert p.snapshots() == [("run_start", 0, "inventory"), ("run_return_unpaired", 1, "inventory")]
    assert p.results[0].error == recorder.ZONE_CHANGED
    assert p.loot() == {} and p.store.runs_with_loot_data(["run1"]) == set()


def test_a_quick_hideout_trip_is_read_as_you_go_back_in(tmp_path: Path) -> None:
    """In and out of the hideout before your inventory was read: nothing could change in a
    second there, so the read as you go back in ends the segment before, too."""
    p = Play(tmp_path)
    c = "Chaos Orb"
    p.enter_map("run1")
    p.settle()
    p.account.inventory = [currency(c, 10, "c")]
    p.back_home()
    p.enter_map("run1")  # a second later, back into the map
    p.settle()
    assert p.loot() == {c: 10}
    p.account.inventory = [currency(c, 15, "c")]
    p.back_home()
    p.settle()
    assert p.loot() == {c: 15}
    assert [s[:2] for s in p.snapshots()] == [("run_start", 1), ("run_return", 0),
                                              ("run_return", 1), ("run_resume", 1),
                                              ("run_return", 1)]  # fmt: skip


def test_a_longer_trip_without_a_read_isnt_guessed(tmp_path: Path) -> None:
    p = Play(tmp_path)
    c = "Chaos Orb"
    p.enter_map("run1")
    p.settle()
    p.account.inventory = [currency(c, 10, "c")]
    p.account.api.wait = 100.0  # rate limited: the read back home waits
    p.back_home()
    p.settle()
    p.account.inventory = []  # stashed it all
    p.clock.tick(30)
    p.enter_map("run1")
    p.account.api.wait = 0.0
    p.settle(100)
    p.account.inventory = [currency(c, 5, "c2")]
    p.back_home()
    p.settle()
    assert p.loot() == {c: 5}  # the first trip's loot isn't known, and isn't made up


def test_a_zone_change_during_the_read_spoils_it(tmp_path: Path) -> None:
    p = Play(tmp_path)
    p.enter_map("run1")
    p.account.during_read = p.load  # you go into a side area while it's being read
    p.settle()
    assert p.snapshots() == [("run_start", 0, "inventory")]


def test_a_failed_read_is_tried_again_in_the_same_zone(tmp_path: Path) -> None:
    from poefire.api.session import SessionError, SessionExpiredError

    p = Play(tmp_path)
    p.account.fail = [SessionError("network error on /x"), SessionError("HTTP 503 from /x")]
    p.enter_map("run1")
    p.settle()
    assert p.snapshots() == [] and len(p.account.reads) == 1  # tried again in 5 s
    p.settle(5)
    assert len(p.account.reads) == 2
    p.settle(15)
    assert p.snapshots() == [("run_start", 1, "full")] and p.account.reads.count("inventory") == 3
    assert [r.complete for r in p.results] == [True]  # only the outcome is reported

    p.account.fail = [SessionExpiredError("The website rejected your POESESSID")]
    p.back_home()
    p.settle()
    assert p.snapshots()[-1] == ("run_return", 0, "inventory")  # an expired cookie: no retry
    assert not p.results[-1].complete and "POESESSID" in (p.results[-1].error or "")


def test_a_rate_limit_is_waited_out_before_reading(tmp_path: Path) -> None:
    p = Play(tmp_path)
    p.account.api.wait = 100.0
    p.enter_map("run1")
    p.settle()
    assert p.account.reads == []  # not sent into the rate limit
    p.account.api.wait = 0.0
    p.settle(100)
    assert p.account.reads[0] == "inventory" and p.snapshots()[0][:2] == ("run_start", 1)


def test_a_stash_penalty_doesnt_hold_up_the_inventory_reads(tmp_path: Path) -> None:
    """The map sections have a limit of their own: a penalty there isn't the inventory's."""
    p = Play(tmp_path)
    p.account.tabs = {}
    p.account.api.wait, p.account.api.inventory_wait = 300.0, 0.0
    p.account.inventory = [currency("Chaos Orb", 1, "c")]
    p.enter_map("run1")
    p.settle()
    p.account.inventory = [currency("Chaos Orb", 4, "c")]
    p.back_home()
    p.settle()
    assert p.loot() == {"Chaos Orb": 3} and p.account.reads == ["inventory", "inventory"]


def test_the_tabs_are_read_every_30_minutes_while_mapping(tmp_path: Path) -> None:
    p = Play(tmp_path)
    for i in range(4):  # a map every 10 minutes
        p.enter_map(f"run{i}")
        p.settle()
        p.clock.tick(590)
        p.back_home()
        p.settle()
    starts = [s[2] for s in p.snapshots() if s[0] == "run_start"]
    assert starts == ["full", "inventory", "inventory", "full"]
    assert p.account.api.asked == [(1, recorder.STASH_RESERVE)] * 2  # checked before reading


def test_the_tabs_wait_for_room_in_the_rate_limits(tmp_path: Path) -> None:
    p = Play(tmp_path)
    p.account.api.room = False
    p.enter_map("run1")
    p.settle()
    assert p.account.reads == ["inventory"]
    p.account.api.room = True
    p.back_home()
    p.settle()
    p.enter_map("run2")  # 5 minutes after: not tried again yet
    p.settle()
    assert "tab dump" not in p.account.reads
    p.clock.tick(recorder.STASH_RETRY)
    p.back_home()
    p.settle()
    p.enter_map("run3")
    p.settle()
    assert p.account.reads[-2:] == ["inventory", "tab dump"]


def test_reading_the_tabs_stops_when_you_are_back(tmp_path: Path) -> None:
    p = Play(tmp_path)
    p.account.tabs = {"dump": [], "currency": [currency("Chaos Orb", 1, "c")]}
    p.account.inventory = [currency("Chaos Orb", 5, "i")]
    p.enter_map("run1")

    def back_after_the_first_tab() -> None:
        p.account.during_tabs = None
        p.back_home()

    p.account.during_tabs = back_after_the_first_tab
    p.settle()
    assert p.account.reads == ["inventory", "tab dump"]  # stopped before the next tab
    p.account.inventory = [currency("Chaos Orb", 7, "i")]
    p.settle()
    assert p.snapshots() == [("run_start", 1, "inventory"), ("run_return", 1, "inventory")]
    assert p.loot() == {"Chaos Orb": 2}
    assert p.store.latest_snapshot(scope="full") is None  # nothing half-read is kept


def test_the_game_closed_in_a_map(tmp_path: Path) -> None:
    p = Play(tmp_path)
    p.enter_map("run1")
    p.settle()
    p.account.inventory = [currency("Chaos Orb", 3, "c")]
    p.clock.tick(60)
    assert p.run is not None
    p.run.segments[-1].end = p.clock.now()
    p.finish()  # "Closing game gracefully": the game saved the character
    p.settle()
    assert p.loot() == {"Chaos Orb": 3}


def test_a_new_map_from_the_hideout_is_no_closed_game(tmp_path: Path) -> None:
    p = Play(tmp_path)
    p.enter_map("run1")
    p.settle()
    p.back_home()
    p.settle()
    p.enter_map("run2")  # run1 finishes as run2's map is made: nothing more to read for it
    p.settle()
    assert [s[0] for s in p.snapshots()] == ["run_start", "run_return", "run_start"]


@pytest.mark.parametrize(
    ("notice", "expected"),
    [
        (RunNotice(NoticeKind.STARTED, run()), [(Trigger.RUN_START, "run1")]),
        (RunNotice(NoticeKind.RESUMED, run()), [(Trigger.RUN_RESUME, "run1")]),
        (SafeZoneEntered(T, "Hideout", ZoneKind.HIDEOUT, "run1", True, T),
         [(Trigger.RUN_RETURN, "run1")]),
        (SafeZoneEntered(T, "Hideout", ZoneKind.HIDEOUT, "run1", False), []),  # from town
        (SafeZoneEntered(T, "Hideout", ZoneKind.HIDEOUT, None, False), []),
        (RunNotice(NoticeKind.FINISHED, run()), []),  # finished from the hideout
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
    rec.request(Trigger.RUN_START, "run1")
    rec.request(Trigger.RUN_RETURN, "run1", delay=0.05)
    rec.request(Trigger.RUN_RETURN, "run1", delay=0.1)  # supersedes the previous one
    rec.start()
    assert done.wait(5)
    rec.stop()
    assert [r.trigger for r in results] == [Trigger.RUN_START, Trigger.RUN_RETURN]
    assert {d.name: d.quantity for d in store.run_loot("run1")} == {"Chaos Orb": 2}


def test_snapshot_now_counts_the_change_and_keeps_the_map_going(tmp_path: Path) -> None:
    p = Play(tmp_path)
    c = "Chaos Orb"
    p.account.inventory = [currency(c, 10, "c")]
    p.rec.request(Trigger.MANUAL, None)
    p.settle()
    assert p.results[-1].baseline and p.results[-1].scope == "full"
    p.enter_map("run1")
    p.settle()  # the tabs were read just now: the inventory only
    p.account.inventory = [currency(c, 12, "c")]
    p.rec.request(Trigger.MANUAL, None)  # Snapshot now, in the map
    p.settle()
    r = p.results[-1]
    assert r.kind == "manual" and {d.name: d.quantity for d in r.deltas} == {c: 2}  # inventory
    p.account.inventory = [currency(c, 20, "c")]
    p.back_home()
    p.settle()
    assert p.loot() == {c: 10}  # still measured from the map's start


def test_large_gap_starts_new_baseline(tmp_path: Path) -> None:
    rec, _ = make_recorder(
        tmp_path, [snap(0, [currency("Chaos Orb", 1)]), snap(600, [currency("Chaos Orb", 99)])]
    )
    rec.record(Trigger.MANUAL)
    assert rec.record(Trigger.MANUAL).baseline


def test_only_the_snapshot_items_still_needed_are_kept(tmp_path: Path) -> None:
    p = Play(tmp_path)
    p.account.inventory = [currency("Chaos Orb", 1, "c")]
    p.enter_map("run1")
    p.settle()  # full: the Overview's holdings
    for _ in range(3):
        p.back_home()
        p.settle()
        p.enter_map("run1")
        p.settle()
    (ids,) = p.store.conn.execute(
        "SELECT GROUP_CONCAT(DISTINCT snapshot_id) FROM snapshot_items").fetchone()  # fmt: skip
    kept = sorted(int(i) for i in ids.split(","))
    full = p.store.latest_snapshot(scope="full", items=False)
    assert full is not None and kept[0] == full.id  # the holdings
    assert len(kept) == 3  # and the newest two (one of them the segment start)


def test_end_to_end_log_to_run_loot(tmp_path: Path) -> None:
    from poefire.service import TrackerService

    log = tmp_path / "Client.txt"
    log.write_text("")
    db = tmp_path / "db.sqlite3"
    clock = Clock()
    clock.t = datetime(2024, 7, 27, 20, 0, 40)
    account = Account(clock)
    account.inventory = [currency("Chaos Orb", 5, "c")]  # the map is in the map device
    rec = LootRecorder(account, lambda: Store(db), return_delay=0, now=clock.now)  # type: ignore[arg-type]
    results: list[LootResult] = []
    got = threading.Semaphore(0)

    def on_result(r: LootResult) -> None:
        results.append(r)
        got.release()

    rec.handlers.append(on_result)
    store = Store(db)
    svc = TrackerService(store, log)
    svc.handlers.append(rec.on_notice)
    svc.line_observers.append(rec.on_event)
    svc.start()
    rec.start()
    try:
        prefix = "2024/07/27 20:{} 1 abc [{} Client 1] "
        with open(log, "a") as f:
            f.write(
                prefix.format("00:30", "DEBUG")
                + 'Generating level 83 area "MapWorldsStrand" with seed 7\n'
            )
            f.write(prefix.format("00:32", "INFO") + ": You have entered Strand.\n")
        svc.poll()
        assert got.acquire(timeout=5)
        account.inventory = [
            currency("Chaos Orb", 9, "c"),
            gear("Headhunter", "Leather Belt", "hh"),
            frag("Xoph's Breachstone", "xb"),
        ]
        clock.t = datetime(2024, 7, 27, 20, 3, 10)
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
    assert [r.trigger for r in results] == [Trigger.RUN_START, Trigger.RUN_RETURN]
    assert {d.name: d.quantity for d in store.run_loot(run_row.id)} == {
        "Chaos Orb": 4,
        "Xoph's Breachstone": 1,  # the Headhunter is gear: not counted
    }


def test_segment_triggers_match_the_store() -> None:
    from poefire.store import SEGMENT_ENDS, SEGMENT_STARTS

    assert (Trigger.RUN_START.value, Trigger.RUN_RESUME.value) == SEGMENT_STARTS
    assert (Trigger.RUN_RETURN.value, recorder.UNPAIRED) == SEGMENT_ENDS


def test_a_settings_change_keeps_the_pending_reads(tmp_path: Path) -> None:
    p = Play(tmp_path)
    p.enter_map("run1")
    db = tmp_path / "db.sqlite3"
    fresh = LootRecorder(p.account, lambda: Store(db), now=p.clock.now,  # type: ignore[arg-type]
                         monotonic=p.clock.mono)  # fmt: skip
    fresh.adopt(p.rec)
    assert p.rec._pending == [] and [r.trigger for r in fresh._pending] == [Trigger.RUN_START]
    assert fresh._last_load == p.rec._last_load and fresh._segment == p.rec._segment


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


def test_interval_mode_snapshots(tmp_path: Path) -> None:
    """Every 10 minutes while mapping (a map in the last 30 min), none around maps; one at
    once when mapping starts again after a while."""
    from poefire.loot import recorder as r

    rec, _ = make_recorder(tmp_path, [])
    rec.interval = r.INTERVAL
    clock = [10_000.0]
    rec.monotonic = lambda: clock[0]
    assert rec._auto_due() is None  # not mapping
    rec.on_notice(RunNotice(NoticeKind.STARTED, run("m1", T)))
    assert [p.trigger for p in rec._pending] == [Trigger.INTERVAL]  # starting: one now
    rec._pending.clear()
    rec._last_snap = clock[0]
    assert rec._auto_due() == (Trigger.INTERVAL, clock[0] + r.INTERVAL)
    rec.on_notice(SafeZoneEntered(T, "Hideout", ZoneKind.HIDEOUT, "m1", True))
    rec.on_notice(RunNotice(NoticeKind.STARTED, run("m2", T)))
    assert rec._pending == []  # maps don't take snapshots in this mode
    mapped = rec._mapping_at or 0.0
    rec._last_snap = mapped + r.MAPPING_IDLE - 100  # the last one in the 30 minutes
    assert rec._auto_due() is None  # the next would come after 30 min without a map: none
    clock[0] = mapped + 2 * r.MAPPING_IDLE  # a break
    rec.on_notice(RunNotice(NoticeKind.STARTED, run("m3", T)))
    assert [p.trigger for p in rec._pending] == [Trigger.INTERVAL]  # back: one at once
    rec._pending.clear()
    rec._last_snap = clock[0]
    rec.on_notice(RunNotice(NoticeKind.STARTED, run("m4", T)))
    assert rec._pending == []  # one was just taken


def test_interval_mode_records_session_totals(tmp_path: Path) -> None:
    from poefire.pricing.book import PriceBook
    from poefire.pricing.ninja import PriceLine
    from poefire.web import sessions, views

    c, d = "Chaos Orb", "Divine Orb"
    rec, store = make_recorder(tmp_path, [
        snap(0, [currency(c, 10, "c")]),
        snap(10, [currency(c, 30, "c"), currency(d, 1, "d")]),
        snap(20, [currency(c, 25, "c"), currency(d, 2, "d")]),
    ])  # fmt: skip
    for _ in range(3):
        result = rec.record(Trigger.INTERVAL)
    assert result.kind == "interval"
    deltas, changes = store.loot_between("interval", None, None)
    assert changes == 2 and {x.name: x.quantity for x in deltas} == {c: 15, d: 2}
    book = PriceBook([("exchange", "Currency", [PriceLine(d, 100.0)])])
    runs = [mkrun_finished(f"r{i}", T + timedelta(minutes=5 * i)) for i in range(4)]
    for x in runs:
        store.upsert_run(x)
    span = sessions.find(store, None)
    assert span is not None
    rows = views.run_rows(store, book, sessions.runs_of(store, span), T, None)
    totals = sessions.interval_totals(store, book, span, rows, 100.0)
    assert totals["chaos"] == 215.0 and totals["maps"] == 4 and totals["avg_chaos"] == 53.8
    assert totals["divine"] == 2.15 and totals["snapshots"] == 2
    assert [i["name"] for i in totals["items"]] == [d, c]


def mkrun_finished(rid: str, start: datetime) -> Run:
    from poefire.tracker import RunStatus, Segment

    end = start + timedelta(minutes=4)
    return Run(rid, "MapWorldsStrand", "Strand", 83, 1, start, RunStatus.FINISHED,
               segments=[Segment(start, end)], ended_at=end)  # fmt: skip
