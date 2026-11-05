"""Everything the UI shows, running in background threads: log tailing + run tracking,
loot snapshots, and price refreshes. The web layer only reads from this object, except for
`reconfigure()`, which the settings page uses to apply a new config without a restart."""

from __future__ import annotations

import contextlib
import copy
import logging
import os
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from poefire import __version__, campaign, wiring
from poefire.api.session import SessionStore
from poefire.builds import BuildWatcher
from poefire.campaign import CampaignTracker
from poefire.config import Config
from poefire.hotkey import GlobalHotkey, HotkeyError, beep
from poefire.leagues import default_price_league, is_private
from poefire.loot.items import ItemEntry
from poefire.loot.recorder import LootRecorder, LootResult, Trigger
from poefire.pricing.ninja import NinjaClient
from poefire.pricing.service import PriceService
from poefire.pricing.valuation import value_holdings
from poefire.service import TrackerService
from poefire.store import Store
from poefire.tracker import Notice, RunStatus, TrackerState
from poefire.updater import (
    UpdateError,
    UpdateInfo,
    Updater,
    can_self_update,
    cleanup_staging,
    install_dir,
    staging_root,
    start_install,
)
from poefire.web import sessions
from poefire.web.cache import BookCache

log = logging.getLogger(__name__)

LEAGUE_KEY = "ui_league"
SESSION_GAP = timedelta(minutes=30)
FALLBACK_LEAGUES = ("Standard", "Hardcore")


