from datetime import datetime

import pytest

from poefire.events import (
    AreaGenerated,
    ClientClosed,
    LevelUp,
    LogOpened,
    PlayerSlain,
    StatusMode,
    StatusModeChanged,
    TradeAccepted,
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


# --- lines from a real 2026 client log (tests/data/real_client_2026-11-01.txt) ---

REAL = "2026/11/01 16:05:44 843527078 cffb065b [INFO Client 46384] "
REAL_TS = datetime(2026, 11, 1, 16, 5, 44)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (
            "2026/11/01 16:05:44 843518031 1186a8a3 [DEBUG Client 46384] "
            'Generating level 70 area "MapWorldsSpiderLair" with seed 616541807',
            AreaGenerated(REAL_TS, 70, "MapWorldsSpiderLair", 616541807),
        ),
        (
            "2026/11/01 16:05:44 843518031 1186a8a3 [DEBUG Client 46384] "
            'Generating level 60 area "HideoutSpace" with seed 1',
            AreaGenerated(REAL_TS, 60, "HideoutSpace", 1),
        ),
        (REAL + ": You have entered Celestial Nebula Hideout.",
         ZoneEntered(REAL_TS, "Celestial Nebula Hideout")),
        (REAL + ": xyz_bow has been slain.", PlayerSlain(REAL_TS, "xyz_bow")),
        (REAL + ": xyz_bow has committed suicide.", PlayerSlain(REAL_TS, "xyz_bow", suicide=True)),
        (REAL + ": xyz_bow (Deadeye) is now level 67", LevelUp(REAL_TS, "xyz_bow", "Deadeye", 67)),
        (REAL + ": Trade accepted.", TradeAccepted(REAL_TS)),
        ("2026/11/01 16:05:44 843717921 f9532a48 [INFO Client 46384] Closing game gracefully",
         ClientClosed(REAL_TS)),
    ],
)  # fmt: skip
def test_real_lines(line: str, expected: object) -> None:
    assert parse_line(line) == expected


@pytest.mark.parametrize(
    "msg",
    [
        "Tujen, the Haggler: Local! Need your aid!",  # NPC dialogue looks like local chat
        "The Envoy: They fought with such ferocity. Now the woods burn.",
        ": You have received an Atlas Skill Point.",
        ": 6 Items identified",
        "[LOADING SCREEN] (Spider Lair) Duration = 0.920201 seconds",
        "[SCENE] Set Source [Spider Lair]",
        "Joined guild named someguild with 15 members",
        "Client-Safe Instance ID = 3053050573",
        "Successfully allocated passive skill id: life1119, name: Evasion and Life",
    ],
)
def test_real_noise_is_ignored(msg: str) -> None:
    assert parse_line(REAL + msg) is None
