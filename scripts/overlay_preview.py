"""The README's overlay picture: the overlay as drawn in the game, on a stand-in backdrop.

uv run python scripts/overlay_preview.py docs/images/overlay.png
"""

from __future__ import annotations

import sys

from PIL import Image, ImageDraw, ImageFilter

from poefire.web.overlay_render import frame, render

CAMPAIGN = {
    "mode": "campaign", "rta": 2105, "paused": False, "act": 3, "class": "Deadeye",
    "base_class": "Ranger", "pb_class": "Ranger", "areas": [
        {"name": "The Crossroads", "rta": 2105, "pb_rta": 2160, "delta": -55},
        {"name": "The Sewers", "rta": 1950, "pb_rta": 1938, "delta": 12},
        {"name": "The Ebony Barracks", "rta": 1712, "pb_rta": 1760, "delta": -48},
    ],
}  # fmt: skip
MAPPING = {"mode": "mapping", "maps": 21, "avg_chaos": 228.6, "avg_divine": 1.26,
           "chaos": 4801, "divine": 26.38,
           "current": {"name": "Crimson Temple", "status": "active", "seconds": 154}}  # fmt: skip


def backdrop(w: int, h: int) -> Image.Image:
    """A dark, blurred 'scene' (no game art)."""
    img = Image.new("RGB", (w, h), (18, 16, 14))
    d = ImageDraw.Draw(img)
    for x, y, r, c in [
        (120, 300, 260, (70, 52, 30)),
        (640, 120, 300, (40, 48, 58)),
        (900, 420, 220, (88, 46, 22)),
        (360, 80, 160, (52, 60, 44)),
    ]:
        d.ellipse((x - r, y - r, x + r, y + r), fill=c)
    return img.filter(ImageFilter.GaussianBlur(70))


def main(out: str) -> None:
    scale = 2.0
    a = render(frame(CAMPAIGN, 0), scale, 85).image
    b = render(frame(MAPPING, 0), scale, 85).image
    pad = 40
    w = a.width + b.width + pad * 3
    h = max(a.height, b.height) + pad * 2
    img = backdrop(w, h).convert("RGBA")
    img.alpha_composite(a, (pad, pad))
    img.alpha_composite(b, (pad * 2 + a.width, pad))
    img.convert("RGB").save(out, optimize=True)


if __name__ == "__main__":
    main(sys.argv[1])
