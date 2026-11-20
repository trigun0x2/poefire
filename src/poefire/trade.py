"""Price check on the official trade site (step 4 of docs/scope/price-check.md).

The item's mod lines are matched to the trade site's stat ids using GGG's own trade data
(`/api/trade/data/stats`, `/items`, `/static`; fetched on first use, kept for a day). The
popup lists them as filters; ticked ones (all explicit mods by default, at least 90% of
their roll) go into one search. The 20 cheapest listings are fetched: the 5 cheapest are
shown, with the average of the 20 without the cheapest and dearest few.

Only on your key press or Search click; never in the background. The trade site's
rate-limit headers are followed and never waited out: a search that would have to wait
says how long instead (its penalties last minutes).
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

from poefire.api.ratelimit import RateLimiter
from poefire.pricecheck import ParsedItem
from poefire.pricing.book import PriceBook

log = logging.getLogger(__name__)

type Json = dict[str, Any]

TRADE_BASE = "https://www.pathofexile.com"
DATA_MAX_AGE = 24 * 3600.0
DEFAULT_SHARE = 0.9  # ticked mods search for at least 90% of the item's roll
SHOWN = 5  # cheapest listings shown
AVERAGED = 20  # listings fetched for the average
FETCH_CHUNK = 10  # the trade site's limit per fetch
# "available" = Instant Buyout listings (merchant tabs), as the trade site encodes its status
# dropdown; "online" would be In Person only.
LISTING_STATUS = "available"

# Item classes whose damage/defence mods are "local" (they change the item's own numbers).
_LOCAL_CLASSES = frozenset({
    "Body Armours", "Helmets", "Gloves", "Boots", "Shields", "Bows", "Claws", "Daggers",
    "Rune Daggers", "One Hand Swords", "Thrusting One Hand Swords", "Two Hand Swords",
    "One Hand Axes", "Two Hand Axes", "One Hand Maces", "Two Hand Maces", "Sceptres",
    "Staves", "Warstaves", "Wands", "Fishing Rods",
})  # fmt: skip

# Lines the game marks with their kind; anything else is an explicit mod.
_KIND_SUFFIX = re.compile(r"\s*\((implicit|crafted|fractured|enchant)\)$")
_NUM = re.compile(r"\d+(?:\.\d+)?")
_PROPERTY_LINE = re.compile(r"^[A-Za-z' ]+:( |$)")


class TradeError(Exception):
    pass


class TradeRateLimited(TradeError):
    def __init__(self, wait: float) -> None:
        super().__init__(f"The trade site's rate limit: try again in {math.ceil(wait)}s")
        self.wait = wait


# --- GGG's trade data ---


@dataclass(frozen=True)
class ModMatch:
    stat_id: str
    text: str  # the trade site's wording, e.g. "+#% to Fire Resistance"
    value: float | None  # the item's roll (the average of "Adds # to #"); negative if reduced


class TradeData:
    """Stat templates per kind (explicit, implicit, ...), item bases and currency names."""

    def __init__(self, stats: Json, items: Json | None = None, static: Json | None = None):
        self.stats: dict[str, dict[str, str]] = {}  # kind -> text -> stat id
        for group in stats.get("result") or ():
            for e in group.get("entries") or ():
                kind, text, sid = e.get("type"), e.get("text"), e.get("id")
                if not (kind and text and sid) or "\n" in text or e.get("option"):
                    continue
                self.stats.setdefault(kind, {}).setdefault(text, sid)
        self.bases: set[str] = set()
        for group in (items or {}).get("result") or ():
            for e in group.get("entries") or ():
                if not e.get("name") and e.get("type"):
                    self.bases.add(e["type"])
        self.currency: dict[str, str] = {}  # trade id ("divine") -> name ("Divine Orb")
        for group in (static or {}).get("result") or ():
            if group.get("id") == "Currency":
                for e in group.get("entries") or ():
                    if e.get("id") and e.get("text"):
                        self.currency[e["id"]] = e["text"]

    def match(self, line: str, kind: str, local: bool) -> ModMatch | None:
        table = self.stats.get(kind) or {}
        values = [float(v) for v in _NUM.findall(line)]
        template = _NUM.sub("#", line)
        tries = [(template, 1)]
        if template.startswith("-#"):  # "-10% to Cold Resistance"
            tries.append(("+" + template[1:], -1))
        for a, b in (("increased", "reduced"), ("more", "less")):
            if re.search(rf"\b{b}\b", template):
                tries.append((re.sub(rf"\b{b}\b", a, template, count=1), -1))
        for text, sign in tries:
            sid = (table.get(f"{text} (Local)") if local else None) or table.get(text)
            if sid:
                value = sign * sum(values) / len(values) if values else None
                text = f"{text} (Local)" if local and f"{text} (Local)" in table else text
                return ModMatch(sid, text, value)
        return None

    def base_of(self, item: ParsedItem) -> str:
        """The item's base type; a magic item's line holds affixes around it."""
        if item.rarity != "Magic" or not self.bases:
            return item.base
        found = [b for b in self.bases if b in item.base]
        return max(found, key=len) if found else item.base

    def to_json(self) -> Json:
        return {"stats": self.stats, "bases": sorted(self.bases), "currency": self.currency}

    @classmethod
    def from_json(cls, d: Json) -> TradeData:
        data = cls({})
        data.stats = d.get("stats") or {}
        data.bases = set(d.get("bases") or ())
        data.currency = d.get("currency") or {}
        return data


def load_trade_data(path: Path, fetch: Callable[[str], Json], now: float | None = None,
                    max_age: float = DATA_MAX_AGE) -> TradeData:  # fmt: skip
    """GGG's trade data, from the copy at `path` while it's fresh, else fetched (and saved).
    A failed fetch falls back to an old copy."""
    now = time.time() if now is None else now
    cached: Json | None = None
    with contextlib.suppress(OSError, ValueError):
        cached = json.loads(path.read_text(encoding="utf-8"))
    if cached and now - float(cached.get("fetched_at", 0)) < max_age:
        return TradeData.from_json(cached)
    try:
        data = TradeData(fetch("stats"), fetch("items"), fetch("static"))
    except Exception:
        if cached:
            log.warning("trade data refresh failed; using the copy from before", exc_info=True)
            return TradeData.from_json(cached)
        raise
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**data.to_json(), "fetched_at": now}), encoding="utf-8")
    except OSError:
        pass
    return data


# --- the filters the popup shows ---


@dataclass
class Filter:
    key: str  # "stat:<id>" or an item filter: ilvl, corrupted, links, gem_level, quality,
    # map_tier, influence:<name>
    label: str
    group: str  # "item", "pseudo", "implicit", "explicit", "crafted", "fractured", "enchant"
    enabled: bool
    value: float | None = None  # the item's own value
    min: float | None = None
    max: float | None = None
    stat_id: str | None = None

    def to_json(self) -> Json:
        return asdict(self)


@dataclass
class Check:
    """What the popup searches for: the item's base (and name, for uniques) and filters."""

    name: str | None  # uniques only
    base: str
    rarity: str
    filters: list[Filter] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)  # mod lines the trade site can't take

    def to_json(self) -> Json:
        return {"name": self.name, "base": self.base, "rarity": self.rarity,
                "filters": [f.to_json() for f in self.filters],
                "unmatched": self.unmatched}  # fmt: skip


