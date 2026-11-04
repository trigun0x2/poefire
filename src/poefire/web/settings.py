"""Settings page backend: read/update the config, save the POESESSID, test the account.

The POESESSID is write-only here: the API reports whether one is saved, never its value."""

from __future__ import annotations

import os
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from poefire import hotkey
from poefire.api.session import SessionStore
from poefire.config import WINDOWS_LOG_CANDIDATES, Config
from poefire.leagues import is_private
from poefire.store import Store
from poefire.web.runtime import AppRuntime

POESESSID_RE = re.compile(r"[0-9a-fA-F]{32}")


class SettingsIn(BaseModel):
    """Partial update: fields left out keep their current value."""

    log_path: str | None = None  # "" = detect automatically
    account_name: str | None = None
    league: str | None = None
    price_league: str | None = None  # "" = automatic
    character: str | None = None  # "" = the account's current character
    characters: list[str] | None = None  # whose deaths count; [] = everyone's
    stash_tabs: list[str] | None = None
    snapshot_on_run_start: bool | None = None
    snapshot_delay: float | None = Field(default=None, ge=0, le=60)
    check_updates: bool | None = None
    update_channel: Literal["stable", "beta"] | None = None
    campaign_hotkey: str | None = None
    campaign_hotkey_sound: bool | None = None


class SessionIn(BaseModel):
    poesessid: str = Field(max_length=200)


class SettingsError(ValueError):
    pass


def _session_store(rt: AppRuntime) -> SessionStore:
    return SessionStore(rt.cfg.session_path)


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
    detected = next((p for p in WINDOWS_LOG_CANDIDATES if p.exists()), None)
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
        "snapshot_on_run_start": cfg.snapshot_on_run_start,
        "snapshot_delay": cfg.snapshot_delay,
        "loot": {"enabled": rt.loot.enabled, "reason": rt.loot.reason},
        "check_updates": cfg.check_updates,
        "update_channel": cfg.update_channel,
        "campaign_hotkey": cfg.campaign_hotkey,
        "campaign_hotkey_sound": cfg.campaign_hotkey_sound,
        "version": rt.update.current,
    }


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
    if body.snapshot_on_run_start is not None:
        changes["snapshot_on_run_start"] = body.snapshot_on_run_start
    if body.check_updates is not None:
        changes["check_updates"] = body.check_updates
    if body.update_channel is not None:
        changes["update_channel"] = body.update_channel
    if body.campaign_hotkey is not None:
        spec = body.campaign_hotkey.strip()
        if spec:
            try:
                spec = hotkey.normalize(spec)
            except hotkey.HotkeyError as e:
                raise SettingsError(f"campaign hotkey: {e}") from e
        changes["campaign_hotkey"] = spec
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
    rt.reconfigure(rt.cfg)
    return warnings


def clear_session(rt: AppRuntime) -> None:
    _session_store(rt).clear()
    rt.reconfigure(rt.cfg)


def check_account(rt: AppRuntime) -> dict[str, Any]:
    """List characters and the league's stash tabs, to check the cookie and fill the pickers.
    Never raises: errors are returned for display."""
    from poefire import wiring

    try:
        api = rt.recorder.snapshotter.api if rt.recorder is not None else wiring.account_api(rt.cfg)
        chars = api.list_characters()
        rt.note_account_leagues(str(c.get("league") or "") for c in chars)
        tabs = api.list_stashes(rt.league)
    except Exception as e:  # shown to the user as is (never contains the cookie)
        return {"ok": False, "error": str(e)}
    return {
        "ok": True,
        "characters": [
            {"name": c.get("name"), "league": c.get("league"), "level": c.get("level"),
             "class": c.get("class"), "current": bool(c.get("current"))}
            for c in chars
        ],
        "tabs": [{"id": t.get("id"), "name": t.get("name"), "type": t.get("type")}
                 for t in tabs],
    }  # fmt: skip
