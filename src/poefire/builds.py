"""Build snapshots: the character's gear, gems and passive tree as it levels.

A snapshot is taken at levels 5 and 10, then at every level from 15 on. The website only sees
gear and passive changes once the game saves them, which happens on zone changes, so a level
is captured at the first zone change after reaching it (two levels in one zone make one
snapshot, labelled with the higher one).

The raw website responses are kept (compressed), so later features (Path of Building codes)
can be built from them. `summarize()` turns either source into what the Builds tab shows.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from poefire.events import LevelUp, LogEvent, ZoneEntered
from poefire.loot.items import clean

log = logging.getLogger(__name__)

type Json = dict[str, Any]

RETRY_AFTER = 180.0  # seconds before a failed snapshot is tried once more
# Lines older than this (read while catching up on the log after a restart) never trigger a
# fetch: the website only has the build as it is now, which would be labelled with an old level.
FRESH = timedelta(minutes=10)


def wants_snapshot(level: int) -> bool:
    """Levels 5 and 10, then every level from 15."""
    return level in (5, 10) or level >= 15


class BuildApi(Protocol):
    def list_characters(self) -> list[Json]: ...
    def get_build(self, name: str) -> Json: ...


class BuildStore(Protocol):
    def add_build_snapshot(self, snap: BuildSnapshot) -> int: ...


@dataclass
class BuildSnapshot:
    character: str
    level: int
    taken_at: datetime
    league: str | None = None
    char_class: str | None = None
    data: Json | None = None  # raw responses; None when the snapshot failed
    error: str | None = None
    id: int | None = None

    @property
    def ok(self) -> bool:
        return self.data is not None


# --- reading the raw data (website or OAuth) ---

# website inventoryId -> display slot, in the order the tab lists them
SLOTS: dict[str, str] = {
    "Weapon": "Weapon", "Offhand": "Offhand", "Helm": "Helmet", "BodyArmour": "Body Armour",
    "Gloves": "Gloves", "Boots": "Boots", "Amulet": "Amulet", "Ring": "Ring 1",
    "Ring2": "Ring 2", "Belt": "Belt", "Flask": "Flask", "Weapon2": "Swap Weapon",
    "Offhand2": "Swap Offhand",
}  # fmt: skip
RARITY = {0: "normal", 1: "magic", 2: "rare", 3: "unique", 4: "gem", 5: "currency",
          9: "relic", 10: "unique"}  # fmt: skip


def parts(data: Json) -> tuple[Json, list[Json], Json, list[Json]]:
    """(character, equipped items, passives, tree jewels) from either source."""
    if data.get("source") == "oauth":
        char = data.get("character") or {}
        return (
            char,
            list(char.get("equipment") or []),
            dict(char.get("passives") or {}),
            list(char.get("jewels") or []),
        )
    items = data.get("items") or {}
    passives = data.get("passives") or {}
    equipped = [i for i in items.get("items") or () if i.get("inventoryId") in SLOTS]
    return items.get("character") or {}, equipped, passives, list(passives.get("items") or [])


def _prop(item: Json, name: str) -> str | None:
    for p in item.get("properties") or ():
        if p.get("name") == name and p.get("values"):
            return str(p["values"][0][0])
    return None


def _links(item: Json) -> str:
    """Socket colours grouped by link, e.g. 'R-R-G B'."""
    groups: dict[int, list[str]] = {}
    for s in item.get("sockets") or ():
        groups.setdefault(int(s.get("group", 0)), []).append(str(s.get("sColour", "?")))
    return " ".join("-".join(g) for _, g in sorted(groups.items()))


def _gem(g: Json) -> Json:
    level, quality = _prop(g, "Level"), _prop(g, "Quality")
    return {
        "name": clean(g.get("typeLine") or g.get("baseType") or ""),
        "support": bool(g.get("support")),
        "level": int(level.split()[0]) if level and level.split()[0].isdigit() else None,
        "quality": int(quality.strip("+%")) if quality and quality.strip("+%").isdigit() else 0,
        "icon": g.get("icon"),
    }


def _gem_groups(item: Json) -> list[list[Json]]:
    """Socketed gems grouped by link group, the way they're linked in the item."""
    sockets = item.get("sockets") or []
    groups: dict[int, list[Json]] = {}
    for g in item.get("socketedItems") or ():
        if g.get("abyssJewel"):
            continue
        idx = g.get("socket")
        group = sockets[idx]["group"] if isinstance(idx, int) and idx < len(sockets) else 0
        groups.setdefault(int(group), []).append(_gem(g))
    return [groups[k] for k in sorted(groups)]


