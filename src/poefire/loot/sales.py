"""Trade sales aren't loot.

When an item sells from a merchant tab, its price lands in your stash, sometimes at once,
sometimes only when you collect it. A loot snapshot would count that currency as dropped in
the map you were running. The website's trade history (your account's sales, with time and
price) says what to take back out.

How: every snapshot also fetches the history (throttled). A sale not yet accounted for whose
price shows up as a gain of that currency (at least the whole price) in the change since the
previous snapshot is taken out of that change. It goes in as its own minus line, e.g. "Sold on
trade · Bladefall of Impaling, -44 Divine Orb", priced like the currency it was paid in. So
the map's loot total drops by the sale, and the list shows why. Sales are matched oldest
first; one never counts twice; a sale whose payment never shows up in a watched tab is
dropped after a week.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from poefire.loot.diff import ItemDelta
from poefire.loot.items import Json, clean

log = logging.getLogger(__name__)

SOLD_PREFIX = "sold|"
KEEP_PENDING = timedelta(days=7)
KEEP_SEEN = 2000  # sale ids remembered, so a sale is never counted twice

# The trade site's currency ids -> the item's name in your stash. The usual ways to price.
CURRENCY = {
    "chaos": "Chaos Orb",
    "divine": "Divine Orb",
    "exalted": "Exalted Orb",
    "mirror": "Mirror of Kalandra",
    "alch": "Orb of Alchemy",
    "alt": "Orb of Alteration",
    "fusing": "Orb of Fusing",
    "jewellers": "Jeweller's Orb",
    "chrome": "Chromatic Orb",
    "chance": "Orb of Chance",
    "scour": "Orb of Scouring",
    "regal": "Regal Orb",
    "blessed": "Blessed Orb",
    "vaal": "Vaal Orb",
    "gcp": "Gemcutter's Prism",
    "annul": "Orb of Annulment",
    "chisel": "Cartographer's Chisel",
    "regret": "Orb of Regret",
    "transmute": "Orb of Transmutation",
    "aug": "Orb of Augmentation",
}


@dataclass(frozen=True)
class Sale:
    item_id: str
    sold_at: datetime
    amount: int
    currency: str  # the stash name, e.g. "Divine Orb"
    item_name: str

    @classmethod
    def from_api(cls, raw: Json) -> Sale | None:
        """One entry of /api/trade/history/<league>; None for what can't be matched (an
        unknown currency, a fractional price)."""
        try:
            price = raw.get("price") or {}
            currency = CURRENCY.get(str(price.get("currency", "")))
            amount = float(price["amount"])
            sold_at = datetime.fromisoformat(str(raw["time"]))
            if sold_at.tzinfo is not None:  # UTC from the site -> local, like snapshot times
                sold_at = sold_at.astimezone().replace(tzinfo=None)
            item_id = str(raw.get("item_id") or (raw.get("item") or {}).get("id") or "")
        except (KeyError, TypeError, ValueError):
            return None
        if currency is None or not item_id or amount <= 0 or amount != int(amount):
            return None
        item = raw.get("item") or {}
        name = clean(item.get("name", ""))
        type_line = clean(item.get("typeLine", "") or item.get("baseType", ""))
        return cls(item_id, sold_at, int(amount), currency,
                   f"{name} {type_line}".strip() or "an item")  # fmt: skip

    def to_json(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "sold_at": self.sold_at.isoformat(),
            "amount": self.amount,
            "currency": self.currency,
            "item_name": self.item_name,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Sale:
        return cls(d["item_id"], datetime.fromisoformat(d["sold_at"]), int(d["amount"]),
                   d["currency"], d["item_name"])  # fmt: skip


def parse_history(data: Any) -> list[Sale]:
    rows = data.get("result") if isinstance(data, dict) else None
    out = [s for r in rows or () if isinstance(r, dict) and (s := Sale.from_api(r))]
    return sorted(out, key=lambda s: s.sold_at)


@dataclass
class SaleBook:
    """What's been seen of the trade history: sales waiting for their payment to show up,
    and the ids of every sale already handled (kept in the database between runs)."""

    pending: list[Sale]
    seen: list[str]

    @classmethod
    def from_json(cls, d: Any) -> SaleBook:
        if not isinstance(d, dict):
            return cls([], [])
        pending = []
        for p in d.get("pending") or ():
            try:
                pending.append(Sale.from_json(p))
            except (KeyError, TypeError, ValueError):
                continue
        return cls(pending, [str(i) for i in d.get("seen") or ()])

    def to_json(self) -> dict[str, Any]:
        return {"pending": [s.to_json() for s in self.pending], "seen": self.seen[-KEEP_SEEN:]}

    def add(self, sales: Iterable[Sale], first_fetch: bool) -> None:
        """New sales from the history. On the very first fetch, the sales already there are
        only remembered: their payments were collected before poefire was watching."""
        known = set(self.seen)
        for s in sales:
            if s.item_id in known:
                continue
            known.add(s.item_id)
            self.seen.append(s.item_id)
            if not first_fetch:
                self.pending.append(s)
        self.pending.sort(key=lambda s: s.sold_at)

    def offsets(self, deltas: list[ItemDelta], until: datetime) -> list[ItemDelta]:
        """Minus lines for the pending sales whose payment is among `deltas` (the change up
        to `until`); those sales are done. Sales older than KEEP_PENDING are dropped."""
        gains = {d.name: d for d in deltas if d.quantity > 0 and d.frame_type == 5}
        left = {name: d.quantity for name, d in gains.items()}
        out: list[ItemDelta] = []
        keep: list[Sale] = []
        for s in self.pending:
            if s.sold_at > until:
                keep.append(s)
                continue
            if s.currency in gains and left[s.currency] >= s.amount:
                left[s.currency] -= s.amount
                cur = gains[s.currency]
                out.append(ItemDelta(f"{SOLD_PREFIX}{s.item_id}", f"Sold on trade · {s.item_name}",
                                     cur.frame_type, -s.amount, cur.sample))  # fmt: skip
                log.info("trade sale %s for %d %s taken out of the loot", s.item_name, s.amount,
                         s.currency)  # fmt: skip
            elif until - s.sold_at <= KEEP_PENDING:
                keep.append(s)
            else:
                log.info("trade sale %s: its payment never showed up in a watched tab",
                         s.item_name)  # fmt: skip
        self.pending = keep
        return out
