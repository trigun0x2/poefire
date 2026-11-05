"""Wires tailer -> parser -> tracker -> store.

Each poll's events, run updates, log offset and tracker state are committed in one transaction,
so a restart resumes exactly where it stopped (including catching up on lines logged while the
tracker was closed).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from poefire.events import LogEvent
from poefire.parser import parse_line
from poefire.store import Store
from poefire.tailer import LogTailer, TailLine, find_last_line_offset, head_fingerprint
from poefire.tracker import (
    Notice,
    RunNotice,
    RunTracker,
    state_from_dict,
    state_to_dict,
)
from poefire.zones import ZoneRules

log = logging.getLogger(__name__)

CURSOR_KEY = "log_cursor"
STATE_KEY = "tracker_state"

type NoticeHandler = Callable[[Notice], None]
# Sees every log line (parsed or not), inside the poll's transaction.
type LineObserver = Callable[[LogEvent | None, str], None]


class TrackerService:
    def __init__(
        self,
        store: Store,
        log_path: Path,
        rules: ZoneRules | None = None,
        characters: Iterable[str] = (),
    ) -> None:
        self.store = store
        self.log_path = log_path
        self.rules = rules or ZoneRules()
        self.characters = tuple(characters)
        self.tracker = RunTracker(self.rules, self.characters)
        self.tailer: LogTailer | None = None
        self.handlers: list[NoticeHandler] = []
        self.line_observers: list[LineObserver] = []
        self.observer_errors = 0  # lines an observer failed on (each is logged, then skipped)
        self.lines_seen = 0
        self.events_seen = 0

    def start(self, from_beginning: bool = False) -> None:
        """Pick the start offset: saved cursor if it still matches this file, else the last
        area-generation line near the end (so a map already in progress is picked up)."""
        offset: int | None
        if from_beginning:
            offset = 0
        else:
            offset = self._resume_offset()
            if offset is None:
                self._discard_stale_state()
                offset = find_last_line_offset(self.log_path, b"] Generating level ")
        self.tailer = LogTailer(self.log_path, start_offset=offset)
        try:
            st = self.log_path.stat()
            log.info("reading %s (%d bytes) from offset %s", self.log_path, st.st_size,
                     self.tailer.offset)  # fmt: skip
        except OSError as e:
            log.warning("Client.txt not readable at %s: %s (waiting for it)", self.log_path, e)

    def _resume_offset(self) -> int | None:
        cursor: dict[str, Any] | None = self.store.get_json(CURSOR_KEY)
        if not cursor or cursor.get("path") != str(self.log_path):
            return None
        if cursor.get("head") != head_fingerprint(self.log_path):
            return None
        try:
            if self.log_path.stat().st_size < cursor["offset"]:
                return None
        except OSError:
            return None
        state = self.store.get_json(STATE_KEY)
        if state is not None:
            self.tracker.state = state_from_dict(state)
        return int(cursor["offset"])

    def _discard_stale_state(self) -> None:
        state = self.store.get_json(STATE_KEY)
        if state is None:
            return
        self.tracker.state = state_from_dict(state)
        notices = self.tracker.finish_stale()
        with self.store.transaction():
            self._apply_notices(notices)
            self.store.set_json(STATE_KEY, state_to_dict(self.tracker.state))

    def poll(self) -> list[Notice]:
        """Process every complete line currently available and notify handlers."""
        notices = self.process_available()
        self.dispatch(notices)
        return notices

    def process_available(self) -> list[Notice]:
        """Like poll(), without notifying handlers (lets callers hold a lock only while
        state changes, not while handlers run)."""
        if self.tailer is None:
            raise RuntimeError("start() not called")
        notices: list[Notice] = []
        while lines := self.tailer.poll():
            notices.extend(self._process(lines))
        return notices

    def dispatch(self, notices: list[Notice]) -> None:
        for n in notices:
            for handler in self.handlers:
                handler(n)

    def _process(self, lines: list[TailLine]) -> list[Notice]:
        assert self.tailer is not None
        notices: list[Notice] = []
        with self.store.transaction():
            for line in lines:
                event = parse_line(line.text)
                self.lines_seen += 1
                for observe in self.line_observers:
                    try:
                        observe(event, line.text)
                    except Exception:  # one bad line mustn't stall the whole tail
                        self.observer_errors += 1
                        if self.observer_errors <= 20 or self.observer_errors % 1000 == 0:
                            log.exception("%s failed on line: %s",
                                          getattr(observe, "__qualname__", observe),
                                          line.text[:300])  # fmt: skip
                if event is None:
                    continue
                self.events_seen += 1
                emitted = self.tracker.handle(event)
                self._apply_notices(emitted)
                notices.extend(emitted)
                run = self.tracker.run
                self.store.add_event(event, run.id if run and self.tracker.in_run else None)
            self.store.set_json(
                CURSOR_KEY,
                {
                    "path": str(self.log_path),
                    "offset": self.tailer.offset,
                    "head": head_fingerprint(self.log_path),
                },
            )
            self.store.set_json(STATE_KEY, state_to_dict(self.tracker.state))
        return notices

    def _apply_notices(self, notices: list[Notice]) -> None:
        for n in notices:
            if isinstance(n, RunNotice):
                self.store.upsert_run(n.run)

    def run_forever(self, stop: threading.Event, interval: float = 0.5) -> None:
        if self.tailer is None:
            self.start()
        while not stop.is_set():
            self.poll()
            stop.wait(interval)
