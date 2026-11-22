"""A new league: the banner offering to switch, and the check for missing stash tabs."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from poefire.config import Config
from poefire.loot.snapshot import Snapshotter
from poefire.web.app import create_app
from poefire.web.runtime import AppRuntime

H = {"X-Poefire": "1"}


class Account:
    def __init__(self) -> None:
        self.chars = [
            {"name": "OldMain", "league": "Keepers", "level": 95, "current": False},
            {"name": "Fresh", "league": "Newleague", "level": 12, "current": True},
        ]
        self.tabs = {"Keepers": [{"id": "a", "name": "Dump", "type": "NormalStash"}],
                     "Newleague": [{"id": "b", "name": "1", "type": "NormalStash"}]}  # fmt: skip

    def list_characters(self) -> list[dict[str, Any]]:
        return self.chars

    def list_stashes(self, league: str) -> list[dict[str, Any]]:
        return self.tabs.get(league, [])

    def get_character(self, name: str) -> dict[str, Any]:
        return {}

    def get_stash(self, league: str, stash_id: str, substash_id: str | None = None) -> Any:
        return {}


def test_league_hint_and_tabs(tmp_path: Path) -> None:
    cfg = Config(db_path=tmp_path / "db", league="Keepers", stash_tabs=["Dump"])
    rt = AppRuntime(cfg, recorder=None, log_path=tmp_path / "Client.txt")
    rt.refresh_prices = lambda force=False: None  # type: ignore[method-assign]
    account = Account()
    snapshotter = Snapshotter(account, "Keepers", ["Dump"])
    fake = SimpleNamespace(snapshotter=snapshotter, stop=lambda: None)
    rt.recorder = fake  # type: ignore[assignment]
    client = TestClient(create_app(rt))
    client.headers["host"] = "127.0.0.1:8460"
    try:
        rt.check_league()
        st = client.get("/api/status").json()
        assert st["league_hint"] == {"league": "Newleague", "current": "Keepers",
                                     "character": "Fresh", "level": 12}  # fmt: skip
        assert st["tabs_hint"] is None  # "Dump" is in Keepers

        client.post("/api/league", json={"league": "Newleague"}, headers=H)
        assert rt.league_hint is None
        rt.check_league()  # the new league has no "Dump" tab
        assert rt.league_hint is None
        assert rt.tabs_hint == {"league": "Newleague", "missing": ["Dump"], "none_found": True}

        account.chars[0]["current"], account.chars[1]["current"] = True, False  # back on OldMain
        rt.check_league()
        assert rt.league_hint is not None and rt.league_hint["league"] == "Keepers"
        assert client.post("/api/league/hint/dismiss", headers=H).json() == {"ok": True}
        rt.check_league()
        assert rt.league_hint is None  # not suggested again once dismissed
    finally:
        rt.stop()


def test_changing_watched_tabs(tmp_path: Path) -> None:
    """Per map, loot doesn't depend on the tabs: they're read at the next map start. In the
    interval mode, a snapshot is taken at once, so what's in a new tab isn't counted."""
    rt = AppRuntime(Config(db_path=tmp_path / "db", league="Keepers", stash_tabs=["Dump"]),
                    recorder=None, log_path=tmp_path / "Client.txt")  # fmt: skip
    rt.refresh_prices = lambda force=False: None  # type: ignore[method-assign]
    snaps: list[bool] = []
    soon: list[bool] = []
    rt.snapshot_now = lambda: snaps.append(True) or True  # type: ignore[method-assign,func-returns-value]
    rt.recorder = SimpleNamespace(stash_soon=lambda: soon.append(True), stop=lambda: None)  # type: ignore[assignment]
    client = TestClient(create_app(rt))
    client.headers["host"] = "127.0.0.1:8460"
    try:
        r = client.post("/api/settings", json={"stash_tabs": ["dump"]}, headers=H).json()
        assert r["snapshot"] is False and snaps == soon == []  # same tab (any case)
        r = client.post("/api/settings", json={"stash_tabs": ["Dump", "Currency"]}, headers=H)
        assert r.json()["snapshot"] is False and snaps == [] and soon == [True]
        client.post("/api/settings", json={"loot_mode": "interval"}, headers=H)
        r = client.post("/api/settings", json={"stash_tabs": ["Dump"]}, headers=H)
        assert r.json()["snapshot"] is True and snaps == [True] and soon == [True]
        r = client.post("/api/settings", json={"check_updates": False}, headers=H)
        assert r.json()["snapshot"] is False and snaps == [True]
    finally:
        rt.stop()


def test_setup_wizard_is_shown_until_done(tmp_path: Path) -> None:
    rt = AppRuntime(Config(db_path=tmp_path / "db"), recorder=None, log_path=tmp_path / "x.txt")
    client = TestClient(create_app(rt))
    client.headers["host"] = "127.0.0.1:8460"
    try:
        assert client.get("/api/settings").json()["onboarded"] is False
        assert client.post("/api/onboarding/done", headers=H).json() == {"ok": True}
        assert client.get("/api/settings").json()["onboarded"] is True
    finally:
        rt.stop()


def test_a_failed_league_check_is_tried_again_soon(tmp_path: Path) -> None:
    from poefire.web.runtime import LEAGUE_CHECK, LEAGUE_RETRY

    rt = AppRuntime(Config(db_path=tmp_path / "db", league="Keepers"), recorder=None,
                    log_path=tmp_path / "Client.txt")  # fmt: skip
    calls: list[bool] = []
    waits: list[float] = []

    def check(tabs: bool = False) -> None:
        calls.append(tabs)
        if len(calls) == 1:
            raise OSError("offline")

    class Now:
        def clear(self) -> None:
            pass

        def set(self) -> None:
            pass

        def wait(self, seconds: float) -> bool:
            waits.append(seconds)
            if len(waits) == 2:
                rt._stop.set()
            return False

    rt._pick_default_league = lambda: None  # type: ignore[method-assign]
    rt.check_league = check  # type: ignore[method-assign]
    rt._league_check_now = Now()  # type: ignore[assignment]
    rt._league_check_tabs = True  # Settings asked for the tabs to be checked
    stop_wait = rt._stop.wait
    rt._stop.wait = lambda timeout=None: False  # type: ignore[method-assign]
    try:
        rt._league_loop()
    finally:
        rt._stop.wait = stop_wait  # type: ignore[method-assign]
        rt.stop()
    assert waits == [LEAGUE_RETRY, LEAGUE_CHECK]
    assert calls == [True, True]  # the tab check asked for isn't dropped by the failure


def test_switching_league_watches_that_leagues_tabs(tmp_path: Path) -> None:
    cfg = Config(db_path=tmp_path / "db", league="Keepers", stash_tabs=["Dump"],
                 league_tabs=["Newleague|1", "Newleague|2"])  # fmt: skip
    rt = AppRuntime(cfg, recorder=None, log_path=tmp_path / "Client.txt")
    rt.refresh_prices = lambda force=False: None  # type: ignore[method-assign]
    snapshotter = Snapshotter(Account(), "Keepers", ["Dump"])
    rt.recorder = SimpleNamespace(snapshotter=snapshotter, stop=lambda: None)  # type: ignore[assignment]
    try:
        rt.set_league("Newleague")
        assert snapshotter.wanted == ["1", "2"] and snapshotter.league == "Newleague"
        rt.set_league("Keepers")
        assert snapshotter.wanted == ["Dump"]  # no picks of its own: stash_tabs
    finally:
        rt.stop()


def test_league_switches_by_itself_when_asked(tmp_path: Path) -> None:
    cfg = Config(db_path=tmp_path / "db", league="Keepers", stash_tabs=["Dump"],
                 league_autoswitch=True)  # fmt: skip
    rt = AppRuntime(cfg, recorder=None, log_path=tmp_path / "Client.txt")
    rt.refresh_prices = lambda force=False: None  # type: ignore[method-assign]
    snapshotter = Snapshotter(Account(), "Keepers", ["Dump"])
    rt.recorder = SimpleNamespace(snapshotter=snapshotter, stop=lambda: None)  # type: ignore[assignment]
    client = TestClient(create_app(rt))
    client.headers["host"] = "127.0.0.1:8460"
    try:
        rt.check_league()  # "Fresh" plays in Newleague
        st = client.get("/api/status").json()
        assert rt.league == "Newleague" and snapshotter.league == "Newleague"
        assert st["league_hint"] is None
        assert st["league_switched"] == {"league": "Newleague", "previous": "Keepers",
                                         "character": "Fresh", "level": 12}  # fmt: skip
        client.post("/api/league/switched/dismiss", headers=H)
        assert client.get("/api/status").json()["league_switched"] is None
    finally:
        rt.stop()


def test_mapping_checks_the_league_again(tmp_path: Path) -> None:
    from poefire.loot.recorder import LootResult, Trigger
    from poefire.loot.snapshot import OTHER_LEAGUE

    rt = AppRuntime(Config(db_path=tmp_path / "db", league="Keepers"), recorder=None,
                    log_path=tmp_path / "Client.txt")  # fmt: skip
    try:
        now = rt._league_check_now
        rt._league_checked_at = time.monotonic()  # just checked
        rt._on_loot(LootResult(Trigger.RUN_START, "r1", 1, True, True))
        assert not now.is_set()
        rt._league_checked_at -= 11 * 60  # a map starts 10+ minutes later: checked again
        rt._on_loot(LootResult(Trigger.RUN_START, "r2", 2, True, True))
        assert now.is_set()
        now.clear()
        rt._league_checked_at = time.monotonic() - 3 * 60
        rt._on_loot(
            LootResult(
                Trigger.RUN_RETURN,
                "r2",
                3,
                False,
                False,
                error=f"character Fresh is in Newleague, not Keepers{OTHER_LEAGUE}",
            )
        )
        assert now.is_set()  # the character plays elsewhere: checked at once
    finally:
        rt.stop()
