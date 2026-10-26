from datetime import datetime

import pytest

from poefire.events import (
    AreaGenerated,
    LevelUp,
    LogOpened,
    PlayerSlain,
    StatusMode,
    StatusModeChanged,
    Whisper,
    WhisperDirection,
    ZoneEntered,
)
from poefire.parser import parse_line

PREFIX = "2024/07/27 21:04:11 123456789 cffb0719 [INFO Client 12345] "
TS = datetime(2024, 7, 27, 21, 4, 11)


def test_area_generated() -> None:
    line = (
        "2024/07/27 21:04:11 123456789 cdb3d1e2 [DEBUG Client 12345] "
        'Generating level 83 area "MapWorldsStrand" with seed 2918734501'
    )
    assert parse_line(line) == AreaGenerated(TS, 83, "MapWorldsStrand", 2918734501)


def test_zone_entered() -> None:
    assert parse_line(PREFIX + ": You have entered Felled Hideout.") == ZoneEntered(
        TS, "Felled Hideout"
    )


def test_zone_with_apostrophe_and_crlf() -> None:
    assert parse_line(PREFIX + ": You have entered Lioneye's Watch.\r\n") == ZoneEntered(
        TS, "Lioneye's Watch"
    )


def test_slain() -> None:
    assert parse_line(PREFIX + ": TestWitch has been slain.") == PlayerSlain(TS, "TestWitch")


def test_level_up() -> None:
    assert parse_line(PREFIX + ": Test_Witch (Necromancer) is now level 92") == LevelUp(
        TS, "Test_Witch", "Necromancer", 92
    )


@pytest.mark.parametrize(
    ("msg", "expected"),
    [
        (
            "@From Buyer: wtb",
            Whisper(TS, WhisperDirection.FROM, "Buyer", None, "wtb"),
        ),
        (
            "@From <TRADE> Buyer_One: Hi, I'd like: 1 divine",
            Whisper(TS, WhisperDirection.FROM, "Buyer_One", "TRADE", "Hi, I'd like: 1 divine"),
        ),
        (
            "@To Seller: ty",
            Whisper(TS, WhisperDirection.TO, "Seller", None, "ty"),
        ),
    ],
)
def test_whispers(msg: str, expected: Whisper) -> None:
    assert parse_line(PREFIX + msg) == expected


def test_afk_on_off() -> None:
    assert parse_line(
        PREFIX + ': AFK mode is now ON. Autoreply "This player is AFK."'
    ) == StatusModeChanged(TS, StatusMode.AFK, True, "This player is AFK.")
    assert parse_line(PREFIX + ": AFK mode is now OFF.") == StatusModeChanged(
        TS, StatusMode.AFK, False, None
    )


def test_dnd_on() -> None:
    assert parse_line(PREFIX + ': DND mode is now ON. Autoreply "busy"') == StatusModeChanged(
        TS, StatusMode.DND, True, "busy"
    )


def test_log_opening() -> None:
    assert parse_line("2024/07/27 21:04:11 ***** LOG FILE OPENING *****") == LogOpened(TS)


@pytest.mark.parametrize(
    "line",
    [
        "",
        "garbage",
        PREFIX + "[SHADER] Delay: OFF",
        PREFIX + "Bob: You have entered Hideout.",  # local chat must not spoof zone entry
        PREFIX + "#Bob: Alice has been slain.",  # global chat
        PREFIX + "$Bob: You have entered Hideout.",  # trade chat
        PREFIX + "@From Bob",  # truncated whisper
        PREFIX + ": Bob has been slain by a monster.",
        PREFIX + 'Generating level x area "A" with seed 1',
    ],
)
def test_ignored_lines(line: str) -> None:
    assert parse_line(line) is None
