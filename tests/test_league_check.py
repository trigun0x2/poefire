"""A new league: the banner offering to switch, and the check for missing stash tabs."""

from __future__ import annotations

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


def test_changing_watched_tabs_takes_a_snapshot(tmp_path: Path) -> None:
    rt = AppRuntime(Config(db_path=tmp_path / "db", league="Keepers", stash_tabs=["Dump"]),
                    recorder=None, log_path=tmp_path / "Client.txt")  # fmt: skip
    rt.refresh_prices = lambda force=False: None  # type: ignore[method-assign]
    snaps: list[bool] = []
    rt.snapshot_now = lambda: snaps.append(True) or True  # type: ignore[method-assign,func-returns-value]
    client = TestClient(create_app(rt))
    client.headers["host"] = "127.0.0.1:8460"
    try:
        r = client.post("/api/settings", json={"stash_tabs": ["dump"]}, headers=H).json()
        assert r["snapshot"] is False and snaps == []  # same tab (any case): nothing to do
        r = client.post("/api/settings", json={"stash_tabs": ["Dump", "Currency"]}, headers=H)
        assert r.json()["snapshot"] is True and snaps == [True]
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
