from __future__ import annotations

import os
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from poefire import __version__, wiring
from poefire.api.ratelimit import RateLimiter
from poefire.api.session import SessionApi, SessionError, SessionExpiredError, SessionStore
from poefire.config import Config
from poefire.loot.snapshot import Snapshotter

COOKIE = "0123456789abcdef0123456789abcdef"
UA = "poefire/0.1.0 (+https://github.com/trigun0x2/poefire)"


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


class Site:
    """Fake pathofexile.com website endpoints."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.tabs = [
            {"n": "Dump", "i": 0, "id": "aaa", "type": "PremiumStash"},
            {"n": "Currency", "i": 1, "id": "bbb", "type": "CurrencyStash"},
            {"n": "Maps", "i": 2, "id": "mmm", "type": "MapStash"},
        ]
        self.items = {0: [{"id": "x1", "typeLine": "Hubris Circlet", "frameType": 0}],
                      1: [{"id": "c1", "typeLine": "Chaos Orb", "frameType": 5,
                           "stackSize": 7, "maxStackSize": 20}]}  # fmt: skip
        self.status: int | None = None

    def __call__(self, r: httpx.Request) -> httpx.Response:
        self.requests.append(r)
        if self.status:
            return httpx.Response(self.status, headers={"Retry-After": "30"})
        q = {k: v[0] for k, v in parse_qs(r.url.query.decode()).items()}
        path = r.url.path
        if path == "/character-window/get-characters":
            return httpx.Response(200, json=[
                {"name": "OldChar", "league": "Standard", "level": 90},
                {"name": "xyz_bow", "league": "Keepers", "level": 67, "lastActive": True},
            ])  # fmt: skip
        if path == "/character-window/get-items":
            return httpx.Response(200, json={
                "character": {"name": q["character"], "league": "Keepers"},
                "items": [
                    {"id": "inv1", "typeLine": "Divine Orb", "frameType": 5, "stackSize": 1,
                     "maxStackSize": 20, "inventoryId": "MainInventory"},
                    {"id": "eq1", "typeLine": "Spine Bow", "frameType": 2, "inventoryId": "Weapon"},
                    {"id": "fl1", "typeLine": "Divine Life Flask", "frameType": 0,
                     "inventoryId": "Flask"},
                ],
            })  # fmt: skip
        if path == "/character-window/get-stash-items":
            if q["tabs"] == "1":
                return httpx.Response(200, json={"numTabs": 3, "tabs": self.tabs, "items": []})
            return httpx.Response(200, json={"items": self.items.get(int(q["tabIndex"]), [])})
        if path == "/api/stash/Keepers/mmm":
            return httpx.Response(200, json={"stash": {"id": "mmm", "children": [
                {"id": "t16", "name": "Tier 16"}, {"id": "t15", "name": "Tier 15"}]}})  # fmt: skip
        if path.startswith("/api/stash/Keepers/mmm/"):
            child = path.rsplit("/", 1)[1]
            return httpx.Response(200, json={"stash": {"id": child, "items": [
                {"id": f"map-{child}", "typeLine": "Strand Map", "frameType": 0}]}})  # fmt: skip
        return httpx.Response(404)


def make(site: Site, **kw: Any) -> tuple[SessionApi, FakeClock]:
    clock = FakeClock()
    api = SessionApi(
        COOKIE, "Exile#1234", UA,
        http=httpx.Client(transport=httpx.MockTransport(site)),
        limiter=RateLimiter(clock=clock, sleep=clock.sleep),
        clock=clock, sleep=clock.sleep, **kw,
    )  # fmt: skip
    return api, clock


def test_requests_carry_cookie_honest_ua_and_account() -> None:
    site = Site()
    api, _ = make(site)
    chars = api.list_characters()
    (r,) = site.requests
    assert r.url.host == "www.pathofexile.com"
    assert r.headers["Cookie"] == f"POESESSID={COOKIE}"
    assert r.headers["User-Agent"] == UA  # identifies itself, doesn't pose as a browser
    assert parse_qs(r.url.query.decode()) == {"accountName": ["Exile#1234"], "realm": ["pc"]}
    assert [c["current"] for c in chars] == [False, True]


def test_character_inventory_excludes_equipment_and_flasks() -> None:
    api, _ = make(Site())
    char = api.get_character("xyz_bow")
    assert [i["id"] for i in char["inventory"]] == ["inv1"]
    assert char["league"] == "Keepers"


def test_stash_tabs_by_index() -> None:
    site = Site()
    api, _ = make(site)
    tabs = api.list_stashes("Keepers")
    assert [(t["id"], t["name"], t["type"]) for t in tabs][:2] == [
        ("aaa", "Dump", "PremiumStash"), ("bbb", "Currency", "CurrencyStash")]  # fmt: skip
    stash = api.get_stash("Keepers", "bbb")
    assert stash["items"][0]["stackSize"] == 7
    q = parse_qs(site.requests[-1].url.query.decode())
    assert q["tabIndex"] == ["1"] and q["tabs"] == ["0"] and q["league"] == ["Keepers"]


def test_min_interval_between_requests() -> None:
    api, clock = make(Site(), min_interval=1.5)
    api.list_characters()
    api.list_characters()
    assert clock.slept == [1.5]


@pytest.mark.parametrize(
    ("status", "error", "match"),
    [
        (401, SessionExpiredError, "set-session"),
        (403, SessionError, "Name#1234"),
        (429, SessionError, "retry after 30"),
        (500, SessionError, "HTTP 500"),
    ],
)
def test_errors_never_include_the_cookie(status: int, error: type[Exception], match: str) -> None:
    site = Site()
    site.status = status
    api, _ = make(site)
    with pytest.raises(error, match=match) as e:
        api.list_characters()
    assert COOKIE not in str(e.value)
    assert COOKIE not in repr(api)


def test_429_is_not_retried_but_blocks_the_bucket() -> None:
    site = Site()
    site.status = 429
    api, _ = make(site)
    with pytest.raises(SessionError):
        api.list_characters()
    assert len(site.requests) == 1
    assert api.limiter.wait_time("character") == 30.0


def test_snapshot_through_session_api_with_map_sections() -> None:
    site = Site()
    api, _ = make(site)
    snap = Snapshotter(api, "Keepers", ["Dump", "Currency", "Maps"],
                       clock=lambda: datetime(2026, 11, 1)).take()  # fmt: skip
    assert snap.complete, snap.error
    assert sorted(e.name for e in snap.items) == [
        "Chaos Orb", "Divine Orb", "Hubris Circlet", "Strand Map", "Strand Map"]  # fmt: skip
    assert {e.source for e in snap.items} == {"inventory", "aaa", "bbb", "t16", "t15"}


def test_session_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SessionStore.ENV, raising=False)
    store = SessionStore(tmp_path / "session.json")
    assert store.load() is None
    store.save(COOKIE)
    assert store.load() == COOKIE
    if sys.platform != "win32":
        assert os.stat(store.path).st_mode & 0o777 == 0o600
    monkeypatch.setenv(SessionStore.ENV, "fromenv")
    assert store.load() == "fromenv"
    store.clear()
    monkeypatch.delenv(SessionStore.ENV)
    assert store.load() is None


@pytest.mark.parametrize(
    ("setup", "match"),
    [
        (lambda c, s: None, "account_name"),
        (lambda c, s: setattr(c, "account_name", "Exile#1234"), "set-session"),
        (lambda c, s: setattr(c, "auth", "magic"), "unknown auth"),
    ],
)
def test_account_api_configuration_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    setup: Callable[[Config, SessionStore], None],
    match: str,
) -> None:
    monkeypatch.delenv(SessionStore.ENV, raising=False)
    cfg = Config(session_path=tmp_path / "session.json")
    setup(cfg, SessionStore(cfg.session_path))
    with pytest.raises(wiring.ConfigError, match=match):
        wiring.account_api(cfg)


def test_account_api_session_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SessionStore.ENV, raising=False)
    cfg = Config(session_path=tmp_path / "session.json", account_name="Exile#1234")
    SessionStore(cfg.session_path).save(COOKIE)
    api = wiring.account_api(cfg)
    assert isinstance(api, SessionApi)
    assert api.user_agent == f"poefire/{__version__} (+https://github.com/trigun0x2/poefire)"


def test_network_error_is_a_session_error() -> None:
    def boom(r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=r)

    clock = FakeClock()
    http = httpx.Client(transport=httpx.MockTransport(boom))
    api = SessionApi(COOKIE, "Exile#1234", UA, http=http, clock=clock, sleep=clock.sleep)
    with pytest.raises(SessionError, match="network error") as e:
        api.list_characters()
    assert COOKIE not in str(e.value)
