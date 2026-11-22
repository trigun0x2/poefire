"""Settings page backend: read/update the config, save the POESESSID, test the account.

The POESESSID is write-only here: the API reports whether one is saved, never its value."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from poefire import gameinput, hotkey
from poefire.api.session import LeagueError, SessionExpiredError, SessionStore
from poefire.config import WINDOWS_LOG_CANDIDATES, Config, newest_existing
from poefire.leagues import is_private
from poefire.store import Store
from poefire.web.runtime import AppRuntime

log = logging.getLogger(__name__)

POESESSID_RE = re.compile(r"[0-9a-fA-F]{32}")
MAX_TAB_LEAGUES = 6  # leagues whose stash tabs a connection check lists (a request each)


ONBOARDED_KEY = "onboarded"


class ChatCommandIn(BaseModel):
    key: str = ""  # hotkey; "" = off
    text: str = Field(min_length=1, max_length=256)


class SearchStringIn(BaseModel):
    key: str = ""
    name: str = Field(default="", max_length=60)
    text: str = Field(min_length=1, max_length=250)  # the game's search box takes 250


class RegexIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    text: str = Field(min_length=1, max_length=250)


class SettingsIn(BaseModel):
    """Partial update: fields left out keep their current value."""

    chat_commands: list[ChatCommandIn] | None = None
    search_strings: list[SearchStringIn] | None = None
    regexes: list[RegexIn] | None = None
    regex_active: str | None = None  # a name from regexes; "" = none
    regex_hotkey: str | None = None

    log_path: str | None = None  # "" = detect automatically
    account_name: str | None = None
    league: str | None = None
    price_league: str | None = None  # "" = automatic
    character: str | None = None  # "" = the account's current character
    characters: list[str] | None = None  # whose deaths count; [] = everyone's
    stash_tabs: list[str] | None = None
    league_tabs: dict[str, list[str]] | None = None  # league -> its watched tabs
    snapshot_delay: float | None = Field(default=None, ge=0, le=60)
    check_updates: bool | None = None
    update_channel: Literal["stable", "beta"] | None = None
    campaign_hotkey: str | None = None
    build_snapshots: bool | None = None
    build_stale_hours: int | None = Field(default=None, ge=0, le=24 * 30)
    trade_sales: bool | None = None
    league_autoswitch: bool | None = None
    loot_mode: Literal["per_map", "interval"] | None = None
    overlay_enabled: bool | None = None
    overlay_toggle_hotkey: str | None = None
    overlay_move_hotkey: str | None = None
    overlay_opacity: int | None = Field(default=None, ge=20, le=100)
    debug_logging: bool | None = None
    campaign_hotkey_sound: bool | None = None


class SessionIn(BaseModel):
    poesessid: str = Field(max_length=200)


class SettingsError(ValueError):
    pass


def _session_store(rt: AppRuntime) -> SessionStore:
    return SessionStore(rt.cfg.session_path)


def _account_cache_path(rt: AppRuntime) -> Path:
    return rt.cfg.session_path.with_name("account-cache.json")


def account_cache(rt: AppRuntime) -> dict[str, Any] | None:
    """The last connection check's characters and stash tabs, so Settings shows the tabs without
    asking pathofexile.com again (Refresh tabs does). None if missing or for another league."""
    try:
        data = json.loads(_account_cache_path(rt).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    listed = {str(g.get("league")) for g in data.get("leagues") or () if isinstance(g, dict)}
    if data.get("league") != rt.league and rt.league not in listed:
        return None
    return data


def _save_account_cache(rt: AppRuntime, result: dict[str, Any]) -> None:
    try:
        path = _account_cache_path(rt)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {**result, "league": rt.league, "fetched_at": time.time()}
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass  # only a cache


def log_characters(rt: AppRuntime) -> list[dict[str, Any]]:
    """Character names seen in the log recently (deaths and level-ups), with their deaths and
    whether the current "count deaths for" setting counts them."""
    wanted = {c.casefold() for c in rt.cfg.characters}
    seen: dict[str, dict[str, Any]] = {}
    with Store(rt.cfg.db_path) as s:
        for kind in ("PlayerSlain", "LevelUp"):
            for e in s.list_events(kind=kind, limit=500):
                name = str(e.get("character") or "")
                if not name:
                    continue
                c = seen.setdefault(name.casefold(), {"name": name, "deaths": 0, "in_maps": 0,
                                                      "last_seen": e["ts"]})  # fmt: skip
                c["last_seen"] = max(c["last_seen"], e["ts"])
                if kind == "PlayerSlain":
                    c["deaths"] += 1
                    c["in_maps"] += 1 if e.get("run_id") else 0
    out = sorted(seen.values(), key=lambda c: c["last_seen"], reverse=True)
    for c in out:
        c["counted"] = not wanted or c["name"].casefold() in wanted
    return out


def settings_json(rt: AppRuntime) -> dict[str, Any]:
    cfg = rt.cfg
    detected = rt.detected_log or newest_existing(WINDOWS_LOG_CANDIDATES)
    effective = rt.log_path
    from_env = bool(os.environ.get(SessionStore.ENV, "").strip())
    return {
        "config_path": str(rt.config_path) if rt.config_path else None,
        "log_path": str(cfg.log_path) if cfg.log_path else "",
        "log_path_effective": str(effective) if effective else None,
        "log_path_exists": bool(effective and effective.is_file()),
        "log_path_detected": str(detected) if detected else None,
        "log_path_candidates": [str(p) for p in WINDOWS_LOG_CANDIDATES],
        "account_name": cfg.account_name or "",
        "session_saved": _session_store(rt).load() is not None,
        "session_from_env": from_env,
        "session_health": _session_store(rt).health(),
        "auth": cfg.auth,
        "league": rt.league,
        "league_is_private": rt.is_private,
        "price_league": cfg.price_league or "",
        "price_league_effective": rt.price_league,
        "ninja_leagues": rt.ninja_leagues(),
        "character": cfg.character or "",
        "characters": cfg.characters,
        "log_characters": log_characters(rt),
        "stash_tabs": cfg.stash_tabs,
        "league_tabs": cfg.own_league_tabs(),  # leagues picked one by one; others: stash_tabs
        "account_cache": account_cache(rt),
        "snapshot_delay": cfg.snapshot_delay,
        "loot": {"enabled": rt.loot.enabled, "reason": rt.loot.reason},
        "check_updates": cfg.check_updates,
        "update_channel": cfg.update_channel,
        "campaign_hotkey": cfg.campaign_hotkey,
        "build_snapshots": cfg.build_snapshots,
        "build_stale_hours": cfg.build_stale_hours,
        "chat_commands": [
            {"key": c.key, "text": c.text} for c in gameinput.chat_commands(cfg.chat_commands)
        ],
        "search_strings": [
            {"key": s.key, "name": s.name, "text": s.text}
            for s in gameinput.search_strings(cfg.search_strings)
        ],
        "regexes": [{"name": r.name, "text": r.text} for r in gameinput.saved_regexes(cfg.regexes)],
        "regex_active": cfg.regex_active,
        "regex_hotkey": cfg.regex_hotkey,
        "game_hotkeys": rt.game_hotkeys_json(),
        "trade_sales": cfg.trade_sales,
        "league_autoswitch": cfg.league_autoswitch,
        "loot_mode": cfg.loot_mode,
        "overlay_enabled": cfg.overlay_enabled,
        "overlay_toggle_hotkey": cfg.overlay_toggle_hotkey,
        "overlay_move_hotkey": cfg.overlay_move_hotkey,
        "overlay_opacity": cfg.overlay_opacity,
        "debug_logging": cfg.debug_logging,
        # the setup wizard was finished or skipped (or loot tracking already works)
        "onboarded": bool(rt.loot.enabled or rt.kv_get(ONBOARDED_KEY)),
        "campaign_hotkey_sound": cfg.campaign_hotkey_sound,
        "version": rt.update.current,
    }


def _in_game_shortcuts(rt: AppRuntime, body: SettingsIn, changes: dict[str, Any],
                       used: set[str]) -> None:  # fmt: skip
    """Chat commands and search strings: one line each, hotkeys valid and all different (from
    each other and from the app's other hotkeys)."""

    def key(spec: str, what: str) -> str:
        spec = spec.strip()
        if not spec:
            return ""
        try:
            spec = hotkey.normalize(spec)
        except hotkey.HotkeyError as e:
            raise SettingsError(f"{what}: {e}") from e
        if spec in used:
            raise SettingsError(f"{what}: {spec} is already used by another hotkey")
        used.add(spec)
        return spec

    def one_line(text: str, what: str) -> str:
        if "\n" in text or "\r" in text:
            raise SettingsError(f"{what}: must be one line")
        return text.strip()

    cmds = body.chat_commands
    if cmds is None:
        cmds = [ChatCommandIn(key=c.key, text=c.text)
                for c in gameinput.chat_commands(rt.cfg.chat_commands)]  # fmt: skip
    else:
        changes["chat_commands"] = []
    out = []
    for c in cmds:
        text = one_line(c.text, f"chat command {c.text!r}")
        out.append(gameinput.ChatCommand(key(c.key, f"chat command {text}"), text).format())
    if body.chat_commands is not None:
        changes["chat_commands"] = out
    if body.search_strings is not None:
        changes["search_strings"] = [
            gameinput.SearchString(key(s.key, f"search {s.name or s.text!r}"),
                                   one_line(s.name, "search name"),
                                   one_line(s.text, f"search {s.name!r}")).format()
            for s in body.search_strings
        ]  # fmt: skip
    else:
        for s in gameinput.search_strings(rt.cfg.search_strings):
            key(s.key, f"search {s.name or s.text!r}")
    _regexes(rt, body, changes, key, one_line)


def _regexes(
    rt: AppRuntime,
    body: SettingsIn,
    changes: dict[str, Any],
    key: Callable[[str, str], str],
    one_line: Callable[[str, str], str],
) -> None:
    """Saved regexes: names one line, unique, without "|"; the active one must be among them.
    The regex hotkey only counts against the other hotkeys while a regex is picked: without
    one it isn't registered, so it can't be in the way."""
    if body.regexes is None:
        names = [r.name for r in gameinput.saved_regexes(rt.cfg.regexes)]
    else:
        out, names = [], []
        for r in body.regexes:
            name = one_line(r.name, "regex name")
            if not name:
                raise SettingsError("regex name is required")
            if "|" in name:
                raise SettingsError(f"regex {name!r}: the name can't contain |")
            if name.casefold() in (n.casefold() for n in names):
                raise SettingsError(f"regex {name!r}: two regexes have this name")
            names.append(name)
            out.append(gameinput.SavedRegex(name, one_line(r.text, f"regex {name!r}")).format())
        changes["regexes"] = out
    active = body.regex_active if body.regex_active is not None else rt.cfg.regex_active
    active = active.strip()
    if active and active not in names:
        if body.regex_active is not None:
            raise SettingsError(f"regex {active!r} isn't saved")
        active = ""  # the active regex was removed
    if active != rt.cfg.regex_active:
        changes["regex_active"] = active
    hk = body.regex_hotkey if body.regex_hotkey is not None else rt.cfg.regex_hotkey
    if active:
        spec = key(hk, "regex hotkey")
    else:
        try:
            spec = hotkey.normalize(hk.strip()) if hk.strip() else ""
        except hotkey.HotkeyError as e:
            raise SettingsError(f"regex hotkey: {e}") from e
    if body.regex_hotkey is not None:
        changes["regex_hotkey"] = spec


def update_settings(rt: AppRuntime, body: SettingsIn) -> list[str]:
    """Validate, save to config.toml, apply live. Returns warnings (saved anyway)."""
    warnings: list[str] = []
    changes: dict[str, Any] = {}
    if body.log_path is not None:
        raw = body.log_path.strip().strip('"')
        if raw:
            path = Path(raw)
            if path.is_dir():
                path = (
                    path / "logs" / "Client.txt"
                    if (path / "logs").is_dir()
                    else path / "Client.txt"
                )
            if not path.is_file():
                warnings.append(
                    f"{path} doesn't exist yet; it will be read once the game creates it"
                )
            changes["log_path"] = path
        else:
            changes["log_path"] = None
    if body.account_name is not None:
        name = body.account_name.strip()
        if name and not re.fullmatch(r"[^\s#]+#\d{4}", name):
            warnings.append(
                "account names now end in #1234; use the full name if loot tracking fails"
            )
        changes["account_name"] = name or None
    if body.league is not None:
        league = body.league.strip()
        if not league or len(league) > 100:
            raise SettingsError("league name is required")
        changes["league"] = league
    if body.price_league is not None:
        price = body.price_league.strip()
        if len(price) > 100:
            raise SettingsError("price league name is too long")
        if price and is_private(price):
            raise SettingsError("poe.ninja has no prices for private leagues; pick a public one")
        changes["price_league"] = price or None
    if body.character is not None:
        changes["character"] = body.character.strip() or None
    if body.characters is not None:
        names = [c.strip() for c in body.characters if c.strip()]
        known = {c["name"].casefold() for c in log_characters(rt)}
        unknown = [n for n in names if known and n.casefold() not in known]
        if unknown:
            warnings.append(
                f"{', '.join(unknown)} hasn't died or leveled up in your log yet. Deaths only "
                "count for the listed characters, so check the spelling (it's the character "
                "name, not the account name)"
            )
        changes["characters"] = names
    if body.stash_tabs is not None:
        changes["stash_tabs"] = list(dict.fromkeys(t.strip() for t in body.stash_tabs if t.strip()))
    if body.league_tabs is not None:
        picks: dict[str, list[str]] = {}
        for league, tabs in body.league_tabs.items():
            name = league.strip()
            if not name or len(name) > 100 or "|" in name or "\n" in name:
                raise SettingsError(f"stash tabs: bad league name {league!r}")
            picks[name] = list(dict.fromkeys(t.strip() for t in tabs if t.strip()))
        merged = rt.cfg.with_league_tabs(picks)
        if merged != rt.cfg.league_tabs:
            changes["league_tabs"] = merged
    if body.check_updates is not None:
        changes["check_updates"] = body.check_updates
    if body.update_channel is not None:
        changes["update_channel"] = body.update_channel
    for field, label in (("campaign_hotkey", "campaign hotkey"),
                         ("overlay_toggle_hotkey", "overlay hotkey"),
                         ("overlay_move_hotkey", "overlay move hotkey")):  # fmt: skip
        value = getattr(body, field)
        if value is None:
            continue
        spec = value.strip()
        if spec:
            try:
                spec = hotkey.normalize(spec)
            except hotkey.HotkeyError as e:
                raise SettingsError(f"{label}: {e}") from e
        changes[field] = spec
    keys = [changes.get(f, getattr(rt.cfg, f)) for f in
            ("campaign_hotkey", "overlay_toggle_hotkey", "overlay_move_hotkey")]  # fmt: skip
    used = [k for k in keys if k]
    if len(set(used)) != len(used):
        raise SettingsError("the campaign and overlay hotkeys must all be different")
    _in_game_shortcuts(rt, body, changes, set(used))
    if body.overlay_enabled is not None:
        changes["overlay_enabled"] = body.overlay_enabled
    if body.overlay_opacity is not None:
        changes["overlay_opacity"] = body.overlay_opacity
    if body.debug_logging is not None:
        changes["debug_logging"] = body.debug_logging
    if body.build_snapshots is not None:
        changes["build_snapshots"] = body.build_snapshots
    if body.build_stale_hours is not None:
        changes["build_stale_hours"] = body.build_stale_hours
    if body.trade_sales is not None:
        changes["trade_sales"] = body.trade_sales
    if body.league_autoswitch is not None:
        changes["league_autoswitch"] = body.league_autoswitch
    if body.loot_mode is not None:
        changes["loot_mode"] = body.loot_mode
    if body.campaign_hotkey_sound is not None:
        changes["campaign_hotkey_sound"] = body.campaign_hotkey_sound
    if body.snapshot_delay is not None:
        changes["snapshot_delay"] = float(body.snapshot_delay)

    cfg: Config = replace(rt.cfg, **changes)
    if rt.config_path is not None:
        cfg.save(rt.config_path)
    rt.reconfigure(cfg)
    return warnings


def save_session(rt: AppRuntime, poesessid: str) -> list[str]:
    value = poesessid.strip()
    if value.lower().startswith("poesessid="):
        value = value.split("=", 1)[1].strip()
    if not value or re.search(r"[\s;]", value):
        raise SettingsError("paste only the cookie's value (no spaces or semicolons)")
    warnings = []
    if not POESESSID_RE.fullmatch(value):
        warnings.append("a POESESSID is usually 32 hex characters; saved anyway")
    _session_store(rt).save(value)
    rt.reconfigure(rt.cfg, rebuild_loot=True)
    return warnings


def clear_session(rt: AppRuntime) -> None:
    _session_store(rt).clear()
    _account_cache_path(rt).unlink(missing_ok=True)
    rt.reconfigure(rt.cfg, rebuild_loot=True)


def check_account(rt: AppRuntime) -> dict[str, Any]:
    """List characters and the league's stash tabs, to check the cookie and fill the pickers.
    Never raises: errors are returned for display."""
    from poefire import wiring

    try:
        api = (rt.recorder.snapshotter.api if rt.recorder is not None
               else wiring.account_api(rt.cfg, rt.web_limiter))  # fmt: skip
        chars = api.list_characters()  # a public profile answers without a login
        rt.note_account_leagues(str(c.get("league") or "") for c in chars)
    except Exception as e:  # shown to the user as is (never contains the cookie)
        return {"ok": False, "error": str(e)}
    # The stash tabs of the chosen league; if that league is wrong, the newest league's (from
    # poe.ninja), then Standard's: the connection itself is fine either way.
    tabs: list[Any] = []
    tabs_league: str | None = None
    note = None
    for league in dict.fromkeys((rt.league, rt.default_league(), "Standard")):
        try:
            tabs = api.list_stashes(league)
        except SessionExpiredError as e:  # the stash list is what needs the login
            return {"ok": False, "error": str(e)}
        except LeagueError as e:
            note = note or str(e)
            continue
        except Exception as e:  # network, rate limit: not about the league
            note = note or str(e)
            break
        tabs_league = league
        break
    if tabs_league == rt.league:
        note = None
    elif tabs_league is not None:
        note = f"{note} Showing the stash tabs of {tabs_league} instead."

    def listed(tabs: list[Any]) -> list[dict[str, Any]]:
        return [{"id": t.get("id"), "name": t.get("name"), "type": t.get("type")} for t in tabs]

    # the tabs of each league your characters are in (a request each), to pick per league
    groups = [{"league": tabs_league, "tabs": listed(tabs)}] if tabs_league is not None else []
    for league in dict.fromkeys(str(c.get("league") or "") for c in chars):
        if not league or league == tabs_league or len(groups) >= MAX_TAB_LEAGUES:
            continue
        try:
            more = api.list_stashes(league)
        except SessionExpiredError as e:
            return {"ok": False, "error": str(e)}
        except LeagueError:
            continue
        except Exception as e:  # network, rate limit: the rest can wait for the next refresh
            log.info("stash tabs of %s not listed: %s", league, e)
            break
        groups.append({"league": league, "tabs": listed(more)})
    result: dict[str, Any] = {
        "ok": True,
        "tabs_league": tabs_league,
        "league_note": note,
        "characters": [
            {"name": c.get("name"), "league": c.get("league"), "level": c.get("level"),
             "class": c.get("class"), "current": bool(c.get("current"))}
            for c in chars
        ],
        "tabs": listed(tabs),
        "leagues": groups,
    }  # fmt: skip
    if tabs_league is not None:
        _save_account_cache(rt, result)
        result["fetched_at"] = time.time()
    return result
