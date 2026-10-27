"""Turns tracker notices into snapshots and attributes each snapshot-to-snapshot delta.

Snapshots form one chain; the delta from the previous complete snapshot to a new one is
labelled by what triggered the new one:

- returning to hideout/town from a run  -> kind "run":     loot of that run
- entering a new map (optional)         -> kind "between": hideout activity before that run
                                                           (trades, crafting, the map you used)
- manual                                -> kind "manual"

Taking the extra snapshot on map entry keeps trading and crafting in the hideout out of the
run's loot. Without it, each run's delta includes everything done in the hideout before it.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from poefire.loot.diff import ItemDelta, diff_items
from poefire.loot.snapshot import Snapshotter
from poefire.store import Store
from poefire.tracker import Notice, NoticeKind, RunNotice, SafeZoneEntered

log = logging.getLogger(__name__)


class Trigger(StrEnum):
    RUN_START = "run_start"
    RUN_RETURN = "run_return"
    MANUAL = "manual"


_KIND = {Trigger.RUN_RETURN: "run", Trigger.RUN_START: "between", Trigger.MANUAL: "manual"}


@dataclass(frozen=True)
class LootResult:
    trigger: Trigger
    run_id: str | None
    snapshot_id: int
    complete: bool
    baseline: bool  # no usable previous snapshot: nothing to diff against yet
    deltas: list[ItemDelta] = field(default_factory=list)
    error: str | None = None

    @property
    def kind(self) -> str:
        return _KIND[self.trigger]


@dataclass(frozen=True)
class _Request:
    trigger: Trigger
    run_id: str | None
    due: float  # monotonic


class LootRecorder:
    def __init__(
        self,
        snapshotter: Snapshotter,
        store_factory: Callable[[], Store],
        return_delay: float = 3.0,
        snapshot_on_run_start: bool = True,
        max_gap: timedelta = timedelta(hours=2),
        max_notice_age: timedelta = timedelta(minutes=2),
        now: Callable[[], datetime] = datetime.now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """`return_delay`: wait after reaching the hideout so the server has saved the
        character. `max_gap`: a previous snapshot older than this is not diffed against
        (too much may have happened in between). `max_notice_age`: ignore notices for old
        log lines (catch-up after a restart) since the API only shows the present."""
        self.snapshotter = snapshotter
        self.store_factory = store_factory
        self.return_delay = return_delay
        self.snapshot_on_run_start = snapshot_on_run_start
        self.max_gap = max_gap
        self.max_notice_age = max_notice_age
        self.now = now
        self.monotonic = monotonic
        self.handlers: list[Callable[[LootResult], None]] = []
        self._store: Store | None = None
        self._pending: list[_Request] = []
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- notice intake (called from the log thread) ---

    def on_notice(self, n: Notice) -> None:
        if isinstance(n, RunNotice) and n.kind is NoticeKind.STARTED:
            if self.snapshot_on_run_start and self._fresh(n.run.started_at):
                self.request(Trigger.RUN_START, n.run.id)
        elif isinstance(n, SafeZoneEntered) and n.left_run and n.run_id and self._fresh(n.ts):
            self.request(Trigger.RUN_RETURN, n.run_id, self.return_delay)

    def _fresh(self, ts: datetime) -> bool:
        return self.now() - ts <= self.max_notice_age

    def request(self, trigger: Trigger, run_id: str | None, delay: float = 0.0) -> None:
        with self._cond:
            self._pending.append(_Request(trigger, run_id, self.monotonic() + delay))
            self._cond.notify()

    # --- worker ---

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="loot-recorder", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify()
        if self._thread:
            self._thread.join(timeout)
        if self._store is not None:
            self._store.close()
            self._store = None

    def _next(self) -> _Request | None:
        with self._cond:
            while not self._stop.is_set():
                if self._pending:
                    req = self._pending[0]
                    wait = req.due - self.monotonic()
                    if wait <= 0:
                        self._pending.pop(0)
                        # A newer identical request supersedes this one (e.g. two quick
                        # hideout trips): one snapshot covers both.
                        if any((r.trigger, r.run_id) == (req.trigger, req.run_id)
                               for r in self._pending):  # fmt: skip
                            continue
                        return req
                    self._cond.wait(wait)
                else:
                    self._cond.wait()
        return None

    def _loop(self) -> None:
        while (req := self._next()) is not None:
            try:
                result = self.record(req.trigger, req.run_id)
            except Exception:
                log.exception("loot snapshot failed")
                continue
            for h in self.handlers:
                h(result)

    # --- the actual work (synchronous; also used for manual snapshots) ---

    def record(self, trigger: Trigger, run_id: str | None = None) -> LootResult:
        if self._store is None:
            self._store = self.store_factory()
        store = self._store
        snap = self.snapshotter.take()  # network I/O outside the DB transaction
        with store.transaction():
            prev = store.last_complete_snapshot()
            sid = store.add_snapshot(snap, trigger.value, run_id)
            if not snap.complete:
                return LootResult(trigger, run_id, sid, False, False, error=snap.error)
            if prev is None or snap.taken_at - prev[1] > self.max_gap:
                store.prune_snapshot_items()
                return LootResult(trigger, run_id, sid, True, True)
            prev_id, _, prev_items = prev
            deltas = diff_items(prev_items, snap.items)
            store.add_loot(
                None if trigger is Trigger.MANUAL else run_id, _KIND[trigger], prev_id, sid, deltas
            )
            store.prune_snapshot_items()
        return LootResult(trigger, run_id, sid, True, False, deltas)
