from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from poefire.api.client import API_BASE, ApiError, PoeApi
from poefire.api.oauth import OAuthClient, TokenManager, TokenSet, TokenStore
from poefire.api.ratelimit import RateLimiter, Window, parse_headers


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


def headers(policy: str = "stash-request-limit", **rules: tuple[str, str]) -> httpx.Headers:
    h = {"X-Rate-Limit-Policy": policy, "X-Rate-Limit-Rules": ",".join(rules)}
    for name, (limit, state) in rules.items():
        h[f"X-Rate-Limit-{name}"] = limit
        h[f"X-Rate-Limit-{name}-State"] = state
    return httpx.Headers(h)


def test_parse_headers() -> None:
    info = parse_headers(headers(Account=("10:5:10,30:300:300", "1:5:0,4:300:0")))
    assert info is not None and info.policy == "stash-request-limit"
    (rule,) = info.rules
    assert rule.name == "Account"
    assert rule.limits == (Window(10, 5, 10), Window(30, 300, 300))
    assert rule.states == (Window(1, 5, 0), Window(4, 300, 0))
    assert parse_headers(httpx.Headers({})) is None


def limiter() -> tuple[RateLimiter, FakeClock]:
    clock = FakeClock()
    return RateLimiter(clock=clock, sleep=clock.sleep, margin=1), clock


def test_unknown_bucket_is_not_delayed() -> None:
    rl, clock = limiter()
    rl.acquire("stash")
    assert clock.slept == []


def test_sliding_window_respects_limit_with_margin() -> None:
    rl, clock = limiter()
    # limit 3 per 10s, margin 1 -> 2 requests per window
    rl.acquire("stash")
    rl.update("stash", headers(Client=("3:10:60", "1:10:0")), 200)
    clock.t = 1
    rl.acquire("stash")
    rl.update("stash", headers(Client=("3:10:60", "2:10:0")), 200)
    clock.t = 2
    rl.acquire("stash")  # must wait until the first hit (t=0) leaves the window
    assert clock.slept == [8.0]


def test_hits_from_other_tools_are_counted() -> None:
    rl, _clock = limiter()
    rl.acquire("stash")
    rl.update("stash", headers(Account=("5:60:60", "4:60:0")), 200)  # 3 hits elsewhere
    assert rl.wait_time("stash") == 60.0


def test_active_restriction_blocks() -> None:
    rl, _clock = limiter()
    rl.acquire("stash")
    rl.update("stash", headers(Client=("10:5:10", "11:5:10")), 429)
    assert rl.wait_time("stash") >= 10.0


def test_retry_after_without_policy() -> None:
    rl, _clock = limiter()
    rl.update("x", httpx.Headers({"Retry-After": "7"}), 429)
    assert rl.wait_time("x") == 7.0
    rl.update("y", httpx.Headers({}), 429)  # no headers at all: default back-off
    assert rl.wait_time("y") == 60.0


def test_policies_shared_between_buckets() -> None:
    rl, _clock = limiter()
    for bucket in ("a", "b"):
        rl.acquire(bucket)
        rl.update(bucket, headers("shared", Client=("2:10:10", "1:10:0")), 200)
    assert rl.wait_time("a") > 0 and rl.wait_time("b") > 0


# --- PoeApi ---


def make_api(
    tmp_path: Path, handler: Callable[[httpx.Request], httpx.Response], realm: str | None = None
) -> tuple[PoeApi, FakeClock]:
    store = TokenStore(tmp_path / "t.json")
    store.save(TokenSet("AT", "RT", 1e12, "s"))
    refresh_http = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"access_token": "AT2", "expires_in": 3600})
        )
    )
    tokens = TokenManager(OAuthClient("app", "UA", http=refresh_http), store)
    clock = FakeClock()
    api = PoeApi(
        tokens,
        "OAuth app/1 (contact: x)",
        realm,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        limiter=RateLimiter(clock=clock, sleep=clock.sleep),
    )
    return api, clock


def test_requests_carry_auth_and_user_agent(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        return httpx.Response(200, json={"stashes": [{"id": "a"}]})

    api, _ = make_api(tmp_path, handler)
    assert api.list_stashes("Hardcore Settlers") == [{"id": "a"}]
    assert seen[0].url.raw_path == b"/stash/Hardcore%20Settlers"
    assert seen[0].headers["Authorization"] == "Bearer AT"
    assert seen[0].headers["User-Agent"] == "OAuth app/1 (contact: x)"


@pytest.mark.parametrize(
    ("realm", "call", "path"),
    [
        (None, lambda a: a.get_stash("Std", "s1"), "/stash/Std/s1"),
        ("pc", lambda a: a.get_stash("Std", "s1", "c2"), "/stash/Std/s1/c2"),
        ("xbox", lambda a: a.get_character("Me"), "/character/xbox/Me"),
        ("sony", lambda a: a.list_characters(), "/character/sony"),
    ],
)
def test_paths(
    tmp_path: Path, realm: str | None, call: Callable[[PoeApi], object], path: str
) -> None:
    seen: list[str] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(r.url.path)
        return httpx.Response(200, json={"stash": {}, "character": {}, "characters": []})

    api, _ = make_api(tmp_path, handler, realm)
    call(api)
    assert seen == [path]
    assert str(API_BASE).startswith("https://api.pathofexile.com")


def test_429_waits_retry_after_then_succeeds(tmp_path: Path) -> None:
    calls = 0

    def handler(r: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429,
                headers={
                    "X-Rate-Limit-Policy": "p",
                    "X-Rate-Limit-Rules": "Account",
                    "X-Rate-Limit-Account": "10:60:120",
                    "X-Rate-Limit-Account-State": "11:60:120",
                    "Retry-After": "120",
                },
            )
        return httpx.Response(200, json={"characters": []})

    api, clock = make_api(tmp_path, handler)
    assert api.list_characters() == []
    assert calls == 2
    assert sum(clock.slept) >= 120


def test_401_refreshes_token_once(tmp_path: Path) -> None:
    auths: list[str] = []

    def handler(r: httpx.Request) -> httpx.Response:
        auths.append(r.headers["Authorization"])
        if r.headers["Authorization"] == "Bearer AT":
            return httpx.Response(401, json={"error": {"code": 8, "message": "Unauthorized"}})
        return httpx.Response(200, json={"characters": []})

    api, _ = make_api(tmp_path, handler)
    api.list_characters()
    assert auths == ["Bearer AT", "Bearer AT2"]


def test_client_error_raises(tmp_path: Path) -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": 1, "message": "Resource not found"}})

    api, _ = make_api(tmp_path, handler)
    with pytest.raises(ApiError, match="Resource not found") as e:
        api.get_stash("Std", "nope")
    assert e.value.status == 404