def _default_bound(value: float | None) -> tuple[float | None, float | None]:
    """(min, max) at 90% of the roll: a minimum for positive rolls, a maximum for negative."""
    if value is None:
        return None, None
    v = value * DEFAULT_SHARE
    v = math.floor(v) if v > 0 and value == int(value) else round(v, 1)
    if value < 0:
        return None, math.ceil(value * DEFAULT_SHARE) if value == int(value) else v
    return v, None


# --- pseudo totals: the trade site's sums over all of an item's mods ---

PSEUDO_ELE_RES = "pseudo.pseudo_total_elemental_resistance"
PSEUDO_RES = "pseudo.pseudo_total_resistance"
PSEUDO_LIFE = "pseudo.pseudo_total_life"

_ELEMENTS = frozenset({"fire", "cold", "lightning"})
_RES_LINE = re.compile(r"^([+-]\d+)% to (all Elemental|(?:Fire|Cold|Lightning|Chaos)"
                       r"(?: and (?:Fire|Cold|Lightning|Chaos))?) Resistances?$")  # fmt: skip
_LIFE_LINE = re.compile(r"^([+-]\d+) to maximum Life$")
_STR_LINE = re.compile(
    r"^\+(\d+) to (?:Strength|all Attributes|Strength and (?:Dexterity|Intelligence))$"
)


