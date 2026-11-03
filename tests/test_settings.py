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
