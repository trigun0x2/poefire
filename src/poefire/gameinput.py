"""In-game shortcuts: preset chat commands and saved search strings, on hotkeys.

poefire's input rule: **one key press = one action**. A hotkey you press may send one action to
the game (open chat, paste one command, Enter; or paste one search string into the search box).
Nothing is repeated, timed, chained across server actions, or sent without a key press.

How a shortcut is sent (Windows, `SendInput`):
- only while Path of Exile is the foreground window (its class is POEWindowClass);
- after the hotkey's own modifier keys are released (else Ctrl+Enter would go out);
- the text goes through the clipboard (paste), never as typed letters: if chat or the search
  box didn't open, typed letters would land in the game as skill and panel keys, a paste does
  nothing. What was on the clipboard is put back afterwards.

The hotkeys themselves are only registered while the game has focus (`GameHotkeys`), so a key
like F5 still refreshes your browser when you're not playing.

Config (config.toml), one entry per line: `chat_commands = ["F5|/hideout", "F9|/exit",
"|/kingsmarch"]` (hotkey | command; an empty hotkey = off) and `search_strings =
["Ctrl+Shift+1|Bad maps|!ele|!reg"]` (hotkey | name | text; the text may contain "|").
Saved regexes share one hotkey: `regexes = ["Bad maps|!ele|!reg"]` (name | text),
`regex_active = "Bad maps"` picks the one `regex_hotkey` pastes into the search box.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from poefire import hotkey

log = logging.getLogger(__name__)

GAME_CLASS = "POEWindowClass"
VK_RETURN, VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN = 0x0D, 0x10, 0x11, 0x12, 0x5B, 0x5C
VK_A, VK_F, VK_V = 0x41, 0x46, 0x56
MODIFIERS = (VK_CONTROL, VK_SHIFT, VK_MENU, VK_LWIN, VK_RWIN)
STEP = 0.03  # seconds between key events (the game reads input per frame)
RESTORE_AFTER = 0.3  # seconds before the clipboard is put back
RELEASE_WAIT = 0.6  # seconds to wait for the hotkey's modifiers to be let go

DEFAULT_CHAT_COMMANDS = ["|/hideout", "|/exit", "|/kingsmarch", "|/menagerie", "|/delve"]


# --- config entries ---


@dataclass(frozen=True)
class ChatCommand:
    key: str  # hotkey; "" = off
    text: str  # the command, e.g. "/hideout"

    @classmethod
    def parse(cls, entry: str) -> ChatCommand:
        key, _, text = entry.partition("|")
        return cls(key.strip(), text.strip())

    def format(self) -> str:
        return f"{self.key}|{self.text}"


@dataclass(frozen=True)
class SearchString:
    key: str  # hotkey; "" = off
    name: str
    text: str  # what goes in the search box (may contain "|", e.g. a regex)

    @classmethod
    def parse(cls, entry: str) -> SearchString:
        key, _, rest = entry.partition("|")
        name, _, text = rest.partition("|")
        return cls(key.strip(), name.strip(), text)

    def format(self) -> str:
        return f"{self.key}|{self.name}|{self.text}"


@dataclass(frozen=True)
class SavedRegex:
    name: str  # unique, no "|"
    text: str  # what goes in the search box (may contain "|")

    @classmethod
    def parse(cls, entry: str) -> SavedRegex:
        name, _, text = entry.partition("|")
        return cls(name.strip(), text)

    def format(self) -> str:
        return f"{self.name}|{self.text}"


def saved_regexes(entries: Iterable[str]) -> list[SavedRegex]:
    return [r for r in map(SavedRegex.parse, entries) if r.name and r.text]


def active_regex(entries: Iterable[str], active: str) -> SavedRegex | None:
    return next((r for r in saved_regexes(entries) if active and r.name == active), None)


def chat_commands(entries: Iterable[str]) -> list[ChatCommand]:
    return [c for c in map(ChatCommand.parse, entries) if c.text]


def search_strings(entries: Iterable[str]) -> list[SearchString]:
    return [s for s in map(SearchString.parse, entries) if s.text]


# --- sending ---


class Backend(Protocol):
    def game_focused(self) -> bool: ...
    def key_down(self, vk: int) -> bool: ...  # is the key held now
    def send_key(self, vk: int, up: bool) -> None: ...
    def get_clipboard(self) -> str | None: ...
    def set_clipboard(self, text: str) -> bool: ...


class GameInput:
    """Sends one shortcut at a time (a lock: two hotkeys can't interleave)."""

    def __init__(self, backend: Backend | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:  # fmt: skip
        self.backend = backend if backend is not None else win32_backend()
        self.sleep = sleep
        self._lock = threading.Lock()
        self.last: str | None = None  # what was sent last, for Settings

    def chat(self, text: str) -> bool:
        """Open chat, paste `text`, press Enter. False if the game isn't focused."""
        return self._paste(text, before=[(VK_RETURN,)], after=[(VK_RETURN,)], what="chat")

    def search(self, text: str) -> bool:
        """Focus the open stash/vendor search box (Ctrl+F), replace its text with `text`."""
        return self._paste(text, before=[(VK_CONTROL, VK_F)], after=[], what="search")

    def _paste(self, text: str, before: list[tuple[int, ...]], after: list[tuple[int, ...]],
               what: str) -> bool:  # fmt: skip
        b = self.backend
        if b is None or not b.game_focused():
            return False
        with self._lock:
            self._release_modifiers(b)
            old = b.get_clipboard()
            if not b.set_clipboard(text):
                log.warning("%s shortcut: the clipboard is busy", what)
                return False
            for combo in [*before, (VK_CONTROL, VK_A), (VK_CONTROL, VK_V), *after]:
                self._combo(b, combo)
            self.last = text
            self.sleep(RESTORE_AFTER)  # let the game read it before putting the old one back
            if old is not None and b.get_clipboard() == text:
                b.set_clipboard(old)
        log.info("%s shortcut sent: %s", what, text[:60])
        return True

    def _combo(self, b: Backend, keys: tuple[int, ...]) -> None:
        for vk in keys:
            b.send_key(vk, up=False)
        for vk in reversed(keys):
            b.send_key(vk, up=True)
        self.sleep(STEP)

    def _release_modifiers(self, b: Backend) -> None:
        """Wait for the hotkey's modifiers to be let go; release any still held."""
        waited = 0.0
        while waited < RELEASE_WAIT and any(b.key_down(k) for k in MODIFIERS):
            self.sleep(STEP)
            waited += STEP
        for k in MODIFIERS:
            if b.key_down(k):
                b.send_key(k, up=True)


# --- hotkeys that only exist while the game has focus ---


class GameHotkeys:
    """Registers `bindings` (hotkey -> action) while Path of Exile is the foreground window
    and unregisters them when it isn't, so the keys work normally in other programs."""

    POLL = 0.25

    def __init__(self, bindings: dict[str, Callable[[], object]],
                 game_focused: Callable[[], bool],
                 make: Callable[[str, Callable[[], object]], hotkey.GlobalHotkey] = (
                     hotkey.GlobalHotkey)) -> None:  # fmt: skip
        self.bindings = bindings
        self.game_focused = game_focused
        self.make = make
        self.errors: dict[str, str] = {}
        self._active: dict[str, hotkey.GlobalHotkey] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def registered(self) -> list[str]:
        return list(self._active)

    def start(self) -> None:
        if not self.bindings:
            return
        self._thread = threading.Thread(target=self._run, name="game-hotkeys", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        self._unregister()

    def tick(self) -> None:
        """One check: register on gaining focus, unregister on losing it."""
        focused = self.game_focused()
        if focused and not self._active:
            self._register()
        elif not focused and self._active:
            self._unregister()

    def _run(self) -> None:
        while not self._stop.wait(self.POLL):
            try:
                self.tick()
            except Exception:
                log.exception("in-game hotkeys")

    def _register(self) -> None:
        for spec, action in self.bindings.items():
            hk = self.make(spec, action)
            if hk.start():
                self._active[spec] = hk
                self.errors.pop(spec, None)
            else:
                self.errors[spec] = hk.error or "couldn't register the hotkey"

    def _unregister(self) -> None:
        for hk in self._active.values():
            hk.stop()
        self._active.clear()


# --- Windows ---


def win32_backend() -> Backend | None:
    if sys.platform != "win32":
        return None
    return _Win32()


class _Win32:  # pragma: no cover - Windows only (exercised by the Windows build's tests)
    u: Any
    k: Any
    ct: Any
    INPUT: Any
    KEYBDINPUT: Any
    CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002
    INPUT_KEYBOARD, KEYEVENTF_KEYUP, KEYEVENTF_EXTENDEDKEY = 1, 0x0002, 0x0001

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("Windows only")
        import ctypes
        from ctypes import wintypes

        self.ct = ctypes
        u, k = ctypes.WinDLL("user32", use_last_error=True), ctypes.WinDLL("kernel32")

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ctypes.c_size_t)]  # fmt: skip

        class MOUSEINPUT(ctypes.Structure):  # the union's largest member sets INPUT's size
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]  # fmt: skip

        class _U(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("u", _U)]

        self.INPUT, self.KEYBDINPUT = INPUT, KEYBDINPUT
        u.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        u.SendInput.restype = wintypes.UINT
        u.GetForegroundWindow.restype = wintypes.HWND
        u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetAsyncKeyState.argtypes = [ctypes.c_int]
        u.GetAsyncKeyState.restype = ctypes.c_short
        u.OpenClipboard.argtypes = [wintypes.HWND]
        u.GetClipboardData.argtypes = [wintypes.UINT]
        u.GetClipboardData.restype = wintypes.HANDLE
        u.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        u.SetClipboardData.restype = wintypes.HANDLE
        k.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        k.GlobalAlloc.restype = wintypes.HGLOBAL
        k.GlobalLock.argtypes = [wintypes.HGLOBAL]
        k.GlobalLock.restype = wintypes.LPVOID
        k.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        k.GlobalFree.argtypes = [wintypes.HGLOBAL]
        self.u, self.k = u, k

    def game_focused(self) -> bool:
        hwnd = self.u.GetForegroundWindow()
        if not hwnd:
            return False
        buf = self.ct.create_unicode_buffer(64)
        self.u.GetClassNameW(hwnd, buf, 64)
        return bool(buf.value == GAME_CLASS)

    def key_down(self, vk: int) -> bool:
        return bool(self.u.GetAsyncKeyState(vk) & 0x8000)

    def send_key(self, vk: int, up: bool) -> None:
        flags = self.KEYEVENTF_KEYUP if up else 0
        if vk in (VK_RWIN,):
            flags |= self.KEYEVENTF_EXTENDEDKEY
        ki = self.KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=0)
        inp = self.INPUT(type=self.INPUT_KEYBOARD)
        inp.u.ki = ki
        self.u.SendInput(1, self.ct.byref(inp), self.ct.sizeof(self.INPUT))

    def _open(self) -> bool:
        for _ in range(10):  # another program may hold it for a moment
            if self.u.OpenClipboard(None):
                return True
            time.sleep(0.01)
        return False

    def get_clipboard(self) -> str | None:
        if not self._open():
            return None
        try:
            h = self.u.GetClipboardData(self.CF_UNICODETEXT)
            if not h:
                return None
            p = self.k.GlobalLock(h)
            try:
                return self.ct.wstring_at(p) if p else None
            finally:
                self.k.GlobalUnlock(h)
        finally:
            self.u.CloseClipboard()

    def set_clipboard(self, text: str) -> bool:
        data = (text + "\0").encode("utf-16-le")
        h = self.k.GlobalAlloc(self.GMEM_MOVEABLE, len(data))
        if not h:
            return False
        p = self.k.GlobalLock(h)
        self.ct.memmove(p, data, len(data))
        self.k.GlobalUnlock(h)
        if not self._open():
            self.k.GlobalFree(h)
            return False
        try:
            self.u.EmptyClipboard()
            if not self.u.SetClipboardData(self.CF_UNICODETEXT, h):
                self.k.GlobalFree(h)
                return False
            return True  # the clipboard owns the memory now
        finally:
            self.u.CloseClipboard()
