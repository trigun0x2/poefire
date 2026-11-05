"""The in-game overlay window (Windows app only).

A small frameless, transparent, always-on-top window that shows /overlay.html. It:

- shows while Path of Exile is the foreground window (and hides otherwise), without taking
  the focus from the game;
- lets mouse clicks through to the game while locked; the move hotkey unlocks it so it can be
  dragged, and pressing it again locks it and saves its position;
- is turned on and off with the toggle hotkey (and the Settings switch).

It only reads which window is in front (by title/class) and draws over the game: no input is
sent to the game and nothing of the game is read.
"""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

WIDTH, HEIGHT = 340, 196
POSITION_KEY = "overlay_position"
GAME_TITLES = ("Path of Exile",)
GAME_CLASSES = ("POEWindowClass",)
POLL = 0.25  # seconds between foreground checks

GWL_EXSTYLE = -20
WS_EX_LAYERED, WS_EX_TRANSPARENT = 0x80000, 0x20
WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80, 0x8000000
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4


class Win32:
    """The few user32 calls the overlay needs (replaced by a fake in tests)."""

    def __init__(self) -> None:
        import ctypes

        self.user32 = ctypes.windll.user32  # type: ignore[attr-defined,unused-ignore]
        self.ctypes = ctypes

    def foreground(self) -> int:
        return int(self.user32.GetForegroundWindow() or 0)

    def window_info(self, hwnd: int) -> tuple[str, str]:
        """(title, class name) of a window."""
        buf = self.ctypes.create_unicode_buffer(256)
        self.user32.GetWindowTextW(hwnd, buf, 256)
        cls = self.ctypes.create_unicode_buffer(256)
        self.user32.GetClassNameW(hwnd, cls, 256)
        return buf.value, cls.value

    def set_click_through(self, hwnd: int, on: bool) -> None:
        style = self.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        style |= WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        style = style | WS_EX_TRANSPARENT if on else style & ~WS_EX_TRANSPARENT
        self.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)

    def show(self, hwnd: int, visible: bool) -> None:
        self.user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE if visible else SW_HIDE)


class OverlayController:
    def __init__(self, window: Any, win32: Any, save_position: Callable[[int, int], None],
                 enabled: bool = True) -> None:  # fmt: skip
        self.window = window  # pywebview window
        self.win32 = win32
        self.save_position = save_position
        self.enabled = enabled  # the toggle hotkey / Settings switch
        self.moving = False  # unlocked: draggable, clicks no longer pass through
        self.visible = False
        self.hwnd = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- setup (once the native window exists) ---

    def attach(self, hwnd: int) -> None:
        self.hwnd = hwnd
        self.win32.set_click_through(hwnd, True)
        self.win32.show(hwnd, False)
        self.visible = False
        self._thread = threading.Thread(target=self._run, name="overlay", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # --- hotkeys ---

    def toggle(self) -> None:
        self.set_enabled(not self.enabled)

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        self.update()

    def toggle_move(self) -> None:
        """Unlock to drag the overlay; again to lock it where it is (and remember that)."""
        self.moving = not self.moving
        if self.hwnd:
            self.win32.set_click_through(self.hwnd, not self.moving)
        self._js(f"setMoving({'true' if self.moving else 'false'})")
        if not self.moving:
            try:
                self.save_position(int(self.window.x), int(self.window.y))
            except Exception:
                log.exception("couldn't save the overlay position")
        self.update()

    # --- showing and hiding ---

    def game_in_front(self) -> bool:
        fg = self.win32.foreground()
        if not fg:
            return False
        if fg == self.hwnd:
            return True  # the overlay itself (while being dragged)
        title, cls = self.win32.window_info(fg)
        return title in GAME_TITLES or cls in GAME_CLASSES

    def wanted(self) -> bool:
        return self.enabled and (self.moving or self.game_in_front())

    def update(self) -> None:
        if not self.hwnd:
            return
        want = self.wanted()
        if want != self.visible:
            self.win32.show(self.hwnd, want)
            self.visible = want

    def _run(self) -> None:
        while not self._stop.wait(POLL):
            try:
                self.update()
            except Exception:
                log.exception("overlay update failed")

    def _js(self, code: str) -> None:
        try:
            self.window.evaluate_js(code)
        except Exception:  # the page may not be loaded yet
            log.debug("overlay js failed: %s", code)


def native_handle(window: Any) -> int:
    """The HWND of a pywebview (WinForms) window."""
    handle = window.native.Handle
    return int(handle.ToInt64()) if hasattr(handle, "ToInt64") else int(handle)


def create(
    webview: Any,
    url: str,
    position: dict[str, int] | None,
    save_position: Callable[[int, int], None],
    enabled: bool,
) -> OverlayController | None:
    """Create the overlay window (call before webview.start). None off Windows."""
    if sys.platform != "win32":
        return None
    pos = position or {}
    window = webview.create_window(
        "poefire overlay", url, width=WIDTH, height=HEIGHT, x=pos.get("x"), y=pos.get("y"),
        frameless=True, easy_drag=True, on_top=True, transparent=True, focus=False,
        resizable=False, shadow=False, background_color="#000000",
    )  # fmt: skip
    controller = OverlayController(window, Win32(), save_position, enabled)

    def shown() -> None:
        try:
            controller.attach(native_handle(window))
        except Exception:
            log.exception("overlay window setup failed")

    window.events.shown += shown
    return controller
