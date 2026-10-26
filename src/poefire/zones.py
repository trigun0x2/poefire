"""Zone classification by internal area ID (zone display names are a fallback only).

DRAFT: the town/hideout ID lists need checking against real logs.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum


class ZoneKind(StrEnum):
    HIDEOUT = "hideout"
    TOWN = "town"
    MAP = "map"  # starts (or resumes) a run
    OTHER = "other"  # campaign, side areas, lab, ...

    @property
    def is_safe(self) -> bool:
        """Safe zones pause the current run."""
        return self in (ZoneKind.HIDEOUT, ZoneKind.TOWN)


_ACT_TOWN_RE = re.compile(r"^\d+_\d+_town$")  # e.g. 1_1_town, 2_10_town

DEFAULT_TOWN_IDS: frozenset[str] = frozenset(
    {
        "HeistHub",  # Rogue Harbour
        "Delve_Main",  # Azurite Mine
    }
)
DEFAULT_TOWN_NAMES: frozenset[str] = frozenset(
    {
        "Lioneye's Watch",
        "The Forest Encampment",
        "The Sarn Encampment",
        "Highgate",
        "Overseer's Tower",
        "The Bridge Encampment",
        "Oriath Docks",
        "Oriath",
        "Karui Shores",
        "The Rogue Harbour",
        "Azurite Mine",
        "Kingsmarch",
    }
)


@dataclass(frozen=True)
class ZoneRules:
    map_prefixes: tuple[str, ...] = ("Map",)
    hideout_prefixes: tuple[str, ...] = ("Hideout",)
    town_ids: frozenset[str] = DEFAULT_TOWN_IDS
    town_names: frozenset[str] = DEFAULT_TOWN_NAMES
    # Area IDs that would otherwise match a map prefix but are not runs.
    excluded_ids: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def with_extras(
        cls,
        extra_map_prefixes: Iterable[str] = (),
        extra_town_ids: Iterable[str] = (),
        excluded_ids: Iterable[str] = (),
    ) -> ZoneRules:
        base = cls()
        return cls(
            map_prefixes=base.map_prefixes + tuple(extra_map_prefixes),
            town_ids=base.town_ids | frozenset(extra_town_ids),
            excluded_ids=frozenset(excluded_ids),
        )

    def classify(self, area_id: str) -> ZoneKind:
        if area_id.startswith(self.hideout_prefixes):
            return ZoneKind.HIDEOUT
        if area_id in self.town_ids or _ACT_TOWN_RE.match(area_id):
            return ZoneKind.TOWN
        if area_id not in self.excluded_ids and area_id.startswith(self.map_prefixes):
            return ZoneKind.MAP
        return ZoneKind.OTHER

    def classify_name(self, zone: str) -> ZoneKind:
        """Fallback when no `Generating` line preceded the entry. Can never yield MAP."""
        if zone.endswith("Hideout"):
            return ZoneKind.HIDEOUT
        if zone in self.town_names:
            return ZoneKind.TOWN
        return ZoneKind.OTHER
