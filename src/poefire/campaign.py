"""Campaign speedrun tracker: splits from Client.txt, like a LiveSplit auto-splitter.

A run is started by hand (the "Start new run" button). From then on the log drives it:

- **Timer.** Real time from the start, plus a load-removed time. Loads are timed with the
  client's millisecond tick counter (the third field of every log line), from the start of a
  load (`Got Instance Details` / `Generating level`) to `You have entered <zone>.`.
- **Splits.** The first visit to each zone of an act, and every completed act (the first time
  an area of a later act is entered). Entering an area past act 10 (the epilogue) finishes
  the run.
- **Character.** The first character that levels up is the run's character; its class comes
  from the level-up line (`Name (Class) is now level N`), so it turns into the ascendancy once
  you ascend. Level-ups and deaths of that character are recorded.

Campaign area ids look like `<part>_<act>_<zone>` (`1_1_1` is The Twilight Strand,
`2_6_town` is Lioneye's Watch in act 6); anything else (hideouts, the labyrinth, maps) does
not change the act.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from poefire.events import AreaGenerated, LevelUp, LogEvent, PlayerSlain, ZoneEntered

FINAL_ACT = 10
MAX_LOAD_MS = 5 * 60 * 1000  # longer "loads" are a sleeping PC or a reboot, not a load

_ACT_RE = re.compile(r"^\d+_(\d+)_")
_TICK_RE = re.compile(r"^\d{4}/\d\d/\d\d \d\d:\d\d:\d\d (\d+) ")

ASCENDANCIES: dict[str, str] = {
    **dict.fromkeys(("Juggernaut", "Berserker", "Chieftain"), "Marauder"),
    **dict.fromkeys(("Deadeye", "Raider", "Warden", "Pathfinder"), "Ranger"),
    **dict.fromkeys(("Necromancer", "Elementalist", "Occultist"), "Witch"),
    **dict.fromkeys(("Slayer", "Gladiator", "Champion"), "Duelist"),
    **dict.fromkeys(("Inquisitor", "Hierophant", "Guardian"), "Templar"),
    **dict.fromkeys(("Assassin", "Trickster", "Saboteur"), "Shadow"),
    "Ascendant": "Scion",
}


def base_class(cls: str | None) -> str | None:
    return ASCENDANCIES.get(cls, cls) if cls else None


def act_of(area_id: str) -> int | None:
    m = _ACT_RE.match(area_id)
    return int(m.group(1)) if m else None


def log_tick(line: str) -> int | None:
    """The client's millisecond tick counter from a raw log line."""
    m = _TICK_RE.match(line)
    return int(m.group(1)) if m else None


class SplitKind:
    ZONE = "zone"
    ACT = "act"
    LEVEL = "level"


@dataclass
class Split:
    kind: str
    name: str
    act: int | None
    at: datetime
    rta: float  # seconds since the start
    igt: float  # the same, without loading screens
    level: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "name": self.name, "act": self.act,
                "at": self.at.isoformat(), "rta": round(self.rta, 3), "igt": round(self.igt, 3),
                "level": self.level}  # fmt: skip

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Split:
        return cls(d["kind"], d["name"], d.get("act"), datetime.fromisoformat(d["at"]),
                   float(d["rta"]), float(d["igt"]), d.get("level"))  # fmt: skip


@dataclass
class CampaignRun:
    started_at: datetime
    id: int | None = None
    status: str = "running"  # running | finished | abandoned
    ended_at: datetime | None = None
    character: str | None = None
    char_class: str | None = None
    level: int | None = None
    deaths: int = 0
    act: int | None = None
    load_ms: int = 0
    notes: str = ""
    splits: list[Split] = field(default_factory=list)
    paused_at: datetime | None = None  # set while the timer is paused
    paused_ms: int = 0  # earlier pauses, in total
    mule: str | None = None  # the Ranger mule this run started with (its name)

    @property
    def running(self) -> bool:
        return self.status == "running"

    @property
    def paused(self) -> bool:
        return self.paused_at is not None

    def paused_seconds(self, now: datetime) -> float:
        current = (now - self.paused_at).total_seconds() if self.paused_at else 0.0
        return self.paused_ms / 1000 + max(0.0, current)

    def rta(self, now: datetime) -> float:
        """Timer time: from the start, without the time spent paused."""
        end = self.ended_at or now
        return max(0.0, (end - self.started_at).total_seconds() - self.paused_seconds(end))

    def igt(self, now: datetime) -> float:
        return max(0.0, self.rta(now) - self.load_ms / 1000)

    def act_splits(self) -> list[Split]:
        return [s for s in self.splits if s.kind == SplitKind.ACT]


