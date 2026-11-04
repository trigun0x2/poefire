from __future__ import annotations

import sys

import pytest

from poefire import hotkey
from poefire.hotkey import MOD_ALT, MOD_CONTROL, MOD_SHIFT, GlobalHotkey, HotkeyError


@pytest.mark.parametrize(
    ("spec", "mods", "vk"),
    [
        ("Ctrl+Alt+R", MOD_CONTROL | MOD_ALT, ord("R")),
        ("alt + ctrl + r", MOD_CONTROL | MOD_ALT, ord("R")),
        ("Shift+F9", MOD_SHIFT, 0x78),
        ("F8", 0, 0x77),
        ("Pause", 0, 0x13),
        ("Ctrl+Num5", MOD_CONTROL, 0x65),
        ("Ctrl+PgUp", MOD_CONTROL, 0x21),
    ],
)
def test_parse(spec: str, mods: int, vk: int) -> None:
    assert hotkey.parse(spec) == (mods, vk)


@pytest.mark.parametrize("spec", ["", "R", "Space", "Hyper+R", "Ctrl+Banana", "Ctrl+"])
def test_parse_rejects(spec: str) -> None:
    with pytest.raises(HotkeyError):
        hotkey.parse(spec)


def test_normalize() -> None:
    assert hotkey.normalize("shift+alt+ctrl+f5") == "Ctrl+Alt+Shift+F5"
    assert hotkey.normalize("ctrl+del") == "Ctrl+DELETE"


@pytest.mark.skipif(sys.platform == "win32", reason="registers a real hotkey on Windows")
def test_not_available_off_windows() -> None:
    hk = GlobalHotkey("Ctrl+Alt+R", lambda: None)
    assert hk.start() is False and "Windows" in (hk.error or "") and not hk.active


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_registers_and_fires_on_windows() -> None:
    if sys.platform != "win32":  # (narrows the platform for mypy)
        return
    import ctypes
    import threading

    fired = threading.Event()
    hk = GlobalHotkey("Ctrl+Alt+Shift+F24", fired.set)  # a combination nothing else uses
    assert hk.start(), hk.error
    assert hk.active
    # deliver a WM_HOTKEY to the listener thread, as Windows does on a key press
    ctypes.windll.user32.PostThreadMessageW(hk._thread_id, hotkey.WM_HOTKEY, 1, 0)
    assert fired.wait(2)
    second = GlobalHotkey("Ctrl+Alt+Shift+F24", lambda: None)
    assert second.start() is False and "already used" in (second.error or "")
    hk.stop()
    assert not hk.active
