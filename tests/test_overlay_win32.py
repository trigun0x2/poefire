"""The real overlay window on Windows (runs in the Windows release build's tests): it opens,
shows the panel, and everything around the panel is see-through."""

from __future__ import annotations

import sys
import threading
import time
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows overlay window")

MAPPING = {"mode": "mapping", "maps": 3, "avg_chaos": 12.5, "avg_divine": 0.07, "chaos": 37.5,
           "divine": 0.21, "current": None}  # fmt: skip
X, Y = 200, 200


def wait_for(cond: Any, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def px(image: Any, xy: tuple[int, int]) -> tuple[int, ...]:
    return tuple(image.convert("RGB").getpixel(xy))


def close(a: tuple[int, ...], b: tuple[int, ...], tol: int) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a[:3], b[:3], strict=True))


def test_real_overlay_window_is_transparent_around_the_panel() -> None:
    from PIL import ImageGrab

    from poefire.web import overlay_render, overlay_window

    saved: list[tuple[int, int]] = []
    try:
        before = ImageGrab.grab((X, Y, X + 800, Y + 400), include_layered_windows=True)
    except OSError as e:
        pytest.skip(f"no screen to capture: {e}")
    ov = overlay_window.start(lambda: MAPPING, {"x": X, "y": Y},
                              lambda x, y: saved.append((x, y)), True, 100)  # fmt: skip
    assert ov is not None
    try:
        assert wait_for(lambda: ov.hwnd != 0), "overlay window wasn't created"
        ov.set_opacity(100, preview=True)  # shown even though no game is in front
        assert wait_for(lambda: ov.visible), "overlay didn't show"
        time.sleep(0.5)
        left, top, right, bottom = ov.win32.rect(ov.hwnd)
        scale = ov.win32.scale(ov.hwnd)
        assert (left, top) == (X, Y)
        assert right - left == round(overlay_render.WIDTH * scale)
        after = ImageGrab.grab((X, Y, X + 800, Y + 400), include_layered_windows=True)
        # the window's top-left corner is outside the rounded panel: unchanged screen
        corner = (1, 1)
        assert close(px(after, corner), px(before, corner), 8)
        # just inside the panel's left edge: the panel's colour (at 100% opacity)
        inside = px(after, (round(9 * scale), (bottom - top) // 2))
        assert close(inside, overlay_render.PANEL, 10), inside
        # and to the right of the window: untouched
        outside = (right - left + 5, 5)
        assert close(px(after, outside), px(before, outside), 8)
    finally:
        ov.stop()
    assert wait_for(lambda: not any(t.name == "overlay" for t in threading.enumerate()))
