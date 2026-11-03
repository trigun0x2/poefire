"""Everything the UI shows, running in background threads: log tailing + run tracking,
loot snapshots, and price refreshes. The web layer only reads from this object, except for
`reconfigure()`, which the settings page uses to apply a new config without a restart."""

from __future__ import annotations

import copy
import logging
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from poefire import __version__, wiring
from poefire.config import Config
from poefire.leagues import default_price_league, is_private
from poefire.loot.recorder import LootRecorder, LootResult, Trigger
from poefire.pricing.ninja import NinjaClient
from poefire.pricing.service import PriceService
from poefire.service import TrackerService
from poefire.store import Store
from poefire.tracker import Notice, TrackerState

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
        config_path: Path | None = None,
    ) -> None:
        """`recorder=True` builds one from the config when possible (and again on
        reconfigure); a LootRecorder instance is used as is; None disables loot tracking.
        `log_path` overrides the config's (tests, previews)."""
        self.cfg = cfg
        self.config_path = config_path
        self.now = now
        self._store = Store(cfg.db_path)  # used by the tail thread, under _lock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._started = False
        self._log_override = log_path
        self._auto_recorder = recorder is True

        self.log_path: Path | None = None
        self.service: TrackerService | None = None
        self._build_service()

        self.prices = price_service or PriceService(
            NinjaClient(f"poefire/{__version__} (+{wiring.PROJECT_URL})"),
            lambda: Store(cfg.db_path),
        )

        self._ninja_leagues: list[str] = []
        self._ninja_leagues_at = float("-inf")
        self._account_leagues: list[str] = []
        stored = self._store.get_json(LEAGUE_KEY)
        self._league: str = stored or cfg.league or FALLBACK_LEAGUES[0]

        self.loot = LootStatus(enabled=False)
        self.recorder: LootRecorder | None = None
        if isinstance(recorder, LootRecorder):
            self._install_recorder(recorder)
        elif recorder is True:
            self._build_recorder()
        else:
            self.loot.reason = "loot tracking disabled"

    # --- building blocks (rebuilt by reconfigure) ---

    def _build_service(self) -> None:
        self.log_path = self._log_override or self.cfg.resolve_log_path()
        self.service = None
        if self.log_path is not None:
            self.service = TrackerService(
                self._store, self.log_path, self.cfg.zone_rules(), self.cfg.characters
            )
            self.service.handlers.append(self._dispatch)
            if self._started:
                self.service.start()

    def _build_recorder(self) -> None:
        self.loot = LootStatus(enabled=False)
        try:
            rec = wiring.recorder(self.cfg, wiring.account_api(self.cfg))
        except wiring.ConfigError as e:
            self.loot.reason = str(e)
            return
        self._install_recorder(rec)

    def _install_recorder(self, rec: LootRecorder) -> None:
        self.recorder = rec
        self.loot = LootStatus(enabled=True)
        rec.handlers.append(self._on_loot)
        rec.snapshotter.set_league(self._league)
        if self._started:
            rec.start()

    def _dispatch(self, n: Notice) -> None:
        rec = self.recorder
        if rec is not None:
            rec.on_notice(n)

    def reconfigure(self, cfg: Config) -> None:
        """Apply a new config: re-open the log if its path or tracking rules changed, and
        rebuild loot tracking (account, session cookie, tabs...)."""
        old = self.cfg
        self.cfg = cfg
        log_changed = (
            old.resolve_log_path() != cfg.resolve_log_path()
            or old.characters != cfg.characters
            or old.zone_rules() != cfg.zone_rules()
        )
        if log_changed:
            with self._lock:
                self._build_service()
        if self._auto_recorder:
            previous = self.recorder
            self.recorder = None
            if previous is not None:
                previous.stop()
            self._build_recorder()
        if cfg.league and cfg.league != self._league:
            self.set_league(cfg.league)
        elif cfg.price_league != old.price_league:
            self.refresh_prices()

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

    def ninja_leagues(self) -> list[str]:
        """League names poe.ninja has prices for (cached for an hour; 5 min after a failure)."""
        now = time.monotonic()
        ttl = 3600 if self._ninja_leagues else 300
        if now - self._ninja_leagues_at >= ttl:
            self._ninja_leagues = self.prices.client.leagues()
            self._ninja_leagues_at = now
        return self._ninja_leagues

    def note_account_leagues(self, leagues: Iterable[str]) -> None:
        """Leagues the account's characters are in (private leagues show up here)."""
        self._account_leagues = list(dict.fromkeys(x for x in leagues if x))

    def leagues(self) -> list[str]:
        out = [self._league, self.cfg.league or "", *self._account_leagues,
               *self.ninja_leagues(), *FALLBACK_LEAGUES]  # fmt: skip
        return list(dict.fromkeys(x for x in out if x))

    @property
    def is_private(self) -> bool:
        return is_private(self._league)

    @property
    def price_league(self) -> str:
        """The league prices come from: the configured one, else the played league, or for a
        private league the challenge league it's most likely based on."""
        if self.cfg.price_league:
            return self.cfg.price_league
        if not is_private(self._league):
            return self._league
        return default_price_league(self._league, self.ninja_leagues())

    # --- live state ---

    def state(self) -> TrackerState:
        with self._lock:
            if self.service is None:
                return TrackerState()
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
            target=self.prices.refresh, args=(self.price_league, force), daemon=True
        ).start()

    # --- lifecycle ---

    def start(self) -> None:
        self._started = True
        if self.service is not None:
            self.service.start()
        self._spawn("log-tail", self._tail_loop)
        if self.recorder is not None:
            self.recorder.start()
        self._spawn(
            "prices", lambda: self.prices.run_forever(lambda: self.price_league, self._stop)
        )

    def _spawn(self, name: str, target: Callable[[], None]) -> None:
        t = threading.Thread(target=target, name=name, daemon=True)
        self._threads.append(t)
        t.start()

    def _tail_loop(self) -> None:
        while not self._stop.is_set():
            try:
                with self._lock:
                    service = self.service
                    notices = service.process_available() if service is not None else []
                if service is not None:
                    service.dispatch(notices)
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