# A "mule": a throwaway Ranger made at the start of a run (to grab early items), which only
# reaches level 2 and at most Lioneye's Watch before the real character is created.
MULE_CLASS = "Ranger"
MULE_MAX_LEVEL = 2
MULE_ZONES = frozenset({"The Twilight Strand", "Lioneye's Watch"})


def _mule_zones_only(run: CampaignRun) -> bool:
    zones = {s.name for s in run.splits if s.kind == SplitKind.ZONE}
    return (run.act or 1) <= 1 and zones <= MULE_ZONES


def is_mule_of(run: CampaignRun) -> bool:
    """The run's character looks like a mule (another character just levelled up)."""
    return (
        run.char_class == MULE_CLASS
        and (run.level or 1) <= MULE_MAX_LEVEL
        and _mule_zones_only(run)
    )


def is_mule(event: LevelUp, run: CampaignRun) -> bool:
    """`event` is a mule's level-up (before the run's named character levelled)."""
    return (
        event.character_class == MULE_CLASS
        and event.level <= MULE_MAX_LEVEL
        and _mule_zones_only(run)
    )


class CampaignTracker:
    """Feeds log lines into the active campaign run and saves it after every change.
    Not thread-safe: the runtime calls it under its lock."""

    def __init__(self, store: Any, now: Callable[[], datetime] = datetime.now) -> None:
        self.store = store
        self.now = now
        self.active: CampaignRun | None = store.active_campaign_run()
        self._load_start: int | None = None
        self._next_act: int | None = None
        self._last_zone: tuple[str, int, datetime] | None = None  # campaign zone you're in

    # --- manual controls ---

    def start(self, character: str | None = None) -> CampaignRun:
        now = self.now()
        if self.active is not None:
            self._end("abandoned", now)
        run = CampaignRun(started_at=now, character=(character or "").strip() or None)
        run.id = self.store.save_campaign_run(run)
        self.active = run
        self._load_start = self._next_act = self._last_zone = None
        return run

    def finish(self) -> CampaignRun | None:
        return self._end("finished", self.now())

    def pause(self) -> bool:
        run = self.active
        if run is None or run.paused:
            return False
        run.paused_at = self.now()
        self.store.save_campaign_run(run)
        return True

    def resume(self) -> bool:
        run = self.active
        if run is None or not run.paused:
            return False
        self._unpause(run, self.now())
        self.store.save_campaign_run(run)
        return True

    def toggle(self) -> str:
        """The hotkey: start a run if none is going, else pause / resume it."""
        if self.active is None:
            self.start()
            return "started"
        if self.active.paused:
            self.resume()
            return "resumed"
        self.pause()
        return "paused"

    @staticmethod
    def _unpause(run: CampaignRun, at: datetime) -> None:
        if run.paused_at is not None:
            run.paused_ms += max(0, int((at - run.paused_at).total_seconds() * 1000))
            run.paused_at = None

    def stop(self) -> CampaignRun | None:
        return self._end("abandoned", self.now())

    def _end(self, status: str, at: datetime) -> CampaignRun | None:
        run = self.active
        if run is None:
            return None
        if run.paused_at is not None:
            at = max(at, run.paused_at)
            self._unpause(run, at)
        run.status, run.ended_at = status, at
        self.store.save_campaign_run(run)
        self.active = None
        return run

    # --- log lines ---

    def handle(self, event: LogEvent | None, line: str) -> None:
        run = self.active
        if run is None:
            return
        if event is None:
            if "] Got Instance Details" in line:
                self._load_start = log_tick(line)
            return
        if event.ts < run.started_at:
            return  # catching up on lines from before the start
        if isinstance(event, AreaGenerated):
            if self._load_start is None:
                self._load_start = log_tick(line)
            self._next_act = act_of(event.area_id)
        elif isinstance(event, ZoneEntered):
            self._entered(run, event, log_tick(line))
        elif isinstance(event, LevelUp):
            self._levelled(run, event)
        elif isinstance(event, PlayerSlain):
            if run.character and event.character.casefold() == run.character.casefold():
                run.deaths += 1
                self.store.save_campaign_run(run)

    def _split(self, run: CampaignRun, kind: str, name: str, at: datetime,
               level: int | None = None) -> None:  # fmt: skip
        run.splits.append(Split(kind, name, run.act, at, run.rta(at), run.igt(at), level))

    def _entered(self, run: CampaignRun, event: ZoneEntered, tick: int | None) -> None:
        if self._load_start is not None and tick is not None:
            load = tick - self._load_start
            if 0 <= load <= MAX_LOAD_MS and not run.paused:  # paused: the timer is stopped
                run.load_ms += load
        self._load_start = None
        act, self._next_act = self._next_act, None
        if act is not None and (run.act is None or act > run.act):
            if run.act is not None:
                self._split(run, SplitKind.ACT, f"Act {run.act}", event.ts)
            run.act = act
            if act > FINAL_ACT:
                self.store.save_campaign_run(run)
                self._end("finished", event.ts)
                return
        if act is not None and act == run.act:
            self._last_zone = (event.zone, act, event.ts)
        if (
            act is not None
            and act == run.act
            and not any(
                s.kind == SplitKind.ZONE and s.act == act and s.name == event.zone
                for s in run.splits
            )
        ):
            self._split(run, SplitKind.ZONE, event.zone, event.ts)
        self.store.save_campaign_run(run)

    def _drop_mule(self, run: CampaignRun, mule: str) -> None:
        """The timer keeps running; the mule's level-ups, zones and deaths don't count, so the
        real character's first visits are its splits. The real character is already in its
        first zone (it levels there), so that visit is kept."""
        run.mule = mule
        run.splits = [s for s in run.splits if s.kind == SplitKind.ACT]
        run.deaths = 0
        run.level = None
        if self._last_zone is not None:
            zone, act, at = self._last_zone
            run.splits.append(Split(SplitKind.ZONE, zone, act, at, run.rta(at), run.igt(at)))

    def _levelled(self, run: CampaignRun, event: LevelUp) -> None:
        if run.character is None:
            run.character = event.character
        elif event.character.casefold() != run.character.casefold():
            if run.level is None and run.mule is None and is_mule(event, run):
                # The run was started for a named character and a Ranger mule levelled first.
                self._drop_mule(run, event.character)
                self.store.save_campaign_run(run)
                return
            elif (
                run.level is not None
                and run.mule is None
                and is_mule_of(run)
                and event.level <= MULE_MAX_LEVEL
            ):  # a freshly made character
                # The run's character was a Ranger mule; this is the real character.
                self._drop_mule(run, run.character)
                run.character = event.character
            elif (
                run.mule is not None
                and event.character.casefold() == run.mule.casefold()
                and event.level > MULE_MAX_LEVEL
            ):
                # The "mule" kept levelling: it was the real character after all.
                run.character, run.mule = event.character, None
            else:
                return  # another character
        run.character = event.character
        run.char_class = event.character_class
        run.level = event.level
        self._split(run, SplitKind.LEVEL, f"Level {event.level}", event.ts, event.level)
        self.store.save_campaign_run(run)


