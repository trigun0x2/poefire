"""In-game overlay: its data (campaign / mapping), window behaviour and settings."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_campaign import T0, H, Log, env  # noqa: F401  (env is a fixture)
from test_web import HEADERS, app  # noqa: F401  (app is a fixture)

from poefire.web.overlay_window import OverlayController
from poefire.web.runtime import AppRuntime


def test_campaign_overlay_shows_last_three_areas_against_the_class_pb(
    env: tuple[TestClient, AppRuntime, Path],  # noqa: F811
) -> None:
    client, rt, path = env
    assert client.get("/api/overlay").json()["mode"] == "mapping"  # no campaign run
    log = Log()
    zones = [("1_1_1", "The Twilight Strand"), ("1_1_town", "Lioneye's Watch"),
             ("1_1_2", "The Coast"), ("1_1_3", "The Mud Flats")]  # fmt: skip

    def play(pace: int) -> None:
        log.wait(1)
        log.enter(*zones[0])
        log.level(2, "Ranger", "Speedy")
        for area in zones[1:]:
            log.wait(pace)
            log.enter(*area)

    client.post("/api/campaign/start", json={}, headers=H)  # the PB run
    play(60)
    path.write_text("\n".join(log.lines) + "\n")
    rt.service.poll()  # type: ignore[union-attr]
    client.post("/api/campaign/finish", headers=H)

    client.post("/api/campaign/start", json={}, headers=H)  # a faster run
    log.lines.clear()
    log.t = T0
    play(50)
    with path.open("a") as f:
        f.write("\n".join(log.lines) + "\n")
    rt.service.poll()  # type: ignore[union-attr]
    d = client.get("/api/overlay").json()
    assert d["mode"] == "campaign" and d["class"] == "Ranger" and d["pb_class"] == "Ranger"
    assert [a["name"] for a in d["areas"]] == ["The Mud Flats", "The Coast", "Lioneye's Watch"]
    mud = d["areas"][0]
    assert mud["pb_rta"] is not None and mud["delta"] == mud["rta"] - mud["pb_rta"] < 0
    rt.stop()


def test_mapping_overlay(app: TestClient) -> None:  # noqa: F811
    d = app.get("/api/overlay").json()
    assert d["mode"] == "mapping" and d["maps"] == 5
    assert d["avg_chaos"] == 220.0 and d["chaos"] == 220.0 and d["avg_divine"] == 1.1
    assert d["current"]["name"] == "Spider Lair" and d["current"]["status"] == "active"
    assert d["current"]["seconds"] == 10 + 70 + 32


class FakeWin32:
    def __init__(self) -> None:
        self.front = 0
        self.titles = {100: ("Path of Exile", "POEWindowClass"), 200: ("Discord", "Chrome")}
        self.shown: list[bool] = []
        self.click_through: list[bool] = []

    def foreground(self) -> int:
        return self.front

    def window_info(self, hwnd: int) -> tuple[str, str]:
        return self.titles.get(hwnd, ("", ""))

    def set_click_through(self, hwnd: int, on: bool) -> None:
        self.click_through.append(on)

    def show(self, hwnd: int, visible: bool) -> None:
        self.shown.append(visible)


class FakeWindow:
    x, y = 40, 60

    def __init__(self) -> None:
        self.js: list[str] = []

    def evaluate_js(self, code: str) -> None:
        self.js.append(code)


def test_overlay_window_follows_the_game_and_hotkeys() -> None:
    win, saved = FakeWin32(), []
    window = FakeWindow()
    ov = OverlayController(window, win, lambda x, y: saved.append((x, y)))
    ov.attach(7)
    ov.stop()  # drive update() by hand
    assert win.click_through == [True] and win.shown == [False]
    win.front = 100  # the game is in front
    ov.update()
    assert win.shown[-1] is True and ov.visible
    win.front = 200  # alt-tabbed
    ov.update()
    assert win.shown[-1] is False
    win.front = 100
    ov.toggle()  # hidden by the hotkey even with the game in front
    assert win.shown[-1] is False and not ov.enabled
    ov.toggle()
    assert win.shown[-1] is True
    ov.toggle_move()  # unlocked: draggable, clicks stop passing through, shown
    assert win.click_through[-1] is False and window.js[-1] == "setMoving(true)"
    win.front = 7  # dragging it makes the overlay itself the foreground window
    ov.update()
    assert ov.visible
    ov.toggle_move()  # locked again where it is
    assert win.click_through[-1] is True and saved == [(40, 60)]


def test_overlay_settings_and_hotkeys(app: TestClient) -> None:  # noqa: F811
    s = app.get("/api/settings").json()
    assert (s["overlay_enabled"], s["overlay_toggle_hotkey"], s["overlay_move_hotkey"]) == (
        True, "Ctrl+Alt+O", "Ctrl+Alt+L")  # fmt: skip
    r = app.post("/api/settings", headers=HEADERS,
                 json={"overlay_toggle_hotkey": "ctrl+alt+r"})  # fmt: skip
    assert r.status_code == 400 and "different" in r.json()["detail"]  # campaign's hotkey
    r = app.post("/api/settings", headers=HEADERS,
                 json={"overlay_toggle_hotkey": "shift+f10", "overlay_enabled": False})  # fmt: skip
    assert r.json()["overlay_toggle_hotkey"] == "Shift+F10" and r.json()["overlay_enabled"] is False
    st = app.get("/api/status").json()["overlay"]
    assert st["available"] is False and st["toggle"]["key"] == "Shift+F10"


@pytest.mark.skipif(__import__("sys").platform == "win32", reason="registers real hotkeys")
def test_hotkeys_report_why_they_are_off(tmp_path: Path) -> None:
    from poefire.config import Config

    rt: Any = AppRuntime(Config(db_path=tmp_path / "db"), recorder=None,
                         log_path=tmp_path / "none.txt")  # fmt: skip
    rt._build_hotkeys()
    assert set(rt.hotkey_errors) == {"campaign", "overlay", "overlay_move"}
    assert "Windows" in rt.hotkey_errors["overlay"]
    assert rt.overlay_json()["toggle"] == {"key": "Ctrl+Alt+O", "active": False,
                                           "error": rt.hotkey_errors["overlay"]}  # fmt: skip
    rt.stop()
