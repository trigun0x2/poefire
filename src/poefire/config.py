r"""Configuration: TOML file + defaults. All fields optional.

Example `config.toml`:

    log_path = 'D:\Games\Path of Exile\logs\Client.txt'  # single quotes: no escaping
    characters = ["MyWitch"]
    poll_interval = 0.5
    extra_map_prefixes = ["Heist"]
    extra_town_ids = []
    excluded_area_ids = []

    # Loot tracking
    auth = "session"                     # "session" (POESESSID) or "oauth"
    account_name = "MyAccount#1234"      # session auth: your full account name
    league = "Settlers"
    character = "MyWitch"                # default: the account's current character
    stash_tabs = ["Dump", "Currency"]    # names or ids of tabs to include in snapshots
    league_tabs = ["Settlers|Dump"]      # per league ("League|tab"; "League|" = none); a
                                         # league without entries uses stash_tabs
"""

from __future__ import annotations

import json
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
    Path(r"C:\Program Files\Epic Games\PathOfExile\logs\Client.txt"),
    Path(r"C:\Program Files (x86)\Epic Games\PathOfExile\logs\Client.txt"),
)


def newest_existing(paths: tuple[Path, ...] | list[Path]) -> Path | None:
    """The most recently written of these files (the install you actually play from)."""
    found = []
    for p in paths:
        try:
            found.append((p.stat().st_mtime, p))
        except OSError:
            continue
    return max(found)[1] if found else None


def default_data_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        return Path(base) / "poefire" if base else Path.home() / "AppData" / "Roaming" / "poefire"
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "poefire"


def default_config_path() -> Path:
    return default_data_dir() / "config.toml"


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    # JSON string escaping is valid TOML basic-string escaping (\\, \", \n, \uXXXX)
    return json.dumps(str(value), ensure_ascii=False)


@dataclass
class Config:
    log_path: Path | None = None
    db_path: Path = field(default_factory=lambda: default_data_dir() / "poefire.sqlite3")
    poll_interval: float = 0.5
    characters: list[str] = field(default_factory=list)
    extra_map_prefixes: list[str] = field(default_factory=list)
    extra_town_ids: list[str] = field(default_factory=list)
    excluded_area_ids: list[str] = field(default_factory=list)
    # --- loot tracking ---
    auth: str = "session"  # "session": POESESSID cookie; "oauth": official API app
    account_name: str | None = None  # session auth: full name with discriminator (Name#1234)
    session_path: Path = field(default_factory=lambda: default_data_dir() / "session.json")
    oauth_client_id: str | None = None
    oauth_redirect_uri: str = DEFAULT_REDIRECT_URI
    contact: str | None = None
    token_path: Path = field(default_factory=lambda: default_data_dir() / "token.json")
    league: str | None = None
    price_league: str | None = None  # poe.ninja league to price from; None = automatic
    realm: str = "pc"
    character: str | None = None
    stash_tabs: list[str] = field(default_factory=list)  # leagues without picks of their own
    # the watched tabs picked per league: "League|tab", "League|" = none (see tabs_for)
    league_tabs: list[str] = field(default_factory=list)
    snapshot_delay: float = 3.0  # seconds after a zone change before the inventory is read
    check_updates: bool = True
    update_channel: str = "stable"  # "stable": public releases; "beta": also pre-releases
    build_snapshots: bool = True  # gear/gems/tree at levels 5, 10 and 15+ (needs loot setup)
    # from level 80: a snapshot after this many hours without one, kept only if the build
    # changed (0 = off)
    build_stale_hours: int = 24
    # take trade sales' payments out of map loot (reads the website's trade history: more
    # requests, and the trade site rate-limits hard), off by default
    trade_sales: bool = False
    # switch the league by itself when your newest character plays in another one (else a
    # banner suggests it)
    league_autoswitch: bool = False
    # "per_map": snapshots around each map (loot per map); "interval": one every 10 minutes
    # while mapping, far fewer requests, only the session's total
    loot_mode: str = "per_map"
    # --- campaign ---
    campaign_hotkey: str = "Ctrl+Alt+R"  # start / pause / resume the campaign timer; "" = off
    campaign_hotkey_sound: bool = True  # beep when the hotkey is pressed
    # --- in-game overlay (Windows app; the game must run windowed or windowed fullscreen) ---
    overlay_enabled: bool = True
    overlay_toggle_hotkey: str = "Ctrl+Alt+O"  # show / hide the overlay
    overlay_move_hotkey: str = "Ctrl+Alt+L"  # unlock to drag it, press again to lock
    overlay_opacity: int = 90  # percent, 20-100
    # --- in-game shortcuts (poefire.gameinput): one key press = one action, only in the game ---
    chat_commands: list[str] = field(  # "hotkey|command"; an empty hotkey = off (the default)
        default_factory=lambda: ["|/hideout", "|/exit", "|/kingsmarch", "|/menagerie",
                                 "|/delve"])  # fmt: skip
    search_strings: list[str] = field(default_factory=list)  # "hotkey|name|text"
    # saved regexes ("name|text") and the one the regex hotkey pastes (by name; "" = none)
    regexes: list[str] = field(default_factory=list)
    regex_active: str = ""
    regex_hotkey: str = "Ctrl+B"  # pastes the active regex into the search box; "" = off
    # --- troubleshooting ---
    debug_logging: bool = False  # verbose log file (Settings > Diagnostics)

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        path = path or default_config_path()
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
            elif isinstance(current, int):
                value = int(value)
            elif isinstance(current, float):
                value = float(value)
            else:
                value = str(value)
            setattr(cfg, f.name, value)
        return cfg

    def to_toml(self) -> str:
        """Settings that differ from the defaults, as TOML."""
        default = Config()
        lines = ["# poefire settings (written by the Settings page; edits here also work)"]
        for f in fields(self):
            value = getattr(self, f.name)
            if value == getattr(default, f.name) or value is None:
                continue
            lines.append(f"{f.name} = {_toml_value(value)}")
        return "\n".join(lines) + "\n"

    def save(self, path: Path | None = None) -> Path:
        path = path or default_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(self.to_toml(), encoding="utf-8")
        tmp.replace(path)
        return path

    def own_league_tabs(self) -> dict[str, list[str]]:
        """League -> its watched tabs, for the leagues picked one by one (league_tabs)."""
        out: dict[str, list[str]] = {}
        for entry in self.league_tabs:
            league, sep, tab = entry.partition("|")
            if sep and league:
                out.setdefault(league, [])
                if tab and tab not in out[league]:
                    out[league].append(tab)
        return out

    def tabs_for(self, league: str | None) -> list[str]:
        """The watched tabs in `league`: its own picks, else stash_tabs."""
        own = self.own_league_tabs()
        return list(own[league]) if league in own else list(self.stash_tabs)

    def with_league_tabs(self, picks: dict[str, list[str]]) -> list[str]:
        """league_tabs with `picks` (league -> tabs) replacing those leagues' entries."""
        own = {**self.own_league_tabs(), **picks}
        return [f"{league}|{tab}" for league, tabs in own.items() for tab in (tabs or [""])]

    def zone_rules(self) -> ZoneRules:
        return ZoneRules.with_extras(
            self.extra_map_prefixes, self.extra_town_ids, self.excluded_area_ids
        )

    def resolve_log_path(self) -> Path | None:
        """Configured path, else the first default install location that exists."""
        if self.log_path is not None:
            return self.log_path
        return newest_existing(WINDOWS_LOG_CANDIDATES)
