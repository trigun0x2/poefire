"""poe.ninja league price data (PoE1).

Endpoints (base https://poe.ninja/poe1/api/economy):
    /stash/current/currency/overview?league&type=Currency|Fragment
        -> {lines: [{currencyTypeName, chaosEquivalent, ...}]}
    /stash/current/item/overview?league&type=<DivinationCard|Scarab|UniqueArmour|Map|...>
        -> {lines: [{name, baseType, chaosValue, divineValue, mapTier, gemLevel, gemQuality,
                     corrupted, links, variant, icon, ...}]}
    /exchange/current/overview?league&type=Currency
        -> {core: {primary: "chaos"}, lines: [{id, primaryValue}], items: [{id, name}]}
        (the in-game currency exchange; currency mostly trades there now)

poe.ninja updates PoE1 data about every 15 minutes and HTTP-caches responses, so polling
faster than that only adds load. Each response is reduced to the fields used for pricing.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

NINJA_BASE = "https://poe.ninja/poe1/api/economy"
MIN_REFRESH_SECONDS = 15 * 60

# (source, type). "currency" = stash currency overview, "item" = stash item overview,
# "exchange" = currency exchange overview (preferred over the stash value when present).
PRICE_SOURCES: tuple[tuple[str, str], ...] = (
    ("exchange", "Currency"),
    ("currency", "Currency"),
    ("currency", "Fragment"),
    *(
        ("item", t)
        for t in (
            "DivinationCard", "Scarab", "Essence", "Fossil", "Resonator", "Oil",
            "DeliriumOrb", "Incubator", "Omen", "Tattoo", "Invitation", "Artifact", "Vial",
            "Beast", "Map", "UniqueMap", "UniqueWeapon", "UniqueArmour", "UniqueAccessory",
            "UniqueFlask", "UniqueJewel", "SkillGem",
        )
    ),
)  # fmt: skip

type Json = dict[str, Any]

_ITEM_FIELDS = ("name", "baseType", "chaosValue", "divineValue", "mapTier", "gemLevel",
                "gemQuality", "corrupted", "links", "variant", "icon")  # fmt: skip


@dataclass(frozen=True)
class PriceLine:
    name: str
    chaos: float
    base_type: str | None = None
    map_tier: int | None = None
    gem_level: int | None = None
    gem_quality: int | None = None
    corrupted: bool = False
    links: int | None = None
    variant: str | None = None
    icon: str | None = None

    def to_json(self) -> Json:
        return {k: v for k, v in self.__dict__.items() if v not in (None, False)}

    @classmethod
    def from_json(cls, d: Json) -> PriceLine:
        return cls(**d)


class NinjaError(Exception):
    pass


class NinjaClient:
    def __init__(
        self,
        user_agent: str,
        http: httpx.Client | None = None,
        min_interval: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.user_agent = user_agent
        self.http = http or httpx.Client(timeout=30)
        self.min_interval = min_interval
        self.clock = clock
        self.sleep = sleep
        self._last = float("-inf")

    def _get(self, path: str, params: dict[str, str]) -> Json | None:
        """None on 404 (type retired this league); raises NinjaError on other failures."""
        wait = self._last + self.min_interval - self.clock()
        if wait > 0:
            self.sleep(wait)
        self._last = self.clock()
        try:
            r = self.http.get(
                NINJA_BASE + path,
                params=params,
                headers={"User-Agent": self.user_agent, "Accept": "application/json"},
            )
        except httpx.HTTPError as e:
            raise NinjaError(f"network error: {e}") from e
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise NinjaError(f"HTTP {r.status_code} for {path} {params.get('type')}")
        try:
            data: Json = r.json()
        except ValueError as e:
            raise NinjaError(f"non-JSON response for {path}") from e
        return data

    def fetch(self, source: str, league: str, type_: str) -> list[PriceLine]:
        params = {"league": league, "type": type_}
        if source == "currency":
            data = self._get("/stash/current/currency/overview", params)
            return [
                PriceLine(str(line["currencyTypeName"]), float(line["chaosEquivalent"]))
                for line in (data or {}).get("lines") or ()
                if isinstance(line.get("chaosEquivalent"), int | float)
                and line.get("currencyTypeName")
            ]
        if source == "exchange":
            data = self._get("/exchange/current/overview", params) or {}
            if (data.get("core") or {}).get("primary") != "chaos":
                return []
            names = {i.get("id"): i.get("name") for i in data.get("items") or ()}
            return [
                PriceLine(str(names[line["id"]]), float(line["primaryValue"]))
                for line in data.get("lines") or ()
                if names.get(line.get("id")) and isinstance(line.get("primaryValue"), int | float)
            ]
        if source == "item":
            data = self._get("/stash/current/item/overview", params)
            if data is None:  # poe.ninja moved this type to its currency exchange
                return self.fetch("exchange", league, type_)
            out = []
            for line in (data or {}).get("lines") or ():
                if not line.get("name") or not isinstance(line.get("chaosValue"), int | float):
                    continue
                d = {k: line.get(k) for k in _ITEM_FIELDS}
                out.append(
                    PriceLine(
                        name=str(d["name"]),
                        chaos=float(d["chaosValue"]),
                        base_type=d["baseType"] or None,
                        map_tier=d["mapTier"] or None,
                        gem_level=d["gemLevel"],
                        gem_quality=d["gemQuality"],
                        corrupted=bool(d["corrupted"]),
                        links=d["links"] or None,
                        variant=d["variant"] or None,
                        icon=d["icon"] or None,
                    )
                )
            return out
        raise ValueError(f"unknown source {source!r}")

    def leagues(self) -> list[str]:
        """League names poe.ninja tracks. Response shape varies, so accept several."""
        try:
            r = self.http.get(
                NINJA_BASE + "/leagues",
                headers={"User-Agent": self.user_agent, "Accept": "application/json"},
            )
            if r.status_code != 200:
                return []
            data = r.json()
        except (httpx.HTTPError, ValueError):
            return []
        arr = data if isinstance(data, list) else (
            data.get("leagues") or data.get("economyLeagues") or []
        )  # fmt: skip
        names = [x if isinstance(x, str) else (x.get("name") or x.get("id") or "") for x in arr]
        return [n for n in names if n]
