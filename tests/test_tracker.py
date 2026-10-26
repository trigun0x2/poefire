from __future__ import annotations

import itertools
from datetime import datetime, timedelta

from poefire.events import AreaGenerated, LogEvent, LogOpened, PlayerSlain, ZoneEntered
from poefire.tracker import (
    NoticeKind,
    RunNotice,
    RunStatus,
    RunTracker,
    SafeZoneEntered,
    state_from_dict,
    state_to_dict,
)

T0 = datetime(2024, 7, 27, 20, 0, 0)


class Script:
    """Builds event sequences with a moving clock. Loads take 2 seconds."""

    def __init__(self) -> None:
        self.t = T0
        self.events: list[LogEvent] = []

    def wait(self, seconds: float) -> Script:
        self.t += timedelta(seconds=seconds)
        return self

    def go(self, area_id: str, zone: str, seed: int, level: int = 83) -> Script:
        self.events.append(AreaGenerated(self.t, level, area_id, seed))
        self.t += timedelta(seconds=2)
        self.events.append(ZoneEntered(self.t, zone))
        return self

    def hideout(self) -> Script:
        return self.go("HideoutFelled", "Felled Hideout", 1, level=1)

    def die(self, who: str = "Me") -> Script:
        self.events.append(PlayerSlain(self.t, who))
        return self


def tracker(characters: tuple[str, ...] = ()) -> RunTracker:
    counter = itertools.count(1)
    return RunTracker(characters=characters, new_id=lambda: f"run{next(counter)}")


def feed(t: RunTracker, events: list[LogEvent]) -> list[RunNotice]:
    out: list[RunNotice] = []
    for e in events:
        out.extend(n for n in t.handle(e) if isinstance(n, RunNotice))
    return out


def kinds(notices: list[RunNotice]) -> list[tuple[str, NoticeKind]]:
    return [(n.run.id, n.kind) for n in notices]


def test_hideout_pauses_and_same_seed_resumes() -> None:
    s = (
        Script()
        .hideout().wait(10)
        .go("MapWorldsStrand", "Strand", 111).wait(120)
        .hideout().wait(60)
        .go("MapWorldsStrand", "Strand", 111).wait(30)
        .hideout()
    )  # fmt: skip
    t = tracker()
    notices = feed(t, s.events)
    assert kinds(notices) == [
        ("run1", NoticeKind.STARTED),
        ("run1", NoticeKind.PAUSED),
        ("run1", NoticeKind.RESUMED),
        ("run1", NoticeKind.PAUSED),
    ]
    run = t.run
    assert run is not None
    assert run.status is RunStatus.PAUSED
    assert run.active_seconds() == 150  # loads and hideout time excluded
    assert len(run.segments) == 2


def test_new_map_finishes_previous_run() -> None:
    s = (
        Script()
        .go("MapWorldsStrand", "Strand", 111).wait(100)
        .hideout().wait(20)
        .go("MapWorldsCemetery", "Cemetery", 222).wait(50)
    )  # fmt: skip
    t = tracker()
    notices = feed(t, s.events)
    assert kinds(notices) == [
        ("run1", NoticeKind.STARTED),
        ("run1", NoticeKind.PAUSED),
        ("run1", NoticeKind.FINISHED),
        ("run2", NoticeKind.STARTED),
    ]
    finished = notices[2].run
    assert finished.status is RunStatus.FINISHED
    assert finished.active_seconds() == 100
    assert finished.ended_at == T0 + timedelta(seconds=102)  # left the map, not next map start
    assert t.run is not None and t.run.map_name == "Cemetery"


def test_same_map_new_seed_is_new_run() -> None:
    s = (
        Script()
        .go("MapWorldsStrand", "Strand", 111).wait(10)
        .hideout()
        .go("MapWorldsStrand", "Strand", 999)
    )  # fmt: skip
    t = tracker()
    feed(t, s.events)
    assert t.run is not None and t.run.id == "run2" and t.run.seed == 999


