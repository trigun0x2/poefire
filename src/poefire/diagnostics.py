"""A debug report: one .zip a user can send when something doesn't work.

It holds:
- report.txt: version, system, settings, where Client.txt is and how it's being read, the
  campaign tracker's state, hotkeys/overlay/loot status, and a check of the log's last lines;
- logs/: poefire's own log files;
- client-log.txt: the game's system lines from the end of Client.txt (area changes, zone
  entries, level-ups, deaths: the lines poefire reads). No chat, whispers or trade messages;
- campaign.json: the run in progress and the latest runs, with their splits.

No secrets: the POESESSID cookie and OAuth tokens are never included.
"""

from __future__ import annotations

import contextlib
import io
import json
import platform
import re
import sys
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from poefire import __version__, logs
from poefire.campaign import run_summary
from poefire.events import AreaGenerated, LevelUp, ZoneEntered
from poefire.parser import parse_line

if TYPE_CHECKING:
    from poefire.web.runtime import AppRuntime

REPORTS_DIR = "reports"
KEEP_REPORTS = 5
CLIENT_TAIL_BYTES = 16 * 1024 * 1024
CLIENT_MAX_LINES = 5000
_MSG = re.compile(
    r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} (?:\d+ [0-9a-fA-F]+ \[[A-Z]+ Client \d+\] )?"
)
# Other players' names in "X has joined/left the area" are left out.
_PRIVATE = re.compile(r" has (?:joined|left) the area\.$")


def client_lines(path: Path | None) -> tuple[list[str], dict[str, Any]]:
    """The game's system lines from the end of Client.txt, and what they contain."""
    info: dict[str, Any] = {"path": str(path) if path else None}
    if path is None:
        info["error"] = "no Client.txt path"
        return [], info
    try:
        st = path.stat()
        info.update(size=st.st_size, modified=datetime.fromtimestamp(st.st_mtime).isoformat())
        with open(path, "rb") as f:
            f.seek(max(0, st.st_size - CLIENT_TAIL_BYTES))
            data = f.read()
    except OSError as e:
        info["error"] = f"{type(e).__name__}: {e}"
        return [], info
    text = data.decode("utf-8", errors="replace").splitlines()
    if st.st_size > CLIENT_TAIL_BYTES and text:
        text = text[1:]  # probably cut mid-line
    kept: list[str] = []
    counts: Counter[str] = Counter()
    unknown: Counter[str] = Counter()
    for line in text:
        m = _MSG.match(line)
        if m is None:
            if "LOG FILE OPENING" in line:
                kept.append(line)
            continue
        msg = line[m.end() :]
        system = msg.startswith(": ")
        if not (system or msg.startswith("Generating level ") or "LOG FILE OPENING" in msg):
            continue
        if system and _PRIVATE.search(msg[2:]):
            continue
        kept.append(line)
        event = parse_line(line)
        kind = type(event).__name__ if event is not None else None
        if kind:
            counts[kind] += 1
        elif system:
            unknown[re.sub(r"\d+", "N", msg[2:])[:80]] += 1
    info["lines_scanned"] = len(text)
    info["counts"] = dict(counts)
    info["unrecognised_system_messages"] = [m for m, _ in unknown.most_common(15)]
    info["last_line"] = text[-1][:200] if text else None
    info["hints"] = hints(counts, len(text))
    age_days = (datetime.now().timestamp() - st.st_mtime) / 86400
    if age_days > 1:
        info["hints"].insert(0, (
            f"Client.txt was last written {age_days:.0f} days ago: the game probably runs from "
            "another install. Set the right one in Settings > Game log (poefire also switches "
            "to the running game's own Client.txt by itself)."
        ))  # fmt: skip
    return kept[-CLIENT_MAX_LINES:], info


def hints(counts: Counter[str], scanned: int) -> list[str]:
    out = []
    if scanned == 0:
        out.append("Client.txt is empty here: is this the game's current log file?")
    if counts[AreaGenerated.__name__] and not counts[ZoneEntered.__name__]:
        out.append(
            "areas are generated but no 'You have entered' lines were recognised: the game is "
            "probably not set to English (poefire reads the English messages)"
        )
    if counts[ZoneEntered.__name__] and not counts[LevelUp.__name__]:
        out.append("no level-ups recognised in the scanned part of the log")
    if not counts[AreaGenerated.__name__] and scanned:
        out.append("no area changes in the scanned part of the log (old or wrong Client.txt?)")
    return out


def _settings(rt: AppRuntime) -> dict[str, Any]:
    from poefire.web import settings

    try:
        data = settings.settings_json(rt)
    except Exception as e:  # report what we can
        return {"error": repr(e)}
    data.pop("ninja_leagues", None)
    return data


