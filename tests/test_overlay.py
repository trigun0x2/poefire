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
        self.mouse = (0, 0)
        self.front = 0
        self.titles = {100: ("Path of Exile", "POEWindowClass"), 200: ("Discord", "Chrome")}
        self.shown: list[bool] = []
        self.click_through: list[bool] = []
        self.layered: list[tuple[tuple[int, int, int], int]] = []

    def foreground(self) -> int:
        return self.front

    def window_info(self, hwnd: int) -> tuple[str, str]:
        return self.titles.get(hwnd, ("", ""))

    def set_click_through(self, hwnd: int, on: bool) -> None:
        self.click_through.append(on)

    def set_layered(self, hwnd: int, key: tuple[int, int, int], alpha: int) -> None:
        self.layered.append((key, alpha))

    def show(self, hwnd: int, visible: bool) -> None:
        self.shown.append(visible)

    def cursor(self) -> tuple[int, int]:
        return self.mouse

    def rect(self, hwnd: int) -> tuple[int, int, int, int]:
        return (1000, 500, 1000 + 340 * 2, 500 + 196 * 2)  # at 200% scaling

    def scale(self, hwnd: int) -> float:
        return 2.0


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
    assert s["overlay_opacity"] == 90
    r = app.post("/api/settings", headers=HEADERS, json={"overlay_opacity": 5})
    assert r.status_code == 422  # 20-100
    r = app.post("/api/overlay/opacity", headers=HEADERS, json={"percent": 60, "save": False})
    assert r.json() == {"ok": True}
    assert app.get("/api/settings").json()["overlay_opacity"] == 90  # only previewed
    app.post("/api/overlay/opacity", headers=HEADERS, json={"percent": 60})
    assert app.get("/api/settings").json()["overlay_opacity"] == 60


def test_overlay_is_keyed_transparent_with_adjustable_opacity() -> None:
    from poefire.web.overlay_window import KEY_COLOR, PREVIEW

    win = FakeWin32()
    ov = OverlayController(FakeWindow(), win, lambda x, y: None, opacity=80)
    now = [1000.0]
    ov.clock = lambda: now[0]
    ov.attach(7)
    ov.stop()
    assert win.layered == [(KEY_COLOR, 204)]  # the background keyed out, the rest at 80%
    win.front = 200  # not the game: hidden...
    ov.set_opacity(40, preview=True)  # ...but shown for a moment to see the change
    assert win.layered[-1] == (KEY_COLOR, 102) and ov.visible
    now[0] += PREVIEW + 0.1
    ov.update()
    assert not ov.visible
    ov.set_opacity(500)
    assert ov.opacity == 100 and win.layered[-1] == (KEY_COLOR, 255)
    ov.set_opacity(0)
    assert ov.opacity == 20


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


def test_move_button_corner_takes_clicks_only_under_the_cursor() -> None:
    from poefire.web.overlay_window import BUTTON_AREA, OverlayApi

    win = FakeWin32()
    ov = OverlayController(FakeWindow(), win, lambda x, y: None)
    ov.attach(7)
    ov.stop()
    win.front = 100
    ov.update()
    assert win.click_through == [True] and not ov.hot
    x0, y0, _, _ = BUTTON_AREA
    win.mouse = (1000 + (x0 + 10) * 2, 500 + (y0 + 10) * 2)  # over the button (200% scale)
    ov.update()
    assert ov.hot and win.click_through[-1] is False  # the button can be clicked
    win.mouse = (1000 + 20, 500 + 150)  # over the splits: clicks go to the game again
    ov.update()
    assert not ov.hot and win.click_through[-1] is True
    api = OverlayApi()
    api._attach(ov)
    assert [n for n in vars(api) if not n.startswith("_")] == []  # nothing for pywebview to walk
    api.move()  # the button
    assert ov.moving and win.click_through[-1] is False
    api.move()  # already moving: no change
    assert ov.moving
    api.lock()
    assert not ov.moving and win.click_through[-1] is True
