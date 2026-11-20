"""Price check: the item text the game copies (Ctrl+C), parsed and priced.

Step 2 of docs/scope/price-check.md: items poe.ninja prices (currency, fragments, divination
cards, essences, oils, uniques, gems, maps...). Rares and other items with mods come with the
trade search (step 4); for them this says so.

The game's item text, sections split by "--------":

    Item Class: Map Fragments
    Rarity: Normal
    Reality Fragment
    --------
    The Absence of Mercy and Empathy is level 85
    ...

The first section is the header (class, rarity, name, and for rares/uniques a second line
with the base). The others hold properties ("Map Tier: 16", "Stack Size: 3/20", "Level: 20
(Max)", "Quality: +20% (augmented)", "Sockets: R-R-G B", "Item Level: 84"), requirements,
mods, flavour text, and flags ("Corrupted", "Unidentified").
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from poefire.pricing.book import PriceBook, Quote

SEPARATOR = "--------"
FRAME = {"Normal": 0, "Magic": 1, "Rare": 2, "Unique": 3, "Gem": 4, "Currency": 5,
         "Divination Card": 6, "Quest": 7}  # fmt: skip
_PROPERTY_RE = re.compile(r"^(?P<name>[A-Za-z' ]+): (?P<value>.+)$")
_INT_RE = re.compile(r"-?\d[\d,]*")

type Json = dict[str, Any]


@dataclass
class ParsedItem:
    item_class: str
    rarity: str
    name: str  # uniques/rares: the item's own name; others: "" (the base is in `base`)
    base: str
    properties: dict[str, str] = field(default_factory=dict)
    stack: int = 1
    item_level: int | None = None
    sockets: str | None = None  # "R-R-G B": links with "-", groups with spaces
    corrupted: bool = False
    unidentified: bool = False
    mirrored: bool = False
    lines: list[str] = field(default_factory=list)  # every other line (mods, flavour...)

    @property
    def frame_type(self) -> int:
        return FRAME.get(self.rarity, 0)

    @property
    def display_name(self) -> str:
        return f"{self.name} {self.base}".strip() if self.name else self.base

    @property
    def links(self) -> int:
        if not self.sockets:
            return 0
        return max(len(group.split("-")) for group in self.sockets.split())

    def int_property(self, name: str) -> int | None:
        value = self.properties.get(name)
        m = _INT_RE.search(value or "")
        return int(m.group().replace(",", "")) if m else None

    def raw(self) -> Json:
        """The shape of the website's item JSON that the price book reads."""
        props = [{"name": k, "values": [[v.split(" (")[0], 0]]} for k, v in self.properties.items()
                 if k in ("Map Tier", "Level", "Quality")]  # fmt: skip
        out: Json = {
            "name": self.name, "typeLine": self.base, "baseType": self.base,
            "frameType": self.frame_type, "properties": props, "stackSize": self.stack,
        }  # fmt: skip
        if self.sockets:  # the price book counts links from the groups
            out["sockets"] = [{"group": g} for g, group in enumerate(self.sockets.split())
                              for _ in group.split("-")]  # fmt: skip
        if self.corrupted:
            out["corrupted"] = True
        return out


class NotAnItem(ValueError):
    pass


def parse(text: str) -> ParsedItem:
    """Raises NotAnItem for anything that isn't the game's item text."""
    lines = [ln.strip() for ln in text.replace("\r\n", "\n").split("\n")]
    if not lines or not lines[0].startswith("Item Class:") or SEPARATOR not in lines:
        raise NotAnItem("not an item copied from the game (Ctrl+C on an item)")
    sections: list[list[str]] = [[]]
    for ln in lines:
        if ln == SEPARATOR:
            sections.append([])
        elif ln:
            sections[-1].append(ln)
    header = sections[0]
    item_class = header[0].removeprefix("Item Class:").strip()
    rarity = next((h.removeprefix("Rarity:").strip() for h in header if h.startswith("Rarity:")),
                  "Normal")  # fmt: skip
    names = [h for h in header[1:] if not h.startswith("Rarity:")]
    if not names:
        raise NotAnItem("the item text has no name")
    if len(names) >= 2:  # rare / unique: name, then base
        name, base = names[0], names[1]
    else:
        name, base = "", names[0]
    item = ParsedItem(item_class, rarity, name, base)
    for section in sections[1:]:
        for ln in section:
            if ln == "Corrupted":
                item.corrupted = True
            elif ln == "Unidentified":
                item.unidentified = True
            elif ln == "Mirrored":
                item.mirrored = True
            elif (m := _PROPERTY_RE.match(ln)) and m["name"] in _KNOWN_PROPERTIES:
                key, value = m["name"], m["value"].strip()
                if key == "Stack Size":
                    item.stack = int(value.split("/")[0].replace(",", "").replace(".", "") or 1)
                elif key == "Item Level":
                    item.item_level = int(value)
                elif key == "Sockets":
                    item.sockets = value
                else:
                    item.properties[key] = value
            else:
                item.lines.append(ln)
    return item


_KNOWN_PROPERTIES = {"Stack Size", "Item Level", "Sockets", "Map Tier", "Level", "Quality",
                     "Area Level", "Experience"}  # fmt: skip


# --- pricing ---


@dataclass
class PriceResult:
    item: ParsedItem
    chaos_each: float | None
    source: str | None  # where the price came from (poe.ninja table)
    note: str | None = None  # why there's no price, or a caveat

    @property
    def chaos_total(self) -> float | None:
        return None if self.chaos_each is None else self.chaos_each * self.item.stack

    def to_json(self, chaos_per_divine: float | None) -> Json:
        total = self.chaos_total
        return {
            "name": self.item.display_name,
            "class": self.item.item_class,
            "rarity": self.item.rarity,
            "stack": self.item.stack,
            "chaos_each": None if self.chaos_each is None else round(self.chaos_each, 2),
            "chaos_total": None if total is None else round(total, 2),
            "divine_total": round(total / chaos_per_divine, 2)
            if total is not None and chaos_per_divine else None,
            "source": self.source,
            "note": self.note,
            "corrupted": self.item.corrupted,
            "links": self.item.links,
        }  # fmt: skip


def price(item: ParsedItem, book: PriceBook) -> PriceResult:
    """poe.ninja's price for the item, if it has one."""
    if item.rarity in ("Rare", "Magic") and item.item_class != "Maps" and "Map" not in item.base:
        return PriceResult(item, None, None, "needs a trade search (coming soon)")  # fmt: skip
    if item.unidentified and item.rarity == "Unique":
        return PriceResult(item, None, None, "identify it first: unidentified uniques can't be "
                                             "told apart")  # fmt: skip
    raw = item.raw()
    if "Map" in item.base and item.rarity in ("Magic", "Rare"):
        raw = {**raw, "typeLine": _map_base(item, book), "baseType": _map_base(item, book)}
    q: Quote | None = book.price(raw)
    if q is None:
        return PriceResult(item, None, None, "poe.ninja has no price for it")
    return PriceResult(item, q.chaos_each, q.source)


def _map_base(item: ParsedItem, book: PriceBook) -> str:
    """A magic map's line is "<prefix> Strand Map <suffix>": find the known map base in it."""
    if item.rarity == "Rare":
        return item.base
    names = sorted({name for name, _ in book.maps}, key=len, reverse=True)
    return next((n for n in names if n in item.base), item.base)
