"""In-game shortcuts: what's sent for a chat command or a search, the hotkeys only existing while
the game has focus, the config entries and the Settings validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient

from poefire import gameinput as gi
from poefire.config import Config
from poefire.gameinput import ChatCommand, GameHotkeys, GameInput, SearchString

NAMES = {gi.VK_RETURN: "Enter", gi.VK_CONTROL: "Ctrl", gi.VK_SHIFT: "Shift", gi.VK_A: "A",
         gi.VK_F: "F", gi.VK_V: "V", gi.VK_MENU: "Alt"}  # fmt: skip


class FakeGame:
    def __init__(self, focused: bool = True, held: set[int] | None = None) -> None:
        self.focused = focused
        self.held = held or set()  # keys the player still holds
        self.events: list[str] = []
        self.clipboard: str | None = "my own text"
        self.pasted: list[str] = []

    def game_focused(self) -> bool:
        return self.focused

    def key_down(self, vk: int) -> bool:
        return vk in self.held

    def send_key(self, vk: int, up: bool) -> None:
        self.events.append(("-" if up else "+") + NAMES.get(vk, hex(vk)))
        if up:
            self.held.discard(vk)
        if vk == gi.VK_V and not up:
            self.pasted.append(self.clipboard or "")

    def get_clipboard(self) -> str | None:
        return self.clipboard

    def set_clipboard(self, text: str) -> bool:
        self.clipboard = text
        return True


def sender(game: FakeGame) -> GameInput:
    return GameInput(game, sleep=lambda s: None)


def test_chat_command_opens_chat_pastes_and_sends() -> None:
    game = FakeGame()
    assert sender(game).chat("/hideout")
    assert game.events == ["+Enter", "-Enter", "+Ctrl", "+A", "-A", "-Ctrl",
                           "+Ctrl", "+V", "-V", "-Ctrl", "+Enter", "-Enter"]  # fmt: skip
    assert game.pasted == ["/hideout"]
    assert game.clipboard == "my own text"  # put back afterwards


def test_search_fills_the_open_search_box() -> None:
    game = FakeGame()
    assert sender(game).search('"!ele|!reg"')
    assert game.events[:2] == ["+Ctrl", "+F"] and "+Enter" not in game.events  # no Enter
    assert game.pasted == ['"!ele|!reg"'] and game.clipboard == "my own text"


def test_nothing_is_sent_outside_the_game() -> None:
    game = FakeGame(focused=False)
    assert not sender(game).chat("/hideout")
    assert game.events == [] and game.clipboard == "my own text"


def test_held_modifiers_are_released_first() -> None:
    game = FakeGame(held={gi.VK_CONTROL, gi.VK_SHIFT})  # the hotkey was Ctrl+Shift+1, still held
    sender(game).chat("/exit")
    assert game.events[:2] == ["-Ctrl", "-Shift"]  # else Enter would go out as Ctrl+Shift+Enter
    assert game.events[2] == "+Enter"


def test_entries_round_trip() -> None:
    c = ChatCommand.parse(" F5 | /hideout ")
    assert c == ChatCommand("F5", "/hideout") and c.format() == "F5|/hideout"
    s = SearchString.parse("Ctrl+1|Bad maps|!ele|!reg")
    assert s == SearchString("Ctrl+1", "Bad maps", "!ele|!reg")  # "|" in the text is kept
    assert SearchString.parse(s.format()) == s
    assert gi.chat_commands(["|/kingsmarch", "F5|", "bad"]) == [ChatCommand("", "/kingsmarch")]
    cfg = Config(search_strings=[s.format()])
    assert Config.from_dict({"search_strings": cfg.search_strings}).search_strings == [s.format()]
    assert Config().chat_commands[:2] == ["F5|/hideout", "F9|/exit"]


class FakeHotkey:
    made: ClassVar[list[str]] = []
    live: ClassVar[set[str]] = set()

    def __init__(self, spec: str, action: Any) -> None:
        self.spec = spec
        self.error: str | None = None
        FakeHotkey.made.append(spec)

    def start(self) -> bool:
        if self.spec == "F9":
            self.error = "F9 is already used by another program; pick another hotkey"
            return False
        FakeHotkey.live.add(self.spec)
        return True

    def stop(self) -> None:
        FakeHotkey.live.discard(self.spec)


def test_hotkeys_only_exist_while_the_game_has_focus() -> None:
    focused = [False]
    gh = GameHotkeys({"F5": lambda: None, "F9": lambda: None}, lambda: focused[0],
                     make=FakeHotkey)  # type: ignore[arg-type]  # fmt: skip
    gh.tick()
    assert FakeHotkey.live == set()  # browser in front: F5 refreshes it as usual
    focused[0] = True
    gh.tick()
    assert FakeHotkey.live == {"F5"} and "F9" in gh.errors  # taken by another program
    gh.tick()
    assert FakeHotkey.made.count("F5") == 1  # not re-registered every check
    focused[0] = False
    gh.tick()
    assert FakeHotkey.live == set()
    gh.stop()


def test_settings_validate_shortcuts(tmp_path: Path) -> None:
    from test_web import OfflineNinja

    from poefire.pricing.service import PriceService
    from poefire.store import Store
    from poefire.web.app import create_app
    from poefire.web.runtime import AppRuntime

    db = tmp_path / "db"
    rt = AppRuntime(Config(db_path=db, league="Keepers"),
                    PriceService(OfflineNinja(), lambda: Store(db)), recorder=None,
                    config_path=tmp_path / "config.toml")  # fmt: skip
    client = TestClient(create_app(rt))
    client.headers["host"] = "127.0.0.1:8460"
    h = {"X-Poefire": "1"}
    s = client.get("/api/settings").json()
    assert s["chat_commands"][0] == {"key": "F5", "text": "/hideout"}
    assert s["search_strings"] == [] and "available" in s["game_hotkeys"]

    def post(**body: Any) -> Any:
        return client.post("/api/settings", headers=h, json=body)

    maps = {"key": "ctrl+shift+1", "name": "Maps", "text": "!ele|!reg"}
    r = post(chat_commands=[{"key": "f6", "text": "/hideout"}, {"key": "", "text": "/delve"}],
             search_strings=[maps])  # fmt: skip
    assert r.status_code == 200, r.text
    assert rt.cfg.chat_commands == ["F6|/hideout", "|/delve"]
    assert rt.cfg.search_strings == ["Ctrl+Shift+1|Maps|!ele|!reg"]
    assert "F6|/hideout" in (tmp_path / "config.toml").read_text()
    for bad, why in [
        ({"chat_commands": [{"key": "F6", "text": "/a"}, {"key": "F6", "text": "/b"}]},
         "already used"),
        ({"chat_commands": [{"key": "Ctrl+Alt+R", "text": "/a"}]}, "already used"),  # campaign
        ({"chat_commands": [{"key": "H", "text": "/a"}]}, "modifier"),  # would fire in chat
        ({"search_strings": [{"key": "F6", "name": "x", "text": "y"}]}, "already used"),
        ({"chat_commands": [{"key": "", "text": "/a\n/b"}]}, "one line"),
    ]:  # fmt: skip
        r = post(**bad)
        assert r.status_code == 400 and why in r.text, (bad, r.text)
    rt.stop()


@pytest.mark.parametrize("platform_ok", [False])
def test_no_backend_off_windows(platform_ok: bool) -> None:
    import sys

    if sys.platform == "win32":
        pytest.skip("Windows has the real backend")
    assert gi.win32_backend() is None
    assert not GameInput(None).chat("/hideout")  # quietly nothing


@pytest.mark.skipif(not __import__("sys").platform.startswith("win"), reason="Windows only")
def test_windows_backend_clipboard_round_trip() -> None:  # runs in CI's Windows job
    b = gi.win32_backend()
    assert b is not None
    old = b.get_clipboard()
    try:
        assert b.set_clipboard("/hideout ünïcode")
        assert b.get_clipboard() == "/hideout ünïcode"
    finally:
        if old is not None:
            b.set_clipboard(old)
    assert b.game_focused() is False  # no game on the CI runner
    assert b.key_down(gi.VK_CONTROL) is False


def test_saved_regexes() -> None:
    r = gi.SavedRegex.parse(" Bad maps |!ele|!reg")
    assert r == gi.SavedRegex("Bad maps", "!ele|!reg") and gi.SavedRegex.parse(r.format()) == r
    entries = ["Bad maps|!ele|!reg", "|nameless", "Empty|", "Rares|rare"]
    assert [x.name for x in gi.saved_regexes(entries)] == ["Bad maps", "Rares"]
    assert gi.active_regex(entries, "Rares") == gi.SavedRegex("Rares", "rare")
    assert gi.active_regex(entries, "") is None and gi.active_regex(entries, "Gone") is None


def test_settings_save_regexes_and_bind_the_active_one(tmp_path: Path) -> None:
    from test_web import OfflineNinja

    from poefire.pricing.service import PriceService
    from poefire.store import Store
    from poefire.web.app import create_app
    from poefire.web.runtime import AppRuntime

    db = tmp_path / "db"
    rt = AppRuntime(Config(db_path=db, league="Keepers"),
                    PriceService(OfflineNinja(), lambda: Store(db)), recorder=None,
                    config_path=tmp_path / "config.toml")  # fmt: skip
    client = TestClient(create_app(rt))
    client.headers["host"] = "127.0.0.1:8460"
    s = client.get("/api/settings").json()
    assert s["regexes"] == [] and s["regex_active"] == "" and s["regex_hotkey"] == "Ctrl+B"

    def post(**body: Any) -> Any:
        return client.post("/api/settings", headers={"X-Poefire": "1"}, json=body)

    two = [{"name": "Bad maps", "text": "!ele|!reg"}, {"name": "Rares", "text": "rare"}]
    r = post(regexes=two, regex_active="Rares", regex_hotkey="ctrl+alt+g")
    assert r.status_code == 200, r.text
    assert rt.cfg.regexes == ["Bad maps|!ele|!reg", "Rares|rare"]
    assert (rt.cfg.regex_active, rt.cfg.regex_hotkey) == ("Rares", "Ctrl+Alt+G")
    assert client.get("/api/settings").json()["regexes"] == two

    rt.game_input = sender(FakeGame(focused=False))  # nothing gets registered with Windows
    rt._build_game_hotkeys()
    assert rt.game_hotkeys is not None
    rt.game_hotkeys.stop()
    game = FakeGame()
    rt.game_input = sender(game)  # the binding sends through the runtime's current sender
    rt.game_hotkeys.bindings["Ctrl+Alt+G"]()
    assert game.pasted == ["rare"]  # the active regex, into the search box

    assert post(regexes=two[:1]).status_code == 200
    assert rt.cfg.regex_active == ""  # the active one was removed
    bads: list[tuple[dict[str, Any], str]] = [
        ({"regexes": [two[0], {"name": "bad MAPS", "text": "x"}]}, "two regexes"),
        ({"regexes": [{"name": "a|b", "text": "x"}]}, "can't contain"),
        ({"regex_active": "Nope"}, "isn't saved"),
        ({"regex_hotkey": "Ctrl+Alt+R"}, "already used"),  # the campaign hotkey
        ({"search_strings": [{"key": "Ctrl+Alt+G", "name": "x", "text": "y"}],
          "regex_hotkey": "Ctrl+Alt+G"}, "already used"),
    ]  # fmt: skip
    for bad, why in bads:
        r = post(**bad)
        assert r.status_code == 400 and why in r.text, (bad, r.text)
    rt.stop()
