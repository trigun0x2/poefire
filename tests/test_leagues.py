from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from poefire.api.session import SessionStore
from poefire.config import Config
from poefire.leagues import default_price_league, is_private
from poefire.pricing.ninja import NinjaClient, NinjaError, PriceLine
from poefire.pricing.service import PriceService
from poefire.store import Store
from poefire.web.app import create_app
from poefire.web.runtime import AppRuntime

H = {"X-Poefire": "1"}
NINJA = ["Keepers", "Hardcore Keepers", "SSF Keepers", "Standard", "Hardcore"]


@pytest.mark.parametrize(
    ("league", "private"),
    [("My League (PL12345)", True), ("Fire Party (PL7) ", True), ("Keepers", False),
     ("PL12345", False), ("Hardcore Keepers", False)],
)  # fmt: skip
def test_is_private(league: str, private: bool) -> None:
    assert is_private(league) is private


def test_default_price_league() -> None:
    assert default_price_league("Keepers", NINJA) == "Keepers"
    assert default_price_league("Hardcore Keepers", NINJA) == "Hardcore Keepers"
    assert default_price_league("My League (PL1)", NINJA) == "Keepers"
    assert default_price_league("My League (PL1)", ["Standard", "Hardcore"]) == "Standard"
    assert default_price_league("My League (PL1)", []) == "Standard"


class Ninja(NinjaClient):
    def __init__(self) -> None:
        super().__init__("test")
        self.league_calls = 0

    def fetch(self, source: str, league: str, type_: str) -> list[PriceLine]:
        raise NinjaError("offline")

    def leagues(self) -> list[str]:
        self.league_calls += 1
        return NINJA


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, AppRuntime]:
    monkeypatch.delenv(SessionStore.ENV, raising=False)
    db = tmp_path / "db.sqlite3"
    with Store(db) as s, s.transaction():
        s.save_prices("Keepers", "exchange", "Currency", 1e12, [PriceLine("Divine Orb", 150.0)])
        s.save_prices("Hardcore Keepers", "exchange", "Currency", 1e12,
                      [PriceLine("Divine Orb", 400.0)])  # fmt: skip
    cfg = Config(db_path=db, session_path=tmp_path / "s.json", league="Keepers")
    rt = AppRuntime(
        cfg, PriceService(Ninja(), lambda: Store(db)),
        config_path=tmp_path / "config.toml", log_path=tmp_path / "none.txt",
    )  # fmt: skip
    monkeypatch.setattr(rt, "refresh_prices", lambda force=False: None)
    client = TestClient(create_app(rt))
    client.headers["host"] = "127.0.0.1:8460"
    return client, rt


def test_private_league_prices_from_challenge_league(env: tuple[TestClient, AppRuntime]) -> None:
    client, _ = env
    assert client.post("/api/league", headers=H,
                       json={"league": "Fire Party (PL12345)"}).status_code == 200  # fmt: skip
    s = client.get("/api/status").json()
    assert s["league"] == "Fire Party (PL12345)" and s["league_is_private"] is True
    assert s["prices"]["league"] == "Keepers"
    assert s["prices"]["chaos_per_divine"] == 150.0
    assert "Fire Party (PL12345)" in client.get("/api/leagues").json()["options"]


def test_price_league_override(env: tuple[TestClient, AppRuntime]) -> None:
    client, _ = env
    body_in = {"league": "HC Party (PL9)", "price_league": "Hardcore Keepers"}
    r = client.post("/api/settings", headers=H, json=body_in)
    assert r.status_code == 200
    body = r.json()
    assert body["price_league"] == "Hardcore Keepers"
    assert body["price_league_effective"] == "Hardcore Keepers"
    assert client.get("/api/status").json()["prices"]["chaos_per_divine"] == 400.0
    # a private league can't be a price source
    r = client.post("/api/settings", headers=H, json={"price_league": "Other (PL1)"})
    assert r.status_code == 400
    # back to automatic
    body = client.post("/api/settings", headers=H, json={"price_league": ""}).json()
    assert body["price_league"] == "" and body["price_league_effective"] == "Keepers"


def test_public_league_prices_from_itself(env: tuple[TestClient, AppRuntime]) -> None:
    client, _ = env
    client.post("/api/league", headers=H, json={"league": "Hardcore Keepers"})
    assert client.get("/api/status").json()["prices"]["league"] == "Hardcore Keepers"


def test_account_leagues_are_offered(env: tuple[TestClient, AppRuntime]) -> None:
    client, rt = env
    rt.note_account_leagues(["Fire Party (PL12345)", "Standard", ""])
    options = client.get("/api/leagues").json()["options"]
    assert options[:2] == ["Keepers", "Fire Party (PL12345)"]
    assert options.count("Standard") == 1


def test_ninja_league_list_is_cached(env: tuple[TestClient, AppRuntime]) -> None:
    client, rt = env
    for _ in range(5):
        client.get("/api/leagues")
    assert rt.prices.client.league_calls == 1  # type: ignore[attr-defined]
