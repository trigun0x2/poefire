"""Item identity for diffing.

Stackable items (currency, fragments, scarabs, cards...) are compared by quantity per item
key, because stacks merge and split as you stash them. Everything else is compared by the
API's unique item `id`, which stays the same while an item moves between inventory and tabs.
"""

from __future__ import annotations

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
