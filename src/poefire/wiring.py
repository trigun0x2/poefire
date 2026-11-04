"""Builds API/loot objects from the config (shared by the CLI and, later, the web UI)."""

from __future__ import annotations

from poefire import __version__
from poefire.api.client import PoeApi
from poefire.api.oauth import OAuthClient, TokenManager, TokenStore, user_agent
from poefire.api.session import SessionApi, SessionStore
from poefire.config import Config
from poefire.loot.recorder import LootRecorder
from poefire.loot.snapshot import AccountApi, Snapshotter
from poefire.store import Store

PROJECT_URL = "https://github.com/trigun0x2/poefire"


class ConfigError(Exception):
    pass


def oauth_client(cfg: Config) -> OAuthClient:
    if not cfg.oauth_client_id:
        raise ConfigError(
            "set oauth_client_id in config.toml (register an app with GGG: oauth@grindinggear.com)"
        )
    if not cfg.contact:
        raise ConfigError("set contact in config.toml (GGG requires it in the User-Agent)")
    ua = user_agent(cfg.oauth_client_id, __version__, cfg.contact)
    return OAuthClient(cfg.oauth_client_id, ua, cfg.oauth_redirect_uri)


def token_manager(cfg: Config) -> TokenManager:
    return TokenManager(oauth_client(cfg), TokenStore(cfg.token_path))


def session_user_agent(cfg: Config) -> str:
    contact = f"; contact: {cfg.contact}" if cfg.contact else ""
    return f"poefire/{__version__} (+{PROJECT_URL}{contact})"


def account_api(cfg: Config) -> AccountApi:
    """The account-data client for the configured auth method, ready to use."""
    if cfg.auth == "oauth":
        tokens = token_manager(cfg)
        if not tokens.logged_in:
            raise ConfigError("not logged in; run `poefire-cli login`")
        return PoeApi(tokens, tokens.oauth.user_agent, cfg.realm)
    if cfg.auth == "session":
        if not cfg.account_name:
            raise ConfigError("set account_name in config.toml (full name, e.g. Name#1234)")
        sessions = SessionStore(cfg.session_path)
        poesessid = sessions.load()
        if not poesessid:
            raise ConfigError(
                "no POESESSID saved; paste it in Settings (or run `poefire-cli set-session`)"
            )
        return SessionApi(
            poesessid, cfg.account_name, session_user_agent(cfg), cfg.realm,
            on_auth=sessions.record_auth,
        )  # fmt: skip
    raise ConfigError(f'unknown auth {cfg.auth!r} (use "session" or "oauth")')


def snapshotter(cfg: Config, poe: AccountApi) -> Snapshotter:
    if not cfg.league:
        raise ConfigError("set league in config.toml")
    if not cfg.stash_tabs:
        print("warning: no stash_tabs configured; only the inventory is tracked")
    return Snapshotter(poe, cfg.league, cfg.stash_tabs, cfg.character)


def recorder(cfg: Config, poe: AccountApi) -> LootRecorder:
    return LootRecorder(
        snapshotter(cfg, poe),
        lambda: Store(cfg.db_path),
        return_delay=cfg.snapshot_delay,
        snapshot_on_run_start=cfg.snapshot_on_run_start,
    )
