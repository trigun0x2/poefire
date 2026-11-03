"""Item identity for diffing.

Stackable items (currency, fragments, scarabs, cards...) are compared by quantity per item
key, because stacks merge and split as you stash them. Everything else is compared by the
API's unique item `id`, which stays the same while an item moves between inventory and tabs.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from typing import Any

type Json = dict[str, Any]

_MARKUP_RE = re.compile(r"<<[^>]*>>")  # e.g. "<<set:MS>><<set:M>><<set:S>>Doom Spiral"
# Properties that make otherwise-identical items worth different amounts.
_KEY_PROPERTIES = ("Map Tier", "Level", "Quality")


@dataclass(frozen=True)
class ItemEntry:
    item_id: str | None
    key: str
    name: str
    frame_type: int
    quantity: int
    stackable: bool
    source: str  # "inventory" or a stash tab id
    raw: Json

    @classmethod
    def from_api(cls, raw: Json, source: str) -> ItemEntry:
        stackable = "stackSize" in raw or "maxStackSize" in raw
        return cls(
            item_id=raw.get("id"),
            key=item_key(raw),
            name=display_name(raw),
            frame_type=int(raw.get("frameType", 0)),
            quantity=int(raw.get("stackSize", 1)) if stackable else 1,
            stackable=stackable,
            source=source,
            raw=raw,
        )


FRAME_UNIQUE = 3
_GEAR_ICON_RE = re.compile(
    r"2DItems/(Armours|Weapons|Rings|Amulets|Belts|Flasks|Jewels|Quivers|Trinkets)/"
)


def is_tracked(raw: Json) -> bool:
    """Loot that counts toward a map's value: everything except gear. Every unique is ignored
    (unique maps included), and so is normal/magic/rare equipment: weapons, armour,
    jewellery, flasks, jewels. Currency, fragments, scarabs, cards, gems, maps, beasts,
    logbooks... are all tracked."""
    if int(raw.get("frameType", 0)) == FRAME_UNIQUE:
        return False
    return not _is_gear(raw)


def _icon_path(icon: str) -> str:
    """The art path of an item icon. Newer icon URLs carry it base64-encoded:
    .../gen/image/<base64 of [w,h,{"f":"2DItems/Currency/..."}]>/<hash>/<file>.png"""
    if "/gen/image/" not in icon:
        return icon
    segment = icon.split("/gen/image/", 1)[1].split("/", 1)[0]
    try:
        return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)).decode(
            "utf-8", "replace"
        )
    except (ValueError, binascii.Error):
        return icon


def _is_gear(raw: Json) -> bool:
    if "stackSize" in raw or "maxStackSize" in raw:
        return False
    if _property(raw, "Map Tier") is not None:
        return False
    icon = raw.get("icon")
    if isinstance(icon, str) and icon:
        return bool(_GEAR_ICON_RE.search(_icon_path(icon)))
    # no icon to go by: equipment has level/attribute requirements or sockets
    return bool(raw.get("requirements") or raw.get("sockets"))


def clean(text: str) -> str:
    return _MARKUP_RE.sub("", text).strip()


def display_name(raw: Json) -> str:
    name = clean(raw.get("name", ""))
    type_line = clean(raw.get("typeLine", "") or raw.get("baseType", ""))
    return f"{name} {type_line}".strip() if name else type_line


def _property(raw: Json, prop: str) -> str | None:
    for p in raw.get("properties", ()) or ():
        if p.get("name") == prop and p.get("values"):
            value = str(p["values"][0][0])
            return value.split()[0]  # "20 (Max)" -> "20", "+20%" stays
    return None


def item_key(raw: Json) -> str:
    """Stable key for "the same kind of item", for aggregating and (later) pricing."""
    parts = [
        str(raw.get("frameType", 0)),
        clean(raw.get("name", "")),
        clean(raw.get("typeLine", "") or raw.get("baseType", "")),
    ]
    for prop in _KEY_PROPERTIES:
        if (v := _property(raw, prop)) is not None:
            parts.append(f"{prop}={v}")
    if raw.get("corrupted"):
        parts.append("corrupted")
    return "|".join(parts)
