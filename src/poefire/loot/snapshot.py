"""Read the character inventory and the watched stash tabs.

A snapshot is either the inventory alone (one request: what map loot is measured with) or
the inventory and the watched tabs (one request per tab, or per section of a map tab).
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from poefire.api.oauth import NotLoggedInError
from poefire.api.session import LeagueError, SessionExpiredError
from poefire.loot.items import INVENTORY_SOURCE, ItemEntry, Json, inventory_items, is_tracked

log = logging.getLogger(__name__)

FULL, INVENTORY = "full", "inventory"  # Snapshot.scope

# Tabs whose items live only in sections (each section is fetched separately).
SECTIONED_TAB_TYPES = frozenset({"MapStash", "UniqueStash"})
# Unique tabs aren't read at all, saving a request per section: of uniques only unique maps
# count (see is_tracked), and those are read in the map tab.
UNREAD_TAB_TYPES = frozenset({"UniqueStash"})
TAB_LIST_EVERY = 1800.0  # seconds: the tab list (ids, positions, sections) is read again


# Of the map tab, only these sections are read: Tier 16 (originator maps included), Special
# (nightmare, Valdo, guardian maps...) and Unique. The others (lower tiers, Tier 17) aren't.
KEPT_MAP_SECTIONS = frozenset({"Tier 16", "Special", "Unique"})


def is_kept_map_section(section: Json) -> bool:
    return tab_display_name(section) in KEPT_MAP_SECTIONS


def retryable(e: Exception) -> bool:
    """A failure that may go away on its own (network, rate limit, the site hiccuping), not
    one that needs you (an expired cookie, a wrong league or character)."""
    if isinstance(e, SessionExpiredError | LeagueError | NotLoggedInError | SnapshotError):
        return False
    status = getattr(e, "status", None)  # the OAuth client's ApiError
    return not (isinstance(status, int) and 400 <= status < 500 and status != 429)


class AccountApi(Protocol):
    def list_characters(self) -> list[Json]: ...
    def get_character(self, name: str) -> Json: ...
    def list_stashes(self, league: str) -> list[Json]: ...
    def get_stash(self, league: str, stash_id: str, substash_id: str | None = None) -> Json: ...


@dataclass
class Snapshot:
    taken_at: datetime
    items: list[ItemEntry] = field(default_factory=list)
    complete: bool = True
    error: str | None = None
    scope: str = FULL  # FULL: the inventory and the watched tabs; INVENTORY: the inventory
    retry: bool = False  # failed for a reason that may go away (see retryable)

    def inventory(self) -> list[ItemEntry]:
        return inventory_items(self.items)


@dataclass(frozen=True)
class TabRef:
    id: str
    name: str
    type: str
    parent_id: str | None = None  # set for substashes (e.g. map/unique tab sections)
    parent_name: str | None = None

    @property
    def label(self) -> str:
        return f"{self.parent_name} / {self.name}" if self.parent_name else self.name


class SnapshotError(Exception):
    pass


_TYPE_NAMES = {"MapStash": "Maps", "UniqueStash": "Uniques", "CurrencyStash": "Currency",
               "FragmentStash": "Fragments", "DivinationCardStash": "Divination Cards",
               "EssenceStash": "Essences", "Folder": "Folder"}  # fmt: skip


def _section_name(section: str) -> str:
    """GGG's map-section label (`tier16`, `unique`, `special`) -> `Tier 16` / `Unique`."""
    m = re.fullmatch(r"tier(\d+)", section)
    return f"Tier {m.group(1)}" if m else section.replace("_", " ").title()


def tab_display_name(tab: Json) -> str:
    """A readable name for a tab or tab section, never the raw tab id. Map-tab sections
    are named by their `metadata.map.section`; nameless tabs fall back to their type or
    their position."""
    meta = tab.get("metadata") or {}
    section = (meta.get("map") or {}).get("section") if isinstance(meta, dict) else None
    if isinstance(section, str) and section:
        return _section_name(section)
    name = str(tab.get("name") or tab.get("n") or "").strip()
    if name:
        return name
    if tab.get("type") in _TYPE_NAMES:
        return _TYPE_NAMES[str(tab["type"])]
    index = tab.get("index", tab.get("i"))
    if isinstance(index, int):
        return f"Tab {index + 1}"
    t = str(tab.get("type") or "").removesuffix("Stash")
    return f"{t} tab" if t else "Unnamed tab"


