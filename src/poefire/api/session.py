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

The cookie is a full login session for the account: it is kept out of logs and error
messages, and stored separately from the config (see SessionStore).

Unlike some tools, this client identifies itself honestly in the User-Agent rather than
posing as a browser. If GGG's edge protection rejects it, that's a decision to respect.
"""

from __future__ import annotations

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
    """401: the POESESSID is invalid or expired. Log in on the website and set a new one."""


class SessionStore:
    """The cookie lives in its own file (owner-only permissions), never in config.toml, so
    sharing a config can't leak it. `POE_SESSID` in the environment overrides the file."""

    ENV = "POE_SESSID"

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> str | None:
        if env := os.environ.get(self.ENV, "").strip():
            return env
        try:
            value = json.loads(self.path.read_text()).get("poesessid")
        except (FileNotFoundError, json.JSONDecodeError, AttributeError):
            return None
        return str(value) if value else None

    def save(self, poesessid: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"poesessid": poesessid}, f)
        os.replace(tmp, self.path)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


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
        self.clock = clock
        self.sleep = sleep
        self._last_request = float("-inf")
        self._tab_index: dict[str, int] = {}
        self._lock = threading.Lock()

    def __repr__(self) -> str:  # never show the cookie
        return f"SessionApi(account_name={self.account_name!r}, realm={self.realm!r})"

    # --- transport ---

    def _get(self, bucket: str, path: str, params: dict[str, str | int] | None = None) -> Any:
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
        if resp.status_code == 401:
            raise SessionExpiredError(
                "POESESSID invalid or expired (HTTP 401); run `poefire set-session` with a new one"
            )
        if resp.status_code == 403:
            raise SessionError(
                "HTTP 403: check account_name (use the full Name#1234 form) and that the "
                "POESESSID belongs to that account"
            )
        if resp.status_code != 200:
            raise SessionError(f"HTTP {resp.status_code} from {path}")
        try:
            return resp.json()
        except ValueError as e:
            # e.g. an HTML challenge page from the site's edge protection
            raise SessionError(f"non-JSON response from {path} (HTTP {resp.status_code})") from e

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

    def list_stashes(self, league: str) -> list[Json]:
        data = self._get(
            "stash",
            "/character-window/get-stash-items",
            {**self._common(), "league": league, "tabs": 1, "tabIndex": 0},
        )
        tabs: list[Json] = []
        for t in data.get("tabs") or ():
            tab_id = str(t.get("id") or f"index:{t['i']}")
            self._tab_index[tab_id] = int(t["i"])
            tabs.append({"id": tab_id, "name": t.get("n", ""), "type": t.get("type", ""),
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
