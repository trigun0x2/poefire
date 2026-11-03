"""Values loot deltas with a PriceBook."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from poefire.loot.diff import ItemDelta
from poefire.loot.items import ItemEntry
from poefire.pricing.book import PriceBook


@dataclass(frozen=True)
class ValuedItem:
    key: str
    name: str
    quantity: int
    chaos_each: float | None
    icon: str | None
    source: str | None

    @property
    def chaos_total(self) -> float | None:
        return None if self.chaos_each is None else self.chaos_each * self.quantity


@dataclass
class Valuation:
    chaos: float = 0.0  # net value of priced items (gains minus losses)
    items: list[ValuedItem] = field(default_factory=list)
    unpriced: int = 0  # item kinds poe.ninja has no price for

    def divine(self, chaos_per_divine: float | None) -> float | None:
        return self.chaos / chaos_per_divine if chaos_per_divine else None


def value_deltas(deltas: Iterable[ItemDelta], book: PriceBook) -> Valuation:
    v = Valuation()
    for d in deltas:
        q = book.price(d.sample)
        item = ValuedItem(
            d.key, d.name, d.quantity, q.chaos_each if q else None, d.sample.get("icon"),
            q.source if q else None,
        )  # fmt: skip
        v.items.append(item)
        if item.chaos_total is None:
            v.unpriced += 1
        else:
            v.chaos += item.chaos_total
    v.items.sort(key=lambda i: (-(i.chaos_total or 0), -i.quantity, i.name))
    return v


@dataclass(frozen=True)
class HeldItem:
    """One kind of item in a snapshot, all its stacks/copies added up."""

    key: str
    name: str
    quantity: int
    chaos_each: float | None
    icon: str | None
    sources: tuple[str, ...]  # tab ids / "inventory" it sits in

    @property
    def chaos_total(self) -> float | None:
        return None if self.chaos_each is None else self.chaos_each * self.quantity


def value_holdings(entries: Iterable[ItemEntry], book: PriceBook) -> list[HeldItem]:
    """Aggregate snapshot entries by item kind and price them; most valuable first."""
    qty: dict[str, int] = {}
    sample: dict[str, ItemEntry] = {}
    sources: dict[str, set[str]] = {}
    for e in entries:
        qty[e.key] = qty.get(e.key, 0) + e.quantity
        sample.setdefault(e.key, e)
        sources.setdefault(e.key, set()).add(e.source)
    out = []
    for key, n in qty.items():
        e = sample[key]
        q = book.price(e.raw)
        out.append(HeldItem(key, e.name, n, q.chaos_each if q else None, e.raw.get("icon"),
                            tuple(sorted(sources[key]))))  # fmt: skip
    out.sort(key=lambda h: (-(h.chaos_total or 0), h.name))
    return out