def _walk(tabs: Iterable[Json], parent: Json | None = None) -> Iterator[tuple[Json, Json | None]]:
    for tab in tabs:
        yield tab, parent
        yield from _walk(tab.get("children") or (), tab)


def collect_items(stash: Json) -> Iterator[Json]:
    yield from stash.get("items") or ()
    for child in stash.get("children") or ():
        yield from collect_items(child)


class Snapshotter:
    def __init__(
        self,
        api: AccountApi,
        league: str,
        stash_tabs: Iterable[str],
        character: str | None = None,
        include_inventory: bool = True,
        clock: Callable[[], datetime] = datetime.now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """`stash_tabs` are tab names (case-insensitive) or tab ids."""
        self.api = api
        self.league = league
        self.wanted = [t for t in stash_tabs if t]
        self.character = character
        self._configured_character = character
        self.include_inventory = include_inventory
        self.clock = clock
        self.monotonic = monotonic
        self._tabs: list[TabRef] | None = None
        self._tabs_at = float("-inf")  # monotonic time the tab list was read
        self._sectioned = 0  # watched tabs whose sections are listed with a request of their own
        self._listed_names: dict[str, str] = {}
        self.missing_tabs: list[str] = []  # watched names/ids the league's tab list lacks

    def set_league(self, league: str, tabs: Iterable[str] | None = None) -> None:
        """Switch leagues: tab ids and the auto-picked character are per league. `tabs`: the
        watched tabs there (the same ones when not given)."""
        wanted = [t for t in tabs if t] if tabs is not None else self.wanted
        if league != self.league or wanted != self.wanted:
            self.league = league
            self.wanted = wanted
            self._tabs = None
            self._listed_names = {}
            self.character = self._configured_character

    @property
    def tabs_known(self) -> bool:
        """Whether the watched tabs have been resolved (tab_requests is a guess until then)."""
        return self._tabs is not None

    def tab_requests(self) -> int:
        """About how many requests reading the watched tabs takes now (the tab list too when
        it's due), for planning around the rate limits."""
        if not self.wanted:
            return 0
        if self._tabs is None:
            return 2 + 2 * len(self.wanted)  # not read yet: the list, and a guess
        due = self.monotonic() - self._tabs_at >= TAB_LIST_EVERY
        return len(self._tabs) + ((1 + self._sectioned) if due else 0)

    def resolve_character(self) -> str:
        if self.character:
            return self.character
        chars = self.api.list_characters()
        in_league = [c for c in chars if c.get("league") == self.league] or chars
        current = [c for c in in_league if c.get("current")]
        if not in_league:
            raise SnapshotError("account has no characters")
        chosen = (current or in_league)[0]
        self.character = str(chosen["name"])
        log.info("using character %s", self.character)
        return self.character

    def resolve_tabs(self, refresh: bool = False) -> list[TabRef]:
        """Watched tabs, expanded to the tabs that actually hold items (folder contents and
        special-tab sections). Cached, and read again every TAB_LIST_EVERY: tab ids are
        stable, but tabs move (the website reads them by position) and sections appear."""
        if (
            self._tabs is not None
            and not refresh
            and self.monotonic() - self._tabs_at < TAB_LIST_EVERY
        ):
            return self._tabs
        listed = list(_walk(self.api.list_stashes(self.league)))
        self._listed_names = {str(t["id"]): tab_display_name(t) for t, _ in listed if t.get("id")}
        wanted = {w.casefold() for w in self.wanted}
        refs: list[TabRef] = []
        seen: set[str] = set()
        missing = set(wanted)
        sectioned = 0

        def add(tab: Json, parent: Json | None) -> None:
            if tab.get("type") in UNREAD_TAB_TYPES:
                return
            children = tab.get("children") or []
            if children:
                # Folder -> its tabs; special tab (map/unique...) -> its sections only, so
                # nothing is fetched (and counted) twice.
                for child in children:
                    if tab.get("type") == "MapStash" and not is_kept_map_section(child):
                        continue
                    add(child, None if tab.get("type") == "Folder" else tab)
                return
            if tab["id"] in seen:
                return
            seen.add(tab["id"])
            refs.append(
                TabRef(tab["id"], tab_display_name(tab), tab.get("type", ""),
                       parent["id"] if parent else None,
                       tab_display_name(parent) if parent else None)
            )  # fmt: skip

        for tab, parent in listed:
            name, tab_id = str(tab.get("name", "")).casefold(), str(tab["id"]).casefold()
            if name in wanted or tab_id in wanted:
                missing.discard(name)
                missing.discard(tab_id)
                list_children = getattr(self.api, "list_tab_children", None)
                if (
                    not tab.get("children")
                    and tab.get("type") in SECTIONED_TAB_TYPES
                    and tab.get("type") not in UNREAD_TAB_TYPES
                    and list_children
                ):
                    # The POESESSID tab list doesn't include map/unique tab sections.
                    tab = {**tab, "children": list_children(self.league, tab["id"])}
                    sectioned += 1
                add(tab, parent if parent and parent.get("type") != "Folder" else None)
        if missing:
            log.warning("stash tabs not found in %s: %s", self.league, ", ".join(sorted(missing)))
        self.missing_tabs = [w for w in self.wanted if w.casefold() in missing]
        self._tabs = refs
        self._tabs_at = self.monotonic()
        self._sectioned = sectioned
        return refs

    def tab_names(self) -> dict[str, str]:
        """Source id -> display name: every tab in the league's tab list, plus the
        watched tabs and sections (labelled `Parent / Section`)."""
        names = {**self._listed_names, "inventory": "Inventory"}
        names.update({t.id: t.label for t in self._tabs or ()})
        return names

    def read_inventory(self) -> list[ItemEntry]:
        """The character's inventory (one request). Raises on failure."""
        if not self.include_inventory:
            return []
        char = self.api.get_character(self.resolve_character())
        if char.get("league") not in (None, self.league):
            raise SnapshotError(
                f"character {char.get('name')} is in {char.get('league')}, not {self.league}"
            )
        return [
            ItemEntry.from_api(i, INVENTORY_SOURCE)
            for i in char.get("inventory") or ()
            if is_tracked(i)
        ]

    def read_tabs(self, keep_going: Callable[[], bool] | None = None) -> list[ItemEntry] | None:
        """The watched tabs' items (a request per tab or section). `keep_going` is asked
        before reading the tab list (when it's due) and before each tab: when it says no, the
        tabs read so far are dropped and None comes back. Raises on failure."""
        items: list[ItemEntry] = []
        if keep_going is not None and not keep_going():
            return None
        for tab in self.resolve_tabs():
            if keep_going is not None and not keep_going():
                return None
            if tab.parent_id:
                # A substash comes back wrapped in its parent.
                wrapper = self.api.get_stash(self.league, tab.parent_id, tab.id)
                inner = [c for c in wrapper.get("children") or () if c.get("id") == tab.id]
                stash = inner[0] if inner else wrapper
            else:
                stash = self.api.get_stash(self.league, tab.id)
            items.extend(
                ItemEntry.from_api(i, tab.id) for i in collect_items(stash) if is_tracked(i)
            )
        return items

    def take(self, scope: str = FULL) -> Snapshot:
        """A snapshot of the inventory (INVENTORY), or of the inventory and the watched tabs
        (FULL); a failure anywhere makes it incomplete (unusable for diffing)."""
        snap = Snapshot(taken_at=self.clock(), scope=scope)
        try:
            # Inventory first: it is the part that can change while you are in a map.
            snap.items.extend(self.read_inventory())
            if scope == FULL:
                snap.items.extend(self.read_tabs() or ())
        except Exception as e:
            log.warning("snapshot failed: %s", e)
            snap.complete = False
            snap.error = str(e)
            snap.retry = retryable(e)
        return snap