def contributions(text: str) -> tuple[dict[str, float], bool]:
    """What one mod line adds to the totals ({"fire": 12, "cold": 12}, {"life": 45}), and
    whether that's all the mod does, so a total can stand in for it in the search."""
    if m := _RES_LINE.match(text):
        names = m[2].lower()
        found = _ELEMENTS if names == "all elemental" else frozenset(names.split(" and "))
        return dict.fromkeys(found, float(m[1])), True
    if m := _LIFE_LINE.match(text):
        return {"life": float(m[1])}, True
    if m := _STR_LINE.match(text):
        return {"life": float(m[1]) / 2}, False  # 1 life per 2 strength; it's still Strength
    return {}, False


def _pseudo_filters(totals: dict[str, float], has_life: bool, data: TradeData,
                    ) -> tuple[list[Filter], frozenset[str]]:  # fmt: skip
    """The pseudo total filters, and what the ticked ones cover ("fire", ..., "life").
    Only totals GGG's trade data lists are offered."""
    known = set((data.stats.get("pseudo") or {}).values())
    ele = sum(totals.get(e, 0.0) for e in _ELEMENTS)
    chaos, life = totals.get("chaos", 0.0), totals.get("life", 0.0)
    all_res = chaos > 0 and PSEUDO_RES in known  # with chaos: one total for all four
    out: list[Filter] = []
    covered: set[str] = set()

    def add(sid: str, label: str, value: float, enabled: bool, covers: frozenset[str]) -> None:
        lo, hi = _default_bound(value)
        out.append(Filter(f"stat:{sid}", label, "pseudo", enabled, value, lo, hi, sid))
        if enabled:
            covered.update(covers)

    if ele > 0 and PSEUDO_ELE_RES in known:
        add(PSEUDO_ELE_RES, f"+{ele:g}% total Elemental Resistance", ele, not all_res, _ELEMENTS)
    if all_res and ele + chaos > 0:
        add(PSEUDO_RES, f"+{ele + chaos:g}% total Resistance", ele + chaos, True,
            _ELEMENTS | {"chaos"})  # fmt: skip
    if has_life and life > 0 and PSEUDO_LIFE in known:
        add(PSEUDO_LIFE, f"+{life:g} total maximum Life", life, True, frozenset({"life"}))
    return out, frozenset(covered)


def build_check(item: ParsedItem, data: TradeData) -> Check:
    unique = item.rarity == "Unique"
    check = Check(item.name if unique and item.name else None, data.base_of(item), item.rarity)
    f = check.filters
    tier = item.int_property("Map Tier")
    if tier is not None:
        f.append(Filter("map_tier", f"Map tier {tier}", "item", True, tier, tier, tier))
    if item.rarity == "Gem":
        level, quality = item.int_property("Level"), item.int_property("Quality")
        if level is not None:
            f.append(Filter("gem_level", f"Gem level {level}+", "item", True, level, level))
        if quality:
            f.append(Filter("quality", f"Quality {quality}%+", "item", True, quality, quality))
    if item.links >= 5:
        f.append(Filter("links", f"{item.links} links", "item", True, item.links, item.links))
    if item.item_level is not None and item.rarity in ("Rare", "Magic", "Normal"):
        f.append(Filter("ilvl", f"Item level {item.item_level}+", "item", False,
                        item.item_level, item.item_level))  # fmt: skip
    f.append(Filter("corrupted", "Corrupted" if item.corrupted else "Not corrupted", "item",
                    True, 1.0 if item.corrupted else 0.0))  # fmt: skip
    for name in item.influences:
        f.append(Filter(f"influence:{name}", f"{name.title()} influence", "item", True, 1.0))
    local = item.item_class in _LOCAL_CLASSES
    totals: dict[str, float] = {}
    has_life = False
    replaceable: list[tuple[Filter, frozenset[str]]] = []  # mods a ticked total stands in for
    for line in item.lines:
        if _PROPERTY_LINE.match(line):  # "Str: 100", "Attacks per Second: 1.50"
            continue
        m = _KIND_SUFFIX.search(line)
        kind = m.group(1) if m else "explicit"
        text = line[: m.start()] if m else line
        adds, only_totals = contributions(text)
        for k, v in adds.items():
            totals[k] = totals.get(k, 0.0) + v
        has_life = has_life or bool(_LIFE_LINE.match(text))
        match = data.match(text, kind, local)
        if match is None:
            if m or _NUM.search(text):  # looks like a mod: say it can't be searched
                check.unmatched.append(line)
            continue
        lo, hi = _default_bound(match.value)
        f.append(Filter(f"stat:{match.stat_id}", text, kind,
                        kind in ("explicit", "crafted", "fractured"), match.value, lo, hi,
                        match.stat_id))  # fmt: skip
        if only_totals:
            replaceable.append((f[-1], frozenset(adds)))
    if item.rarity in ("Rare", "Magic"):
        pseudo, covered = _pseudo_filters(totals, has_life, data)
        f.extend(pseudo)
        for flt, parts in replaceable:  # the ticked totals are searched instead
            if parts <= covered:
                flt.enabled = False
    return check


