from datetime import datetime
from pathlib import Path

from poefire.cli import main
from poefire.service import TrackerService
from poefire.store import Store
from poefire.tracker import RunStatus

SAMPLE = Path(__file__).parent / "data" / "sample_client.txt"


def replay(tmp_path: Path) -> tuple[Store, TrackerService]:
    store = Store(tmp_path / "db.sqlite3")
    svc = TrackerService(store, SAMPLE, characters=["TestWitch"])
    svc.start(from_beginning=True)
    svc.poll()
    return store, svc


def test_replay_sample_log(tmp_path: Path) -> None:
    store, svc = replay(tmp_path)
    runs = list(reversed(store.list_runs()))
    assert [r.map_name for r in runs] == ["Strand", "Cemetery"]
    strand, cemetery = runs
    assert strand.status is RunStatus.FINISHED
    assert strand.area_level == 83
    assert strand.deaths == 1
    # 20:01:05-20:06:00 (incl. side area) + 20:09:02-20:09:58
    assert strand.active_seconds() == (4 * 60 + 55) + 56
    assert len(strand.segments) == 2
    assert cemetery.status is RunStatus.PAUSED
    assert cemetery.active_seconds() == 3 * 60 + 55
    assert svc.tracker.run is not None and svc.tracker.run.id == cemetery.id

    whispers = store.list_events(kind="Whisper")
    assert {w["direction"] for w in whispers} == {"from", "to"}
    deaths = store.list_events(kind="PlayerSlain")
    assert deaths[0]["run_id"] == strand.id
    assert store.list_events(kind="LevelUp")[0]["level"] == 92


def test_restart_resumes_from_cursor(tmp_path: Path) -> None:
    log = tmp_path / "Client.txt"
    lines = SAMPLE.read_text().splitlines(keepends=True)
    log.write_text("".join(lines[:9]))  # stop inside the Strand side area
    db = tmp_path / "db.sqlite3"

    store = Store(db)
    svc = TrackerService(store, log)
    svc.start(from_beginning=True)
    svc.poll()
    store.close()

    with open(log, "a") as f:
        f.write("".join(lines[9:]))  # played on while the tracker was closed

    store = Store(db)
    svc = TrackerService(store, log)
    svc.start()
    svc.poll()
    runs = list(reversed(store.list_runs()))
    assert [r.map_name for r in runs] == ["Strand", "Cemetery"]
    assert runs[0].active_seconds() == (4 * 60 + 55) + 56


def test_fresh_start_bootstraps_from_last_generation(tmp_path: Path) -> None:
    log = tmp_path / "Client.txt"
    lines = SAMPLE.read_text().splitlines(keepends=True)
    log.write_text("".join(lines[:24]))  # currently inside Cemetery
    store = Store(tmp_path / "db.sqlite3")
    svc = TrackerService(store, log)
    svc.start()
    svc.poll()
    assert svc.tracker.run is not None and svc.tracker.run.map_name == "Cemetery"
    assert [r.map_name for r in store.list_runs()] == ["Cemetery"]


def test_replaced_log_finishes_stale_run(tmp_path: Path) -> None:
    log = tmp_path / "Client.txt"
    lines = SAMPLE.read_text().splitlines(keepends=True)
    log.write_text("".join(lines[:7]))  # inside Strand
    db = tmp_path / "db.sqlite3"
    store = Store(db)
    svc = TrackerService(store, log)
    svc.start(from_beginning=True)
    svc.poll()
    store.close()

    log.write_text("2024/07/28 10:00:00 ***** LOG FILE OPENING *****\n")
    store = Store(db)
    svc = TrackerService(store, log)
    svc.start()
    svc.poll()
    (run,) = store.list_runs()
    assert run.status is RunStatus.FINISHED
    assert svc.tracker.run is None


def test_cli_replay(capsys: object, tmp_path: Path) -> None:
    assert main(["replay", str(SAMPLE)]) == 0
    out = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "Strand" in out and "Cemetery" in out and "2 runs" in out


REAL_LOG = Path(__file__).parent / "data" / "real_client_2026-11-01.txt"


def test_replay_real_log(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    svc = TrackerService(store, REAL_LOG, characters=["xyz_bow"])
    svc.start(from_beginning=True)
    svc.poll()
    runs = list(reversed(store.list_runs()))
    summary = [(r.area_id, r.area_level, int(r.active_seconds()), len(r.segments), r.deaths)
               for r in runs]  # fmt: skip
    assert summary == [
        # died, went to hideout, came back through the same portal (same seed)
        ("MapWorldsArachnidNest", 69, 44 + 77, 2, 1),
        ("MapWorldsCells", 68, 77, 1, 0),
        ("MapWorldsSprings", 69, 82 + 20, 2, 1),
        ("MapWorldsMesa", 70, 115, 1, 0),
        # suicide + slain; closed by "Closing game gracefully" while in hideout
        ("MapWorldsSpiderLair", 70, 10 + 70 + 52, 3, 2),
    ]
    assert [r.map_name for r in runs][2] == "Fungal Hollow"  # zone name, not the area id
    assert all(r.status is RunStatus.FINISHED for r in runs)
    assert runs[-1].ended_at == datetime(2026, 11, 1, 16, 7, 51)
    assert svc.tracker.run is None
    # the death before the first logged map generation has no run to belong to
    deaths = store.list_events(kind="PlayerSlain", limit=10)
    assert [d["run_id"] is None for d in reversed(deaths)] == [True, False, False, False, False]
    assert len(store.list_events(kind="TradeAccepted", limit=20)) == 7