def summarize(snap: BuildSnapshot) -> Json:
    """What the Builds tab shows for one snapshot."""
    base: Json = {
        "id": snap.id, "character": snap.character, "level": snap.level,
        "taken_at": snap.taken_at.isoformat(), "league": snap.league, "class": snap.char_class,
        "ok": snap.ok, "error": snap.error,
    }  # fmt: skip
    if snap.data is None:
        return base
    char, equipped, passives, jewels = parts(snap.data)
    order = list(SLOTS)
    gear = []
    for item in sorted(equipped, key=lambda i: (order.index(i.get("inventoryId", "Flask")),
                                                i.get("x", 0))):  # fmt: skip
        slot = SLOTS[item["inventoryId"]]
        gear.append({
            "slot": slot,
            "name": clean(item.get("name") or ""),
            "base": clean(item.get("typeLine") or item.get("baseType") or ""),
            "rarity": RARITY.get(int(item.get("frameType", 0)), "normal"),
            "icon": item.get("icon"),
            "links": _links(item),
            "gems": _gem_groups(item),
        })  # fmt: skip
    hashes = passives.get("hashes") or []
    return {
        **base,
        "class": char.get("class") or snap.char_class,
        "league": char.get("league") or snap.league,
        "gear": gear,
        "passives": {
            "allocated": len(hashes) + len(passives.get("hashes_ex") or []),
            "masteries": len(passives.get("mastery_effects") or {}),
            "jewels": len(jewels),
            "bandit": passives.get("bandit_choice"),
        },
    }


# --- capture ---


class BuildWatcher:
    """Watches Client.txt for level-ups and takes snapshots in the background."""

    def __init__(
        self,
        api_getter: Callable[[], BuildApi | None],
        store_factory: Callable[[], Any],
        now: Callable[[], datetime] = datetime.now,
        retry_after: float = RETRY_AFTER,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.api_getter = api_getter  # the loot tracker's connection (shared rate limits)
        self.store_factory = store_factory
        self.now = now
        self.retry_after = retry_after
        self.sleep = sleep
        self.enabled = True
        self.pending: dict[str, tuple[int, str]] = {}  # character -> (level, class) to capture
        self.last_error: str | None = None
        self._queue: queue.Queue[tuple[str, int, str, int] | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._own: set[str] | None = None  # the account's characters (lower case)
        self.on_snapshot: Callable[[BuildSnapshot], None] | None = None

    # log lines (called by the log tail, under its lock: must be quick)
    def handle(self, event: LogEvent | None, line: str) -> None:
        if not self.enabled or event is None or self.now() - event.ts > FRESH:
            return
        if isinstance(event, LevelUp):
            if wants_snapshot(event.level):
                self.pending[event.character] = (event.level, event.character_class)
        elif isinstance(event, ZoneEntered) and self.pending:
            if self.api_getter() is None:  # no account connection: nothing can be fetched
                self.last_error = "no account connection (set up loot tracking in Settings)"
                self.pending.clear()
                return
            # The game saved the character on this zone change; one character plays at a time.
            for name, (level, cls) in self.pending.items():
                self._queue.put((name, level, cls, 0))
            self.pending.clear()
            self._ensure_worker()

    def request(self, character: str, level: int = 0, char_class: str = "") -> None:
        """Take a snapshot now (the Builds tab's button); level 0 = as the website reports."""
        self._queue.put((character, level, char_class, 0))
        self._ensure_worker()

    def _ensure_worker(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="builds", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        if self._thread is not None:
            self._queue.put(None)
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        while (job := self._queue.get()) is not None:
            name, level, cls, attempt = job
            try:
                self.capture(name, level, cls, final=attempt > 0)
            except _Retry:
                threading.Timer(self.retry_after,
                                lambda j=(name, level, cls, attempt + 1): self._queue.put(j)
                                ).start()  # fmt: skip

    def _is_own(self, api: BuildApi, name: str) -> tuple[bool, str | None]:
        if self._own is None or name.casefold() not in self._own:
            chars = api.list_characters()  # refreshed when an unknown name shows up
            self._own = {str(c.get("name", "")).casefold() for c in chars}
            for c in chars:
                if str(c.get("name", "")).casefold() == name.casefold():
                    return True, c.get("league")
            return False, None
        return True, None

    def capture(
        self, name: str, level: int, cls: str = "", final: bool = True
    ) -> BuildSnapshot | None:
        """Fetch and store one snapshot. Raises _Retry for a first failure when not `final`;
        a final failure is stored as a missed level."""
        api = self.api_getter()
        snap = BuildSnapshot(name, level, self.now(), char_class=cls or None)
        if api is None:
            snap.error = "no account connection (set up loot tracking in Settings)"
        else:
            try:
                own, league = self._is_own(api, name)
                if not own:
                    log.info("skipping build of %s: not a character on this account", name)
                    return None
                data = api.get_build(name)
                char, *_ = parts(data)
                snap.data, snap.league = data, char.get("league") or league
                if snap.level <= 0:  # "snapshot now": the level the website reports
                    snap.level = int(char.get("level") or 0)
                snap.char_class = char.get("class") or snap.char_class
            except Exception as e:  # network, rate limit, expired cookie...
                snap.error = str(e) or type(e).__name__
                if not final:
                    log.warning("build snapshot of %s (level %d) failed, retrying: %s",
                                name, level, snap.error)  # fmt: skip
                    raise _Retry from e
        self.last_error = snap.error
        with self.store_factory() as store:
            snap.id = store.add_build_snapshot(snap)
        if self.on_snapshot is not None:
            self.on_snapshot(snap)
        return snap


class _Retry(Exception):
    pass