def apply_choices(check: Check, choices: Iterable[Json]) -> None:
    """The popup's ticks and min/max values, by filter key."""
    by_key = {c.get("key"): c for c in choices}
    for f in check.filters:
        c = by_key.get(f.key)
        if c is None:
            continue
        f.enabled = bool(c.get("enabled"))
        for bound in ("min", "max"):
            v = c.get(bound)
            setattr(f, bound, float(v) if isinstance(v, int | float) else None)


def query(check: Check) -> Json:
    q: Json = {"status": {"option": LISTING_STATUS}, "type": check.base}
    if check.name:
        q["name"] = check.name
    stats = []
    misc: Json = {}
    filters: Json = {}
    for f in check.filters:
        if not f.enabled:
            continue
        rng = {k: v for k, v in (("min", f.min), ("max", f.max)) if v is not None}
        if f.stat_id:
            stats.append({"id": f.stat_id, "value": rng, "disabled": False})
        elif f.key == "corrupted":
            misc["corrupted"] = {"option": "true" if f.value else "false"}
        elif f.key.startswith("influence:"):
            misc[f"{f.key.removeprefix('influence:')}_item"] = {"option": "true"}
        elif f.key in ("ilvl", "gem_level", "quality"):
            misc[f.key] = rng
        elif f.key == "links":
            filters["socket_filters"] = {"filters": {"links": rng}}
        elif f.key == "map_tier":
            filters["map_filters"] = {"filters": {"map_tier": rng}}
    if misc:
        filters["misc_filters"] = {"filters": misc}
    if check.rarity in ("Rare", "Magic", "Normal"):
        filters["type_filters"] = {"filters": {"rarity": {"option": "nonunique"}}}
    if filters:
        q["filters"] = filters
    q["stats"] = [{"type": "and", "filters": stats}]
    return {"query": q, "sort": {"price": "asc"}}


# --- results ---


@dataclass
class Listing:
    amount: float
    currency: str
    chaos: float | None  # at poe.ninja's rate for the currency
    indexed: str | None  # when it was listed (ISO time)

    def to_json(self) -> Json:
        return asdict(self)


def listing_from(hit: Json, data: TradeData, book: PriceBook) -> Listing | None:
    price = (hit.get("listing") or {}).get("price") or {}
    amount, currency = price.get("amount"), price.get("currency")
    if not isinstance(amount, int | float) or not currency:
        return None
    name = data.currency.get(currency) or _CURRENCY_NAMES.get(currency) or currency
    quote = book.price({"typeLine": name, "baseType": name, "frameType": 5})
    each = 1.0 if name == "Chaos Orb" else quote.chaos_each if quote else None
    return Listing(float(amount), name, None if each is None else float(amount) * each,
                   (hit.get("listing") or {}).get("indexed"))  # fmt: skip


_CURRENCY_NAMES = {"chaos": "Chaos Orb", "divine": "Divine Orb", "exalted": "Exalted Orb",
                   "alch": "Orb of Alchemy", "mirror": "Mirror of Kalandra"}  # fmt: skip


