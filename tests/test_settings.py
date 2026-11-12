from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from poefire.api.session import SessionApi, SessionStore
from poefire.config import Config
from poefire.pricing.ninja import NinjaClient, NinjaError, PriceLine
from poefire.pricing.service import PriceService
from poefire.store import Store
from poefire.web.app import create_app
from poefire.web.runtime import AppRuntime

COOKIE = "0123456789abcdef0123456789abcdef"
H = {"X-Poefire": "1"}


class OfflineNinja(NinjaClient):
    def __init__(self) -> None:
        super().__init__("test")

    def fetch(self, source: str, league: str, type_: str) -> list[PriceLine]:
        raise NinjaError("offline")

    def leagues(self) -> list[str]:
        return ["Keepers", "Standard"]


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, AppRuntime, Path]:
    monkeypatch.delenv(SessionStore.ENV, raising=False)
    db = tmp_path / "db.sqlite3"
    cfg_path = tmp_path / "config.toml"
    log = tmp_path / "Client.txt"
    log.write_text("")
    cfg = Config(db_path=db, session_path=tmp_path / "session.json", log_path=log,
                 league="Keepers")  # fmt: skip
    rt = AppRuntime(cfg, PriceService(OfflineNinja(), lambda: Store(db)), config_path=cfg_path)
    # a league change refreshes prices in a background thread; keep tests free of stray threads
    monkeypatch.setattr(rt, "refresh_prices", lambda force=False: None)
    client = TestClient(create_app(rt))
    client.headers["host"] = "127.0.0.1:8460"
    return client, rt, tmp_path


def test_get_settings_initial(env: tuple[TestClient, AppRuntime, Path]) -> None:
    client, _, tmp = env
    s = client.get("/api/settings").json()
    assert s["log_path"] == str(tmp / "Client.txt") and s["log_path_exists"]
    assert s["session_saved"] is False and s["account_name"] == ""
    assert s["loot"]["enabled"] is False and "account_name" in s["loot"]["reason"]


def test_save_settings_writes_config_and_applies_live(
    env: tuple[TestClient, AppRuntime, Path],
) -> None:
    client, rt, tmp = env
    game = tmp / "Path of Exile"
    (game / "logs").mkdir(parents=True)
    (game / "logs" / "Client.txt").write_text("")
    r = client.post("/api/settings", headers=H, json={
        "log_path": f'"{game}"',  # pasted folder, with quotes from "Copy as path"
        "account_name": "Exile#1234",
        "league": "Standard",
        "stash_tabs": ["Dump", " Currency ", "Dump", ""],
        "characters": ["xyz_bow"],
        "snapshot_on_run_start": False,
        "snapshot_delay": 5,
    })  # fmt: skip
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["warnings"] == []
    assert body["log_path"] == str(game / "logs" / "Client.txt")
    assert body["stash_tabs"] == ["Dump", "Currency"]
    assert body["league"] == "Standard"
    # applied without a restart
    assert rt.log_path == game / "logs" / "Client.txt"
    assert rt.service is not None and rt.service.log_path == rt.log_path
    assert rt.service.tracker.characters == frozenset({"xyz_bow"})
    # persisted
    saved = Config.from_dict(tomllib.loads((tmp / "config.toml").read_text()))
    assert saved.account_name == "Exile#1234" and saved.snapshot_delay == 5.0
    assert saved.snapshot_on_run_start is False and saved.stash_tabs == ["Dump", "Currency"]


def test_settings_warnings_and_validation(env: tuple[TestClient, AppRuntime, Path]) -> None:
    client, _, tmp = env
    r = client.post("/api/settings", headers=H,
                    json={"log_path": str(tmp / "nope.txt"), "account_name": "Exile"})  # fmt: skip
    assert len(r.json()["warnings"]) == 2
    assert client.post("/api/settings", headers=H, json={"league": " "}).status_code == 400
    assert client.post("/api/settings", headers=H, json={"snapshot_delay": 999}).status_code == 422
    # empty log path = auto-detect
    assert client.post("/api/settings", headers=H, json={"log_path": ""}).json()["log_path"] == ""


