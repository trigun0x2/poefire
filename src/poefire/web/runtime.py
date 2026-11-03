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
from typing import Any

from poefire import __version__, wiring
from poefire.config import Config
from poefire.leagues import default_price_league, is_private
from poefire.loot.items import ItemEntry
from poefire.loot.recorder import LootRecorder, LootResult, Trigger
from poefire.pricing.ninja import NinjaClient
from poefire.pricing.service import PriceService
from poefire.pricing.valuation import value_holdings
from poefire.service import TrackerService
from poefire.store import Store
from poefire.tracker import Notice, TrackerState
from poefire.updater import (
    UpdateError,
    UpdateInfo,
    Updater,
    can_self_update,
    install_dir,
    launch_swap,
    staging_root,
)

log = logging.getLogger(__name__)

LEAGUE_KEY = "ui_league"
SESSION_GAP = timedelta(minutes=30)
FALLBACK_LEAGUES = ("Standard", "Hardcore")


UPDATE_CHECK_EVERY = 6 * 3600


@dataclass
class UpdateState:
    current: str = __version__
    checking: bool = False
    checked_at: datetime | None = None
    available: UpdateInfo | None = None
    error: str | None = None
    stage: str = "idle"  # idle | downloading | installing | failed
    progress: tuple[int, int] = (0, 0)
    can_install: bool = False
    cannot_install_reason: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "current": self.current,
            "checking": self.checking,
            "checked_at": self.checked_at.isoformat() if self.checked_at else None,
            "available": self.available.to_json() if self.available else None,
            "error": self.error,
            "stage": self.stage,
            "progress": list(self.progress),
            "can_install": self.can_install,
            "cannot_install_reason": self.cannot_install_reason,
        }


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
        updater: Updater | None = None,
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

        self.updater = updater or Updater(
            __version__, f"poefire/{__version__} (+{wiring.PROJECT_URL})"
        )
        ok, why = can_self_update()
        self.update = UpdateState(can_install=ok, cannot_install_reason=why)
        self.request_exit: Callable[[], None] | None = None  # set by the UI host (window/server)
        self._update_lock = threading.Lock()

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
        rec.valuer = self.value_snapshot
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

    def value_snapshot(self, items: list[ItemEntry]) -> tuple[str, float, float | None, int]:
        """Total value of a snapshot at current prices, for the Overview history."""
        book = self.prices.book(self.price_league)
        held = value_holdings(items, book)
        chaos = sum(h.chaos_total or 0 for h in held)
        unpriced = sum(1 for h in held if h.chaos_each is None)
        return self._league, chaos, book.chaos_per_divine, unpriced

    def snapshot_now(self) -> bool:
        if self.recorder is None:
            return False
        self.recorder.request(Trigger.MANUAL, None)
        return True

    # --- updates ---

    def check_update(self) -> UpdateState:
        if not self._update_lock.acquire(blocking=False):
            return self.update  # a check or install is already running
        try:
            self.update.checking = True
            self.update.error = None
            beta = self.cfg.update_channel == "beta"
            self.update.available = self.updater.check(include_prereleases=beta)
        except UpdateError as e:
            self.update.error = str(e)
        finally:
            self.update.checking = False
            self.update.checked_at = self.now()
            self._update_lock.release()
        return self.update

    def install_update(self) -> None:
        """Download + verify + stage the available update in the background, then hand over to
        the swap script and exit."""
        info = self.update.available
        if info is None or not self.update.can_install or self.update.stage == "downloading":
            return
        self.update.stage, self.update.error = "downloading", None

        def run() -> None:
            folder = install_dir()
            try:
                assert folder is not None
                staged = self.updater.stage(
                    info, staging_root(),
                    progress=lambda done, total: setattr(self.update, "progress", (done, total)),
                )  # fmt: skip
                self.update.stage = "installing"
                launch_swap(staged, folder, staging_root() / "update.log")
            except (UpdateError, OSError, AssertionError) as e:
                self.update.stage, self.update.error = "failed", str(e) or type(e).__name__
                return
            if self.request_exit is not None:
                self.request_exit()

        threading.Thread(target=run, name="update", daemon=True).start()

    def _update_loop(self) -> None:
        if self._stop.wait(10):  # let the app settle first
            return
        while True:
            if self.cfg.check_updates:
                self.check_update()
            if self._stop.wait(UPDATE_CHECK_EVERY):
                return

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
        self._spawn("updates", self._update_loop)

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