# --- views ---


def personal_best(runs: list[CampaignRun], base: str | None = None) -> CampaignRun | None:
    """The fastest finished run; of base class `base` when given."""
    done = [r for r in runs if r.status == "finished" and r.ended_at is not None
            and (base is None or base_class(r.char_class) == base)]  # fmt: skip
    return min(done, key=lambda r: r.rta(r.started_at), default=None)  # ended: rta is fixed


# Runs that got past Act 1 count for the best split times, finished or not.
BESTS_MIN_ACT = 2


def counts_for_bests(run: CampaignRun) -> bool:
    return run.status != "running" and (run.act or 0) >= BESTS_MIN_ACT


@dataclass(frozen=True)
class Bests:
    """Your fastest time at each split, over runs of one base class that got past Act 1."""

    base: str
    runs: int  # how many runs these come from
    acts: dict[str, Split]  # "Act N" -> fastest time to finish that act
    act_segments: dict[str, float]  # "Act N" -> fastest time spent in that act
    zones: dict[tuple[int | None, str], Split]  # (act, zone) -> fastest time to first get there


def best_splits(runs: Iterable[CampaignRun], base: str) -> Bests:
    """`runs` need their splits; those of another base class, or that didn't get past Act 1,
    are skipped."""
    acts: dict[str, Split] = {}
    segments: dict[str, float] = {}
    zones: dict[tuple[int | None, str], Split] = {}
    count = 0
    for run in runs:
        if base_class(run.char_class) != base or not counts_for_bests(run):
            continue
        count += 1
        prev = 0.0
        for s in run.act_splits():
            if s.name not in acts or s.rta < acts[s.name].rta:
                acts[s.name] = s
            segment = s.rta - prev
            prev = s.rta
            if s.name not in segments or segment < segments[s.name]:
                segments[s.name] = segment
        for s in run.splits:
            key = (s.act, s.name)
            if s.kind == SplitKind.ZONE and (key not in zones or s.rta < zones[key].rta):
                zones[key] = s
    return Bests(base, count, acts, segments, zones)