def test_session_is_write_only_and_enables_loot(
    env: tuple[TestClient, AppRuntime, Path],
) -> None:
    client, rt, tmp = env
    client.post("/api/settings", headers=H, json={"account_name": "Exile#1234"})
    r = client.post("/api/settings/session", headers=H, json={"poesessid": f"POESESSID={COOKIE}"})
    assert r.status_code == 200
    assert COOKIE not in r.text
    assert r.json()["session_saved"] is True and r.json()["loot"]["enabled"] is True
    assert SessionStore(tmp / "session.json").load() == COOKIE
    assert rt.recorder is not None and isinstance(rt.recorder.snapshotter.api, SessionApi)
    assert COOKIE not in client.get("/api/settings").text

    r = client.post("/api/settings/session/clear", headers=H)
    assert r.json()["session_saved"] is False and r.json()["loot"]["enabled"] is False
    assert rt.recorder is None


def test_bad_session_value_rejected(env: tuple[TestClient, AppRuntime, Path]) -> None:
    client, _, tmp = env
    r = client.post("/api/settings/session", headers=H, json={"poesessid": "abc def; x=1"})
    assert r.status_code == 400
    assert not (tmp / "session.json").exists()
    r = client.post("/api/settings/session", headers=H, json={"poesessid": "short"})
    assert r.status_code == 200 and r.json()["warnings"]


def test_settings_post_requires_header(env: tuple[TestClient, AppRuntime, Path]) -> None:
    client, _, _ = env
    assert client.post("/api/settings/session", json={"poesessid": COOKIE}).status_code == 403


class FakeAccount:
    def list_characters(self) -> list[dict[str, Any]]:
        return [{"name": "xyz_bow", "league": "Keepers", "level": 67, "class": "Deadeye",
                 "current": True}]  # fmt: skip

    def list_stashes(self, league: str) -> list[dict[str, Any]]:
        assert league == "Keepers"
        return [{"id": "a", "name": "Dump", "type": "PremiumStash"}]


