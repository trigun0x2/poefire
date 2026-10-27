"""GGG OAuth 2.1 for a public client: authorization code + PKCE (S256), loopback redirect.

Per https://www.pathofexile.com/developer/docs/authorization:
- public clients use only the authorization-code grant with PKCE and a local redirect URI;
- they cannot request `service:*` scopes;
- access tokens are short-lived (documented as 10 hours) and refresh tokens last longer
  (documented as 7 days), so `poefire login` is needed again after a week without use.

Applications must be registered with GGG (oauth@grindinggear.com) to get a client ID; the
redirect URI used here must match the registered one.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

AUTHORIZE_URL = "https://www.pathofexile.com/oauth/authorize"
TOKEN_URL = "https://www.pathofexile.com/oauth/token"
DEFAULT_SCOPES: tuple[str, ...] = ("account:stashes", "account:characters")
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8462/oauth/callback"
REFRESH_MARGIN_SECONDS = 120


class OAuthError(Exception):
    pass


class NotLoggedInError(OAuthError):
    """No usable token (never logged in, or the refresh token expired). Run `poefire login`."""


# --- PKCE ---


@dataclass(frozen=True)
class Pkce:
    verifier: str
    challenge: str

    @classmethod
    def generate(cls) -> Pkce:
        # 64 random bytes -> 86 url-safe chars, within RFC 7636's 43..128.
        verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
        return cls(verifier, pkce_challenge(verifier))


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


# --- tokens ---


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str | None
    expires_at: float  # unix time
    scope: str
    username: str | None = None

    @classmethod
    def from_response(cls, data: dict[str, Any], now: float) -> TokenSet:
        if "access_token" not in data:
            raise OAuthError(f"token response without access_token: {sorted(data)}")
        return cls(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=now + float(data.get("expires_in", 0)),
            scope=data.get("scope", ""),
            username=data.get("username"),
        )

    def expires_soon(self, now: float) -> bool:
        return now >= self.expires_at - REFRESH_MARGIN_SECONDS


class TokenStore:
    """Token JSON in the per-user data dir, readable only by the owner where supported."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> TokenSet | None:
        try:
            data = json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        return TokenSet(**data)

    def save(self, tokens: TokenSet) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(asdict(tokens), f)
        os.replace(tmp, self.path)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


# --- OAuth client ---


class OAuthClient:
    def __init__(
        self,
        client_id: str,
        user_agent: str,
        redirect_uri: str = DEFAULT_REDIRECT_URI,
        scopes: tuple[str, ...] = DEFAULT_SCOPES,
        http: httpx.Client | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.client_id = client_id
        self.user_agent = user_agent
        self.redirect_uri = redirect_uri
        self.scopes = scopes
        self.http = http or httpx.Client(timeout=30)
        self.clock = clock

    def authorize_url(self, state: str, pkce: Pkce) -> str:
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "scope": " ".join(self.scopes),
            "state": state,
            "redirect_uri": self.redirect_uri,
            "code_challenge": pkce.challenge,
            "code_challenge_method": "S256",
        }
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    def exchange_code(self, code: str, pkce: Pkce) -> TokenSet:
        return self._token_request(
            {
                "client_id": self.client_id,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
                "scope": " ".join(self.scopes),
                "code_verifier": pkce.verifier,
            }
        )

    def refresh(self, refresh_token: str) -> TokenSet:
        return self._token_request(
            {
                "client_id": self.client_id,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
        )

    def _token_request(self, form: dict[str, str]) -> TokenSet:
        now = self.clock()
        resp = self.http.post(TOKEN_URL, data=form, headers={"User-Agent": self.user_agent})
        try:
            data: dict[str, Any] = resp.json()
        except ValueError:
            data = {}
        if resp.status_code != 200:
            detail = data.get("error_description") or data.get("error") or resp.text[:200]
            raise OAuthError(f"token endpoint returned {resp.status_code}: {detail}")
        return TokenSet.from_response(data, now)


class TokenManager:
    """Hands out a valid access token, refreshing (and persisting) it when close to expiry."""

    def __init__(self, oauth: OAuthClient, store: TokenStore) -> None:
        self.oauth = oauth
        self.store = store
        self._tokens = store.load()
        self._lock = threading.Lock()

    @property
    def logged_in(self) -> bool:
        return self._tokens is not None

    def access_token(self, force_refresh: bool = False) -> str:
        with self._lock:
            tokens = self._tokens
            if tokens is None:
                raise NotLoggedInError("not logged in; run `poefire login`")
            if force_refresh or tokens.expires_soon(self.oauth.clock()):
                if not tokens.refresh_token:
                    raise NotLoggedInError("token expired and no refresh token; log in again")
                try:
                    tokens = self.oauth.refresh(tokens.refresh_token)
                except OAuthError as e:
                    raise NotLoggedInError(f"token refresh failed ({e}); log in again") from e
                self._tokens = tokens
                self.store.save(tokens)
            return tokens.access_token

    def set_tokens(self, tokens: TokenSet) -> None:
        with self._lock:
            self._tokens = tokens
            self.store.save(tokens)


# --- loopback redirect receiver ---


@dataclass
class CallbackResult:
    code: str | None
    state: str | None
    error: str | None


class LoopbackReceiver:
    """One-shot HTTP server on the redirect URI's loopback host/port."""

    def __init__(self, redirect_uri: str) -> None:
        parts = urlsplit(redirect_uri)
        if parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise OAuthError(f"redirect URI must be http on a loopback host: {redirect_uri}")
        self.path = parts.path or "/"
        self.result: CallbackResult | None = None
        self._done = threading.Event()
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                url = urlsplit(self.path)
                if url.path != receiver.path:
                    self.send_error(404)
                    return
                q = parse_qs(url.query)
                receiver.result = CallbackResult(
                    code=q.get("code", [None])[0],
                    state=q.get("state", [None])[0],
                    error=q.get("error_description", q.get("error", [None]))[0],
                )
                ok = receiver.result.error is None and receiver.result.code is not None
                body = (
                    "<h1>poefire is now connected.</h1><p>You can close this tab.</p>"
                    if ok
                    else "<h1>Login failed.</h1><p>Check the poefire window.</p>"
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                receiver._done.set()

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self.server = HTTPServer((parts.hostname, parts.port or 80), Handler)
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> LoopbackReceiver:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()

    def wait(self, timeout: float) -> CallbackResult:
        if not self._done.wait(timeout):
            raise OAuthError("timed out waiting for the browser login")
        assert self.result is not None
        return self.result


def login(
    oauth: OAuthClient,
    open_browser: Callable[[str], object],
    timeout: float = 300,
) -> TokenSet:
    """Full interactive flow: open the browser, receive the redirect, exchange the code."""
    pkce = Pkce.generate()
    state = secrets.token_urlsafe(32)
    with LoopbackReceiver(oauth.redirect_uri) as receiver:
        open_browser(oauth.authorize_url(state, pkce))
        result = receiver.wait(timeout)
    if result.error:
        raise OAuthError(f"authorization denied: {result.error}")
    if not secrets.compare_digest(result.state or "", state):
        raise OAuthError("state mismatch in OAuth redirect (possible CSRF); try again")
    if not result.code:
        raise OAuthError("no authorization code in redirect")
    return oauth.exchange_code(result.code, pkce)


def user_agent(client_id: str, version: str, contact: str) -> str:
    """Format required by GGG: `OAuth {clientId}/{version} (contact: {contact})`."""
    return f"OAuth {client_id}/{version} (contact: {contact})"
