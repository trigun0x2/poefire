"""Typed events parsed from Client.txt.

Timestamps are naive datetimes in the game client's local time, exactly as logged.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class WhisperDirection(StrEnum):
    FROM = "from"
    TO = "to"


class StatusMode(StrEnum):
    AFK = "afk"
    DND = "dnd"


@dataclass(frozen=True, slots=True)
class LogOpened:
    """`***** LOG FILE OPENING *****`: the game client (re)started."""

    ts: datetime


@dataclass(frozen=True, slots=True)
class ClientClosed:
    """`Closing game gracefully`: the client exited normally."""

    ts: datetime


@dataclass(frozen=True, slots=True)
class AreaGenerated:
    """`Generating level N area "AreaId" with seed S`: logged at the start of every load,
    including re-entering an existing instance (same seed)."""

    ts: datetime
    area_level: int
    area_id: str
    seed: int


@dataclass(frozen=True, slots=True)
class ZoneEntered:
    """`: You have entered <Zone>.`: logged when the load finishes."""

    ts: datetime
    zone: str


@dataclass(frozen=True, slots=True)
class PlayerSlain:
    """`: Name has been slain.` or `: Name has committed suicide.`"""

    ts: datetime
    character: str
    suicide: bool = False


@dataclass(frozen=True, slots=True)
class TradeAccepted:
    """`: Trade accepted.`: a player trade completed (it shows up in loot deltas)."""

    ts: datetime


@dataclass(frozen=True, slots=True)
class LevelUp:
    ts: datetime
    character: str
    character_class: str
    level: int


@dataclass(frozen=True, slots=True)
class Whisper:
    ts: datetime
    direction: WhisperDirection
    character: str
    guild: str | None
    message: str


@dataclass(frozen=True, slots=True)
class StatusModeChanged:
    ts: datetime
    mode: StatusMode
    enabled: bool
    autoreply: str | None


type LogEvent = (
    LogOpened
    | ClientClosed
    | AreaGenerated
    | ZoneEntered
    | PlayerSlain
    | LevelUp
    | Whisper
    | StatusModeChanged
    | TradeAccepted
)


def event_kind(event: LogEvent) -> str:
    return type(event).__name__


def event_payload_json(event: LogEvent) -> str:
    """JSON of every field except `ts` (stored in its own column)."""
    data: dict[str, Any] = {
        f.name: getattr(event, f.name) for f in dataclasses.fields(event) if f.name != "ts"
    }
    return json.dumps(data, ensure_ascii=False, sort_keys=True)