UPDATE_CHECK_EVERY = 6 * 3600
EVENT_RETENTION = timedelta(days=90)
EXIT_GRACE = 15  # seconds the app gets to close before an update force-exits it


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

        self.campaign = CampaignTracker(self._store, now)  # under _lock, like the tail
        self.builds = BuildWatcher(self._build_api, lambda: Store(cfg.db_path), now)
        self.builds.enabled = cfg.build_snapshots
        self.values = BookCache()  # loot values of settled maps, sessions, stash (per prices)
        self._session_health: tuple[Any, dict[str, Any]] | None = None
        self.hotkey: GlobalHotkey | None = None  # registered by start()
        self.hotkey_error: str | None = None
        self.hotkey_last: tuple[str, datetime] | None = None
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
        sessions.ensure_session(self._store, SESSION_GAP)
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
            self.service.line_observers.append(self.campaign.handle)
            self.service.line_observers.append(self.builds.handle)
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

    def _build_api(self) -> Any:
        """The loot tracker's website connection, shared so both respect one rate limit."""
        rec = self.recorder
        api = getattr(rec.snapshotter, "api", None) if rec is not None else None
        return api if api is not None and hasattr(api, "get_build") else None

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
        self.builds.enabled = cfg.build_snapshots
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
        if self._started and cfg.campaign_hotkey != old.campaign_hotkey:
            self._build_hotkey()
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

    # --- farming sessions ---

    def new_session(self) -> int:
        """Start a fresh session: the Session page only counts maps from now on. A map you're
        inside right now moves to the new session too."""
        with self._lock:
            start = self.now()
            live = self.service.tracker.run if self.service is not None else None
            if live is not None and live.status is RunStatus.ACTIVE:
                start = min(start, live.started_at)
            return self._store.add_session(start)

    def delete_session(self, session_id: int) -> None:
        """Delete a past session and its maps. Raises LookupError / ValueError."""
        with self._lock:
            span = sessions.find(self._store, session_id)
            if span is None:
                raise LookupError(session_id)
            if span.current:
                raise ValueError("the current session can't be deleted; start a new one first")
            runs = sessions.runs_of(self._store, span)
            if any(r.status is not RunStatus.FINISHED for r in runs):
                raise ValueError("this session has a map in progress")
            with self._store.transaction():
                self._store.delete_runs([r.id for r in runs])
                if span.id:
                    self._store.delete_session(span.id)
            self.values.clear()  # cached values of the deleted maps and session

    # --- campaign runs (shares the tail thread's connection and lock) ---

    def _personal_best(
        self, runs: list[campaign.CampaignRun], run: campaign.CampaignRun | None
    ) -> campaign.CampaignRun | None:
        """The best finished run of `run`'s base class, with its splits (the list is read
        without them). None until the run's class is known."""
        base = campaign.base_class(run.char_class) if run is not None else None
        pb = campaign.personal_best(runs, base) if base else None
        return self._store.get_campaign_run(pb.id) if pb is not None and pb.id else pb

    def campaign_overview(self, limit: int = 200) -> dict[str, Any]:
        now = self.now()
        with self._lock:
            runs = self._store.list_campaign_runs(limit, splits=False)
            active = self.campaign.active
            pb = self._personal_best(runs, active) if active else None
        return {
            "active": campaign.run_detail(active, now, pb) if active else None,
            "pb_ids": campaign.best_per_class(runs),  # base class -> its best run
            "runs": [campaign.run_summary(r, now) for r in runs],
        }

    def campaign_run(self, run_id: int) -> dict[str, Any] | None:
        with self._lock:
            run = self._store.get_campaign_run(run_id)
            pb = self._personal_best(self._store.list_campaign_runs(1000, splits=False), run)
        if run is None:
            return None
        return campaign.run_detail(run, self.now(), pb)

    def campaign_start(self, character: str | None = None) -> None:
        with self._lock:
            self.campaign.start(character)

    def campaign_toggle(self, from_hotkey: bool = False) -> str:
        """Start a run if none is going, else pause / resume it. Returns what happened."""
        with self._lock:
            action = self.campaign.toggle()
        self.hotkey_last = (action, self.now())
        if from_hotkey and self.cfg.campaign_hotkey_sound:
            beep(high=action != "paused")
        return action

    def campaign_pause(self, pause: bool) -> bool:
        with self._lock:
            return self.campaign.pause() if pause else self.campaign.resume()

    def _build_hotkey(self) -> None:
        if self.hotkey is not None:
            self.hotkey.stop()
            self.hotkey = None
        self.hotkey_error = None
        spec = self.cfg.campaign_hotkey.strip()
        if not spec:
            return
        try:
            hk = GlobalHotkey(spec, lambda: self.campaign_toggle(from_hotkey=True))
        except HotkeyError as e:
            self.hotkey_error = str(e)
            return
        if hk.start():
            self.hotkey = hk
        else:
            self.hotkey_error = hk.error

    def hotkey_json(self) -> dict[str, Any]:
        last = self.hotkey_last
        return {
            "key": self.cfg.campaign_hotkey.strip() or None,
            "active": self.hotkey is not None and self.hotkey.active,
            "error": self.hotkey_error,
            "last_action": last[0] if last else None,
            "last_at": last[1].isoformat() if last else None,
        }

    def campaign_end(self, finished: bool) -> bool:
        with self._lock:
            run = self.campaign.finish() if finished else self.campaign.stop()
        return run is not None

    def campaign_notes(self, run_id: int, notes: str) -> bool:
        with self._lock:
            active = self.campaign.active
            if active is not None and active.id == run_id:
                active.notes = notes  # the live copy is saved again on the next split
            return self._store.set_campaign_notes(run_id, notes)

    def campaign_delete(self, run_id: int) -> bool:
        with self._lock:
            active = self.campaign.active
            if active is not None and active.id == run_id:
                self.campaign.active = None
            return self._store.delete_campaign_run(run_id)

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
                start_install(staged, folder, staging_root() / "update.log")
            except (UpdateError, OSError, AssertionError) as e:
                self.update.stage, self.update.error = "failed", str(e) or type(e).__name__
                return
            if self.request_exit is not None:
                self.request_exit()
            # The installer waits for this process to end; don't let a stuck thread or
            # window keep it alive.
            threading.Timer(EXIT_GRACE, self.hard_exit).start()

        threading.Thread(target=run, name="update", daemon=True).start()

    def hard_exit(self) -> None:
        log.warning("still running %ss after the update asked to exit; exiting now", EXIT_GRACE)
        os._exit(0)

    def session_health(self) -> dict[str, Any] | None:
        """The POESESSID's health for the status bar; re-read only when its file changes."""
        if self.cfg.auth != "session":
            return None
        path = self.cfg.session_path
        try:
            st = path.stat()
            stamp: Any = (str(path), st.st_mtime_ns, st.st_size, os.environ.get("POESESSID"))
        except OSError:
            stamp = (str(path), None, None, os.environ.get("POESESSID"))
        cached = self._session_health
        if cached is None or cached[0] != stamp:
            cached = (stamp, SessionStore(path).health())
            self._session_health = cached
        return cached[1]

    def _maintenance_loop(self) -> None:
        """Once a day: drop log events older than EVENT_RETENTION (nothing reads them)."""
        if self._stop.wait(30):
            return
        while True:
            try:
                with Store(self.cfg.db_path) as store:
                    removed = store.prune_events(self.now() - EVENT_RETENTION)
                if removed:
                    log.info("removed %d log events older than %s", removed, EVENT_RETENTION)
            except Exception:
                log.exception("event clean-up failed")
            if self._stop.wait(24 * 3600):
                return

    def _update_loop(self) -> None:
        if self.update.can_install:  # versions staged by earlier updates
            with contextlib.suppress(OSError):
                cleanup_staging(staging_root(), keep=install_dir())
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
        self._spawn("maintenance", self._maintenance_loop)
        self._build_hotkey()

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
        self.builds.stop()
        if self.hotkey is not None:
            self.hotkey.stop()
        if self.recorder is not None:
            self.recorder.stop()
        for t in self._threads:
            t.join(timeout=5)
        self._store.close()
