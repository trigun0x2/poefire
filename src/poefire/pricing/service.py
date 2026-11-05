"""Keeps a PriceBook per league: loaded from the SQLite cache, refreshed from poe.ninja."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from poefire.pricing.book import PriceBook
from poefire.pricing.ninja import (
    MIN_REFRESH_SECONDS,
    PRICE_SOURCES,
    NinjaClient,
    NinjaError,
    PriceLine,
)
from poefire.store import Store

log = logging.getLogger(__name__)

type CachedTable = tuple[str, str, float, list[PriceLine]]
RETRY_AFTER_FAILURE_SECONDS = 5 * 60
MAX_FAILURES_PER_REFRESH = 3


@dataclass
class PriceStatus:
    league: str
    entries: int = 0
    fetched_at: float | None = None  # oldest table in the book (unix time)
    refreshing: bool = False
    last_error: str | None = None
    chaos_per_divine: float | None = None
    failed_types: list[str] = field(default_factory=list)
    last_attempt: float | None = None


class PriceService:
    def __init__(
        self,
        client: NinjaClient,
        store_factory: Callable[[], Store],
        refresh_seconds: float = 30 * 60,
        sources: tuple[tuple[str, str], ...] = PRICE_SOURCES,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.client = client
        self.store_factory = store_factory
        self.refresh_seconds = max(refresh_seconds, MIN_REFRESH_SECONDS)
        self.sources = sources
        self.clock = clock
        self._books: dict[str, PriceBook] = {}
        self._status: dict[str, PriceStatus] = {}
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()

    def book(self, league: str) -> PriceBook:
        """Current book for the league (from cache if not yet refreshed; may be empty)."""
        with self._lock:
            if league in self._books:
                return self._books[league]
        self._load_cached(league)
        with self._lock:
            return self._books[league]

    def status(self, league: str) -> PriceStatus:
        self.book(league)
        with self._lock:
            return self._status[league]

    def _load_cached(self, league: str) -> None:
        with self.store_factory() as store:
            cached = store.load_prices(league)
        self._install(league, cached)

    def _install(self, league: str, tables: list[CachedTable]) -> None:
        book = PriceBook((src, t, lines) for src, t, _, lines in tables)
        with self._lock:
            st = self._status.setdefault(league, PriceStatus(league))
            st.entries = len(book)
            st.fetched_at = min((f for _, _, f, _ in tables), default=None)
            st.chaos_per_divine = book.chaos_per_divine
            self._books[league] = book

    def is_stale(self, league: str) -> bool:
        st = self.status(league)
        now = self.clock()
        if (
            st.failed_types
            and st.last_attempt
            and now - st.last_attempt < RETRY_AFTER_FAILURE_SECONDS
        ):
            return False  # poe.ninja was failing a moment ago; don't hammer it
        return st.fetched_at is None or now - st.fetched_at >= self.refresh_seconds

    def refresh(self, league: str, force: bool = False) -> PriceStatus:
        """Fetch every table older than the refresh interval (all of them if `force`).
        A failed table keeps its previous cached lines."""
        with self._refresh_lock:
            with self.store_factory() as store:
                cached = {(s, t): f for s, t, f, _ in store.load_prices(league)}
                st = self.status(league)
                with self._lock:
                    st.refreshing = True
                    st.last_attempt = self.clock()
                    st.failed_types = []
                    st.last_error = None
                try:
                    for source, type_ in self.sources:
                        fetched = cached.get((source, type_))
                        if not force and fetched and self.clock() - fetched < self.refresh_seconds:
                            continue
                        try:
                            lines = self.client.fetch(source, league, type_)
                        except NinjaError as e:
                            with self._lock:
                                st.failed_types.append(f"{source}:{type_}")
                                st.last_error = str(e)
                            if len(st.failed_types) >= MAX_FAILURES_PER_REFRESH:
                                # poe.ninja is down or unreachable: stop, retry later
                                log.warning("poe.ninja unreachable, retrying later: %s", e)
                                break
                            continue
                        with store.transaction():
                            store.save_prices(league, source, type_, self.clock(), lines)
                    tables = store.load_prices(league)
                finally:
                    with self._lock:
                        st.refreshing = False
            self._install(league, tables)
            return self.status(league)

    def run_forever(self, league_getter: Callable[[], str], stop: threading.Event) -> None:
        while not stop.is_set():
            league = league_getter()
            try:
                if self.is_stale(league):
                    self.refresh(league)
            except Exception:  # never let one bad refresh end the refresh loop
                log.exception("price refresh for %s failed", league)
            stop.wait(60)
