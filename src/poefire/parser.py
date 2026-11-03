"""Client.txt line parser.

Line layout (English client):

    2024/07/27 21:04:11 123456789 cffb0719 [INFO Client 12345] : You have entered Hideout.
    <date>     <time>   <uptime>  <hash>   [<lvl> Client <pid>] <message>

The `LOG FILE OPENING` banner has only the date and time. System messages (zone entry,
deaths, level-ups, AFK/DND, trades) are prefixed with ": "; whispers start with "@"; area
generation and `Closing game gracefully` have no prefix. Anchoring on those prefixes keeps
chat and NPC lines such as "Tujen, the Haggler: Local! Need your aid!" from matching.

Verified against a real 2026 client log (tests/data/real_client_2026-11-01.txt): area
generation, zone entry, slain/suicide, level-up, trade accepted, game closing. Still
unverified: whispers, AFK/DND, the log-opening banner.
"""

from __future__ import annotations

import re
from datetime import datetime

from poefire.events import (
    AreaGenerated,
    ClientClosed,
    LevelUp,
    LogEvent,
    LogOpened,
    PlayerSlain,
    StatusMode,
    StatusModeChanged,
    TradeAccepted,
    Whisper,
    WhisperDirection,
    ZoneEntered,
)

_LINE_RE = re.compile(
    r"^(?P<y>\d{4})/(?P<mo>\d{2})/(?P<d>\d{2}) (?P<h>\d{2}):(?P<mi>\d{2}):(?P<s>\d{2}) "
    r"(?:\d+ [0-9a-fA-F]+ \[[A-Z]+ Client \d+\] )?"
    r"(?P<msg>.*)$"
)

_GENERATING_RE = re.compile(
    r'^Generating level (?P<level>\d+) area "(?P<area_id>[^"]+)" with seed (?P<seed>\d+)$'
)
_ENTERED_RE = re.compile(r"^You have entered (?P<zone>.+)\.$")
_SLAIN_RE = re.compile(r"^(?P<name>\S+) has (?P<how>been slain|committed suicide)\.$")
_LEVEL_RE = re.compile(r"^(?P<name>\S+) \((?P<cls>[A-Za-z]+)\) is now level (?P<level>\d+)$")
_STATUS_RE = re.compile(
    r'^(?P<mode>AFK|DND) mode is now (?P<state>ON|OFF)\.(?: Autoreply "(?P<reply>.*)")?$'
)
_WHISPER_RE = re.compile(
    r"^@(?P<dir>From|To) (?:<(?P<guild>[^>]*)> )?(?P<name>[^\s:<>]+): (?P<msg>.*)$"
)
_LOG_OPENING = "***** LOG FILE OPENING *****"
_CLIENT_CLOSING = "Closing game gracefully"


def parse_line(line: str) -> LogEvent | None:
    """Parse one raw log line (trailing CR/LF allowed). Returns None for uninteresting lines."""
    m = _LINE_RE.match(line.rstrip("\r\n"))
    if m is None:
        return None
    msg = m["msg"]
    # Cheap prefix dispatch before any message regex: almost every line is noise.
    if msg.startswith(": "):
        return _parse_system(_timestamp(m), msg[2:])
    if msg.startswith("@"):
        return _parse_whisper(_timestamp(m), msg)
    if msg.startswith("Generating level "):
        g = _GENERATING_RE.match(msg)
        if g is None:
            return None
        return AreaGenerated(
            ts=_timestamp(m),
            area_level=int(g["level"]),
            area_id=g["area_id"],
            seed=int(g["seed"]),
        )
    if msg == _LOG_OPENING:
        return LogOpened(ts=_timestamp(m))
    if msg == _CLIENT_CLOSING:
        return ClientClosed(ts=_timestamp(m))
    return None


def _timestamp(m: re.Match[str]) -> datetime:
    return datetime(int(m["y"]), int(m["mo"]), int(m["d"]), int(m["h"]), int(m["mi"]), int(m["s"]))


def _parse_system(ts: datetime, body: str) -> LogEvent | None:
    if body.startswith("You have entered "):
        e = _ENTERED_RE.match(body)
        return ZoneEntered(ts=ts, zone=e["zone"]) if e else None
    if body == "Trade accepted.":
        return TradeAccepted(ts=ts)
    if body.endswith((" has been slain.", " has committed suicide.")):
        s = _SLAIN_RE.match(body)
        if s is None:
            return None
        return PlayerSlain(ts=ts, character=s["name"], suicide=s["how"] == "committed suicide")
    if " is now level " in body:
        lv = _LEVEL_RE.match(body)
        if lv is None:
            return None
        return LevelUp(
            ts=ts, character=lv["name"], character_class=lv["cls"], level=int(lv["level"])
        )
    if " mode is now " in body:
        st = _STATUS_RE.match(body)
        if st is None:
            return None
        return StatusModeChanged(
            ts=ts,
            mode=StatusMode(st["mode"].lower()),
            enabled=st["state"] == "ON",
            autoreply=st["reply"],
        )
    return None


def _parse_whisper(ts: datetime, msg: str) -> Whisper | None:
    w = _WHISPER_RE.match(msg)
    if w is None:
        return None
    return Whisper(
        ts=ts,
        direction=WhisperDirection(w["dir"].lower()),
        character=w["name"],
        guild=w["guild"],
        message=w["msg"],
    )
