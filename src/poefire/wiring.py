"""Builds API/loot objects from the config (shared by the CLI and, later, the web UI)."""

from __future__ import annotations

from poefire import __version__
from poefire.api.client import PoeApi
from poefire.api.oauth import OAuthClient, TokenManager, TokenStore, user_agent
from poefire.config import Config
from poefire.loot.recorder import LootRecorder
from poefire.loot.snapshot import Snapshotter
from poefire.store import Store


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


def api(cfg: Config) -> PoeApi:
    tokens = token_manager(cfg)
    return PoeApi(tokens, tokens.oauth.user_agent, cfg.realm)


def snapshotter(cfg: Config, poe: PoeApi) -> Snapshotter:
    if not cfg.league:
        raise ConfigError("set league in config.toml")
    if not cfg.stash_tabs:
        print("warning: no stash_tabs configured; only the inventory is tracked")
    return Snapshotter(poe, cfg.league, cfg.stash_tabs, cfg.character)


def recorder(cfg: Config, poe: PoeApi) -> LootRecorder:
    return LootRecorder(
        snapshotter(cfg, poe),
        lambda: Store(cfg.db_path),
        return_delay=cfg.snapshot_delay,
        snapshot_on_run_start=cfg.snapshot_on_run_start,
    )
