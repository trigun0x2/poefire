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


def test_unknown_older_hits_dont_fill_the_short_window() -> None:
    """A first response says 60 hits in 30 minutes, 1 in the last minute: the other 59 are
    older than a minute, so the minute window has room; the 30-minute one doesn't."""
    rl, clock = limiter()
    rl.acquire("items")
    rl.update("items", headers("item", Account=("30:60:60,70:1800:600", "1:60:0,60:1800:0")),
              200)  # fmt: skip
    assert rl.fits(["items"], 3) and not rl.fits(["items"], 4)  # we use 63: 70 less 10%
    assert rl.wait_time("items") == 0.0
    for _ in range(3):
        rl.acquire("items")
    assert clock.slept == []
    assert rl.wait_time("items") > 1000  # the 30-minute window is full: wait for old hits


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


def test_a_share_of_every_window_is_kept_free() -> None:
    """The website and other tools share the account's and the IP's limits."""
    rl = RateLimiter()
    assert rl.allowed(100) == 90 and rl.allowed(30) == 27 and rl.allowed(45) == 40
    assert rl.allowed(3) == 2 and rl.allowed(2) == 1 and rl.allowed(1) == 1


def test_fits() -> None:
    rl, clock = limiter()  # margin 1 hit, share 10%
    assert rl.fits(["items", "stash"], 50)  # nothing known yet: nothing to go by
    rl.acquire("items")
    rl.update("items", headers("item", Account=("30:60:60,100:1800:600", "1:60:0,70:1800:0")),
              200)  # fmt: skip
    rl.acquire("stash")
    rl.update("stash", headers("item", Account=("30:60:60,100:1800:600", "2:60:0,71:1800:0")),
              200)  # fmt: skip
    # 71 hits of the 90 we use in 30 minutes (100 less 10%); 2 of 27 in a minute
    assert rl.fits(["items", "stash"], 19) and not rl.fits(["items", "stash"], 20)
    assert not rl.fits(["stash"], 15, reserve=5)
    clock.t += 61
    assert rl.fits(["items"], 19)
    rl.update("stash", headers("item", Account=("30:60:60", "31:60:60")), 429)
    assert not rl.fits(["items"], 1)  # restricted: nothing fits


def test_state_survives_a_restart() -> None:
    wall = [1_000_000.0]
    clock = FakeClock()
    rl = RateLimiter(clock=clock, sleep=clock.sleep, wall=lambda: wall[0] + clock.t)
    rl.acquire("stash")
    rl.update("stash", headers("item", Account=("10:60:60,100:1800:600", "4:60:0,50:1800:0")),
              200)  # fmt: skip
    rl.update("items", headers("item", Account=("10:60:60,100:1800:600", "5:60:0,51:1800:600")),
              429)  # fmt: skip
    saved = rl.to_json()
    assert len(saved["policies"]["item"]["history"]) == 51

    later = FakeClock()  # a new process: its monotonic clock starts over
    later.t = 5.0
    wall[0] += 30  # restarted 30 s of wall time later
    rl2 = RateLimiter(clock=later, sleep=later.sleep, wall=lambda: wall[0] + later.t - 5.0)
    rl2.restore(saved)
    # the 600 s penalty is still on for the 570 s left, on both endpoints of the policy
    assert 560 < rl2.wait_time("items") <= 570 and 560 < rl2.wait_time("stash") <= 570
    assert not rl2.fits(["stash"], 1)
    later.t += 600  # the penalty is over; the 51 hits are still in the 30-minute window
    assert rl2.wait_time("stash") == 0.0
    assert rl2.fits(["stash"], 39) and not rl2.fits(["stash"], 40)


def test_restore_ignores_junk() -> None:
    rl, clock = limiter()
    junk: list[object] = [None, [], "x", {"policies": {"p": {"windows": "bad"}}},
                          {"buckets": {"a": "z"}}]  # fmt: skip
    for j in junk:
        rl.restore(j)
    assert rl.wait_time("a") == 0.0 and rl.fits(["a"], 99)
    assert clock.slept == []


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
