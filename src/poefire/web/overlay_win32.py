"""The overlay's native window (Windows only): a layered, topmost, non-activating popup.

Shown with UpdateLayeredWindow, so each pixel's alpha comes from the rendered image (the
area around the panel is fully transparent). WS_EX_TRANSPARENT makes clicks pass through to
the game; it's turned off while the overlay is unlocked or the cursor is on its button.
WS_EX_NOACTIVATE and MA_NOACTIVATE keep the game focused even when the overlay is clicked.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from poefire.web.overlay_window import OverlayController

assert sys.platform == "win32"

log = logging.getLogger(__name__)

CLASS_NAME = "poefireOverlay"
GWL_EXSTYLE = -20
WS_POPUP = 0x80000000
WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOPMOST = 0x80000, 0x20, 0x8
WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80, 0x8000000
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x1, 0x2, 0x4, 0x10
HWND_TOPMOST = -1
WM_DESTROY, WM_CLOSE, WM_TIMER, WM_APP = 0x2, 0x10, 0x113, 0x8000
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_MOUSEACTIVATE = 0x200, 0x201, 0x202, 0x21
MA_NOACTIVATE = 3
ULW_ALPHA, AC_SRC_OVER, AC_SRC_ALPHA = 2, 0, 1
BI_RGB, DIB_RGB_COLORS = 0, 0
MONITOR_DEFAULTTONULL = 0
IDC_ARROW = 32512
TIMER_MS = 100

LRESULT = wintypes.LPARAM
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                             wintypes.LPARAM)  # fmt: skip


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]  # fmt: skip


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD),
    ]  # fmt: skip


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte),
    ]  # fmt: skip


def _signed16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def _api(dll: Any, name: str, restype: Any, *argtypes: Any) -> Any:
    fn = getattr(dll, name)
    fn.restype = restype
    fn.argtypes = list(argtypes)
    return fn


class Win32Impl:
    """The real thing (see overlay_window.Win32). Own DLL handles, so these argtypes don't
    affect anyone else's calls (pywebview uses ctypes.windll)."""

    def __init__(self) -> None:
        u = ctypes.WinDLL("user32", use_last_error=True)
        g = ctypes.WinDLL("gdi32", use_last_error=True)
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        H, W, L, U, N = wintypes.HWND, wintypes.WPARAM, wintypes.LPARAM, wintypes.UINT, ctypes.c_int
        HDC, B = wintypes.HDC, wintypes.BOOL
        P = ctypes.POINTER
        self._GetForegroundWindow = _api(u, "GetForegroundWindow", H)
        self._GetWindowTextW = _api(u, "GetWindowTextW", N, H, wintypes.LPWSTR, N)
        self._GetClassNameW = _api(u, "GetClassNameW", N, H, wintypes.LPWSTR, N)
        self._GetWindowLongW = _api(u, "GetWindowLongW", wintypes.LONG, H, N)
        self._SetWindowLongW = _api(u, "SetWindowLongW", wintypes.LONG, H, N, wintypes.LONG)
        self._ShowWindow = _api(u, "ShowWindow", B, H, N)
        self._SetWindowPos = _api(u, "SetWindowPos", B, H, H, N, N, N, N, U)
        self._GetCursorPos = _api(u, "GetCursorPos", B, P(wintypes.POINT))
        self._GetWindowRect = _api(u, "GetWindowRect", B, H, P(wintypes.RECT))
        self._GetDpiForWindow = _api(u, "GetDpiForWindow", U, H)
        self._PostMessageW = _api(u, "PostMessageW", B, H, U, W, L)
        self._DefWindowProcW = _api(u, "DefWindowProcW", LRESULT, H, U, W, L)
        self._RegisterClassExW = _api(u, "RegisterClassExW", wintypes.ATOM, P(WNDCLASSEXW))
        self._CreateWindowExW = _api(
            u, "CreateWindowExW", H, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
            wintypes.DWORD, N, N, N, N, H, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
        )  # fmt: skip
        self._DestroyWindow = _api(u, "DestroyWindow", B, H)
        self._LoadCursorW = _api(u, "LoadCursorW", wintypes.HANDLE, wintypes.HINSTANCE,
                                 wintypes.LPVOID)  # fmt: skip
        self._SetTimer = _api(u, "SetTimer", ctypes.c_size_t, H, ctypes.c_size_t, U,
                              wintypes.LPVOID)  # fmt: skip
        self._GetMessageW = _api(u, "GetMessageW", B, P(wintypes.MSG), H, U, U)
        self._TranslateMessage = _api(u, "TranslateMessage", B, P(wintypes.MSG))
        self._DispatchMessageW = _api(u, "DispatchMessageW", LRESULT, P(wintypes.MSG))
        self._PostQuitMessage = _api(u, "PostQuitMessage", None, N)
        self._SetCapture = _api(u, "SetCapture", H, H)
        self._ReleaseCapture = _api(u, "ReleaseCapture", B)
        self._MonitorFromPoint = _api(u, "MonitorFromPoint", wintypes.HMONITOR, wintypes.POINT,
                                      wintypes.DWORD)  # fmt: skip
        self._GetDC = _api(u, "GetDC", HDC, H)
        self._ReleaseDC = _api(u, "ReleaseDC", N, H, HDC)
        self._UpdateLayeredWindow = _api(
            u, "UpdateLayeredWindow", B, H, HDC, P(wintypes.POINT), P(wintypes.SIZE), HDC,
            P(wintypes.POINT), wintypes.COLORREF, P(BLENDFUNCTION), wintypes.DWORD,
        )  # fmt: skip
        self._CreateCompatibleDC = _api(g, "CreateCompatibleDC", HDC, HDC)
        self._CreateDIBSection = _api(
            g, "CreateDIBSection", wintypes.HBITMAP, HDC, P(BITMAPINFO), U,
            P(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD,
        )  # fmt: skip
        self._SelectObject = _api(g, "SelectObject", wintypes.HGDIOBJ, HDC, wintypes.HGDIOBJ)
        self._DeleteObject = _api(g, "DeleteObject", B, wintypes.HGDIOBJ)
        self._DeleteDC = _api(g, "DeleteDC", B, HDC)
        self._GetModuleHandleW = _api(k, "GetModuleHandleW", wintypes.HMODULE, wintypes.LPCWSTR)
        self._wndproc: Any = None  # keep the callback alive
        self._controller: OverlayController | None = None

    # --- queries ---

    def foreground(self) -> int:
        return int(self._GetForegroundWindow() or 0)

    def window_info(self, hwnd: int) -> tuple[str, str]:
        title = ctypes.create_unicode_buffer(256)
        self._GetWindowTextW(hwnd, title, 256)
        cls = ctypes.create_unicode_buffer(256)
        self._GetClassNameW(hwnd, cls, 256)
        return title.value, cls.value

    def cursor(self) -> tuple[int, int]:
        pt = wintypes.POINT()
        self._GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y

    def rect(self, hwnd: int) -> tuple[int, int, int, int]:
        r = wintypes.RECT()
        self._GetWindowRect(hwnd, ctypes.byref(r))
        return r.left, r.top, r.right, r.bottom

    def scale(self, hwnd: int) -> float:
        return float(self._GetDpiForWindow(hwnd) or 96) / 96

    def on_screen(self, x: int, y: int) -> bool:
        return bool(self._MonitorFromPoint(wintypes.POINT(x + 20, y + 20), MONITOR_DEFAULTTONULL))

    # --- changes ---

    def set_click_through(self, hwnd: int, on: bool) -> None:
        style = self._GetWindowLongW(hwnd, GWL_EXSTYLE)
        style = style | WS_EX_TRANSPARENT if on else style & ~WS_EX_TRANSPARENT
        self._SetWindowLongW(hwnd, GWL_EXSTYLE, style)

    def show(self, hwnd: int, visible: bool) -> None:
        if visible:  # back on top of the game (which may have been raised meanwhile)
            flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
            self._SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, flags)
        self._ShowWindow(hwnd, SW_SHOWNOACTIVATE if visible else SW_HIDE)

    def move(self, hwnd: int, x: int, y: int) -> None:
        self._SetWindowPos(hwnd, None, x, y, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)

    def capture(self, hwnd: int, on: bool) -> None:
        if on:
            self._SetCapture(hwnd)
        else:
            self._ReleaseCapture()

    def wake(self, hwnd: int) -> None:
        self._PostMessageW(hwnd, WM_APP, 0, 0)

    def close(self, hwnd: int) -> None:
        self._PostMessageW(hwnd, WM_CLOSE, 0, 0)

    def present(self, hwnd: int, image: Any) -> None:
        """Show an RGBA Pillow image as the window's content, with its alpha."""
        from PIL import Image

        w, h = image.size
        r, g, b, a = image.convert("RGBa").split()  # premultiplied, as Windows wants it
        data = Image.merge("RGBA", (b, g, r, a)).tobytes()  # BGRA byte order
        screen = self._GetDC(None)
        mem = self._CreateCompatibleDC(screen)
        bmi = BITMAPINFO()
        hdr = bmi.bmiHeader
        hdr.biSize, hdr.biWidth, hdr.biHeight = ctypes.sizeof(BITMAPINFOHEADER), w, -h  # top-down
        hdr.biPlanes, hdr.biBitCount, hdr.biCompression = 1, 32, BI_RGB
        bits = ctypes.c_void_p()
        bmp = self._CreateDIBSection(mem, ctypes.byref(bmi), DIB_RGB_COLORS, ctypes.byref(bits),
                                     None, 0)  # fmt: skip
        try:
            if not bmp or not bits.value:
                raise OSError(ctypes.get_last_error(), "CreateDIBSection failed")
            ctypes.memmove(bits.value, data, len(data))
            old = self._SelectObject(mem, bmp)
            blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
            ok = self._UpdateLayeredWindow(
                hwnd, screen, None, ctypes.byref(wintypes.SIZE(w, h)), mem,
                ctypes.byref(wintypes.POINT(0, 0)), 0, ctypes.byref(blend), ULW_ALPHA,
            )  # fmt: skip
            self._SelectObject(mem, old)
            if not ok:
                raise OSError(ctypes.get_last_error(), "UpdateLayeredWindow failed")
        finally:
            if bmp:
                self._DeleteObject(bmp)
            self._DeleteDC(mem)
            self._ReleaseDC(None, screen)

    # --- the window and its thread ---

    def create(self, controller: OverlayController, x: int, y: int) -> int:
        self._controller = controller
        self._wndproc = WNDPROC(self._proc)
        hinst = self._GetModuleHandleW(None)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinst
        wc.hCursor = self._LoadCursorW(None, ctypes.c_void_p(IDC_ARROW))
        wc.lpszClassName = CLASS_NAME
        self._RegisterClassExW(ctypes.byref(wc))  # fails harmlessly if already registered
        ex = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW
        hwnd = self._CreateWindowExW(ex | WS_EX_NOACTIVATE, CLASS_NAME, "poefire overlay",
                                     WS_POPUP, x, y, 1, 1, None, None, hinst, None)  # fmt: skip
        if not hwnd:
            raise OSError(ctypes.get_last_error(), "CreateWindowExW failed")
        return int(hwnd)

    def run(self, controller: OverlayController, x: int, y: int) -> None:
        """The overlay thread: create the window, then pump its messages until it closes."""
        try:
            hwnd = self.create(controller, x, y)
            controller.attach(hwnd)
            self._SetTimer(hwnd, 1, TIMER_MS, None)
        except Exception:
            log.exception("overlay window failed")
            return
        msg = wintypes.MSG()
        while self._GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            self._TranslateMessage(ctypes.byref(msg))
            self._DispatchMessageW(ctypes.byref(msg))

    def _proc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        c = self._controller
        try:
            if c is not None:
                if msg in (WM_TIMER, WM_APP):
                    c.update()
                    return 0
                if msg == WM_MOUSEACTIVATE:
                    return MA_NOACTIVATE
                if msg in (WM_LBUTTONDOWN, WM_LBUTTONUP, WM_MOUSEMOVE):
                    x, y = _signed16(lparam), _signed16(lparam >> 16)
                    if msg == WM_LBUTTONDOWN:
                        c.mouse_down(x, y)
                    elif msg == WM_LBUTTONUP:
                        c.mouse_up(x, y)
                    else:
                        c.mouse_move(x, y)
                    return 0
            if msg == WM_CLOSE:
                self._DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                self._PostQuitMessage(0)
                return 0
        except Exception:  # never let an exception escape into Windows
            log.exception("overlay message %#x failed", msg)
            return 0
        return int(self._DefWindowProcW(hwnd, msg, wparam, lparam))
