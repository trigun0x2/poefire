"""Net change between two snapshots."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from poefire.loot.items import ItemEntry, Json


@dataclass(frozen=True)
class ItemDelta:
    key: str
    name: str
    frame_type: int
    quantity: int  # > 0 gained, < 0 lost/spent
    sample: Json  # one representative item, for display and pricing


def diff_items(before: Iterable[ItemEntry], after: Iterable[ItemEntry]) -> list[ItemDelta]:
    """Gains are positive, losses negative; unchanged keys are omitted.

    Where an item sits (inventory vs. which watched tab) does not matter: moving loot from the
    inventory into a watched tab nets to zero.
    """
    qty: dict[str, int] = {}
    samples: dict[str, ItemEntry] = {}

    def add(e: ItemEntry, n: int) -> None:
        qty[e.key] = qty.get(e.key, 0) + n
        if n > 0 or e.key not in samples:
            samples[e.key] = e  # prefer a sample from the gaining side

    before_ids: dict[str, ItemEntry] = {}
    after_ids: dict[str, ItemEntry] = {}
    for e in before:
        if e.stackable or e.item_id is None:
            add(e, -e.quantity)
        else:
            before_ids[e.item_id] = e
    for e in after:
        if e.stackable or e.item_id is None:
            add(e, e.quantity)
        else:
            after_ids[e.item_id] = e
    for item_id, e in before_ids.items():
        if item_id not in after_ids:
            add(e, -1)
    for item_id, e in after_ids.items():
        if item_id not in before_ids:
            add(e, 1)

    deltas = [
        ItemDelta(k, samples[k].name, samples[k].frame_type, n, samples[k].raw)
        for k, n in qty.items()
        if n != 0
    ]
    deltas.sort(key=lambda d: (-d.quantity, d.name))
    return deltas
