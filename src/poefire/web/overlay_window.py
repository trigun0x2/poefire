"""The in-game overlay window (Windows app only).

A small always-on-top layered window drawn by poefire itself (overlay_render.py makes the
image, Windows shows it with per-pixel alpha), so only the rounded panel is visible. It:

- shows while Path of Exile is the foreground window (and hides otherwise), without taking
  the focus from the game;
- lets mouse clicks through to the game, except on its Move button while the cursor is over
  it; Move (or the move hotkey) unlocks it to be dragged, Lock saves where it is;
- is turned on and off with the toggle hotkey (and the Settings switch).

It only reads which window is in front (by title/class) and the cursor position, and draws
over the game: no input is sent to the game and nothing of the game is read.

Threads: the window and its message loop run on their own thread (overlay_win32.py), which
calls `update()` 10 times a second; the data is fetched once a second on another thread.
Hotkeys and Settings call the request methods (toggle, set_opacity, ...) from anywhere: they
only change state and wake the window thread.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

from poefire.web import overlay_render

log = logging.getLogger(__name__)

POSITION_KEY = "overlay_position"
DEFAULT_POSITION = (24, 240)
GAME_TITLES = ("Path of Exile",)
GAME_CLASSES = ("POEWindowClass",)
TICK = 0.1  # seconds between foreground / cursor checks (and timer redraws)
FETCH = 1.0  # seconds between data fetches while shown
PREVIEW = 3.0  # seconds the overlay stays shown after an opacity change (to see it)
MIN_OPACITY, MAX_OPACITY = 20, 100  # percent (of the panel's background)

type Json = dict[str, Any]
type Box = tuple[int, int, int, int]


class Win32(Protocol):
    """What the controller needs from Windows (overlay_win32.Win32; a fake in tests)."""

    def foreground(self) -> int: ...
    def window_info(self, hwnd: int) -> tuple[str, str]: ...
    def set_click_through(self, hwnd: int, on: bool) -> None: ...
    def show(self, hwnd: int, visible: bool) -> None: ...
    def present(self, hwnd: int, image: Any) -> None: ...
    def move(self, hwnd: int, x: int, y: int) -> None: ...
    def cursor(self) -> tuple[int, int]: ...
    def rect(self, hwnd: int) -> Box: ...
    def scale(self, hwnd: int) -> float: ...
    def capture(self, hwnd: int, on: bool) -> None: ...
    def wake(self, hwnd: int) -> None: ...
    def close(self, hwnd: int) -> None: ...


def clamp_opacity(percent: int) -> int:
    return max(MIN_OPACITY, min(MAX_OPACITY, int(percent)))


class OverlayController:
    def __init__(
        self,
        win32: Win32,
        state: Callable[[], Json | None],
        save_position: Callable[[int, int], None],
        enabled: bool = True,
        opacity: int = 90,
    ) -> None:
        self.win32 = win32
        self.state = state  # /api/overlay's data
        self.save_position = save_position
        self.enabled = enabled  # the toggle hotkey / Settings switch
        self.opacity = clamp_opacity(opacity)
        self.moving = False  # unlocked: draggable, takes clicks
        self.visible = False
        self.hot = False  # the cursor is over the Move button (it takes clicks)
        self.hwnd = 0
        self.clock: Callable[[], float] = time.monotonic
        self.button: Box = (0, 0, 0, 0)  # px from the window's top-left
        self._data: tuple[Json | None, float] = (None, 0.0)  # (data, fetched at)
        self._through: bool | None = None  # click-through as last set
        self._was_moving = False
        self._drawn: tuple[Any, ...] | None = None
        self._preview_until = 0.0
        self._drag: tuple[int, int] | None = None  # cursor offset in the window while dragging
        self._pressed = False  # mouse went down on the button
        self._stop = threading.Event()
        self._fetch_now = threading.Event()

    # --- requests (any thread) ---

    def toggle(self) -> None:
        self.set_enabled(not self.enabled)

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        self._wake()

    def set_moving(self, moving: bool) -> None:
        if moving != self.moving:
            self.toggle_move()

    def toggle_move(self) -> None:
        """Unlock to drag the overlay; again to lock it where it is (and remember that)."""
        self.moving = not self.moving
        self._wake()

    def set_opacity(self, percent: int, preview: bool = False) -> None:
        """`preview`: show it for a few seconds even when the game isn't in front."""
        self.opacity = clamp_opacity(percent)
        if preview:
            self._preview_until = self.clock() + PREVIEW
        self._wake()

    def stop(self) -> None:
        """Close the window and end its threads."""
        self._stop.set()
        self._fetch_now.set()
        if self.hwnd:
            self.win32.close(self.hwnd)

    def _wake(self) -> None:
        if self.hwnd:
            self.win32.wake(self.hwnd)

    # --- data (its own thread) ---

    def fetch(self) -> None:
        try:
            data = self.state()
        except Exception:
            log.exception("overlay data failed")
            return
        self._data = (data, self.clock())
        self._wake()

    def run_fetcher(self) -> None:
        while not self._stop.is_set():
            if self.visible or self._fetch_now.is_set():
                self._fetch_now.clear()
                self.fetch()
            self._fetch_now.wait(FETCH)

    # --- the window thread ---

    def attach(self, hwnd: int) -> None:
        self.hwnd = hwnd
        self._fetch_now.set()
        self.update()

    def game_in_front(self) -> bool:
        fg = self.win32.foreground()
        if not fg:
            return False
        if fg == self.hwnd:
            return True
        title, cls = self.win32.window_info(fg)
        return title in GAME_TITLES or cls in GAME_CLASSES

    def wanted(self) -> bool:
        previewing = self.clock() < self._preview_until
        return self.enabled and (self.moving or previewing or self.game_in_front())

    def in_button(self, x: int, y: int) -> bool:
        x0, y0, x1, y1 = self.button
        return x0 <= x < x1 and y0 <= y < y1

    def over_button(self) -> bool:
        left, top, _, _ = self.win32.rect(self.hwnd)
        x, y = self.win32.cursor()
        return self.in_button(x - left, y - top)

    def update(self) -> None:
        if not self.hwnd:
            return
        want = self.wanted()
        if want and not self.visible:
            self._fetch_now.set()  # fresh numbers as it appears
        self.hot = want and not self.moving and self.over_button()
        through = not (self.moving or self.hot)
        if through != self._through:
            self.win32.set_click_through(self.hwnd, through)
            self._through = through
        if self._was_moving and not self.moving:
            self._drag = None
            self.win32.capture(self.hwnd, False)
            left, top, _, _ = self.win32.rect(self.hwnd)
            try:
                self.save_position(left, top)
            except Exception:
                log.exception("couldn't save the overlay position")
        self._was_moving = self.moving
        if want:
            self._draw()
        if want != self.visible:
            self.win32.show(self.hwnd, want)
            self.visible = want

    def _draw(self) -> None:
        data, fetched_at = self._data
        fr = overlay_render.frame(data, self.clock() - fetched_at, self.moving, self.hot)
        scale = self.win32.scale(self.hwnd)
        key = (fr, scale, self.opacity)
        if key == self._drawn:
            return
        r = overlay_render.render(fr, scale, self.opacity)
        self.win32.present(self.hwnd, r.image)
        self.button = r.button
        self._drawn = key

    # mouse, in px from the window's top-left (only while it takes clicks)

    def mouse_down(self, x: int, y: int) -> None:
        if self.in_button(x, y):
            self._pressed = True
        elif self.moving:
            self._drag = (x, y)
            self.win32.capture(self.hwnd, True)

    def mouse_move(self, x: int, y: int) -> None:
        if self._drag is not None:
            cx, cy = self.win32.cursor()
            self.win32.move(self.hwnd, cx - self._drag[0], cy - self._drag[1])

    def mouse_up(self, x: int, y: int) -> None:
        if self._drag is not None:
            self._drag = None
            self.win32.capture(self.hwnd, False)
        elif self._pressed and self.in_button(x, y):
            self.toggle_move()
            self.update()
        self._pressed = False


def start(
    state: Callable[[], Json | None],
    position: dict[str, int] | None,
    save_position: Callable[[int, int], None],
    enabled: bool,
    opacity: int,
) -> OverlayController | None:
    """Create the overlay window and its threads. None off Windows."""
    if sys.platform != "win32":
        return None
    from poefire.web.overlay_win32 import Win32Impl  # type: ignore[attr-defined,unused-ignore]

    win32 = Win32Impl()
    controller = OverlayController(win32, state, save_position, enabled, opacity)
    pos = position or {}
    xy = (int(pos.get("x", DEFAULT_POSITION[0])), int(pos.get("y", DEFAULT_POSITION[1])))
    if not win32.on_screen(*xy):  # e.g. a monitor that's gone
        xy = DEFAULT_POSITION
    threading.Thread(target=win32.run, args=(controller, *xy), name="overlay", daemon=True).start()
    threading.Thread(target=controller.run_fetcher, name="overlay-data", daemon=True).start()
    return controller
