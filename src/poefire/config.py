r"""Configuration: TOML file + defaults. All fields optional.

Example `config.toml`:

    log_path = 'D:\Games\Path of Exile\logs\Client.txt'  # single quotes: no escaping
    characters = ["MyWitch"]
    poll_interval = 0.5
    extra_map_prefixes = ["Heist"]
    extra_town_ids = []
    excluded_area_ids = []

    # Loot tracking (official API)
    oauth_client_id = "myregisteredclient"
    contact = "me@example.com"           # goes into the User-Agent, as GGG requires
    league = "Settlers"
    character = "MyWitch"                # default: the account's current character
    stash_tabs = ["Dump", "Currency"]    # names or ids of tabs to include in snapshots
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from poefire.api.oauth import DEFAULT_REDIRECT_URI
from poefire.zones import ZoneRules

WINDOWS_LOG_CANDIDATES: tuple[Path, ...] = (
    Path(r"C:\Program Files (x86)\Grinding Gear Games\Path of Exile\logs\Client.txt"),
    Path(r"C:\Program Files (x86)\Steam\steamapps\common\Path of Exile\logs\Client.txt"),
    Path(r"C:\Program Files\Steam\steamapps\common\Path of Exile\logs\Client.txt"),
)


def default_data_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        return Path(base) / "poefire" if base else Path.home() / "AppData" / "Roaming" / "poefire"
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "poefire"


@dataclass
class Config:
    log_path: Path | None = None
    db_path: Path = field(default_factory=lambda: default_data_dir() / "poefire.sqlite3")
    poll_interval: float = 0.5
    characters: list[str] = field(default_factory=list)
    extra_map_prefixes: list[str] = field(default_factory=list)
    extra_town_ids: list[str] = field(default_factory=list)
    excluded_area_ids: list[str] = field(default_factory=list)
    # --- loot tracking via the official API ---
    oauth_client_id: str | None = None
    oauth_redirect_uri: str = DEFAULT_REDIRECT_URI
    contact: str | None = None
    token_path: Path = field(default_factory=lambda: default_data_dir() / "token.json")
    league: str | None = None
    realm: str = "pc"
    character: str | None = None
    stash_tabs: list[str] = field(default_factory=list)
    snapshot_delay: float = 3.0
    snapshot_on_run_start: bool = True

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        path = path or default_data_dir() / "config.toml"
        if not path.exists():
            return cls()
        with open(path, "rb") as f:
            return cls.from_dict(tomllib.load(f))

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Config:
        cfg = cls()
        for f in fields(cls):
            if f.name not in d:
                continue
            value = d[f.name]
            current = getattr(cfg, f.name)
            if f.name.endswith("_path"):
                value = Path(value)
            elif isinstance(current, list):
                value = [str(x) for x in value]
            elif isinstance(current, bool):
                value = bool(value)
            elif isinstance(current, float):
                value = float(value)
            else:
                value = str(value)
            setattr(cfg, f.name, value)
        return cfg

    def zone_rules(self) -> ZoneRules:
        return ZoneRules.with_extras(
            self.extra_map_prefixes, self.extra_town_ids, self.excluded_area_ids
        )

    def resolve_log_path(self) -> Path | None:
        """Configured path, else the first default install location that exists."""
        if self.log_path is not None:
            return self.log_path
        return next((p for p in WINDOWS_LOG_CANDIDATES if p.exists()), None)
