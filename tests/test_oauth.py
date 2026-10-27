from __future__ import annotations

import os
import socket
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from poefire.api.oauth import (
    AUTHORIZE_URL,
    TOKEN_URL,
    NotLoggedInError,
    OAuthClient,
    OAuthError,
    Pkce,
    TokenManager,
    TokenSet,
    TokenStore,
    login,
    pkce_challenge,
    user_agent,
)

UA = user_agent("myapp", "0.1.0", "me@example.com")


def test_user_agent_format() -> None:
    assert UA == "OAuth myapp/0.1.0 (contact: me@example.com)"


def test_pkce_rfc7636_vector() -> None:
    # RFC 7636 appendix B
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert pkce_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_pkce_generate() -> None:
    p = Pkce.generate()
    assert 43 <= len(p.verifier) <= 128
    assert p.challenge == pkce_challenge(p.verifier)
    assert Pkce.generate().verifier != p.verifier


def test_authorize_url() -> None:
    client = OAuthClient("myapp", UA, "http://127.0.0.1:8462/oauth/callback")
    pkce = Pkce("v" * 43, "challenge")
    url = client.authorize_url("st", pkce)
    assert url.startswith(AUTHORIZE_URL + "?")
    q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert q == {
        "client_id": "myapp",
        "response_type": "code",
        "scope": "account:stashes account:characters",
        "state": "st",
        "redirect_uri": "http://127.0.0.1:8462/oauth/callback",
        "code_challenge": "challenge",
        "code_challenge_method": "S256",
    }


class TokenEndpoint:
    def __init__(self, status: int = 200, body: dict[str, object] | None = None) -> None:
        self.status = status
        self.body = body or {
            "access_token": "AT",
            "expires_in": 36000,
            "token_type": "bearer",
            "scope": "account:stashes account:characters",
            "username": "Exile#1234",
            "refresh_token": "RT",
        }
        self.forms: list[dict[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url) == TOKEN_URL
        assert request.headers["User-Agent"] == UA
        self.forms.append({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
        return httpx.Response(self.status, json=self.body)


def make_client(endpoint: TokenEndpoint, now: float = 1000.0, redirect: str = "") -> OAuthClient:
    return OAuthClient(
        "myapp",
        UA,
        redirect or "http://127.0.0.1:8462/oauth/callback",
        http=httpx.Client(transport=httpx.MockTransport(endpoint)),
        clock=lambda: now,
    )


def test_exchange_code() -> None:
    ep = TokenEndpoint()
    tokens = make_client(ep).exchange_code("CODE", Pkce("VERIFIER" * 6, "x"))
    assert ep.forms == [
        {
            "client_id": "myapp",
            "grant_type": "authorization_code",
            "code": "CODE",
            "redirect_uri": "http://127.0.0.1:8462/oauth/callback",
            "scope": "account:stashes account:characters",
            "code_verifier": "VERIFIER" * 6,
        }
    ]
    assert tokens == TokenSet("AT", "RT", 1000.0 + 36000, "account:stashes account:characters",
                              "Exile#1234")  # fmt: skip


def test_token_error_is_reported() -> None:
    ep = TokenEndpoint(400, {"error": "invalid_grant", "error_description": "code expired"})
    with pytest.raises(OAuthError, match="code expired"):
        make_client(ep).exchange_code("CODE", Pkce.generate())


def test_token_manager_refreshes_and_persists(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "token.json")
    store.save(TokenSet("OLD", "RT0", expires_at=1050.0, scope="s"))  # expires within margin
    ep = TokenEndpoint()
    mgr = TokenManager(make_client(ep, now=1000.0), store)
    assert mgr.access_token() == "AT"
    assert ep.forms[0] == {"client_id": "myapp", "grant_type": "refresh_token",
                           "refresh_token": "RT0"}  # fmt: skip
    loaded = store.load()
    assert loaded is not None and loaded.access_token == "AT" and loaded.refresh_token == "RT"
    assert mgr.access_token() == "AT"
    assert len(ep.forms) == 1  # still fresh, no second refresh


def test_token_manager_valid_token_no_refresh(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "token.json")
    store.save(TokenSet("CUR", "RT0", expires_at=99999.0, scope="s"))
    ep = TokenEndpoint()
    assert TokenManager(make_client(ep), store).access_token() == "CUR"
    assert ep.forms == []


def test_token_manager_not_logged_in(tmp_path: Path) -> None:
    mgr = TokenManager(make_client(TokenEndpoint()), TokenStore(tmp_path / "none.json"))
    assert not mgr.logged_in
    with pytest.raises(NotLoggedInError):
        mgr.access_token()


def test_failed_refresh_requires_login(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "token.json")
    store.save(TokenSet("OLD", "RT0", expires_at=0.0, scope="s"))
    mgr = TokenManager(make_client(TokenEndpoint(400, {"error": "invalid_grant"})), store)
    with pytest.raises(NotLoggedInError, match="log in again"):
        mgr.access_token()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_token_file_is_private(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "token.json")
    store.save(TokenSet("A", None, 0.0, ""))
    assert os.stat(store.path).st_mode & 0o777 == 0o600


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def test_login_full_flow() -> None:
    redirect = f"http://127.0.0.1:{free_port()}/oauth/callback"
    ep = TokenEndpoint()
    client = make_client(ep, redirect=redirect)

    def browser(url: str) -> None:
        # Simulate the user approving: GGG redirects to our loopback with code + state.
        q = parse_qs(urlsplit(url).query)
        r = httpx.get(f"{redirect}?code=THECODE&state={q['state'][0]}", trust_env=False)
        assert r.status_code == 200 and "connected" in r.text

    tokens = login(client, browser, timeout=5)
    assert tokens.access_token == "AT"
    assert ep.forms[0]["code"] == "THECODE"
    assert len(ep.forms[0]["code_verifier"]) >= 43


def test_login_rejects_state_mismatch() -> None:
    redirect = f"http://127.0.0.1:{free_port()}/oauth/callback"
    ep = TokenEndpoint()

    def browser(url: str) -> None:
        httpx.get(f"{redirect}?code=C&state=forged", trust_env=False)

    with pytest.raises(OAuthError, match="state mismatch"):
        login(make_client(ep, redirect=redirect), browser, timeout=5)
    assert ep.forms == []  # code never exchanged


def test_login_reports_denial() -> None:
    redirect = f"http://127.0.0.1:{free_port()}/oauth/callback"

    def browser(url: str) -> None:
        httpx.get(f"{redirect}?error=access_denied", trust_env=False)

    with pytest.raises(OAuthError, match="access_denied"):
        login(make_client(TokenEndpoint(), redirect=redirect), browser, timeout=5)


def test_non_loopback_redirect_rejected() -> None:
    client = make_client(TokenEndpoint(), redirect="https://example.com/cb")
    with pytest.raises(OAuthError, match="loopback"):
        login(client, lambda url: None, timeout=1)
