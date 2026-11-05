"""In-game overlay: its data (campaign / mapping), window behaviour and settings."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_campaign import T0, H, Log, env  # noqa: F401  (env is a fixture)
from test_web import HEADERS, app  # noqa: F401  (app is a fixture)

from poefire.web import overlay_render
from poefire.web.overlay_window import PREVIEW, OverlayController
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
        self.images: list[Any] = []
        self.pos = (1000, 500)
        self.captured = False
        self.wakes = 0
        self.closed = False

    def foreground(self) -> int:
        return self.front

    def window_info(self, hwnd: int) -> tuple[str, str]:
        return self.titles.get(hwnd, ("", ""))

    def set_click_through(self, hwnd: int, on: bool) -> None:
        self.click_through.append(on)

    def show(self, hwnd: int, visible: bool) -> None:
        self.shown.append(visible)

    def present(self, hwnd: int, image: Any) -> None:
        self.images.append(image)

    def move(self, hwnd: int, x: int, y: int) -> None:
        self.pos = (x, y)

    def cursor(self) -> tuple[int, int]:
        return self.mouse

    def rect(self, hwnd: int) -> tuple[int, int, int, int]:
        x, y = self.pos
        w, h = self.images[-1].size if self.images else (1, 1)
        return (x, y, x + w, y + h)

    def scale(self, hwnd: int) -> float:
        return 2.0

    def capture(self, hwnd: int, on: bool) -> None:
        self.captured = on

    def wake(self, hwnd: int) -> None:
        self.wakes += 1

    def close(self, hwnd: int) -> None:
        self.closed = True


CURRENT = {"name": "Strand Map", "status": "active", "seconds": 30}
MAPPING: dict[str, Any] = {"mode": "mapping", "maps": 3, "avg_chaos": 12.5, "avg_divine": 0.07,
                           "chaos": 37.5, "divine": 0.21, "current": CURRENT}  # fmt: skip


def overlay(**kw: Any) -> tuple[OverlayController, FakeWin32, list[tuple[int, int]]]:
    win, saved = FakeWin32(), []
    ov = OverlayController(win, lambda: MAPPING, lambda x, y: saved.append((x, y)), **kw)
    now = [100.0]
    ov.clock = lambda: now[0]
    ov.attach(7)
    ov.fetch()
    return ov, win, saved


def test_overlay_window_follows_the_game_and_hotkeys() -> None:
    ov, win, saved = overlay()
    assert win.shown == [] and win.click_through == [True] and not ov.visible
    win.front = 100  # the game is in front
    ov.update()
    assert win.shown[-1] is True and ov.visible and len(win.images) == 1
    ov.update()
    assert len(win.images) == 1  # nothing changed: not redrawn
    win.front = 200  # alt-tabbed
    ov.update()
    assert win.shown[-1] is False
    win.front = 100
    ov.toggle()  # hidden by the hotkey even with the game in front
    ov.update()
    assert win.shown[-1] is False and not ov.enabled and win.wakes
    ov.toggle()
    ov.update()
    assert win.shown[-1] is True
    ov.toggle_move()  # unlocked: draggable, takes clicks
    ov.update()
    assert win.click_through[-1] is False
    win.front = 200  # shown while moving even if something else is in front
    ov.update()
    assert ov.visible
    ov.toggle_move()  # locked again where it is
    ov.update()
    assert win.click_through[-1] is True and saved == [(1000, 500)]
    ov.stop()
    assert win.closed


def test_overlay_is_drawn_with_transparent_surroundings_and_background_opacity() -> None:
    ov, win, _ = overlay(opacity=80)
    win.front = 200  # not the game: hidden...
    ov.set_opacity(40, preview=True)  # ...but shown for a moment to see the change
    ov.update()
    assert ov.visible
    img = win.images[-1]
    assert img.size[0] == 340 * 2  # drawn at the window's DPI (200%)
    assert img.getpixel((0, 0))[3] == 0  # around the panel: fully transparent
    inside = img.getpixel((img.size[0] // 2, img.size[1] - 20))
    assert 95 <= inside[3] <= 140  # the panel's background at 40% (over its soft shadow)
    ov.clock = lambda: 100.0 + PREVIEW + 0.1
    ov.update()
    assert not ov.visible
    ov.set_opacity(500)
    assert ov.opacity == 100
    ov.set_opacity(0)
    assert ov.opacity == 20


def test_move_button_takes_clicks_only_under_the_cursor_and_dragging() -> None:
    ov, win, saved = overlay()
    win.front = 100
    ov.update()
    assert win.click_through == [True] and not ov.hot
    x0, y0, x1, y1 = ov.button
    bx, by = (x0 + x1) // 2, (y0 + y1) // 2
    win.mouse = (1000 + bx, 500 + by)  # over the button
    ov.update()
    assert ov.hot and win.click_through[-1] is False  # the button can be clicked
    win.mouse = (1000 + 20, 500 + 60)  # over the numbers: clicks go to the game again
    ov.update()
    assert not ov.hot and win.click_through[-1] is True
    win.mouse = (1000 + bx, 500 + by)
    ov.update()
    ov.mouse_down(bx, by)
    ov.mouse_up(bx, by)  # click: Move
    assert ov.moving and win.click_through[-1] is False
    ov.mouse_down(30, 40)  # drag from the panel
    assert win.captured
    win.mouse = (1200, 640)
    ov.mouse_move(31, 41)
    assert win.pos == (1200 - 30, 640 - 40)
    ov.mouse_up(30, 40)
    assert not win.captured and ov.moving  # dropped, still unlocked
    ov.set_moving(False)  # the Lock button / hotkey
    ov.update()
    assert saved == [(1170, 600)] and win.click_through[-1] is True


def test_overlay_text() -> None:
    fr = overlay_render.frame(MAPPING, 5.4)
    assert (fr.avg_chaos, fr.avg_divine, fr.total_chaos, fr.maps, fr.map_name, fr.map_time) == (
        "12.5c", "0.07d", "37.5c", "3 maps", "Strand Map", "00:35")  # fmt: skip
    hideout = overlay_render.frame({**MAPPING, "current": {**CURRENT, "status": "paused"}}, 9)
    assert hideout.map_time == "00:30 (hideout)"
    area = {"name": "Karui Shores", "rta": 3600, "pb_rta": 3650, "delta": -50}
    camp = overlay_render.frame({"mode": "campaign", "rta": 3700, "act": 11, "class": "Deadeye",
                                 "base_class": "Ranger", "pb_class": None, "areas": [area]},
                                0.5)  # fmt: skip
    assert (camp.timer, camp.meta, camp.pb) == ("1:01:40", "Epilogue · Deadeye",
                                                "no finished Ranger run yet")  # fmt: skip
    assert camp.areas[0] == overlay_render.Area(
        "Karui Shores", "1:00:00", "1:00:50", overlay_render.MINUS + "00:50", "good"
    )
    assert overlay_render.chaos(1234.4) == "1,234c" and overlay_render.chaos(3.0) == "3c"
    for fr in (camp, overlay_render.frame(None, 0), overlay_render.frame(MAPPING, 0, moving=True)):
        assert overlay_render.render(fr, 1.25).image.size[0] == 425


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
