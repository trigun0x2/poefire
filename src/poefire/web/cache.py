"""Values computed at the current prices, kept until the prices change.

Finished maps, past sessions and stash snapshots don't change, but valuing them means decoding
and pricing thousands of items. The UI asks for them every few seconds, so the results are
kept, per price book: a new book (prices refreshed, or another league) starts over.
"""

from __future__ import annotations

import threading
from collections.abc import Hashable
from datetime import timedelta
from typing import Any

from poefire.pricing.book import PriceBook

# A map that just ended can still get the loot of its last hideout return (the snapshot is
# taken a few seconds later), so it's only cached once it has been over for a while.
SETTLED_AFTER = timedelta(minutes=2)


class BookCache:
    """Thread-safe {key: value} for one price book; cleared when the book changes."""

    def __init__(self, max_entries: int = 50_000) -> None:
        self._lock = threading.Lock()
        self._version: int | None = None
        self._data: dict[Hashable, Any] = {}
        self.max_entries = max_entries

    def _sync(self, book: PriceBook) -> None:
        if self._version != book.version or len(self._data) > self.max_entries:
            self._data.clear()
            self._version = book.version

    def get(self, book: PriceBook, key: Hashable) -> Any:
        with self._lock:
            self._sync(book)
            return self._data.get(key)

    def put(self, book: PriceBook, key: Hashable, value: Any) -> None:
        with self._lock:
            self._sync(book)
            self._data[key] = value

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