def test_check_account(
    env: tuple[TestClient, AppRuntime, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, _ = env
    r = client.post("/api/settings/check-account", headers=H).json()
    assert r["ok"] is False and "account_name" in r["error"]
    monkeypatch.setattr("poefire.wiring.account_api", lambda cfg: FakeAccount())
    r = client.post("/api/settings/check-account", headers=H).json()
    assert r["ok"] and r["characters"][0]["name"] == "xyz_bow"
    assert r["tabs"] == [{"id": "a", "name": "Dump", "type": "PremiumStash"}]


def test_config_toml_roundtrip(tmp_path: Path) -> None:
    cfg = Config(log_path=Path(r"C:\Program Files (x86)\Path of Exile\logs\Client.txt"),
                 stash_tabs=['Quote"s', "Dump"], snapshot_on_run_start=False,
                 account_name="Exile#1234")  # fmt: skip
    path = cfg.save(tmp_path / "config.toml")
    assert Config.load(path) == cfg


def test_death_names_seen_in_log_and_unknown_name_warning(
    env: tuple[TestClient, AppRuntime, Path],
) -> None:
    from datetime import datetime

    from poefire.events import LevelUp, PlayerSlain

    client, rt, _ = env
    with Store(rt.cfg.db_path) as s, s.transaction():
        s.add_event(PlayerSlain(datetime(2026, 11, 1, 16, 0), "xyz_bow"), "run1")
        s.add_event(PlayerSlain(datetime(2026, 11, 1, 16, 5), "xyz_bow"), None)
        s.add_event(LevelUp(datetime(2026, 11, 1, 16, 6), "alt_char", "Witch", 12), None)
    seen = {c["name"]: c for c in client.get("/api/settings").json()["log_characters"]}
    assert seen["xyz_bow"]["deaths"] == 2 and seen["xyz_bow"]["in_maps"] == 1
    assert seen["alt_char"]["deaths"] == 0 and all(c["counted"] for c in seen.values())

    r = client.post("/api/settings", headers=H, json={"characters": ["MyAccount#1234"]}).json()
    assert any("MyAccount#1234" in w and "character name" in w for w in r["warnings"])
    seen = {c["name"]: c for c in r["log_characters"]}
    assert not seen["xyz_bow"]["counted"]

    r = client.post("/api/settings", headers=H, json={"characters": ["XYZ_BOW"]}).json()
    assert r["warnings"] == [] and {c["name"]: c for c in r["log_characters"]}["xyz_bow"]["counted"]


def test_campaign_hotkey_setting(env: tuple[TestClient, AppRuntime, Path]) -> None:
    client, rt, _ = env
    assert client.get("/api/settings").json()["campaign_hotkey"] == "Ctrl+Alt+R"
    r = client.post("/api/settings", headers=H, json={"campaign_hotkey": "shift+f9"})
    assert r.json()["campaign_hotkey"] == "Shift+F9" and rt.cfg.campaign_hotkey == "Shift+F9"
    bad = client.post("/api/settings", headers=H, json={"campaign_hotkey": "R"})
    assert bad.status_code == 400 and "modifier" in bad.json()["detail"]
    off = client.post("/api/settings", headers=H, json={"campaign_hotkey": ""})
    assert off.json()["campaign_hotkey"] == ""


class WrongLeagueAccount(FakeAccount):
    def __init__(self, ok: set[str]) -> None:
        self.ok = ok
        self.asked: list[str] = []

    def list_stashes(self, league: str) -> list[dict[str, Any]]:
        from poefire.api.session import LeagueError

        self.asked.append(league)
        if league not in self.ok:
            raise LeagueError(league, "HTTP 403 from /character-window/get-stash-items")
        return [{"id": "s", "name": f"{league} dump", "type": "PremiumStash"}]


@pytest.mark.parametrize(
    ("ok", "tabs_league"), [({"Keepers"}, "Keepers"), ({"Standard"}, "Standard"), (set(), None)]
)
def test_check_account_with_a_wrong_league(
    env: tuple[TestClient, AppRuntime, Path], monkeypatch: pytest.MonkeyPatch,
    ok: set[str], tabs_league: str | None,
) -> None:  # fmt: skip
    client, rt, _ = env
    rt.set_league("Typo League")
    monkeypatch.setattr(rt, "default_league", lambda: "Keepers")  # newest on poe.ninja
    acc = WrongLeagueAccount(ok)
    monkeypatch.setattr("poefire.wiring.account_api", lambda cfg: acc)
    r = client.post("/api/settings/check-account", headers=H).json()
    assert r["ok"] and r["characters"][0]["name"] == "xyz_bow"  # the cookie works
    assert r["tabs_league"] == tabs_league
    assert "'Typo League'" in r["league_note"]
    assert acc.asked[0] == "Typo League"
    if tabs_league:
        assert r["tabs"][0]["name"] == f"{tabs_league} dump"
        assert r["league_note"].endswith(f"Showing the stash tabs of {tabs_league} instead.")
    else:
        assert r["tabs"] == [] and acc.asked == ["Typo League", "Keepers", "Standard"]


def test_first_start_picks_the_newest_league(
    env: tuple[TestClient, AppRuntime, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, rt, _ = env
    monkeypatch.setattr(rt, "ninja_leagues", lambda: ["Keepers", "Hardcore Keepers", "Standard"])
    rt._league_chosen = False  # as on a first start without a league in config.toml
    rt._pick_default_league()
    assert rt.league == "Keepers"
    rt.set_league("Standard")  # the user's choice sticks
    rt._pick_default_league()
    assert rt.league == "Standard"
    monkeypatch.setattr(rt, "ninja_leagues", lambda: [])  # poe.ninja unreachable
    assert rt.default_league() == "Standard"