def test_side_area_counts_as_active() -> None:
    s = (
        Script()
        .go("MapWorldsStrand", "Strand", 111).wait(60)
        .go("AbyssalDepths", "Abyssal Depths", 5).wait(30)
        .go("MapWorldsStrand", "Strand", 111).wait(10)
        .hideout()
    )  # fmt: skip
    t = tracker()
    notices = feed(t, s.events)
    assert kinds(notices) == [("run1", NoticeKind.STARTED), ("run1", NoticeKind.PAUSED)]
    assert t.run is not None
    # One continuous segment: side-area loads stay inside the run.
    assert len(t.run.segments) == 1
    assert t.run.active_seconds() == 60 + 2 + 30 + 2 + 10


def test_map_like_side_area_does_not_split_run() -> None:
    s = (
        Script()
        .go("MapWorldsStrand", "Strand", 111).wait(60)
        .go("MapSideArea_Vaal", "Vaal Temple", 5).wait(30)
        .go("MapWorldsStrand", "Strand", 111)
    )  # fmt: skip
    t = tracker()
    notices = feed(t, s.events)
    assert kinds(notices) == [("run1", NoticeKind.STARTED)]
    assert t.run is not None and t.run.area_id == "MapWorldsStrand"


def test_other_zone_from_hideout_keeps_run_paused() -> None:
    s = (
        Script()
        .go("MapWorldsStrand", "Strand", 111).wait(60)
        .hideout()
        .go("1_1_2", "The Twilight Strand", 7, level=1).wait(300)
    )  # fmt: skip
    t = tracker()
    feed(t, s.events)
    assert t.run is not None and t.run.status is RunStatus.PAUSED
    assert not t.in_run
    assert t.run.active_seconds() == 60


def test_deaths_only_count_inside_run_and_for_tracked_characters() -> None:
    s = (
        Script()
        .hideout().die("Me")
        .go("MapWorldsStrand", "Strand", 111).wait(5)
        .die("Me").die("PartyMember").wait(5)
        .die("Me")
    )  # fmt: skip
    t = tracker(characters=("Me",))
    feed(t, s.events)
    assert t.run is not None and t.run.deaths == 2

    t_all = tracker()
    feed(t_all, s.events)
    assert t_all.run is not None and t_all.run.deaths == 3


def test_log_opened_finishes_run_at_last_seen_event() -> None:
    s = Script().go("MapWorldsStrand", "Strand", 111).wait(40).die()
    t = tracker()
    feed(t, s.events)
    notices = feed(t, [LogOpened(s.t + timedelta(hours=5))])
    assert kinds(notices) == [("run1", NoticeKind.FINISHED)]
    assert notices[0].run.active_seconds() == 40
    assert t.run is None and t.state.current is None


def test_safe_zone_notice_carries_run_id() -> None:
    s = Script().go("MapWorldsStrand", "Strand", 111).wait(5).hideout()
    t = tracker()
    safe = [n for e in s.events for n in t.handle(e) if isinstance(n, SafeZoneEntered)]
    assert len(safe) == 1 and safe[0].run_id == "run1" and safe[0].zone == "Felled Hideout"


def test_zone_entered_without_generation_uses_name_fallback() -> None:
    t = tracker()
    s = Script().go("MapWorldsStrand", "Strand", 111).wait(10)
    feed(t, s.events)
    feed(t, [ZoneEntered(s.t, "Felled Hideout")])
    assert t.run is not None and t.run.status is RunStatus.PAUSED


def test_notice_runs_are_snapshots() -> None:
    s = Script().go("MapWorldsStrand", "Strand", 111).wait(10).die()
    t = tracker()
    notices = feed(t, s.events)
    assert notices[0].run.deaths == 0
    assert t.run is not None and t.run.deaths == 1


def test_state_roundtrip_mid_run() -> None:
    s = Script().go("MapWorldsStrand", "Strand", 111).wait(10).die().hideout()
    t = tracker()
    feed(t, s.events)
    restored = RunTracker(state=state_from_dict(state_to_dict(t.state)))
    assert restored.state == t.state
    feed(restored, Script().go("MapWorldsStrand", "Strand", 111).events)
    assert restored.run is not None and restored.run.status is RunStatus.ACTIVE
