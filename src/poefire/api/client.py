"""Typed-ish wrapper around the official PoE API (https://api.pathofexile.com).

Endpoints (https://www.pathofexile.com/developer/docs/reference):
    GET /character[/<realm>]                                  account:characters
    GET /character[/<realm>]/<name>                           account:characters
    GET /stash[/<realm>]/<league>                             account:stashes (PoE1)
    GET /stash[/<realm>]/<league>/<stash_id>[/<substash_id>]  account:stashes (PoE1)

Realm is omitted for PC; otherwise "xbox" or "sony".
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from poefire.api.oauth import TokenManager
from poefire.api.ratelimit import RateLimiter

API_BASE = "https://api.pathofexile.com"

type Json = dict[str, Any]


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"PoE API {status}: {message}")
        self.status = status


class PoeApi:
    def __init__(
        self,
        tokens: TokenManager,
        user_agent: str,
        realm: str | None = None,
        http: httpx.Client | None = None,
        limiter: RateLimiter | None = None,
        max_retries: int = 3,
    ) -> None:
        self.tokens = tokens
        self.user_agent = user_agent
        self.realm = None if realm in (None, "", "pc") else realm
        self.http = http or httpx.Client(timeout=30)
        self.limiter = limiter or RateLimiter()
        self.max_retries = max_retries

    def _path(self, root: str, *parts: str) -> str:
        segs = [root, *([self.realm] if self.realm else []), *parts]
        return "/" + "/".join(quote(s, safe="") for s in segs)

    def _get(self, bucket: str, path: str) -> Json:
        refreshed = False
        for _attempt in range(self.max_retries + 1):
            token = self.tokens.access_token()
            self.limiter.acquire(bucket)
            resp = self.http.get(
                API_BASE + path,
                headers={"Authorization": f"Bearer {token}", "User-Agent": self.user_agent},
            )
            self.limiter.update(bucket, resp.headers, resp.status_code)
            if resp.status_code == 429:
                continue  # limiter now blocks for Retry-After
            if resp.status_code == 401 and not refreshed:
                self.tokens.access_token(force_refresh=True)
                refreshed = True
                continue
            if resp.status_code >= 500:
                continue
            if resp.status_code != 200:
                raise ApiError(resp.status_code, _error_message(resp))
            data: Json = resp.json()
            return data
        raise ApiError(resp.status_code, f"giving up after retries: {_error_message(resp)}")

    def list_characters(self) -> list[Json]:
        chars: list[Json] = self._get("character", self._path("character"))["characters"]
        return chars

    def get_character(self, name: str) -> Json:
        char: Json = self._get("character", self._path("character", name))["character"]
        return char

    def list_stashes(self, league: str) -> list[Json]:
        tabs: list[Json] = self._get("stash", self._path("stash", league))["stashes"]
        return tabs

    def get_stash(self, league: str, stash_id: str, substash_id: str | None = None) -> Json:
        parts = [league, stash_id, *([substash_id] if substash_id else [])]
        stash: Json = self._get("stash", self._path("stash", *parts))["stash"]
        return stash


def _error_message(resp: httpx.Response) -> str:
    try:
        err = resp.json().get("error", {})
        if isinstance(err, dict):
            return str(err.get("message") or err)
        return str(err)
    except ValueError:
        return resp.text[:200]
