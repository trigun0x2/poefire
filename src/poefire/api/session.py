"""Account data through the website's own endpoints, authenticated with the POESESSID cookie.

For when an OAuth app isn't available (GGG has closed new registrations). These are the
endpoints the pathofexile.com website uses, not the documented developer API:

    GET /character-window/get-characters?accountName&realm         -> [character, ...]
    GET /character-window/get-items?accountName&character&realm    -> {items, character}
    GET /character-window/get-stash-items?accountName&realm&league&tabs=1&tabIndex=0
                                                         -> {numTabs, tabs: [{n, i, id, type}]}
    GET /character-window/get-stash-items?...&tabIndex=N&tabs=0    -> {items}
    GET /api/stash/<league>/<tab id>[/<child id>]                  -> {stash: {children|items}}
        (the only way to read map/unique tab sections; same cookie)
    GET /api/trade/history/<league>                                -> {result: [sale, ...]}
        (your merchant tab sales, so their payments aren't counted as loot)

The cookie is a full login session for the account: it is kept out of logs and error
messages, and stored separately from the config (see SessionStore).

Unlike some tools, this client identifies itself honestly in the User-Agent rather than
posing as a browser. If GGG's edge protection rejects it, that's a decision to respect.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from poefire.api.ratelimit import RateLimiter

WEB_BASE = "https://www.pathofexile.com"

type Json = dict[str, Any]


class SessionError(Exception):
    pass


class SessionExpiredError(SessionError):
    """The website rejected the POESESSID: it expired, or you logged out on pathofexile.com."""


class LeagueError(SessionError):
    """The website wouldn't list stash tabs in this league (the cookie itself is fine)."""

    def __init__(self, league: str, detail: str) -> None:
        self.league = league
        super().__init__(
            f"Couldn't read your stash tabs in the league {league!r} ({detail}). Pick the "
            "league you play in Settings: its exact name, one your account has characters or "
            "stash tabs in."
        )


EXPIRED_HELP = (
    "Log in on pathofexile.com, copy the new POESESSID cookie and paste it in "
    "Settings → Path of Exile account."
)


class SessionStore:
    """The cookie lives in its own file (owner-only permissions), never in config.toml, so
    sharing a config can't leak it. `POE_SESSID` in the environment overrides the file.

    The file also records the cookie's health, to show how long a POESESSID actually lasts:
    when it was saved, when it last worked, when the website rejected it, and the same for
    previous cookies. GGG decides the lifetime (it usually lasts until you log out of the
    website, change your password, or the session is ended on their side), so it's measured,
    not assumed."""

    ENV = "POE_SESSID"
    HISTORY = 10
    OK_WRITE_INTERVAL = 300  # seconds: don't rewrite the file on every successful request

    def __init__(self, path: Path, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.clock = clock
        self._lock = threading.Lock()

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, self.path)

    @property
    def from_env(self) -> bool:
        return bool(os.environ.get(self.ENV, "").strip())

    def load(self) -> str | None:
        if env := os.environ.get(self.ENV, "").strip():
            return env
        value = self._read().get("poesessid")
        return str(value) if value else None

    def save(self, poesessid: str) -> None:
        with self._lock:
            old = self._read()
            history = list(old.get("history") or [])
            if old.get("saved_at"):
                history.insert(0, {k: old.get(k) for k in ("saved_at", "last_ok", "expired_at")})
            self._write({"poesessid": poesessid, "saved_at": self.clock(), "last_ok": None,
                         "expired_at": None, "history": history[: self.HISTORY]})  # fmt: skip

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)

    def record_auth(self, ok: bool) -> None:
        """Called by SessionApi after each request: did the website accept the cookie?"""
        if self.from_env:
            return  # nothing on disk to describe
        with self._lock:
            data = self._read()
            if not data.get("poesessid"):
                return
            now = self.clock()
            if ok:
                if (
                    data.get("expired_at") is None
                    and now - (data.get("last_ok") or 0) < self.OK_WRITE_INTERVAL
                ):
                    return
                data["last_ok"], data["expired_at"] = now, None
            elif data.get("expired_at") is None:
                data["expired_at"] = now
            else:
                return
            self._write(data)

    def health(self) -> dict[str, Any]:
        """Cookie health for display. Never includes the cookie."""
        data = self._read()
        saved, last_ok, expired = data.get("saved_at"), data.get("last_ok"), data.get("expired_at")

        def lasted(rec: dict[str, Any]) -> float | None:
            end = rec.get("expired_at") or rec.get("last_ok")
            return end - rec["saved_at"] if end and rec.get("saved_at") else None

        past = [r for r in data.get("history") or [] if r.get("saved_at")]
        return {
            "saved": bool(data.get("poesessid")) or self.from_env,
            "from_env": self.from_env,
            "saved_at": saved,
            "last_ok": last_ok,
            "expired_at": expired,
            "expired": expired is not None,
            # how long it has worked so far (or worked in total, once expired)
            "worked_seconds": lasted(data) if saved else None,
            "history": [
                {
                    "saved_at": r["saved_at"],
                    "lasted_seconds": lasted(r),
                    "expired": r.get("expired_at") is not None,
                }
                for r in past
            ],
        }