def best_per_class(runs: list[CampaignRun]) -> dict[str, int]:
    """Base class -> id of its fastest finished run."""
    out: dict[str, int] = {}
    for base in {base_class(r.char_class) for r in runs} - {None}:
        pb = personal_best(runs, base)
        if pb is not None and pb.id is not None and base is not None:
            out[base] = pb.id
    return out


def run_summary(run: CampaignRun, now: datetime) -> dict[str, Any]:
    return {
        "id": run.id,
        "status": run.status,
        "started_at": run.started_at.isoformat(),
        "ended_at": run.ended_at.isoformat() if run.ended_at else None,
        "character": run.character,
        "class": run.char_class,
        "base_class": base_class(run.char_class),
        "level": run.level,
        "deaths": run.deaths,
        "act": run.act,
        "rta": round(run.rta(now), 1),
        "igt": round(run.igt(now), 1),
        "load_seconds": round(run.load_ms / 1000, 1),
        "paused": run.paused,
        "mule": run.mule,
        "notes": run.notes,
    }


def run_detail(run: CampaignRun, now: datetime, bests: Bests | None) -> dict[str, Any]:
    """The summary plus splits; act and zone splits carry the difference to `bests` (the
    fastest times there over the other runs of the same base class that got past Act 1)."""
    pb_acts = bests.acts if bests is not None else {}
    pb_zones = bests.zones if bests is not None else {}
    counted = bests.runs if bests is not None else 0

    def compared(s: Split, best: Split | None) -> dict[str, Any]:
        return {**s.to_json(), "pb_rta": best.rta if best else None,
                "delta": round(s.rta - best.rta, 1) if best else None}  # fmt: skip

    return {
        **run_summary(run, now),
        "pb_runs": counted,  # how many runs the PB times come from
        "pb_class": bests.base if bests is not None and counted else None,
        "acts": [compared(s, pb_acts.get(s.name)) for s in run.act_splits()],
        "zones": [
            compared(s, pb_zones.get((s.act, s.name)))
            for s in run.splits
            if s.kind == SplitKind.ZONE
        ],
        "levels": [s.to_json() for s in run.splits if s.kind == SplitKind.LEVEL],
        "pb_acts": [
            {
                "name": name,
                "rta": s.rta,
                "igt": s.igt,
                "segment": round(bests.act_segments[name], 3) if bests is not None else None,
            }
            for name, s in sorted(pb_acts.items(), key=lambda kv: kv[1].act or 0)
        ],
    }
