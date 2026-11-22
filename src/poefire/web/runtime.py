"""Everything the UI shows, running in background threads: log tailing + run tracking,
loot snapshots, and price refreshes. The web layer only reads from this object, except for
`reconfigure()`, which the settings page uses to apply a new config without a restart."""

from __future__ import annotations

import contextlib
import copy
import logging
import os
import platform
import sys
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from poefire import __version__, campaign, gamepath, layouts, logs, patches, wiring
from poefire.api.ratelimit import RateLimiter
from poefire.api.session import SessionStore
from poefire.builds import BuildWatcher
from poefire.campaign import CampaignTracker
from poefire.config import Config
from poefire.events import LogEvent
from poefire.gameinput import (
    GameHotkeys,
    GameInput,
    active_regex,
    chat_commands,
    search_strings,
)
from poefire.hotkey import GlobalHotkey, HotkeyError, beep, normalize
from poefire.leagues import default_price_league, is_private, latest_league
from poefire.loot import sales
from poefire.loot.items import ItemEntry
from poefire.loot.recorder import INTERVAL, LootRecorder, LootResult, Trigger
from poefire.loot.snapshot import FULL, OTHER_LEAGUE, Snapshotter
from poefire.pricing.ninja import NinjaClient
from poefire.pricing.service import PriceService
from poefire.pricing.valuation import value_holdings
from poefire.service import TrackerService
from poefire.store import Store
from poefire.tracker import Notice, RunStatus, TrackerState
from poefire.updater import (
    ReleaseGoneError,
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
GAME_LOG_KEY = "game_log_path"  # where the running game's Client.txt was last found
RATE_LIMITS_KEY = "rate_limits"  # the website's rate-limit state (RateLimiter.to_json)
GAME_LOG_CHECK = 15.0  # seconds
# seconds between checks of the league your characters play in (the watched tabs are
# checked when they're read, and when the league or the tabs change)
LEAGUE_CHECK = 60 * 60.0
LEAGUE_RETRY = 5 * 60.0  # after a failed check (offline at boot, rate limited...)
# While you map, the league is checked again as a map starts once this long has passed
# (one character list request: its own rate limit, not the loot's), and at once when an
# inventory read finds the character in another league (not more often than LEAGUE_MIN_GAP).
LEAGUE_CHECK_MAPPING = 10 * 60.0
LEAGUE_MIN_GAP = 2 * 60.0
# Settings that loot tracking is built from: a change rebuilds it (others don't, so saving
# e.g. a hotkey doesn't throw away what it knows, like the tab list)
LOOT_FIELDS = ("auth", "account_name", "realm", "league", "stash_tabs", "league_tabs", "character",
               "snapshot_delay", "loot_mode", "trade_sales", "session_path", "token_path",
               "oauth_client_id", "oauth_redirect_uri", "contact", "db_path")  # fmt: skip
DISMISSED_LEAGUES_KEY = "league_hint_dismissed"

LEAGUE_KEY = "ui_league"
SESSION_GAP = timedelta(minutes=30)
FALLBACK_LEAGUES = ("Standard", "Hardcore")


UPDATE_CHECK_EVERY = 6 * 3600
EVENT_RETENTION = timedelta(days=90)
HOTKEY_FIELDS = {"campaign": "campaign_hotkey", "overlay": "overlay_toggle_hotkey",
                 "overlay_move": "overlay_move_hotkey"}  # fmt: skip
HOTKEY_LABELS = {"campaign": "the campaign timer", "overlay": "the overlay toggle",
                 "overlay_move": "moving the overlay"}  # fmt: skip
REGEX_ENTRY = "regex"  # the regex hotkey's key in game_hotkey_errors
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
        # One rate limiter for every website request, kept when loot tracking is rebuilt and
        # saved between runs: a restart mustn't forget a penalty in force or recent hits.
        self.web_limiter = RateLimiter()
        self.web_limiter.restore(self._store.get_json(RATE_LIMITS_KEY))
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._started = False
        self._log_override = log_path
        # the Client.txt of the game last seen running (when none is configured), remembered
        self.detected_log: Path | None = None
        # set by the league check: your newest character plays in another league / none of
        # the watched stash tabs exist in this league
        self.league_hint: dict[str, Any] | None = None
        # set when the league was switched by itself (league_autoswitch), for a banner
        self.league_switched: dict[str, Any] | None = None
        self._league_checked_at = float("-inf")  # monotonic: the last league check that ran
        self.tabs_hint: dict[str, Any] | None = None
        self._league_check_now = threading.Event()
        self._league_check_tabs = False  # the next league check also checks the tabs
        self._auto_recorder = recorder is True

        self.patches = patches.PatchBook(
            lambda: self._store.get_json(patches.CACHE_KEY),
            lambda v: self._store.set_json(patches.CACHE_KEY, v),
            lambda: patches.fetch(f"poefire/{__version__} (+{wiring.PROJECT_URL})"),
            now,
        )  # fmt: skip
        # under _lock, like the tail
        self.campaign = CampaignTracker(self._store, now, self._patch_for_new_run)
        self.builds = BuildWatcher(self._build_api, lambda: Store(cfg.db_path), now)
        self.builds.enabled = cfg.build_snapshots
        self.builds.stale_hours = cfg.build_stale_hours
        self.values = BookCache()  # loot values of settled maps, sessions, stash (per prices)
        self._session_health: tuple[Any, dict[str, Any]] | None = None
        self.hotkeys: dict[str, GlobalHotkey] = {}  # registered by start()
        self.hotkey_errors: dict[str, str] = {}
        self.hotkey_last: tuple[str, datetime] | None = None
        # in-game shortcuts: chat commands and search strings (registered while in the game)
        self.game_input = GameInput()
        self.game_hotkeys: GameHotkeys | None = None
        self.game_hotkey_errors: dict[str, str] = {}  # config entry -> why it's off
        self._bests_cache: dict[tuple[str, tuple[int, ...]], campaign.Bests] = {}
        self.layouts = layouts.LayoutGuide(
            self._kv_get, self._kv_set, f"poefire/{__version__} (+{wiring.PROJECT_URL})"
        )
        self.overlay: Any = None  # the overlay window's controller, when the native window runs
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
        # nobody picked a league yet: the newest one on poe.ninja, once it's been asked
        self._league_chosen = bool(stored or cfg.league)

        self.updater = updater or Updater(
            __version__, f"poefire/{__version__} (+{wiring.PROJECT_URL})"
        )
        ok, why = can_self_update()
        self.update = UpdateState(can_install=ok, cannot_install_reason=why)
        self.request_exit: Callable[[], None] | None = None  # set by the UI host (window/server)
        self.request_show: Callable[[], None] | None = None  # bring the app up (a 2nd launch)
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
        if self.detected_log is None and self._log_override is None:
            saved = self._store.get_json(GAME_LOG_KEY)
            self.detected_log = Path(saved) if saved and Path(saved).is_file() else None
        self.log_path = (
            self._log_override
            or self.cfg.log_path
            or self.detected_log
            or self.cfg.resolve_log_path()
        )
        self.service = None
        if self.log_path is None:
            log.warning("no Client.txt: none configured and none at the usual install "
                        "locations; run and campaign tracking are off until it's set")  # fmt: skip
        else:
            log.info("Client.txt: %s (%s)", self.log_path,
                     "found" if self.log_path.exists() else "doesn't exist yet")  # fmt: skip
        if self.log_path is not None:
            self.service = TrackerService(
                self._store, self.log_path, self.cfg.zone_rules(), self.cfg.characters
            )
            self.service.handlers.append(self._dispatch)
            self.service.line_observers.append(self.campaign.handle)
            self.service.line_observers.append(self.builds.handle)
            self.service.line_observers.append(self._observe_line)
            if self._started:
                self.service.start()

    def _build_recorder(self) -> None:
        self.loot = LootStatus(enabled=False)
        try:
            rec = wiring.recorder(self.cfg, wiring.account_api(self.cfg, self.web_limiter))
        except wiring.ConfigError as e:
            self.loot.reason = str(e)
            return
        self._install_recorder(rec)

    def save_rate_limits(self) -> None:
        try:
            self._kv_set(RATE_LIMITS_KEY, self.web_limiter.to_json())
        except Exception:  # never worth failing a snapshot over
            log.exception("couldn't save the rate-limit state")

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
        rec.interval = INTERVAL if self.interval_mode else None
        rec.snapshotter.set_league(self._league, self.cfg.tabs_for(self._league))
        api = getattr(rec.snapshotter, "api", None)
        # Only the interval mode counts what lands in the tabs: per map, a sale's payment
        # never comes into your inventory, so it's never counted as loot anyway.
        if (
            self.interval_mode
            and self.cfg.trade_sales
            and api is not None
            and hasattr(api, "trade_history")
        ):
            snapshotter = rec.snapshotter
            rec.sales = lambda: sales.parse_history(api.trade_history(snapshotter.league))
        if self._started:
            rec.start()

    def _dispatch(self, n: Notice) -> None:
        rec = self.recorder
        if rec is not None:
            rec.on_notice(n)

    def _observe_line(self, event: LogEvent | None, line: str) -> None:
        rec = self.recorder
        if rec is not None:
            rec.on_event(event, line)

    def reconfigure(self, cfg: Config, rebuild_loot: bool = False) -> None:
        """Apply a new config: re-open the log if its path or tracking rules changed, and
        rebuild loot tracking when its settings changed (or `rebuild_loot`: a new cookie)."""
        old = self.cfg
        self.cfg = cfg
        self.builds.enabled = cfg.build_snapshots
        self.builds.stale_hours = cfg.build_stale_hours
        loot_changed = rebuild_loot or any(getattr(cfg, f) != getattr(old, f) for f in LOOT_FIELDS)
        if loot_changed:  # the account, cookie, league or tabs may have changed: check them
            self._league_check_tabs = True
            self._league_check_now.set()
        if cfg.debug_logging != old.debug_logging:
            logs.set_verbose(cfg.debug_logging)
            log.info("verbose logging %s", "on" if cfg.debug_logging else "off")
        log_changed = (
            old.resolve_log_path() != cfg.resolve_log_path()
            or old.characters != cfg.characters
            or old.zone_rules() != cfg.zone_rules()
        )
        if log_changed:
            with self._lock:
                self._build_service()
        if self._auto_recorder and loot_changed:
            previous = self.recorder
            self.recorder = None
            if previous is not None:
                previous.stop()
            self._build_recorder()
            if previous is not None and self.recorder is not None:
                self.recorder.adopt(previous)
        if self._started and any(
            getattr(cfg, f) != getattr(old, f) for f in HOTKEY_FIELDS.values()
        ):
            self._build_hotkeys()
        if self._started and (
            cfg.chat_commands != old.chat_commands
            or cfg.search_strings != old.search_strings
            or (cfg.regexes, cfg.regex_active, cfg.regex_hotkey)
            != (old.regexes, old.regex_active, old.regex_hotkey)
            or any(getattr(cfg, f) != getattr(old, f) for f in HOTKEY_FIELDS.values())
        ):
            self._build_game_hotkeys()
        if self.overlay is not None:
            self.overlay.set_enabled(cfg.overlay_enabled)
            self.overlay.set_opacity(cfg.overlay_opacity)
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
            self._league_chosen = True
        with Store(self.cfg.db_path) as store, store.transaction():  # own connection: thread
            store.set_json(LEAGUE_KEY, league)
        if self.recorder is not None:
            self.recorder.snapshotter.set_league(league, self.cfg.tabs_for(league))
        self.league_hint = self.tabs_hint = None
        self._league_check_tabs = True
        self._league_check_now.set()  # check the new league's tabs
        self.refresh_prices()

    def ninja_leagues(self) -> list[str]:
        """League names poe.ninja has prices for (cached for an hour; 5 min after a failure)."""
        now = time.monotonic()
        ttl = 3600 if self._ninja_leagues else 300
        if now - self._ninja_leagues_at >= ttl:
            self._ninja_leagues = self.prices.client.leagues()
            self._ninja_leagues_at = now
        return self._ninja_leagues

    def default_league(self) -> str:
        """The newest league on poe.ninja (the current challenge league), else Standard."""
        try:
            return latest_league(self.ninja_leagues())
        except Exception:  # offline
            return FALLBACK_LEAGUES[0]

    def _pick_default_league(self) -> None:
        """First start without a league: use the newest one, not Standard."""
        if self._league_chosen:
            return
        league = self.default_league()
        self._league_chosen = True
        if league != self._league:
            log.info("no league chosen yet: using %s", league)
            self.set_league(league)

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
        since = time.monotonic() - self._league_checked_at
        if (result.trigger is Trigger.RUN_START and since >= LEAGUE_CHECK_MAPPING) or (
            OTHER_LEAGUE in (result.error or "") and since >= LEAGUE_MIN_GAP
        ):
            self._league_check_now.set()  # a new league's character: noticed while mapping
        rec = self.recorder
        if result.complete and result.scope == FULL and rec is not None:
            self._set_tabs_hint(rec.snapshotter)  # the tab list was just read
        self.save_rate_limits()

    @property
    def interval_mode(self) -> bool:
        """The "every 10 minutes" mode: session totals only, no per-map loot."""
        return self.cfg.loot_mode == "interval"

    def rate_limited_until(self) -> datetime | None:
        """When the website's rate limit on the loot connection ends, if it's in force."""
        api = getattr(self.recorder.snapshotter, "api", None) if self.recorder else None
        wait = getattr(api, "rate_limited_for", None)
        try:
            seconds = float(wait()) if wait is not None else 0.0
        except Exception:
            return None
        return self.now() + timedelta(seconds=seconds) if seconds >= 5 else None

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

    def watched_tabs_changed(self) -> bool:
        """After the watched tabs changed in Settings. In the interval mode a snapshot is
        taken now (True), so what's already in a newly watched tab isn't counted as loot (or
        a dropped tab's items as spent). Per map, loot doesn't depend on the tabs: they're
        read at the next map start instead, for the Overview."""
        if self.recorder is None:
            return False
        if self.interval_mode:
            return self.snapshot_now()
        self.recorder.stash_soon()
        return False

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

    def kv_get(self, key: str) -> Any:
        return self._kv_get(key)

    def kv_set(self, key: str, value: Any) -> None:
        self._kv_set(key, value)

    def _kv_get(self, key: str) -> Any:
        with Store(self.cfg.db_path) as store:
            return store.get_json(key)

    def _kv_set(self, key: str, value: Any) -> None:
        with Store(self.cfg.db_path) as store:
            store.set_json(key, value)

    def _patch_for_new_run(self, when: datetime) -> str | None:
        """The patch out at `when`; without a patch list, the latest run's (called under
        _lock, by the campaign tracker)."""
        found = self.patches.at(when)
        if found:
            return found
        latest = self._store.list_campaign_runs(1, splits=False)
        return latest[0].patch if latest else None

    def _personal_best(
        self, runs: list[campaign.CampaignRun], run: campaign.CampaignRun | None
    ) -> campaign.CampaignRun | None:
        """The fastest finished run of `run`'s base class, with its splits (the list is read
        without them). None until the run's class is known."""
        base = campaign.base_class(run.char_class) if run is not None else None
        pb = campaign.personal_best(runs, base) if base else None
        return self._store.get_campaign_run(pb.id) if pb is not None and pb.id else pb

    def _bests(
        self, runs: list[campaign.CampaignRun], run: campaign.CampaignRun | None
    ) -> campaign.Bests | None:
        """The fastest times at each split over the other runs of `run`'s base class that got
        past Act 1 (`runs` is read without splits). None until the run's class is known.
        Ended runs don't change, so this is cached by which runs count."""
        base = campaign.base_class(run.char_class) if run is not None else None
        if run is None or not base:
            return None
        ids = tuple(sorted(
            r.id for r in runs
            if r.id is not None and r.id != run.id and campaign.base_class(r.char_class) == base
            and campaign.counts_for_bests(r)
        ))  # fmt: skip
        key = (base, ids)
        bests = self._bests_cache.get(key)
        if bests is None:
            bests = campaign.best_splits(self._store.get_campaign_runs(ids), base)
            if len(self._bests_cache) >= 16:
                self._bests_cache.clear()
            self._bests_cache[key] = bests
        return bests

    def campaign_overview(self, limit: int = 200) -> dict[str, Any]:
        now = self.now()
        with self._lock:
            runs = self._store.list_campaign_runs(limit, splits=False)
            active = self.campaign.active
            pb = self._personal_best(runs, active) if active else None
            bests = self._bests(runs, active) if active else None
        return {
            "active": campaign.run_detail(active, now, pb, bests) if active else None,
            "pb_ids": campaign.best_per_class(runs),  # base class -> its best run
            "top_ids": campaign.top_runs(runs),  # the fastest finished runs, fastest first
            "runs": [campaign.run_summary(r, now) for r in runs],
        }

    def campaign_run(self, run_id: int) -> dict[str, Any] | None:
        with self._lock:
            run = self._store.get_campaign_run(run_id)
            runs = self._store.list_campaign_runs(1000, splits=False)
            pb = self._personal_best(runs, run)
            bests = self._bests(runs, run)
        if run is None:
            return None
        return campaign.run_detail(run, self.now(), pb, bests)

    def campaign_brief(self) -> dict[str, Any] | None:
        """The run in progress, for the sidebar (the status poll): its timer and where."""
        run = self.campaign.active
        if run is None:
            return None
        now = self.now()
        return {"id": run.id, "rta": round(run.rta(now), 1), "paused": run.paused,
                "act": run.act, "character": run.character}  # fmt: skip

    def campaign_set_patch(self, run_id: int, patch: str | None) -> bool:
        with self._lock:
            ok = self._store.set_campaign_patch(run_id, patch)
            if ok and self.campaign.active is not None and self.campaign.active.id == run_id:
                self.campaign.active.patch = patch
            return ok

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

    def _hotkey_action(self, name: str) -> Callable[[], object]:
        if name == "campaign":
            return lambda: self.campaign_toggle(from_hotkey=True)
        if name == "overlay":
            return lambda: self.overlay.toggle() if self.overlay is not None else None
        return lambda: self.overlay.toggle_move() if self.overlay is not None else None

    def _build_hotkeys(self) -> None:
        """(Re)register the global hotkeys from the config: campaign timer, overlay on/off,
        overlay move."""
        for hk in self.hotkeys.values():
            hk.stop()
        self.hotkeys, self.hotkey_errors = {}, {}
        taken: dict[str, str] = {}
        for name, field in HOTKEY_FIELDS.items():
            spec = str(getattr(self.cfg, field)).strip()
            if not spec:
                continue
            try:
                hk = GlobalHotkey(spec, self._hotkey_action(name))
            except HotkeyError as e:
                self.hotkey_errors[name] = str(e)
                continue
            if hk.spec in taken:
                self.hotkey_errors[name] = f"{hk.spec} is already used by {taken[hk.spec]}"
                continue
            taken[hk.spec] = HOTKEY_LABELS[name]
            if hk.start():
                self.hotkeys[name] = hk
            else:
                self.hotkey_errors[name] = hk.error or "couldn't register the hotkey"
        for name, err in self.hotkey_errors.items():
            log.warning("hotkey for %s is off: %s", HOTKEY_LABELS[name], err)
        if self.hotkeys:
            log.info("hotkeys: %s", ", ".join(f"{hk.spec} ({HOTKEY_LABELS[n]})"
                                             for n, hk in self.hotkeys.items()))  # fmt: skip

    def _build_game_hotkeys(self) -> None:
        """(Re)register the chat command and search string hotkeys. They're only registered
        while Path of Exile has focus (GameHotkeys), so F5 still works in your browser."""
        if self.game_hotkeys is not None:
            self.game_hotkeys.stop()
            self.game_hotkeys = None
        self.game_hotkey_errors = {}
        taken: dict[str, str] = {}  # hotkey -> what uses it (the app's global hotkeys first)
        for name, field in HOTKEY_FIELDS.items():
            with contextlib.suppress(HotkeyError):
                if spec := str(getattr(self.cfg, field)).strip():
                    taken[normalize(spec)] = HOTKEY_LABELS[name]
        bindings: dict[str, Callable[[], object]] = {}
        actions: list[tuple[str, str, Callable[[], object]]] = [
            *((c.format(), c.key, self._chat_action(c.text))
              for c in chat_commands(self.cfg.chat_commands)),
            *((s.format(), s.key, self._search_action(s.text))
              for s in search_strings(self.cfg.search_strings)),
        ]  # fmt: skip
        if regex := active_regex(self.cfg.regexes, self.cfg.regex_active):
            actions.append((REGEX_ENTRY, self.cfg.regex_hotkey, self._search_action(regex.text)))
        for entry, key, action in actions:
            if not key:
                continue
            try:
                spec = normalize(key)
            except HotkeyError as e:
                self.game_hotkey_errors[entry] = str(e)
                continue
            if spec in taken:
                self.game_hotkey_errors[entry] = f"{spec} is already used by {taken[spec]}"
                continue
            taken[spec] = entry
            bindings[spec] = action
        if self.game_input.backend is None:  # Settings says so once, not on every row
            return
        backend = self.game_input.backend
        self.game_hotkeys = GameHotkeys(bindings, backend.game_focused)
        self.game_hotkeys.start()
        if bindings:
            log.info("in-game shortcuts (while the game has focus): %s", ", ".join(bindings))

    def _chat_action(self, text: str) -> Callable[[], object]:
        return lambda: self.game_input.chat(text)

    def _search_action(self, text: str) -> Callable[[], object]:
        return lambda: self.game_input.search(text)

    def game_hotkeys_json(self) -> dict[str, Any]:
        """For Settings: per config entry, why it's off (if it is)."""
        gh = self.game_hotkeys
        return {
            "available": self.game_input.backend is not None,
            "errors": dict(self.game_hotkey_errors) | (
                {} if gh is None else {k: v for k, v in gh.errors.items()}),
            "in_game": bool(gh and gh.registered),
            "last": self.game_input.last,
        }  # fmt: skip

    def hotkey_status(self, name: str) -> dict[str, Any]:
        hk = self.hotkeys.get(name)
        return {
            "key": str(getattr(self.cfg, HOTKEY_FIELDS[name])).strip() or None,
            "active": hk is not None and hk.active,
            "error": self.hotkey_errors.get(name),
        }

    def hotkey_json(self) -> dict[str, Any]:
        """The campaign hotkey's status (Campaign tab, Settings)."""
        last = self.hotkey_last
        return {
            **self.hotkey_status("campaign"),
            "last_action": last[0] if last else None,
            "last_at": last[1].isoformat() if last else None,
        }

    def set_overlay_opacity(self, percent: int, save: bool) -> None:
        """Live from the Settings slider: previews while dragging, saves on release."""
        if self.overlay is not None:
            self.overlay.set_opacity(percent, preview=True)
        if save:
            with self._lock:
                self.cfg = replace(self.cfg, overlay_opacity=percent)
                if self.config_path is not None:
                    self.cfg.save(self.config_path)

    def overlay_json(self) -> dict[str, Any]:
        return {
            "enabled": self.cfg.overlay_enabled,
            "available": self.overlay is not None,
            "shown": bool(self.overlay is not None and self.overlay.enabled),
            "toggle": self.hotkey_status("overlay"),
            "move": self.hotkey_status("overlay_move"),
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
                # The release found by the last check may have been replaced since (the beta
                # is, on every push to dev): install what's published now, and if that is
                # replaced while it downloads, look once more.
                for last in (False, True):
                    current = self._fresh_update(info)
                    try:
                        staged = self.updater.stage(
                            current, staging_root(),
                            progress=lambda done, total: setattr(self.update, "progress",
                                                                 (done, total)),
                        )  # fmt: skip
                        break
                    except ReleaseGoneError:
                        if last:
                            raise
                        log.info("update %s was replaced while downloading; checking again",
                                 current.version)  # fmt: skip
                self.update.stage = "installing"
                start_install(staged, folder, staging_root() / "update.log")
            except (UpdateError, OSError, AssertionError) as e:
                log.warning("update failed: %s", e)
                self.update.stage, self.update.error = "failed", str(e) or type(e).__name__
                return
            if self.request_exit is not None:
                self.request_exit()
            # The installer waits for this process to end; don't let a stuck thread or
            # window keep it alive.
            threading.Timer(EXIT_GRACE, self.hard_exit).start()

        threading.Thread(target=run, name="update", daemon=True).start()

    def _fresh_update(self, found: UpdateInfo) -> UpdateInfo:
        """The release to install now: the newest one published (it replaces `found` in the
        state), or `found` when GitHub can't be asked (rate limited, offline)."""
        try:
            fresh = self.updater.check(include_prereleases=self.cfg.update_channel == "beta")
        except UpdateError as e:
            log.info("update: couldn't check again (%s); installing %s", e, found.version)
            return found
        if fresh is None:
            raise UpdateError(f"{found.version} is no longer published and nothing newer is")
        self.update.available = fresh
        return fresh

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

    def dismiss_league_hint(self) -> None:
        """'Not now' on the league banner: don't suggest that league again."""
        hint = self.league_hint
        if hint is None:
            return
        with Store(self.cfg.db_path) as store:
            dismissed = list(store.get_json(DISMISSED_LEAGUES_KEY) or [])
            store.set_json(DISMISSED_LEAGUES_KEY, [*dismissed, hint["league"]][-20:])
        self.league_hint = None

    def check_league(self, tabs: bool = True) -> None:
        """Your account's newest character in another league -> league_hint; with `tabs`,
        watched stash tabs that don't exist in this league -> tabs_hint (the tab list is read
        again). Uses the loot connection (its rate limits); does nothing while loot tracking
        is off."""
        rec = self.recorder
        if rec is None:
            self.league_hint = self.tabs_hint = None
            return
        snap = rec.snapshotter
        league = self.league
        chars = snap.api.list_characters()
        newest = next((c for c in chars if c.get("current")), None)
        dismissed = set(self._kv_get(DISMISSED_LEAGUES_KEY) or [])
        other = newest.get("league") if newest else None
        if newest and other and other != league and self.cfg.league_autoswitch:
            log.info("league switched to %s: your newest character %s plays there", other,
                     newest.get("name"))  # fmt: skip
            self.set_league(str(other))  # also checks the new league's tabs, soon
            self.league_switched = {
                "league": other, "previous": league,
                "character": newest.get("name"), "level": newest.get("level"),
            }  # fmt: skip
            return
        if newest and other and other != league and other not in dismissed:
            self.league_hint = {
                "league": other, "current": league,
                "character": newest.get("name"), "level": newest.get("level"),
            }  # fmt: skip
        else:
            self.league_hint = None
        if tabs and snap.wanted:
            snap.resolve_tabs(refresh=True)
        if tabs or not snap.wanted:
            self._set_tabs_hint(snap)

    def _set_tabs_hint(self, snap: Snapshotter) -> None:
        """From the tab list the snapshotter read last."""
        missing = list(snap.missing_tabs) if snap.wanted else []
        self.tabs_hint = (
            {"league": self.league, "missing": missing,
             "none_found": len(missing) == len(snap.wanted)}
            if missing
            else None
        )  # fmt: skip

    def _league_loop(self) -> None:
        if self._stop.wait(20):  # let loot tracking start first
            return
        while not self._stop.is_set():
            self._league_check_now.clear()
            tabs, self._league_check_tabs = self._league_check_tabs, False
            wait = LEAGUE_CHECK
            try:
                self._pick_default_league()
                self._league_checked_at = time.monotonic()
                self.check_league(tabs=tabs)
            except Exception as e:  # offline, rate limited, cookie expired: try again soon
                log.info("league check skipped: %s", e)
                self._league_check_tabs = self._league_check_tabs or tabs  # still wanted
                wait = LEAGUE_RETRY
            self._league_check_now.wait(wait)
            if self._stop.is_set():
                return

    def _game_log_loop(self) -> None:
        """Windows: while the game runs, read the Client.txt of the install it runs from
        (unless a path is set in Settings). Picks up a second install, or one in an unusual
        place, without the user finding the file."""
        while not self._stop.wait(GAME_LOG_CHECK):
            if self.cfg.log_path is not None or self._log_override is not None:
                continue
            try:
                found = gamepath.running_game_log()
            except Exception:
                log.exception("couldn't look up the running game's folder")
                continue
            if found is None or found == self.log_path or not found.is_file():
                continue
            log.info("the game runs from %s: reading its Client.txt instead of %s",
                     found.parent.parent, self.log_path)  # fmt: skip
            with self._lock:
                self.detected_log = found
                self._store.set_json(GAME_LOG_KEY, str(found))
                self._build_service()

    def _maintenance_loop(self) -> None:
        """Once a day: fill in town time for campaign runs recorded before it was (from the
        saved log events, before old ones are dropped), drop log events older than
        EVENT_RETENTION, and refresh the game's patch list (noting the patch on runs that
        have none)."""
        if self._stop.wait(30):
            return
        while True:
            try:
                with Store(self.cfg.db_path) as store:
                    filled = store.fill_campaign_town(campaign.town_from_events)
                if filled:
                    log.info("town time filled in for %d earlier campaign runs", filled)
            except Exception:
                log.exception("town time back-fill failed")
            try:
                if self.patches.stale():
                    self.patches.refresh()
                if self.patches.patches:
                    with Store(self.cfg.db_path) as store:
                        store.fill_campaign_patches(self.patches.at)
            except Exception:
                log.exception("patch list refresh failed")
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
        league = self.price_league

        def run() -> None:
            try:
                self.prices.refresh(league, force)
            except Exception:  # logged; the next refresh tries again
                log.exception("price refresh for %s failed", league)

        threading.Thread(target=run, name="price-refresh", daemon=True).start()

    # --- lifecycle ---

    def start(self) -> None:
        self._started = True
        log.info("poefire %s starting (%s, Python %s, %s); data in %s", __version__,
                 "app" if getattr(sys, "frozen", False) else "source", sys.version.split()[0],
                 platform.platform(), self.cfg.db_path.parent)  # fmt: skip
        log.info("league %s, prices from %s, loot tracking %s, campaign run %s", self.league,
                 self.price_league, "on" if self.loot.enabled else f"off ({self.loot.reason})",
                 self.campaign.active.id if self.campaign.active else "none")  # fmt: skip
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
        self._spawn("league-check", self._league_loop)
        if sys.platform == "win32":
            self._spawn("game-log", self._game_log_loop)
        self._build_hotkeys()
        self._build_game_hotkeys()

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
        self._league_check_now.set()
        self.builds.stop()
        for hk in self.hotkeys.values():
            hk.stop()
        if self.game_hotkeys is not None:
            self.game_hotkeys.stop()
        if self.recorder is not None:
            self.recorder.stop()
        for t in self._threads:
            t.join(timeout=5)
        self.save_rate_limits()
        self._store.close()
