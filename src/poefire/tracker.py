"""Map-run state machine. Pure: no I/O, state is JSON-serializable.

Rules:
- Entering a MAP instance (area_id + seed) not belonging to the current run starts a new run.
  Maps can only be opened from a hideout/town, so any area reached directly from the run (even
  one whose ID looks like a map) is treated as a side area of it.
- Time counts while in the run's map or in zones reached from it (side areas).
- Hideout/town pauses the run; returning to the same instance (same seed) resumes it.
- The run finishes when a different map instance is generated, or the client restarts.
- A segment closes when the next area starts loading (`Generating`), opens on `You have entered`,
  so loading screens are not counted.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from poefire.events import AreaGenerated, LogEvent, LogOpened, PlayerSlain, ZoneEntered
from poefire.zones import ZoneKind, ZoneRules


class RunStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    FINISHED = "finished"


@dataclass
class Segment:
    start: datetime
    end: datetime | None = None

    def seconds(self, now: datetime | None = None) -> float:
        end = self.end or now
        if end is None:
            return 0.0
        return max(0.0, (end - self.start).total_seconds())


@dataclass
class Run:
    id: str
    area_id: str
    map_name: str
    area_level: int
    seed: int
    started_at: datetime
    status: RunStatus = RunStatus.ACTIVE
    deaths: int = 0
    segments: list[Segment] = field(default_factory=list)
    ended_at: datetime | None = None

    def active_seconds(self, now: datetime | None = None) -> float:
        """Closed segments, plus the open one up to `now` if given."""
        return sum(s.seconds(now) for s in self.segments)

    @property
    def open_segment(self) -> Segment | None:
        if self.segments and self.segments[-1].end is None:
            return self.segments[-1]
        return None

    @property
    def last_activity(self) -> datetime:
        if not self.segments:
            return self.started_at
        last = self.segments[-1]
        return last.end or last.start

    def is_instance(self, area_id: str | None, seed: int | None) -> bool:
        return self.area_id == area_id and self.seed == seed


@dataclass
class Area:
    area_id: str | None
    area_level: int | None
    seed: int | None
    kind: ZoneKind
    name: str | None = None


class NoticeKind(StrEnum):
    STARTED = "started"
    PAUSED = "paused"
    RESUMED = "resumed"
    UPDATED = "updated"
    FINISHED = "finished"


@dataclass(frozen=True)
class RunNotice:
    kind: NoticeKind
    run: Run  # snapshot, safe to keep


@dataclass(frozen=True)
class SafeZoneEntered:
    """Hook for Phase 2: take a stash/inventory snapshot and attribute it to `run_id`."""

    ts: datetime
    zone: str
    kind: ZoneKind
    run_id: str | None
    left_run: bool = False  # came straight from the run (not e.g. town -> hideout)


type Notice = RunNotice | SafeZoneEntered


@dataclass
class TrackerState:
    current: Area | None = None
    pending: Area | None = None
    run: Run | None = None
    in_run: bool = False
    last_ts: datetime | None = None


class RunTracker:
    def __init__(
        self,
        rules: ZoneRules | None = None,
        characters: Iterable[str] = (),
        state: TrackerState | None = None,
        new_id: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self.rules = rules or ZoneRules()
        # Empty = count every "has been slain" line (fine solo; set names for party play).
        self.characters = frozenset(characters)
        self.state = state or TrackerState()
        self._new_id = new_id

    @property
    def run(self) -> Run | None:
        return self.state.run

    @property
    def in_run(self) -> bool:
        return self.state.in_run

    def handle(self, event: LogEvent) -> list[Notice]:
        notices: list[Notice] = []
        match event:
            case AreaGenerated():
                self._on_generated(event, notices)
            case ZoneEntered():
                self._on_entered(event, notices)
            case PlayerSlain():
                self._on_slain(event, notices)
            case LogOpened():
                self._finish(self.state.last_ts or event.ts, notices)
                self.state.current = self.state.pending = None
            case _:
                pass
        self.state.last_ts = event.ts
        return notices

    def finish_stale(self) -> list[Notice]:
        """Finish an unfinished run whose log context was lost (log replaced, etc.)."""
        notices: list[Notice] = []
        if self.state.run is not None:
            self._finish(self.state.run.last_activity, notices)
        self.state = TrackerState()
        return notices

    # --- transitions ---

    def _on_generated(self, e: AreaGenerated, out: list[Notice]) -> None:
        kind = self.rules.classify(e.area_id)
        self.state.pending = Area(e.area_id, e.area_level, e.seed, kind)
        run = self.state.run
        if run is None:
            return
        if (
            kind is ZoneKind.MAP
            and not self.state.in_run
            and not run.is_instance(e.area_id, e.seed)
        ):
            self._finish(e.ts, out)
        elif self.state.in_run and kind.is_safe:
            self._close_segment(run, e.ts)
        # Moving inside the run (map <-> side area) keeps the segment open through the load.

    def _on_entered(self, e: ZoneEntered, out: list[Notice]) -> None:
        area = self.state.pending or Area(None, None, None, self.rules.classify_name(e.zone))
        area.name = e.zone
        self.state.pending = None
        self.state.current = area
        run = self.state.run

        if area.kind.is_safe:
            if run is not None and run.status is RunStatus.ACTIVE:
                self._close_segment(run, e.ts)
                run.status = RunStatus.PAUSED
                out.append(self._notice(NoticeKind.PAUSED, run))
            left_run = self.state.in_run and run is not None
            self.state.in_run = False
            out.append(SafeZoneEntered(e.ts, e.zone, area.kind, run.id if run else None, left_run))
        elif run is not None and (self.state.in_run or run.is_instance(area.area_id, area.seed)):
            self._resume(run, e.ts, out)  # back into the run, or a side area of it
        elif area.kind is ZoneKind.MAP:
            self._finish(e.ts, out)
            self._start(area, e.ts, out)
        else:
            self.state.in_run = False

    def _on_slain(self, e: PlayerSlain, out: list[Notice]) -> None:
        run = self.state.run
        if run is None or not self.state.in_run:
            return
        if self.characters and e.character not in self.characters:
            return
        run.deaths += 1
        out.append(self._notice(NoticeKind.UPDATED, run))

    # --- helpers ---

    def _start(self, area: Area, ts: datetime, out: list[Notice]) -> None:
        assert area.area_id is not None and area.seed is not None
        run = Run(
            id=self._new_id(),
            area_id=area.area_id,
            map_name=area.name or area.area_id,
            area_level=area.area_level or 0,
            seed=area.seed,
            started_at=ts,
            segments=[Segment(ts)],
        )
        self.state.run = run
        self.state.in_run = True
        out.append(self._notice(NoticeKind.STARTED, run))

    def _resume(self, run: Run, ts: datetime, out: list[Notice]) -> None:
        self.state.in_run = True
        if run.open_segment is None:
            run.segments.append(Segment(ts))
        if run.status is not RunStatus.ACTIVE:
            run.status = RunStatus.ACTIVE
            out.append(self._notice(NoticeKind.RESUMED, run))

    def _finish(self, ts: datetime, out: list[Notice]) -> None:
        run = self.state.run
        if run is None:
            return
        self._close_segment(run, ts)
        run.status = RunStatus.FINISHED
        run.ended_at = run.last_activity
        out.append(self._notice(NoticeKind.FINISHED, run))
        self.state.run = None
        self.state.in_run = False

    @staticmethod
    def _close_segment(run: Run, ts: datetime) -> None:
        seg = run.open_segment
        if seg is not None:
            seg.end = max(ts, seg.start)

    @staticmethod
    def _notice(kind: NoticeKind, run: Run) -> RunNotice:
        return RunNotice(kind, copy.deepcopy(run))


# --- state (de)serialization ---


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _parse_dt(value: Any) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _area_to_dict(a: Area | None) -> dict[str, Any] | None:
    if a is None:
        return None
    return {
        "area_id": a.area_id,
        "area_level": a.area_level,
        "seed": a.seed,
        "kind": a.kind.value,
        "name": a.name,
    }


def _area_from_dict(d: dict[str, Any] | None) -> Area | None:
    if d is None:
        return None
    return Area(d["area_id"], d["area_level"], d["seed"], ZoneKind(d["kind"]), d["name"])


def run_to_dict(r: Run) -> dict[str, Any]:
    return {
        "id": r.id,
        "area_id": r.area_id,
        "map_name": r.map_name,
        "area_level": r.area_level,
        "seed": r.seed,
        "started_at": _dt(r.started_at),
        "status": r.status.value,
        "deaths": r.deaths,
        "segments": [[_dt(s.start), _dt(s.end)] for s in r.segments],
        "ended_at": _dt(r.ended_at),
    }


def run_from_dict(d: dict[str, Any]) -> Run:
    started = _parse_dt(d["started_at"])
    assert started is not None
    segments = []
    for start, end in d["segments"]:
        s = _parse_dt(start)
        assert s is not None
        segments.append(Segment(s, _parse_dt(end)))
    return Run(
        id=d["id"],
        area_id=d["area_id"],
        map_name=d["map_name"],
        area_level=d["area_level"],
        seed=d["seed"],
        started_at=started,
        status=RunStatus(d["status"]),
        deaths=d["deaths"],
        segments=segments,
        ended_at=_parse_dt(d["ended_at"]),
    )


def state_to_dict(s: TrackerState) -> dict[str, Any]:
    return {
        "current": _area_to_dict(s.current),
        "pending": _area_to_dict(s.pending),
        "run": run_to_dict(s.run) if s.run else None,
        "in_run": s.in_run,
        "last_ts": _dt(s.last_ts),
    }


def state_from_dict(d: dict[str, Any]) -> TrackerState:
    return TrackerState(
        current=_area_from_dict(d["current"]),
        pending=_area_from_dict(d["pending"]),
        run=run_from_dict(d["run"]) if d["run"] else None,
        in_run=d["in_run"],
        last_ts=_parse_dt(d["last_ts"]),
    )