class SessionApi:
    """Implements the same AccountApi protocol as the OAuth client (loot.snapshot)."""

    def __init__(
        self,
        poesessid: str,
        account_name: str,
        user_agent: str,
        realm: str = "pc",
        http: httpx.Client | None = None,
        limiter: RateLimiter | None = None,
        min_interval: float = 1.5,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        on_auth: Callable[[bool], None] | None = None,
    ) -> None:
        """`min_interval`: floor between requests on top of the header-driven limiter; these
        endpoints have tight per-IP limits and GGG has banned tools that burst them."""
        if not poesessid or not account_name:
            raise SessionError("POESESSID and account name are required")
        self._cookie = poesessid
        self.account_name = account_name
        self.user_agent = user_agent
        self.realm = realm or "pc"
        self.http = http or httpx.Client(timeout=30)
        self.limiter = limiter or RateLimiter(clock=clock, sleep=sleep)
        self.min_interval = min_interval
        self.on_auth = on_auth  # told whether the website accepted the cookie
        self.clock = clock
        self.sleep = sleep
        self._last_request = float("-inf")
        self._tab_index: dict[str, int] = {}
        self._lock = threading.Lock()

    def __repr__(self) -> str:  # never show the cookie
        return f"SessionApi(account_name={self.account_name!r}, realm={self.realm!r})"

    # --- transport ---

    def _get(self, bucket: str, path: str, params: dict[str, str | int] | None = None,
             auth: bool = True) -> Any:  # fmt: skip
        """`auth`: whether a refusal means the cookie is bad (False for endpoints that can
        refuse for other reasons, like the trade site)."""
        url = WEB_BASE + path + (f"?{urlencode(params)}" if params else "")
        with self._lock:
            self.limiter.acquire(bucket)
            wait = self._last_request + self.min_interval - self.clock()
            if wait > 0:
                self.sleep(wait)
            self._last_request = self.clock()
            try:
                resp = self.http.get(
                    url,
                    headers={
                        "Cookie": f"POESESSID={self._cookie}",
                        "User-Agent": self.user_agent,
                        "Accept": "application/json",
                    },
                )
            except httpx.HTTPError as e:  # message has the URL, never the request headers
                raise SessionError(f"network error on {path}: {e}") from e
            self.limiter.update(bucket, resp.headers, resp.status_code)
        # One request per throttle: a 429 is not retried here. The limiter now blocks this
        # bucket until Retry-After has passed, so the next snapshot waits on its own.
        if resp.status_code == 429:
            raise SessionError(f"rate limited; retry after {resp.headers.get('Retry-After', '?')}s")
        try:
            data = self._check(resp, path)
        except SessionExpiredError as e:
            if not auth:
                raise SessionError(f"HTTP {resp.status_code} from {path}") from e
            self._report(False)
            raise
        self._report(True)
        return data

    def _report(self, ok: bool) -> None:
        if self.on_auth is not None:
            with contextlib.suppress(Exception):  # health tracking must never break a request
                self.on_auth(ok)

    @staticmethod
    def _check(resp: httpx.Response, path: str) -> Any:
        """Body of a successful response; SessionExpiredError when the cookie was rejected."""
        status = resp.status_code
        if status == 401:
            raise SessionExpiredError(
                f"Your POESESSID has expired or was logged out (HTTP 401). {EXPIRED_HELP}"
            )
        if status == 403:
            raise SessionExpiredError(
                "The website rejected your POESESSID (HTTP 403). Usually it has expired or you "
                f"logged out. {EXPIRED_HELP} If you just pasted it, check the account name "
                "(the full Name#1234) matches the account the cookie belongs to."
            )
        if 300 <= status < 400:
            # sent to the login page: the session is gone
            raise SessionExpiredError(
                f"Your POESESSID is no longer logged in (redirected to the login page). "
                f"{EXPIRED_HELP}"
            )
        if status != 200:
            raise SessionError(f"HTTP {status} from {path}")
        try:
            return resp.json()
        except ValueError as e:
            if "login" in resp.text[:20000].lower():
                raise SessionExpiredError(
                    f"Your POESESSID is no longer logged in (got the login page). {EXPIRED_HELP}"
                ) from e
            # e.g. an HTML challenge page from the site's edge protection
            raise SessionError(f"non-JSON response from {path} (HTTP {status})") from e

    def _common(self) -> dict[str, str | int]:
        return {"accountName": self.account_name, "realm": self.realm}

    # --- AccountApi ---

    def list_characters(self) -> list[Json]:
        chars = self._get("character", "/character-window/get-characters", self._common())
        if not isinstance(chars, list):
            raise SessionError("unexpected get-characters response")
        # Same shape the OAuth API uses for "the character you're playing".
        return [{**c, "current": bool(c.get("lastActive"))} for c in chars]

    def get_character(self, name: str) -> Json:
        data = self._get(
            "character", "/character-window/get-items", {**self._common(), "character": name}
        )
        char: Json = dict(data.get("character") or {"name": name})
        # get-items mixes equipment and bags; keep the bags (OAuth's `inventory`).
        char["inventory"] = [
            i for i in data.get("items") or () if "MainInventory" in str(i.get("inventoryId", ""))
        ]
        return char

    def get_build(self, name: str) -> Json:
        """Everything Path of Building's "Import from website" reads: equipped items with
        their socketed gems, and the passive tree with jewels, masteries and bandit."""
        items = self._get(
            "character", "/character-window/get-items", {**self._common(), "character": name}
        )
        passives = self._get(
            "character",
            "/character-window/get-passive-skills",
            {**self._common(), "character": name},
        )
        return {"source": "website", "items": items, "passives": passives}

    def trade_history(self, league: str) -> Any:
        """Your sales from merchant tabs in `league`: {"result": [{time, item_id, item,
        price: {amount, currency}}, ...]} (see poefire.loot.sales)."""
        return self._get("trade", f"/api/trade/history/{quote(league, safe='')}", auth=False)

    def list_stashes(self, league: str) -> list[Json]:
        """The league's stash tabs. A refusal here is about the league (misspelled, or one
        the account has nothing in), not the cookie: the character calls catch an expired
        one. So it raises LeagueError and never marks the cookie as bad."""
        try:
            data = self._get(
                "stash",
                "/character-window/get-stash-items",
                {**self._common(), "league": league, "tabs": 1, "tabIndex": 0},
                auth=False,
            )
        except SessionError as e:
            if "rate limited" in str(e) or "network error" in str(e):
                raise
            raise LeagueError(league, str(e)) from e
        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            raise LeagueError(league, str(err.get("message") if isinstance(err, dict) else err))
        tabs: list[Json] = []
        for t in data.get("tabs") or ():
            tab_id = str(t.get("id") or f"index:{t['i']}")
            self._tab_index[tab_id] = int(t["i"])
            name = t.get("n") or t.get("name") or ""
            tabs.append({"id": tab_id, "name": name, "type": t.get("type", ""),
                         "index": int(t["i"])})  # fmt: skip
        return tabs

    def list_tab_children(self, league: str, tab_id: str) -> list[Json]:
        """Sections of a map/unique tab (the legacy endpoint returns no items for them)."""
        data = self._get("stash", self._api_stash_path(league, tab_id))
        children: list[Json] = (data.get("stash") or {}).get("children") or []
        return children

    def get_stash(self, league: str, stash_id: str, substash_id: str | None = None) -> Json:
        if substash_id:
            data = self._get("stash", self._api_stash_path(league, stash_id, substash_id))
            stash: Json = data.get("stash") or {}
            return stash
        if stash_id not in self._tab_index:
            self.list_stashes(league)
        if stash_id not in self._tab_index:
            raise SessionError(f"stash tab {stash_id} not found in {league}")
        data = self._get(
            "stash",
            "/character-window/get-stash-items",
            {**self._common(), "league": league, "tabs": 0,
             "tabIndex": self._tab_index[stash_id]},
        )  # fmt: skip
        return {"id": stash_id, "items": data.get("items") or []}

    @staticmethod
    def _api_stash_path(league: str, *ids: str) -> str:
        return "/api/stash/" + "/".join(quote(s, safe="") for s in (league, *ids))
