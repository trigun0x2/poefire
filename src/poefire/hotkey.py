"""A system-wide hotkey (Windows `RegisterHotKey`).

The app only *listens* for the key, the way LiveSplit's global hotkeys work: nothing is sent
to the game, so it stays clear of the ToS rule against input automation.
"""

from __future__ import annotations

import contextlib
import logging
import sys
import threading
from collections.abc import Callable

log = logging.getLogger(__name__)

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012

_MODS = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT,
         "win": MOD_WIN}  # fmt: skip
_KEYS: dict[str, int] = {
    **{chr(c): c for c in range(ord("A"), ord("Z") + 1)},
    **{str(d): 0x30 + d for d in range(10)},
    **{f"F{n}": 0x6F + n for n in range(1, 25)},
    **{f"NUM{d}": 0x60 + d for d in range(10)},
    "SPACE": 0x20, "PAGEUP": 0x21, "PAGEDOWN": 0x22, "END": 0x23, "HOME": 0x24,
    "INSERT": 0x2D, "DELETE": 0x2E, "PAUSE": 0x13, "SCROLLLOCK": 0x91,
    "NUM*": 0x6A, "NUM+": 0x6B, "NUM-": 0x6D, "NUM.": 0x6E, "NUM/": 0x6F,
}  # fmt: skip
_ALIASES = {"PGUP": "PAGEUP", "PGDN": "PAGEDOWN", "INS": "INSERT", "DEL": "DELETE",
            "SCROLL": "SCROLLLOCK"}  # fmt: skip


class HotkeyError(ValueError):
    pass


def parse(spec: str) -> tuple[int, int]:
    """'Ctrl+Alt+R' -> (modifiers, virtual-key code)."""
    parts = [p.strip() for p in spec.replace(" ", "").split("+") if p.strip()]
    if not parts:
        raise HotkeyError("empty hotkey")
    mods = 0
    for p in parts[:-1]:
        if p.lower() not in _MODS:
            raise HotkeyError(f"unknown modifier {p!r} (use Ctrl, Alt, Shift or Win)")
        mods |= _MODS[p.lower()]
    key = parts[-1].upper()
    key = _ALIASES.get(key, key)
    if key not in _KEYS:
        raise HotkeyError(f"unsupported key {parts[-1]!r}")
    if mods == 0 and (len(key) == 1 or key == "SPACE"):
        # a bare letter/digit would fire while typing in chat
        raise HotkeyError("a letter, digit or Space needs a modifier (Ctrl, Alt, Shift or Win)")
    return mods, _KEYS[key]


def normalize(spec: str) -> str:
    """Canonical spelling, e.g. 'alt+ctrl+r' -> 'Ctrl+Alt+R'."""
    mods, vk = parse(spec)
    names = [n for n, m in (("Ctrl", MOD_CONTROL), ("Alt", MOD_ALT), ("Shift", MOD_SHIFT),
                            ("Win", MOD_WIN)) if mods & m]  # fmt: skip
    key = next(k for k, v in _KEYS.items() if v == vk)
    return "+".join([*names, key])


class GlobalHotkey:
    """Calls `callback` (on its own thread) whenever the hotkey is pressed, in any app."""

    def __init__(self, spec: str, callback: Callable[[], object]) -> None:
        self.spec = normalize(spec)
        self.callback = callback
        self.error: str | None = None
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()

    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and self.error is None

    def start(self) -> bool:
        """Register the hotkey; False (with `error` set) if that's not possible."""
        if sys.platform != "win32":
            self.error = "global hotkeys are only available on Windows"
            return False
        self._thread = threading.Thread(target=self._run, name="hotkey", daemon=True)
        self._thread.start()
        self._ready.wait(5)
        return self.error is None

    def _run(self) -> None:
        if sys.platform != "win32":  # start() checks; this narrows the platform for mypy
            return
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self._thread_id = kernel32.GetCurrentThreadId()
        mods, vk = parse(self.spec)
        if not user32.RegisterHotKey(None, 1, mods | MOD_NOREPEAT, vk):
            self.error = f"{self.spec} is already used by another program; pick another hotkey"
            self._ready.set()
            return
        self._ready.set()
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    try:
                        self.callback()
                    except Exception:
                        log.exception("hotkey action failed")
        finally:
            user32.UnregisterHotKey(None, 1)

    def stop(self) -> None:
        if self._thread is None or not self._thread_id:
            return
        if sys.platform == "win32":
            import ctypes

            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        self._thread.join(timeout=2)
        self._thread = None


def beep(high: bool) -> None:
    """Short audible confirmation (you're in the game, not looking at the app)."""
    if sys.platform != "win32":
        return
    import winsound

    with contextlib.suppress(RuntimeError):  # no sound device
        winsound.Beep(880 if high else 440, 90)
