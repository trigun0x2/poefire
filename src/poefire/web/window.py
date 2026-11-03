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
