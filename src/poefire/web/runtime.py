"""Everything the UI shows, running in background threads: log tailing + run tracking,
loot snapshots, and price refreshes. The web layer only reads from this object."""

from __future__ import annotations

import copy
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from poefire import __version__, wiring
from poefire.config import Config
from poefire.loot.recorder import LootRecorder, LootResult, Trigger
from poefire.pricing.ninja import NinjaClient
from poefire.pricing.service import PriceService
from poefire.service import TrackerService
from poefire.store import Store
from poefire.tracker import TrackerState

log = logging.getLogger(__name__)

LEAGUE_KEY = "ui_league"
SESSION_GAP = timedelta(minutes=30)
FALLBACK_LEAGUES = ("Standard", "Hardcore")


@dataclass
class LootStatus:
    enabled: bool
    reason: str | None = None  # why it's off
    last: LootResult | None = None
    last_at: datetime | None = None


class AppRuntime:
    def __init__(
        self,
        cfg: Config,
        price_service: PriceService | None = None,
        recorder: LootRecorder | bool | None = True,
        log_path: Path | None = None,
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        """`recorder=True` builds one from the config when possible; None disables it."""
        self.cfg = cfg
        self.now = now
        self._store = Store(cfg.db_path)  # owned by the tail thread after start()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

        self.log_path = log_path or cfg.resolve_log_path()
        self.service: TrackerService | None = None
        if self.log_path is not None:
            self.service = TrackerService(
                self._store, self.log_path, cfg.zone_rules(), cfg.characters
            )

        self.prices = price_service or PriceService(
            NinjaClient(f"poefire/{__version__} (+{wiring.PROJECT_URL})"),
            lambda: Store(cfg.db_path),
        )

        self.loot = LootStatus(enabled=False)
        self.recorder: LootRecorder | None = None
        if recorder is True:
            try:
                self.recorder = wiring.recorder(cfg, wiring.account_api(cfg))
            except wiring.ConfigError as e:
                self.loot.reason = str(e)
        elif isinstance(recorder, LootRecorder):
            self.recorder = recorder
        if self.recorder is not None:
            self.loot.enabled = True
            self.recorder.handlers.append(self._on_loot)
            if self.service is not None:
                self.service.handlers.append(self.recorder.on_notice)
        elif self.loot.reason is None:
            self.loot.reason = "loot tracking disabled"

        stored = self._store.get_json(LEAGUE_KEY)
        self._league: str = stored or cfg.league or FALLBACK_LEAGUES[0]
        if self.recorder is not None:
            self.recorder.snapshotter.set_league(self._league)

    # --- league ---

    @property
    def league(self) -> str:
        return self._league

    def set_league(self, league: str) -> None:
        with self._lock:
            self._league = league
        with Store(self.cfg.db_path) as store, store.transaction():  # own connection: thread
            store.set_json(LEAGUE_KEY, league)
        if self.recorder is not None:
            self.recorder.snapshotter.set_league(league)
        self.refresh_prices()

    def leagues(self) -> list[str]:
        known = self.prices.client.leagues()
        out = [self._league, *(self.cfg.league or "",), *known, *FALLBACK_LEAGUES]
        return list(dict.fromkeys(x for x in out if x))

    # --- live state ---

    def state(self) -> TrackerState:
        if self.service is None:
            return TrackerState()
        with self._lock:
            return copy.deepcopy(self.service.tracker.state)

    def _on_loot(self, result: LootResult) -> None:
        self.loot.last = result
        self.loot.last_at = self.now()

    def snapshot_now(self) -> bool:
        if self.recorder is None:
            return False
        self.recorder.request(Trigger.MANUAL, None)
        return True

    def refresh_prices(self, force: bool = False) -> None:
        threading.Thread(
            target=self.prices.refresh, args=(self._league, force), daemon=True
        ).start()

    # --- lifecycle ---

    def start(self) -> None:
        if self.service is not None:
            self.service.start()
            self._spawn("log-tail", self._tail_loop)
        if self.recorder is not None:
            self.recorder.start()
        self._spawn("prices", lambda: self.prices.run_forever(lambda: self._league, self._stop))

    def _spawn(self, name: str, target: Callable[[], None]) -> None:
        t = threading.Thread(target=target, name=name, daemon=True)
        self._threads.append(t)
        t.start()

    def _tail_loop(self) -> None:
        assert self.service is not None
        while not self._stop.is_set():
            try:
                with self._lock:
                    notices = self.service.process_available()
                self.service.dispatch(notices)
            except Exception:
                log.exception("log tail failed")
            self._stop.wait(self.cfg.poll_interval)

    def stop(self) -> None:
        self._stop.set()
        if self.recorder is not None:
            self.recorder.stop()
        for t in self._threads:
            t.join(timeout=5)
        self._store.close()
