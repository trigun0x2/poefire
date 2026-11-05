"""The in-game overlay window (Windows app only).

A small frameless, transparent, always-on-top window that shows /overlay.html. It:

- shows while Path of Exile is the foreground window (and hides otherwise), without taking
  the focus from the game;
- lets mouse clicks through to the game while locked; the move hotkey unlocks it so it can be
  dragged, and pressing it again locks it and saves its position;
- is turned on and off with the toggle hotkey (and the Settings switch);
- is see-through around the panel and as opaque as the Settings slider says.

pywebview's `transparent` only makes the web view transparent: the native form behind it
still paints its background. So the form paints a key colour, and the window is a layered
window with that colour keyed out (LWA_COLORKEY) and a window-wide alpha (LWA_ALPHA).

It only reads which window is in front (by title/class) and draws over the game: no input is
sent to the game and nothing of the game is read.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

WIDTH, HEIGHT = 340, 196
POSITION_KEY = "overlay_position"
GAME_TITLES = ("Path of Exile",)
GAME_CLASSES = ("POEWindowClass",)
POLL = 0.1  # seconds between foreground / cursor checks
PREVIEW = 3.0  # seconds the overlay stays shown after an opacity change (to see it)
# The Move/Lock button's corner (logical px from the window's top-left): while locked, only
# this area takes clicks, and only while the cursor is over it.
BUTTON_AREA = (WIDTH - 96, 0, WIDTH, 36)

# Painted behind the page and made fully transparent. Near black, so the anti-aliased edges
# of the panel blend into it without a coloured fringe; the panel itself never uses it.
KEY_COLOR = (1, 0, 1)
MIN_OPACITY, MAX_OPACITY = 20, 100  # percent

GWL_EXSTYLE = -20
LWA_COLORKEY, LWA_ALPHA = 0x1, 0x2
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

    def set_layered(self, hwnd: int, key: tuple[int, int, int], alpha: int) -> None:
        """Key colour fully transparent, everything else at `alpha` (0-255)."""
        r, g, b = key
        self.user32.SetLayeredWindowAttributes(
            hwnd, r | (g << 8) | (b << 16), alpha, LWA_COLORKEY | LWA_ALPHA
        )

    def show(self, hwnd: int, visible: bool) -> None:
        self.user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE if visible else SW_HIDE)

    def cursor(self) -> tuple[int, int]:
        from ctypes import wintypes

        pt = wintypes.POINT()
        self.user32.GetCursorPos(self.ctypes.byref(pt))
        return pt.x, pt.y

    def rect(self, hwnd: int) -> tuple[int, int, int, int]:
        from ctypes import wintypes

        r = wintypes.RECT()
        self.user32.GetWindowRect(hwnd, self.ctypes.byref(r))
        return r.left, r.top, r.right, r.bottom

    def scale(self, hwnd: int) -> float:
        try:
            return float(self.user32.GetDpiForWindow(hwnd)) / 96 or 1.0
        except AttributeError:
            return 1.0


class OverlayController:
    def __init__(self, window: Any, win32: Any, save_position: Callable[[int, int], None],
                 enabled: bool = True, opacity: int = MAX_OPACITY) -> None:  # fmt: skip
        self.window = window  # pywebview window
        self.win32 = win32
        self.save_position = save_position
        self.enabled = enabled  # the toggle hotkey / Settings switch
        self.opacity = clamp_opacity(opacity)  # percent
        self.moving = False  # unlocked: draggable, clicks no longer pass through
        self.visible = False
        self.hot = False  # the cursor is over the button corner (it takes clicks)
        self.hwnd = 0
        self.clock: Callable[[], float] = time.monotonic
        self._preview_until = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- setup (once the native window exists) ---

    def attach(self, hwnd: int) -> None:
        self.hwnd = hwnd
        self.win32.set_click_through(hwnd, True)  # also makes it a layered window
        self._apply_opacity()
        self.win32.show(hwnd, False)
        self.visible = False
        self._thread = threading.Thread(target=self._run, name="overlay", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def set_opacity(self, percent: int, preview: bool = False) -> None:
        """`preview`: show it for a few seconds even when the game isn't in front."""
        self.opacity = clamp_opacity(percent)
        self._apply_opacity()
        if preview:
            self._preview_until = self.clock() + PREVIEW
            self.update()

    def _apply_opacity(self) -> None:
        if self.hwnd:
            self.win32.set_layered(self.hwnd, KEY_COLOR, round(self.opacity * 255 / 100))

    # --- hotkeys ---

    def toggle(self) -> None:
        self.set_enabled(not self.enabled)

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        self.update()

    def set_moving(self, moving: bool) -> None:
        """The overlay's own Move / Lock button."""
        if moving != self.moving:
            self.toggle_move()

    def toggle_move(self) -> None:
        """Unlock to drag the overlay; again to lock it where it is (and remember that)."""
        self.moving = not self.moving
        self.hot = False
        if self.hwnd:
            self.win32.set_click_through(self.hwnd, not self.moving)
            self._apply_opacity()
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
        previewing = self.clock() < self._preview_until
        return self.enabled and (self.moving or previewing or self.game_in_front())

    def over_button(self) -> bool:
        left, top, _, _ = self.win32.rect(self.hwnd)
        x, y = self.win32.cursor()
        s = self.win32.scale(self.hwnd)
        x0, y0, x1, y1 = BUTTON_AREA
        return bool(left + x0 * s <= x < left + x1 * s and top + y0 * s <= y < top + y1 * s)

    def update(self) -> None:
        if not self.hwnd:
            return
        want = self.wanted()
        if want != self.visible:
            self.win32.show(self.hwnd, want)
            self.visible = want
        if self.visible and not self.moving:
            # Locked: everything passes clicks to the game, except the Move button's corner
            # while the cursor is over it.
            hot = self.over_button()
            if hot != self.hot:
                self.win32.set_click_through(self.hwnd, not hot)
                self._apply_opacity()
                self.hot = hot

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


