"""Controls for the frameless native window, exposed to the page as `window.pywebview.api`.

The window has no OS title bar: the page draws its own (drag region, minimize / maximize /
close) and resize handles on its right and bottom edges. Maximize fills the monitor's work
area itself, because a borderless WinForms window that is maximized natively covers the
taskbar.
"""

from __future__ import annotations

import sys
from typing import Any, Protocol

MIN_WIDTH, MIN_HEIGHT = 900, 600


class NativeWindow(Protocol):
    x: int
    y: int
    width: int
    height: int

    def minimize(self) -> None: ...
    def maximize(self) -> None: ...
    def resize(self, width: int, height: int) -> None: ...
    def move(self, x: int, y: int) -> None: ...
    def destroy(self) -> None: ...


def work_area(x: int, y: int, width: int, height: int) -> tuple[int, int, int, int] | None:
    """Work area (screen minus taskbar) of the monitor holding the window's center, in the
    logical pixels pywebview uses. None where unavailable (non-Windows)."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    try:
        scale = user32.GetDpiForSystem() / 96
    except AttributeError:
        scale = 1.0

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]  # fmt: skip

    center = wintypes.POINT(int((x + width / 2) * scale), int((y + height / 2) * scale))
    monitor = user32.MonitorFromPoint(center, 2)  # MONITOR_DEFAULTTONEAREST
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return None
    r = info.rcWork
    return (round(r.left / scale), round(r.top / scale),
            round((r.right - r.left) / scale), round((r.bottom - r.top) / scale))  # fmt: skip


class WindowControls:
    """Public methods are callable from JavaScript; underscore names are not exposed."""

    def __init__(self) -> None:
        self._window: NativeWindow | None = None
        self._restore: tuple[int, int, int, int] | None = None

    def _attach(self, window: Any) -> None:
        self._window = window

    def _w(self) -> NativeWindow:
        if self._window is None:
            raise RuntimeError("window not ready")
        return self._window

    def minimize(self) -> None:
        self._w().minimize()

    def is_maximized(self) -> bool:
        return self._restore is not None

    def toggle_maximize(self) -> bool:
        """Returns True if the window is now maximized."""
        w = self._w()
        if self._restore is not None:
            x, y, width, height = self._restore
            self._restore = None
            w.resize(width, height)
            w.move(x, y)
            return False
        self._restore = (w.x, w.y, w.width, w.height)
        area = work_area(*self._restore)
        if area is None:
            w.maximize()
        else:
            x, y, width, height = area
            w.move(x, y)
            w.resize(width, height)
        return True

    def resize(self, width: float, height: float) -> None:
        """Resize from the page's edge handles (logical pixels)."""
        self._restore = None
        self._w().resize(max(MIN_WIDTH, int(width)), max(MIN_HEIGHT, int(height)))

    def close(self) -> None:
        self._w().destroy()

    def _bring_to_front(self) -> None:
        """Another launch of poefire found this one running: show the window (not exposed to
        the page: underscore)."""
        w: Any = self._w()
        for step in ("restore", "show"):
            if hasattr(w, step):
                getattr(w, step)()
        if hasattr(w, "on_top"):  # the usual way to raise a window above the others
            w.on_top = True
            w.on_top = False


def _system_scale() -> float:
    import ctypes

    if sys.platform != "win32":
        return 1.0
    try:
        return float(ctypes.windll.user32.GetDpiForSystem()) / 96
    except AttributeError:
        return 1.0


def cursor_pos() -> tuple[int, int] | None:
    """The mouse cursor, in pywebview's logical pixels. None off Windows."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    pt = wintypes.POINT()
    if not ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
        return None
    scale = _system_scale()
    return round(pt.x / scale), round(pt.y / scale)


def popup_position(cursor: tuple[int, int], size: tuple[int, int],
                   area: tuple[int, int, int, int] | None) -> tuple[int, int]:  # fmt: skip
    """Beside the cursor (right of it, else left), kept inside the monitor's work area."""
    (cx, cy), (w, h) = cursor, size
    x, y = cx + 24, cy - h // 3
    if area is None:
        return max(0, x), max(0, y)
    ax, ay, aw, ah = area
    if x + w > ax + aw:
        x = cx - 24 - w  # no room on the right: left of the cursor
    return max(ax, min(x, ax + aw - w)), max(ay, min(y, ay + ah - h))


class PopupControls:
    """The price-check popup's window: hidden until the price check key, then shown beside
    the cursor; Esc or its close button hides it again (it is reused, never closed)."""

    WIDTH, HEIGHT = 440, 600

    def __init__(self) -> None:
        self._window: Any = None

    def _attach(self, window: Any) -> None:
        self._window = window

    def close(self) -> None:
        if self._window is not None:
            self._window.hide()

    def _show(self) -> None:
        w = self._window
        if w is None:
            return
        pos = cursor_pos()
        if pos is not None:
            area = work_area(pos[0], pos[1], 1, 1)
            w.move(*popup_position(pos, (self.WIDTH, self.HEIGHT), area))
        w.show()
        w.evaluate_js("window.pcLoad && window.pcLoad()")
