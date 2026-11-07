from datetime import datetime
from pathlib import Path

import pytest

from poefire.config import Config
from poefire.events import Whisper, WhisperDirection
from poefire.store import Store
from poefire.tracker import Run, RunStatus, Segment


def test_run_roundtrip() -> None:
    store = Store(":memory:")
    t = datetime(2024, 1, 1, 12)
    run = Run(
        id="r1", area_id="MapWorldsStrand", map_name="Strand", area_level=83, seed=5,
        started_at=t, status=RunStatus.PAUSED, deaths=2,
        segments=[Segment(t, t.replace(minute=3)), Segment(t.replace(minute=5))],
    )  # fmt: skip
    with store.transaction():
        store.upsert_run(run)
    assert store.get_run("r1") == run
    run.status = RunStatus.FINISHED
    run.segments[-1].end = t.replace(minute=6)
    run.ended_at = run.last_activity
    with store.transaction():
        store.upsert_run(run)
    loaded = store.get_run("r1")
    assert loaded == run
    assert loaded is not None and loaded.active_seconds() == 240


def test_events_and_kv() -> None:
    store = Store(":memory:")
    w = Whisper(datetime(2024, 1, 1), WhisperDirection.FROM, "Bob", None, "hi")
    with store.transaction():
        store.add_event(w, None)
        store.set_json("k", {"a": 1})
        store.set_json("k", {"a": 2})
    (ev,) = store.list_events()
    assert ev["kind"] == "Whisper" and ev["character"] == "Bob" and ev["direction"] == "from"
    assert store.get_json("k") == {"a": 2}
    assert store.get_json("missing") is None


def test_reopen_existing_db(tmp_path: Path) -> None:
    Store(tmp_path / "x.db").close()
    Store(tmp_path / "x.db").close()


def test_config_from_toml(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        "log_path = 'D:\\PoE\\logs\\Client.txt'\n"
        'characters = ["Me"]\n'
        'extra_map_prefixes = ["Heist"]\n'
    )
    cfg = Config.load(cfg_file)
    assert cfg.log_path == Path("D:\\PoE\\logs\\Client.txt")
    assert cfg.characters == ["Me"]
    assert cfg.zone_rules().classify("HeistDungeon") == "map"
    assert Config.load(tmp_path / "missing.toml") == Config()


def test_config_int_field_round_trips(tmp_path: Path) -> None:
    path = Config(overlay_opacity=65).save(tmp_path / "config.toml")
    cfg = Config.load(path)
    assert cfg.overlay_opacity == 65 and type(cfg.overlay_opacity) is int


def test_log_path_prefers_the_newest_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    from poefire import config

    old, new = tmp_path / "old" / "Client.txt", tmp_path / "new" / "Client.txt"
    for p, t in ((old, 1_000_000), (new, 2_000_000)):
        p.parent.mkdir()
        p.write_text("x")
        os.utime(p, (t, t))
    monkeypatch.setattr(config, "WINDOWS_LOG_CANDIDATES", (old, tmp_path / "missing.txt", new))
    assert Config().resolve_log_path() == new
    assert Config(log_path=old).resolve_log_path() == old  # a configured path always wins
    from poefire.gamepath import log_for_exe

    assert log_for_exe(Path("D:/Games/PoE/PathOfExile.exe")) == Path("D:/Games/PoE/logs/Client.txt")