def _campaign(rt: AppRuntime) -> dict[str, Any]:
    now = rt.now()
    with rt._lock:
        active = rt.campaign.active
        runs = rt._store.list_campaign_runs(10, splits=True)
    return {
        "active": {**run_summary(active, now), "splits": [s.to_json() for s in active.splits]}
        if active else None,
        "recent": [{**run_summary(r, now), "splits": [s.to_json() for s in r.splits]}
                   for r in runs],
    }  # fmt: skip


def report_text(rt: AppRuntime, client: dict[str, Any], campaign: dict[str, Any]) -> str:
    service = rt.service
    tailer = service.tailer if service is not None else None
    active = campaign.get("active")
    lines = [
        f"poefire debug report, {datetime.now().isoformat(timespec='seconds')}",
        "",
        "== app ==",
        f"version: {__version__} ({'app' if getattr(sys, 'frozen', False) else 'source'})",
        f"system: {platform.platform()}, Python {sys.version.split()[0]}",
        f"data folder: {rt.cfg.db_path.parent}",
        f"log file: {logs.current_file()}",
        f"verbose logging: {rt.cfg.debug_logging}",
        "",
        "== Client.txt ==",
        f"configured: {rt.cfg.log_path or '(automatic)'}",
        f"in use: {rt.log_path}",
        f"running game's (last seen): {rt.detected_log}",
        f"exists: {bool(rt.log_path and rt.log_path.exists())}, size: {client.get('size')}, "
        f"modified: {client.get('modified')}",
        f"reading: {'yes' if tailer is not None else 'NO'}"
        + (f", offset {tailer.offset}, resets {tailer.resets}" if tailer is not None else ""),
        f"lines read since start: {service.lines_seen if service else 0}, "
        f"events: {service.events_seen if service else 0}, "
        f"line handler errors: {service.observer_errors if service else 0}",
        f"last line: {client.get('last_line')}",
        f"scanned {client.get('lines_scanned', 0)} lines from the end: {client.get('counts')}",
        *[f"HINT: {h}" for h in client.get("hints") or []],
        *(["unrecognised system messages (first 15 kinds):"]
          + [f"  {m}" for m in client["unrecognised_system_messages"]]
          if client.get("unrecognised_system_messages") else []),
        *([f"ERROR: {client['error']}"] if client.get("error") else []),
        "",
        "== campaign ==",
        (f"run in progress: #{active['id']} {active['status']}"
         f"{' (paused)' if active['paused'] else ''}, started {active['started_at']}, "
         f"character {active['character']} ({active['class']}), act {active['act']}, "
         f"level {active['level']}, {len(active['splits'])} splits, rta {active['rta']}s"
         if active else "no run in progress"),
        f"recent runs: {len(campaign.get('recent') or [])}",
        "",
        "== status ==",
        f"hotkeys: {json.dumps(rt.hotkey_json(), default=str)}",
        f"overlay: {json.dumps(rt.overlay_json(), default=str)}",
        f"loot tracking: {'on' if rt.loot.enabled else 'off'} {rt.loot.reason or ''}",
        f"league: {rt.league}, prices from {rt.price_league}",
        "",
        "== settings (no secrets) ==",
        json.dumps(_settings(rt), indent=2, default=str),
    ]  # fmt: skip
    return "\n".join(lines) + "\n"


def build(rt: AppRuntime) -> bytes:
    """The report as .zip bytes."""
    lines, client = client_lines(rt.log_path)
    campaign = _campaign(rt)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("report.txt", report_text(rt, client, campaign))
        z.writestr("client-log.txt", "\n".join(lines) + "\n")
        z.writestr("campaign.json", json.dumps(campaign, indent=2, default=str))
        folder = logs.log_dir(rt.cfg.db_path.parent)
        if folder.is_dir():
            for f in sorted(folder.iterdir()):
                if f.is_file():
                    with contextlib.suppress(OSError):  # e.g. locked
                        z.write(f, f"logs/{f.name}")
    return buf.getvalue()


def save(rt: AppRuntime) -> Path:
    """Write a report into <data folder>/reports (keeping the last few); returns its path."""
    folder = rt.cfg.db_path.parent / REPORTS_DIR
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"poefire-report-{datetime.now():%Y%m%d-%H%M%S}.zip"
    path.write_bytes(build(rt))
    for old in sorted(folder.glob("poefire-report-*.zip"))[:-KEEP_REPORTS]:
        old.unlink(missing_ok=True)
    return path
