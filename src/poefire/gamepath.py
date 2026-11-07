"""Where the running game is installed (Windows), to read its own Client.txt.

Finds the game's window by its class, asks Windows for that process's program path
(QueryFullProcessImageNameW, with the minimal query right) and returns <install>/logs/
Client.txt. Like the overlay, this only uses what Windows tells any program about other
windows and processes: nothing of the game's memory is read.
"""

from __future__ import annotations

import sys
from pathlib import Path

GAME_CLASS = "POEWindowClass"
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def running_game_log() -> Path | None:
    """The Client.txt of the game that's running now, or None (not running / not Windows)."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.FindWindowW.restype = wintypes.HWND
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    dword_p = ctypes.POINTER(wintypes.DWORD)
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, dword_p]  # fmt: skip
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    hwnd = user32.FindWindowW(GAME_CLASS, None)
    if not hwnd:
        return None
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return None
    proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not proc:
        return None
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(proc, 0, buf, ctypes.byref(size)):
            return None
        return log_for_exe(Path(buf.value))
    finally:
        kernel32.CloseHandle(proc)


def log_for_exe(exe: Path) -> Path:
    return exe.parent / "logs" / "Client.txt"
