"""Turns what the log says you do into account snapshots, and records the loot.

Per map (the default), a map's loot is what came into your inventory while you were in it.
Your stash can't change while you're in a map (there's no stash there), only your
inventory can, so two inventory reads measure a map, one request each: one as you go in,
one when you're back in your hideout or town. Going back into the same map after a hideout
trip starts a new segment with its own pair of reads, and the map's loot adds up over its
segments. Where your loot goes afterwards (which tab, sold, used) doesn't matter.

The website shows an inventory as the game last saved it, which it does at every zone
change. So any read between two zone changes sees the inventory as it was when you entered
that zone, and a read that finishes after you've changed zones again could hold what you did
since: it's thrown away. A failed read (network, rate limit) is tried again while you're
still in that zone. A segment whose start or end couldn't be read has no loot data (better
than a wrong number); its map counts the loot of its other segments.

The watched stash tabs, for the Overview (what you hold, its value over time), are read every
STASH_EVERY while you map, right after a map segment starts: the stash can't change until
you're back, so those reads agree with each other and with the inventory read. Reading them
stops (and what was read is dropped) when you get back first, and waits for room in the
website's rate limits, so the inventory reads always have some.

The "every 10 minutes" mode (`interval` set) reads everything (inventory and tabs) every
`interval` seconds while you've run a map in the last MAPPING_IDLE, and once at once when you
start mapping again after that long; the change between two reads counts towards the
session's total, so trades and crafting count too. "Snapshot now" reads everything as well.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum

from poefire.events import AreaGenerated, LogEvent
from poefire.loot.diff import ItemDelta, diff_items
from poefire.loot.items import ItemEntry
from poefire.loot.sales import Sale, SaleBook
from poefire.loot.snapshot import FULL, INVENTORY, Snapshot, Snapshotter, retryable
from poefire.store import SEGMENT_ENDS, SEGMENT_STARTS, Store
from poefire.tracker import Notice, NoticeKind, RunNotice, SafeZoneEntered

log = logging.getLogger(__name__)


TAB_NAMES_KEY = "tab_names"

type SnapshotValue = tuple[str, float, float | None, int]  # league, chaos, chaos/divine, unpriced


class Trigger(StrEnum):
    RUN_START = "run_start"  # into a new map: the inventory as you go in
    RUN_RESUME = "run_resume"  # back into the same map after a trip to the hideout or town
    RUN_RETURN = "run_return"  # back in the hideout or town: that segment's loot
    MANUAL = "manual"  # Snapshot now: the inventory and the watched tabs
    INTERVAL = "interval"  # the "every 10 minutes" mode: totals, not per map


UNPAIRED = "run_return_unpaired"  # stored trigger of a return without a read of its start

SALES_KEY = "trade_sales"
SALES_EVERY = 60.0  # seconds between trade history fetches
SALES_RETRY = 1800.0  # after a failure

# The watched tabs are read every STASH_EVERY while you map, when that fits in the website's
# rate limits with STASH_RESERVE requests to spare (the next maps' inventory reads). Not
# fitting or failing, they're tried again STASH_RETRY seconds later; a short rate-limit
# window is waited out (up to STASH_MAX_WAIT seconds), not given up on.
STASH_EVERY = timedelta(minutes=30)
STASH_RESERVE = 20
STASH_RETRY = 600.0
STASH_MAX_WAIT = 65.0

# A failed inventory read is tried again after these waits (seconds), while you're still in
# the zone it reads; rate limits are waited out first.
RETRY_DELAYS = (5.0, 15.0, 30.0, 60.0, 120.0)
WAIT_FIRST = 5.0  # seconds: longer rate-limit waits are waited out before reading
ZONE_CHANGED = "you changed zones before your inventory could be read"
# Back in the hideout for less than this (seconds, until you left again) and gone before your
# inventory was read: too short to change it there, so the read as you go into the next map
# is also the end of the segment before.
QUICK_TRIP = 5.0

# The "every 10 minutes" mode (`interval` set): one snapshot every `interval` seconds while
# you've run a map in the last MAPPING_IDLE, and one when you start mapping again after that
# long without one. One that doesn't fit in the rate limits (with INTERVAL_RESERVE requests
# to spare, for the league check and build snapshots) waits INTERVAL_RETRY seconds.
INTERVAL = 600.0
MAPPING_IDLE = 1800.0
INTERVAL_RETRY = 60.0
INTERVAL_RESERVE = 5

_OLDEST = datetime.min


@dataclass(frozen=True)
class LootResult:
    trigger: Trigger
    run_id: str | None
    snapshot_id: int
    complete: bool
    baseline: bool  # nothing to compare it with (a map's start, or no usable previous one)
    deltas: list[ItemDelta] = field(default_factory=list)
    error: str | None = None
    scope: str = FULL
    kind: str = ""  # what the change counts as: "run" (a map's loot), "interval", "manual"


@dataclass(frozen=True)
class _Request:
    trigger: Trigger
    run_id: str | None
    due: float  # monotonic
    since: datetime | None = None  # entering the zone whose inventory it reads (log time)
    segment: datetime | None = None  # RUN_RETURN: when the segment it ends started
    tries: int = 0


class LootRecorder:
    def __init__(
        self,
        snapshotter: Snapshotter,
        store_factory: Callable[[], Store],
        return_delay: float = 3.0,
        max_gap: timedelta = timedelta(hours=2),
        max_notice_age: timedelta = timedelta(minutes=2),
        now: Callable[[], datetime] = datetime.now,
        monotonic: Callable[[], float] = time.monotonic,
        stash_every: timedelta | None = STASH_EVERY,
    ) -> None:
        """`return_delay`: wait after a zone change before reading, so the server has saved
        the character. `max_gap`: a previous snapshot older than this is not diffed against
        (too much may have happened in between). `max_notice_age`: ignore notices for old
        log lines (catch-up after a restart) since the API only shows the present.
        `stash_every`: how often the watched tabs are read while mapping (None: never, but
        for Snapshot now and the interval mode)."""
        self.snapshotter = snapshotter
        self.store_factory = store_factory
        self.return_delay = return_delay
        self.max_gap = max_gap
        self.max_notice_age = max_notice_age
        self.now = now
        self.monotonic = monotonic
        self.stash_every = stash_every
        self.handlers: list[Callable[[LootResult], None]] = []
        # Set by the app: prices a complete snapshot -> (league, chaos, chaos/divine, unpriced)
        self.valuer: Callable[[list[ItemEntry]], SnapshotValue | None] | None = None
        # Set by the app in the interval mode, when the account connection can read the trade
        # history: your sales, so their payments aren't counted (see poefire.loot.sales).
        self.sales: Callable[[], list[Sale]] | None = None
        self._sales_next = float("-inf")  # monotonic time of the next allowed history fetch
        self.interval: float | None = None  # seconds: the "every 10 minutes" mode; None = per map
        self._last_snap = float("-inf")  # monotonic time of the last complete snapshot
        self._mapping_at: float | None = None  # last map started or left (interval mode)
        self._last_auto = float("-inf")  # last interval snapshot tried
        self._auto_after = float("-inf")  # no interval snapshot before (no room in the limits)
        self._last_load: datetime | None = None  # log time of the newest area load seen
        self._last_safe: datetime | None = None  # ... of the newest hideout/town entered
        self._segment: tuple[str, datetime] | None = None  # (run, start) of the segment you're in
        # (run, segment start) of a segment whose end read was missed on a quick hideout trip
        self._late_end: tuple[str, datetime | None] | None = None
        self._last_full: datetime | None = None  # when the watched tabs were last read
        self._full_known = False  # _last_full loaded from the store
        self._stash_after = float("-inf")  # monotonic: no tab reads before (after no room/failing)
        self._store: Store | None = None
        self._pending: list[_Request] = []
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- log intake (called from the log thread) ---

    def on_event(self, event: LogEvent | None, line: str = "") -> None:
        """Every log line (a line observer of the log tail): area loads are zone changes,
        which end the zone an inventory read is about."""
        if isinstance(event, AreaGenerated):
            with self._cond:
                if self._last_load is None or event.ts > self._last_load:
                    self._last_load = event.ts

    def on_notice(self, n: Notice) -> None:
        if isinstance(n, SafeZoneEntered):
            with self._cond:
                if self._last_safe is None or n.ts > self._last_safe:
                    self._last_safe = n.ts
                self._late_end = None  # another hideout or town visit: the inventory may change
                self._cond.notify_all()  # a tab read in progress stops: the stash may change
        if self.interval is not None:
            self._interval_notice(n)
            return
        if isinstance(n, RunNotice) and n.kind in (NoticeKind.STARTED, NoticeKind.RESUMED):
            start = n.run.segments[-1].start if n.run.segments else n.run.started_at
            with self._cond:
                self._segment = (n.run.id, start)
            if self._fresh(start):
                trigger = Trigger.RUN_START if n.kind is NoticeKind.STARTED else Trigger.RUN_RESUME
                self.request(trigger, n.run.id, self.return_delay, since=start)
        elif isinstance(n, SafeZoneEntered) and n.left_run and n.run_id:
            with self._cond:
                self._segment = None
            if self._fresh(n.ts):
                self.request(Trigger.RUN_RETURN, n.run_id, self.return_delay, since=n.ts,
                             segment=n.segment_start)  # fmt: skip
        elif isinstance(n, RunNotice) and n.kind is NoticeKind.FINISHED:
            # Finished while inside the map (the game was closed): no hideout comes, so the
            # segment ends here; the game saved the character as it closed.
            with self._cond:
                seg = self._segment
                if seg is None or seg[0] != n.run.id:
                    return
                self._segment = None
            end = n.run.ended_at or n.run.last_activity
            if self._fresh(end):
                self.request(Trigger.RUN_RETURN, n.run.id, self.return_delay, since=end,
                             segment=seg[1])  # fmt: skip

    def _interval_notice(self, n: Notice) -> None:
        """Interval mode: maps only tell that you're mapping; starting again after a while
        (no snapshot for an interval) takes one right away."""
        started = isinstance(n, RunNotice) and n.kind is NoticeKind.STARTED
        if not (started or (isinstance(n, SafeZoneEntered) and n.left_run)):
            return
        ts = n.run.started_at if isinstance(n, RunNotice) else n.ts
        if not self._fresh(ts):
            return
        with self._cond:
            now = self.monotonic()
            self._mapping_at = now
            due = started and now - max(self._last_snap, self._last_auto) >= (self.interval or 0)
            self._cond.notify_all()
        if due:
            self.request(Trigger.INTERVAL, None)

    def _fresh(self, ts: datetime) -> bool:
        return self.now() - ts <= self.max_notice_age

    def request(
        self,
        trigger: Trigger,
        run_id: str | None,
        delay: float = 0.0,
        since: datetime | None = None,
        segment: datetime | None = None,
    ) -> None:
        with self._cond:
            self._pending.append(
                _Request(trigger, run_id, self.monotonic() + delay, since, segment)
            )
            self._cond.notify_all()

    def stash_soon(self) -> None:
        """Read the watched tabs at the next chance (they changed in Settings)."""
        with self._cond:
            self._full_known, self._last_full = True, None
            self._stash_after = float("-inf")

    def adopt(self, previous: LootRecorder) -> None:
        """Carry on where `previous` (replaced after a settings change, and stopped) left
        off: what it knows of your zones, and, in the same mode, its pending map reads."""
        with previous._cond:
            zones = previous._last_load, previous._last_safe, previous._segment
            same_mode = (previous.interval is None) == (self.interval is None)
            moved = [r for r in previous._pending if r.since is not None] if same_mode else []
            previous._pending.clear()
        with self._cond:
            self._last_load, self._last_safe, self._segment = zones
            self._pending.extend(moved)
            self._cond.notify_all()

    # --- worker ---

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="loot-recorder", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        if self._thread:
            self._thread.join(timeout)
        if self._store is not None:
            self._store.close()
            self._store = None

    def _auto_due(self) -> tuple[Trigger, float] | None:
        """The next interval snapshot and when it's due (monotonic), or None."""
        if self.interval is None or self._mapping_at is None:
            return None
        due = max(self._last_snap, self._last_auto) + self.interval
        if due > self._mapping_at + MAPPING_IDLE:
            return None
        return Trigger.INTERVAL, max(due, self._auto_after)

    def _superseded(self, req: _Request) -> bool:
        """A newer request for the same thing is pending (e.g. two quick hideout trips): the
        newer one reads the zone you're in, this one could only come too late."""
        return any(
            r.trigger is req.trigger
            and r.run_id == req.run_id
            and (r.since or _OLDEST) >= (req.since or _OLDEST)
            for r in self._pending
        )

    def _next(self) -> _Request | None:
        with self._cond:
            while not self._stop.is_set():
                now = self.monotonic()
                if self._pending:
                    req = min(self._pending, key=lambda r: r.due)
                    if req.due <= now:
                        self._pending.remove(req)
                        if self._superseded(req):
                            continue
                        return req
                    self._cond.wait(req.due - now)
                    continue
                auto = self._auto_due()
                if auto is None:
                    self._cond.wait()
                elif auto[1] <= now:
                    return _Request(auto[0], None, auto[1])
                else:
                    self._cond.wait(auto[1] - now)
        return None

    def _loop(self) -> None:
        while (req := self._next()) is not None:
            try:
                self._handle(req)
            except Exception:
                log.exception("loot snapshot failed")

    def _handle(self, req: _Request) -> None:
        if req.trigger is Trigger.INTERVAL:
            if not self._fits(self.snapshotter.tab_requests() + 1, INTERVAL_RESERVE):
                log.info("interval snapshot waits: not enough room in the website's rate limits")
                with self._cond:
                    self._auto_after = self.monotonic() + INTERVAL_RETRY
                return
            with self._cond:
                self._last_auto = self.monotonic()
        if req.since is not None:
            if not self._same_zone(req.since):
                snap = Snapshot(self.snapshotter.clock(), [], False, ZONE_CHANGED, INVENTORY)
                self._finish(req, snap)
                return
            wait = self._rate_wait()
            if wait > WAIT_FIRST:  # wait it out here, not inside the request (holding it up)
                self._requeue(req, wait, req.tries)
                return
        snap = self._take(req)
        if (
            not snap.complete
            and snap.retry
            and req.since is not None
            and req.tries < len(RETRY_DELAYS)
            and self._same_zone(req.since)
        ):
            delay = RETRY_DELAYS[req.tries]
            log.info("inventory read failed (%s): trying again in %.0fs", snap.error, delay)
            self._requeue(req, delay, req.tries + 1)
            return
        self._finish(req, snap)

    def _requeue(self, req: _Request, delay: float, tries: int) -> None:
        with self._cond:
            self._pending.append(replace(req, due=self.monotonic() + delay, tries=tries))
            self._cond.notify_all()

    def _finish(self, req: _Request, snap: Snapshot) -> None:
        if req.trigger is Trigger.RUN_RETURN and snap.error == ZONE_CHANGED and req.run_id:
            self._note_quick_trip(req)
        result = self._save(req.trigger, req.run_id, snap, req.segment)
        if result.complete:
            with self._cond:
                self._last_snap = self.monotonic()
        for h in self.handlers:
            h(result)

    def _note_quick_trip(self, req: _Request) -> None:
        """The end read of a segment was missed: if you left the hideout again within
        QUICK_TRIP (and went nowhere else safe), the next segment start read stands in."""
        with self._cond:
            left, back = self._last_load, self._last_safe
            if (
                req.since is not None
                and left is not None
                and (left - req.since).total_seconds() <= QUICK_TRIP
                and (back is None or back <= req.since)
            ):
                self._late_end = (str(req.run_id), req.segment)

    def _same_zone(self, since: datetime) -> bool:
        """No area load since `since`: the zone it entered is still the one you're in."""
        with self._cond:
            return self._last_load is None or self._last_load <= since

    def _api(self) -> object:
        return getattr(self.snapshotter, "api", None)

    def _rate_wait(self) -> float:
        wait = getattr(self._api(), "rate_limited_for", None)
        try:
            return float(wait()) if wait is not None else 0.0
        except Exception:
            return 0.0

    def _fits(self, n: int, reserve: int) -> bool:
        fits = getattr(self._api(), "fits", None)
        try:
            return bool(fits(n, reserve)) if fits is not None else True
        except Exception:
            return True

    # --- reading ---

    def _take(self, req: _Request) -> Snapshot:
        if req.trigger in (Trigger.MANUAL, Trigger.INTERVAL):
            return self.snapshotter.take(FULL)
        snap = Snapshot(self.snapshotter.clock(), scope=INVENTORY)
        try:
            snap.items = self.snapshotter.read_inventory()
        except Exception as e:
            log.warning("inventory read failed: %s", e)
            return replace(snap, complete=False, error=str(e), retry=retryable(e))
        if req.since is not None and not self._same_zone(req.since):
            return replace(snap, items=[], complete=False, error=ZONE_CHANGED)
        if req.trigger in (Trigger.RUN_START, Trigger.RUN_RESUME) and self._stash_due():
            tabs = self._read_tabs(req.since)
            if tabs is not None:
                snap = replace(snap, items=[*snap.items, *tabs], scope=FULL)
        return snap

    def _stash_due(self) -> bool:
        """Whether to read the watched tabs now (just after a map segment started)."""
        if self.stash_every is None or not self.snapshotter.wanted:
            return False
        if self.monotonic() < self._stash_after:
            return False
        last = self._last_full_at()
        if last is not None and self.now() - last < self.stash_every:
            return False
        n = self.snapshotter.tab_requests()
        if not self._fits(n, STASH_RESERVE):
            log.info("watched tabs not read now: %d requests don't fit in the website's rate "
                     "limits", n)  # fmt: skip
            self._stash_after = self.monotonic() + STASH_RETRY
            return False
        return True

    def _last_full_at(self) -> datetime | None:
        if not self._full_known:
            latest = self._get_store().latest_snapshot(scope=FULL, items=False)
            self._last_full, self._full_known = (latest.taken_at if latest else None), True
        return self._last_full

    def _read_tabs(self, since: datetime | None) -> list[ItemEntry] | None:
        """The watched tabs, read while you stay in the map; None when you got back first,
        the rate limits had no room or a read failed."""
        why: list[str] = []

        def keep_going() -> bool:
            with self._cond:
                while True:
                    if self._stop.is_set():
                        why.append("stopping")
                        return False
                    back = self._last_safe
                    if since is not None and back is not None and back > since:
                        why.append("you were back before they were all read")
                        return False
                    wait = self._rate_wait()
                    if wait <= 0:
                        return True
                    if wait > STASH_MAX_WAIT:
                        why.append("the website's rate limits had no room")
                        return False
                    self._cond.wait(wait)  # a short window: wait (a return wakes this up)

        try:
            tabs = self.snapshotter.read_tabs(keep_going)
        except Exception as e:
            log.warning("reading the watched tabs failed: %s", e)
            self._stash_after = self.monotonic() + STASH_RETRY
            return None
        if tabs is None:
            log.info("stopped reading the watched tabs: %s", why[0] if why else "stopped")
            if why and "rate limits" in why[0]:
                self._stash_after = self.monotonic() + STASH_RETRY
        return tabs

    # --- storing (synchronous; also used for manual snapshots) ---

    def _get_store(self) -> Store:
        if self._store is None:
            self._store = self.store_factory()
        return self._store

    def _record_full(self, store: Store, snapshot_id: int, snap: Snapshot) -> None:
        """A read of the watched tabs: their names, and the value for the Overview."""
        self._last_full, self._full_known = snap.taken_at, True
        # merged, so tabs dropped from the watch list keep their names in older data
        names = {**(store.get_json(TAB_NAMES_KEY) or {}), **self.snapshotter.tab_names()}
        store.set_json(TAB_NAMES_KEY, names)
        if self.valuer is None:
            return
        try:
            v = self.valuer(snap.items)
        except Exception:
            log.exception("valuing snapshot failed")
            return
        if v is not None:
            store.add_snapshot_value(snapshot_id, snap.taken_at, *v)

    def _fetch_sales(self) -> list[Sale] | None:
        """The trade history, at most once a minute (once per 30 min after a failure)."""
        if self.sales is None or self.monotonic() < self._sales_next:
            return None
        try:
            sales = self.sales()
        except Exception as e:  # network, refused, a changed format
            log.info("trade history not available (%s): sales are counted as loot", e)
            self._sales_next = self.monotonic() + SALES_RETRY
            return None
        self._sales_next = self.monotonic() + SALES_EVERY
        return sales

    def record(self, trigger: Trigger, run_id: str | None = None, since: datetime | None = None,
               segment: datetime | None = None) -> LootResult:  # fmt: skip
        """Take one snapshot for `trigger` and store it (no retries)."""
        snap = self._take(_Request(trigger, run_id, self.monotonic(), since, segment))
        return self._save(trigger, run_id, snap, segment)

    def _save(self, trigger: Trigger, run_id: str | None, snap: Snapshot,
              segment: datetime | None) -> LootResult:  # fmt: skip
        store = self._get_store()
        changes = trigger in (Trigger.MANUAL, Trigger.INTERVAL)
        # network I/O outside the DB transaction
        sales = self._fetch_sales() if changes and snap.complete and snap.scope == FULL else None
        with store.transaction():
            if trigger is Trigger.RUN_RETURN:
                return self._save_return(store, run_id, snap, segment)
            if not changes:  # a segment's start
                with self._cond:
                    late, self._late_end = self._late_end, None
                if late is not None and snap.complete:
                    log.info("map %s: back in the hideout only briefly, its loot is read now",
                             late[0])  # fmt: skip
                    inventory = [e for e in snap.items if e.source == "inventory"]
                    end = Snapshot(snap.taken_at, inventory, scope=INVENTORY)
                    self._save_return(store, late[0], end, late[1])
                sid = store.add_snapshot(snap, trigger.value, run_id)
                if snap.complete:
                    if snap.scope == FULL:
                        self._record_full(store, sid, snap)
                    store.prune_snapshot_items()
                return LootResult(trigger, run_id, sid, snap.complete, True, error=snap.error,
                                  scope=snap.scope)  # fmt: skip
            return self._save_change(store, trigger, snap, sales)

    def _save_return(self, store: Store, run_id: str | None, snap: Snapshot,
                     segment: datetime | None) -> LootResult:  # fmt: skip
        """The end of a map segment: its loot is what the inventory gained since the read
        at the segment's start, if there is one (the newest segment snapshot, a start of this
        run taken in this segment)."""
        start = store.latest_snapshot((*SEGMENT_STARTS, *SEGMENT_ENDS))
        paired = (
            start is not None
            and start.trigger in SEGMENT_STARTS
            and start.run_id == run_id
            and (segment is None or start.taken_at >= segment)
        )
        kept = Trigger.RUN_RETURN.value if paired or not snap.complete else UNPAIRED
        sid = store.add_snapshot(snap, kept, run_id)
        if not snap.complete:
            return LootResult(Trigger.RUN_RETURN, run_id, sid, False, False, error=snap.error,
                              scope=snap.scope)  # fmt: skip
        if not paired or start is None:
            log.info("map %s: no read of your inventory from when you went in: this trip's "
                     "loot isn't known", run_id)  # fmt: skip
            store.prune_snapshot_items()
            return LootResult(Trigger.RUN_RETURN, run_id, sid, True, True, scope=snap.scope)
        deltas = diff_items(start.inventory(), [e for e in snap.items if e.source == "inventory"])
        store.add_loot(run_id, "run", start.id, sid, deltas)
        store.prune_snapshot_items()
        gains = sum(1 for d in deltas if d.quantity > 0)
        log.info("loot of map %s: %d item kinds gained, %d lost", run_id, gains,
                 len(deltas) - gains)  # fmt: skip
        return LootResult(Trigger.RUN_RETURN, run_id, sid, True, False, deltas, scope=snap.scope,
                          kind="run")  # fmt: skip

    def _save_change(self, store: Store, trigger: Trigger, snap: Snapshot,
                     sales: list[Sale] | None) -> LootResult:  # fmt: skip
        """Snapshot now and the interval mode: the change since the previous snapshot (only
        the inventory's, when either of them didn't read the tabs)."""
        prev = store.latest_snapshot()
        sid = store.add_snapshot(snap, trigger.value, None)
        if not snap.complete:
            return LootResult(trigger, None, sid, False, False, error=snap.error, scope=snap.scope)
        if snap.scope == FULL:
            self._record_full(store, sid, snap)
        book: SaleBook | None = None
        if sales is not None:
            saved = store.get_json(SALES_KEY)
            book = SaleBook.from_json(saved)
            book.add(sales, first_fetch=saved is None)
            store.set_json(SALES_KEY, book.to_json())
        if prev is None or snap.taken_at - prev.taken_at > self.max_gap:
            store.prune_snapshot_items()
            return LootResult(trigger, None, sid, True, True, scope=snap.scope)
        both_full = prev.scope == FULL and snap.scope == FULL
        before = prev.items if both_full else prev.inventory()
        after = snap.items if both_full else [e for e in snap.items if e.source == "inventory"]
        deltas = diff_items(before, after)
        # in the interval mode, Snapshot now splits an interval: both parts count
        kind = "interval" if trigger is Trigger.INTERVAL or self.interval is not None else "manual"
        if book is not None and book.pending and both_full:
            deltas += book.offsets(deltas, snap.taken_at)
            store.set_json(SALES_KEY, book.to_json())
        store.add_loot(None, kind, prev.id, sid, deltas)
        store.prune_snapshot_items()
        gains = sum(1 for d in deltas if d.quantity > 0)
        log.info("loot snapshot (%s): %d item kinds gained, %d lost", trigger.value, gains,
                 len(deltas) - gains)  # fmt: skip
        return LootResult(trigger, None, sid, True, False, deltas, scope=snap.scope, kind=kind)
