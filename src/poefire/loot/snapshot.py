"""Fetch the character inventory and watched stash tabs as one snapshot."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from poefire.loot.items import ItemEntry, Json, is_tracked

log = logging.getLogger(__name__)

# Tabs whose items live only in sections (each section is fetched separately).
SECTIONED_TAB_TYPES = frozenset({"MapStash", "UniqueStash"})


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


@dataclass(frozen=True)
class TabRef:
    id: str
    name: str
    type: str
    parent_id: str | None = None  # set for substashes (e.g. map/unique tab sections)


class SnapshotError(Exception):
    pass


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
    ) -> None:
        """`stash_tabs` are tab names (case-insensitive) or tab ids."""
        self.api = api
        self.league = league
        self.wanted = [t for t in stash_tabs if t]
        self.character = character
        self._configured_character = character
        self.include_inventory = include_inventory
        self.clock = clock
        self._tabs: list[TabRef] | None = None

    def set_league(self, league: str) -> None:
        """Switch leagues: tab ids and the auto-picked character are per league."""
        if league != self.league:
            self.league = league
            self._tabs = None
            self.character = self._configured_character

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
        special-tab sections). Cached: tab ids are stable."""
        if self._tabs is not None and not refresh:
            return self._tabs
        listed = list(_walk(self.api.list_stashes(self.league)))
        wanted = {w.casefold() for w in self.wanted}
        refs: list[TabRef] = []
        seen: set[str] = set()
        missing = set(wanted)

        def add(tab: Json, parent: Json | None) -> None:
            children = tab.get("children") or []
            if children:
                # Folder -> its tabs; special tab (map/unique...) -> its sections only, so
                # nothing is fetched (and counted) twice.
                for child in children:
                    add(child, None if tab.get("type") == "Folder" else tab)
                return
            if tab["id"] in seen:
                return
            seen.add(tab["id"])
            refs.append(
                TabRef(tab["id"], tab.get("name", ""), tab.get("type", ""),
                       parent["id"] if parent else None)
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
                    and list_children
                ):
                    # The POESESSID tab list doesn't include map/unique tab sections.
                    tab = {**tab, "children": list_children(self.league, tab["id"])}
                add(tab, parent if parent and parent.get("type") != "Folder" else None)
        if missing:
            log.warning("stash tabs not found in %s: %s", self.league, ", ".join(sorted(missing)))
        self._tabs = refs
        return refs

    def take(self) -> Snapshot:
        snap = Snapshot(taken_at=self.clock())
        try:
            # Inventory first: it is the part that can change while you are in a map.
            if self.include_inventory:
                char = self.api.get_character(self.resolve_character())
                if char.get("league") not in (None, self.league):
                    raise SnapshotError(
                        f"character {char.get('name')} is in {char.get('league')}, "
                        f"not {self.league}"
                    )
                snap.items.extend(
                    ItemEntry.from_api(i, "inventory")
                    for i in char.get("inventory") or ()
                    if is_tracked(i)
                )
            for tab in self.resolve_tabs():
                if tab.parent_id:
                    # A substash comes back wrapped in its parent.
                    wrapper = self.api.get_stash(self.league, tab.parent_id, tab.id)
                    inner = [c for c in wrapper.get("children") or () if c.get("id") == tab.id]
                    stash = inner[0] if inner else wrapper
                else:
                    stash = self.api.get_stash(self.league, tab.id)
                snap.items.extend(
                    ItemEntry.from_api(i, tab.id) for i in collect_items(stash) if is_tracked(i)
                )
        except Exception as e:  # any failure makes the snapshot unusable for diffing
            log.warning("snapshot failed: %s", e)
            snap.complete = False
            snap.error = str(e)
        return snap