class OverlayApi:
    """Called by the overlay page's Move / Lock button (window.pywebview.api)."""

    def __init__(self) -> None:
        # Private on purpose: pywebview exposes every public attribute of a js_api object to
        # the page, walking into it, and the controller leads to the native .NET window.
        self._controller: OverlayController | None = None

    def _attach(self, controller: OverlayController) -> None:
        self._controller = controller

    def move(self) -> None:
        if self._controller is not None:
            self._controller.set_moving(True)

    def lock(self) -> None:
        if self._controller is not None:
            self._controller.set_moving(False)


def clamp_opacity(percent: int) -> int:
    return max(MIN_OPACITY, min(MAX_OPACITY, int(percent)))


def paint_key_color(window: Any) -> None:
    """Make the native form paint KEY_COLOR behind the (transparent) page. Runs the change on
    the form's UI thread; call it from another thread (pywebview's event handlers are)."""
    from System import Action  # type: ignore[import-not-found]  # pythonnet, Windows only
    from System.Drawing import Color  # type: ignore[import-not-found]

    form = window.native

    def apply() -> None:
        form.BackColor = Color.FromArgb(*KEY_COLOR)
        form.Invalidate()

    form.Invoke(Action(apply))


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
    opacity: int = MAX_OPACITY,
) -> OverlayController | None:
    """Create the overlay window (call before webview.start). None off Windows."""
    if sys.platform != "win32":
        return None
    pos = position or {}
    api = OverlayApi()
    window = webview.create_window(
        "poefire overlay", url, width=WIDTH, height=HEIGHT, x=pos.get("x"), y=pos.get("y"),
        frameless=True, easy_drag=False, on_top=True, transparent=True, focus=False,
        resizable=False, shadow=False, background_color="#000000", js_api=api,
    )  # fmt: skip
    controller = OverlayController(window, Win32(), save_position, enabled, opacity)
    api._attach(controller)

    def shown() -> None:
        try:
            paint_key_color(window)
        except Exception:  # still usable, just with a dark box around the panel
            log.exception("overlay background setup failed")
        try:
            controller.attach(native_handle(window))
        except Exception:
            log.exception("overlay window setup failed")

    window.events.shown += shown
    return controller
