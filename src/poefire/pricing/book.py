"""Matches account items (API JSON) to poe.ninja price lines."""

from __future__ import annotations

import itertools
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from poefire.loot.items import clean, is_map, map_base
from poefire.pricing.ninja import PriceLine

type Json = dict[str, Any]

FRAME_GEM = 4
FRAME_UNIQUE = 3
FRAME_CURRENCY = 5
FRAME_DIVINATION = 6


@dataclass(frozen=True)
class Quote:
    chaos_each: float
    source: str  # poe.ninja type the price came from


def _int_prop(raw: Json, prop: str) -> int | None:
    for p in raw.get("properties") or ():
        if p.get("name") == prop and p.get("values"):
            m = re.search(r"\d+", str(p["values"][0][0]))
            return int(m.group()) if m else None
    return None


def _tier_in_name(name: str) -> int | None:
    m = re.search(r"\(Tier (\d+)\)", name)
    return int(m.group(1)) if m else None


def _links(raw: Json) -> int:
    groups: dict[int, int] = defaultdict(int)
    for s in raw.get("sockets") or ():
        groups[int(s.get("group", 0))] += 1
    return max(groups.values(), default=0)


# poe.ninja names blighted maps per map ("Blighted Strand Map"); older data per tier
# ("Blighted Map (Tier 16)").
_BLIGHTED_RE = re.compile(r"^(?P<kind>Blighted|Blight-ravaged) ")
_MAP_TYPES = frozenset({"Map", "UniqueMap", "BlightedMap", "BlightRavagedMap"})
# Map lines with fewer listings than this are one or two sellers' asking prices (e.g. a
# blighted map "worth" 160 divines): left out, so such a map isn't valued at all rather
# than at a made-up price.
MIN_MAP_LISTINGS = 10

_versions = itertools.count(1)


class PriceBook:
    """Built from (source, type, lines) triples. Exchange prices override stash prices.
    Each book gets a new `version`: caches of values keyed by it go stale with the prices."""

    def __init__(self, tables: Iterable[tuple[str, str, list[PriceLine]]]) -> None:
        self.version = next(_versions)
        self.fungible: dict[str, Quote] = {}  # name -> quote
        self.uniques: dict[str, list[tuple[PriceLine, str]]] = defaultdict(list)
        self.maps: dict[tuple[str, int | None], Quote] = {}
        self.gems: dict[str, list[PriceLine]] = defaultdict(list)
        # blighted / blight-ravaged maps: per map, and the cheapest of each tier as fallback
        self.blighted: dict[tuple[str, int], Quote] = {}
        exchange: dict[str, Quote] = {}
        for source, type_, lines in tables:
            for line in lines:
                if (type_ in _MAP_TYPES and line.listings is not None
                        and line.listings < MIN_MAP_LISTINGS):  # fmt: skip
                    continue
                if source == "exchange":
                    exchange[line.name] = Quote(line.chaos, f"exchange:{type_}")
                elif type_.startswith("Unique"):
                    self.uniques[line.name].append((line, type_))
                elif type_ in ("BlightedMap", "BlightRavagedMap"):
                    m = _BLIGHTED_RE.match(line.name)
                    t = line.map_tier or _tier_in_name(line.name)
                    if m and t:
                        if "(Tier" not in line.name:
                            self.maps.setdefault((line.name, t), Quote(line.chaos, type_))
                        key = (m["kind"].lower(), t)
                        old = self.blighted.get(key)
                        if old is None or line.chaos < old.chaos_each:  # err low
                            self.blighted[key] = Quote(line.chaos, type_)
                elif type_ == "Map":
                    self.maps.setdefault((line.name, line.map_tier), Quote(line.chaos, type_))
                elif type_ == "SkillGem":
                    self.gems[line.name].append(line)
                else:
                    self.fungible.setdefault(line.name, Quote(line.chaos, type_))
        self.fungible.update(exchange)
        self.fungible.setdefault("Chaos Orb", Quote(1.0, "fixed"))

    @property
    def chaos_per_divine(self) -> float | None:
        q = self.fungible.get("Divine Orb")
        return q.chaos_each if q and q.chaos_each > 0 else None

    def __len__(self) -> int:
        return (len(self.fungible) + sum(map(len, self.uniques.values())) + len(self.maps)
                + sum(map(len, self.gems.values())))  # fmt: skip

    def price(self, raw: Json) -> Quote | None:
        frame = int(raw.get("frameType", 0))
        name = clean(raw.get("name", ""))
        type_line = clean(raw.get("typeLine", "") or raw.get("baseType", ""))
        base = clean(raw.get("baseType", "")) or type_line
        if frame == FRAME_UNIQUE:
            return self._unique(name, base, raw)
        if frame == FRAME_GEM:
            return self._gem(type_line, raw)
        if is_map(raw):
            return self._map(raw, type_line)
        return self.fungible.get(type_line) or self.fungible.get(base)

    def _map(self, raw: Json, type_line: str) -> Quote | None:
        """By the map's name alone (its base: mods and a rare map's name don't matter), with
        its tier from the "Map Tier" property or the name ("Map (Tier 16)")."""
        base = map_base(raw)
        tier = _int_prop(raw, "Map Tier") or _tier_in_name(base)
        for kind in ("blight-ravaged", "blighted"):
            for name in (type_line, base):
                if name.lower().startswith(kind):
                    q = self.maps.get((name, tier)) if tier else None
                    return q or (self.blighted.get((kind, tier)) if tier else None)
        for key in ((base, tier), (type_line, tier), (base, None)):
            if (q := self.maps.get(key)) is not None:
                return q
        return self.fungible.get(base)

    def _unique(self, name: str, base: str, raw: Json) -> Quote | None:
        candidates = [(line, t) for line, t in self.uniques.get(name, ())
                      if not line.base_type or line.base_type == base]  # fmt: skip
        if not candidates:
            return None
        links = _links(raw)
        want = 6 if links >= 6 else 5 if links == 5 else None
        linked = [c for c in candidates if (c[0].links or None) == want]
        pool = linked or candidates
        # Ambiguous variants (e.g. different implicit rolls): take the cheapest, so values err
        # low rather than high.
        line, t = min(pool, key=lambda c: c[0].chaos)
        return Quote(line.chaos, t)

    def _gem(self, name: str, raw: Json) -> Quote | None:
        lines = self.gems.get(name)
        if not lines:
            return None
        level = _int_prop(raw, "Level") or 1
        quality = _int_prop(raw, "Quality") or 0
        corrupted = bool(raw.get("corrupted"))
        same = [g for g in lines if g.corrupted == corrupted] or lines

        def distance(g: PriceLine) -> tuple[int, int]:
            return (abs((g.gem_level or 1) - level), abs((g.gem_quality or 0) - quality))

        best = min(same, key=distance)
        return Quote(best.chaos, "SkillGem")