def trimmed_average(values: list[float]) -> float | None:
    """Mean without the cheapest and dearest tenth (2 of 20): a price-fixer's bait or an
    absurd listing doesn't move it."""
    if not values:
        return None
    v = sorted(values)
    k = len(v) // 10
    kept = v[k : len(v) - k] if len(v) - 2 * k >= 3 else v
    return sum(kept) / len(kept)


def summarize(listings: list[Listing], total: int, query_id: str | None, league: str) -> Json:
    priced = sorted((x for x in listings if x.chaos is not None), key=lambda x: x.chaos or 0)
    avg = trimmed_average([x.chaos for x in priced if x.chaos is not None])
    return {
        "total": total,
        "cheapest": [x.to_json() for x in priced[:SHOWN]],
        "average_chaos": None if avg is None else round(avg, 2),
        "averaged": len(priced),
        "url": f"{TRADE_BASE}/trade/search/{league}/{query_id}" if query_id else None,
    }


# --- the trade site ---


class TradeClient:
    """Search and fetch on the trade site, with its own rate limiter. Never waits: a request
    the limits don't allow yet raises TradeRateLimited."""

    def __init__(self, user_agent: str, cookie: Callable[[], str | None] = lambda: None,
                 http: httpx.Client | None = None, limiter: RateLimiter | None = None,
                 ) -> None:  # fmt: skip
        self.user_agent = user_agent
        self.cookie = cookie
        self.http = http or httpx.Client(timeout=20)
        self.limiter = limiter or RateLimiter()
        self._lock = threading.Lock()

    def _request(self, bucket: str, method: str, path: str, body: Json | None = None,
                 params: dict[str, str] | None = None) -> Json:  # fmt: skip
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        if cookie := self.cookie():
            headers["Cookie"] = f"POESESSID={cookie}"
        with self._lock:
            if (left := self.limiter.wait_time(bucket)) > 0:
                raise TradeRateLimited(left)
            self.limiter.acquire(bucket)
            try:
                resp = self.http.request(method, TRADE_BASE + path, json=body, params=params,
                                         headers=headers)  # fmt: skip
            except httpx.HTTPError as e:
                raise TradeError(f"network error: {e}") from e
            self.limiter.update(bucket, resp.headers, resp.status_code)
        if resp.status_code == 429:
            raise TradeRateLimited(float(resp.headers.get("Retry-After") or 60))
        try:
            data: Json = resp.json()
        except ValueError as e:
            challenged = resp.headers.get("cf-mitigated") == "challenge"
            raise TradeError(
                "The trade site's bot protection blocked the request"
                if challenged else f"HTTP {resp.status_code} from the trade site (not JSON)"
            ) from e  # fmt: skip
        if resp.status_code != 200:
            msg = ((data.get("error") or {}).get("message")) if isinstance(data, dict) else None
            raise TradeError(f"The trade site said: {msg or f'HTTP {resp.status_code}'}")
        return data

    def data(self, kind: str) -> Json:
        return self._request("trade-data", "GET", f"/api/trade/data/{kind}")

    def search(self, league: str, q: Json) -> tuple[str | None, list[str], int]:
        d = self._request("trade-search", "POST", f"/api/trade/search/{league}", body=q)
        ids = [str(i) for i in d.get("result") or ()]
        return d.get("id"), ids, int(d.get("total") or len(ids))

    def fetch(self, ids: list[str], query_id: str | None) -> list[Json]:
        hits: list[Json] = []
        for i in range(0, len(ids), FETCH_CHUNK):
            chunk = ids[i : i + FETCH_CHUNK]
            params = {"query": query_id} if query_id else None
            d = self._request("trade-fetch", "GET", f"/api/trade/fetch/{','.join(chunk)}",
                              params=params)  # fmt: skip
            hits.extend(h for h in d.get("result") or () if h)
        return hits


def search(client: TradeClient, data: TradeData, book: PriceBook, league: str,
           check: Check) -> Json:  # fmt: skip
    query_id, ids, total = client.search(league, query(check))
    hits = client.fetch(ids[:AVERAGED], query_id) if ids else []
    listings = [x for h in hits if (x := listing_from(h, data, book)) is not None]
    return summarize(listings, total, query_id, league)
