"""Draws the in-game overlay panel as an RGBA image (Pillow).

The overlay window (overlay_window.py) shows this image with per-pixel alpha, so everything
around the rounded panel is fully transparent. `frame()` turns /api/overlay's data into the
text to show (pure, so a redraw only happens when something visible changed); `render()`
draws a frame at a given DPI scale.

Opacity applies to the panel's background only: the text stays fully opaque (with a soft
shadow) so it's readable at any setting.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Literal

from PIL import Image, ImageDraw, ImageFilter, ImageFont

type Json = dict[str, Any]
type Weight = Literal["regular", "semibold", "bold"]
type Font = ImageFont.FreeTypeFont | ImageFont.ImageFont
type Tone = Literal["text", "muted", "accent", "good", "bad", "chaos", "divine"]

WIDTH = 340  # logical px (scaled by the window's DPI)
MARGIN = 6  # transparent edge around the panel, room for its shadow
PAD_X, PAD_TOP, PAD_BOTTOM = 10, 8, 9
RADIUS = 9
BUTTON_H = 22

COLORS: dict[Tone, tuple[int, int, int]] = {
    "text": (241, 242, 245),
    "muted": (163, 169, 182),
    "accent": (255, 138, 61),
    "good": (87, 211, 148),
    "bad": (255, 107, 107),
    "chaos": (226, 189, 119),
    "divine": (169, 203, 255),
}
MINUS = "\u2212"  # a real minus sign: as wide as the plus
PANEL = (14, 16, 20)
BORDER = (58, 62, 70)

# --- what to show -------------------------------------------------------------------------


@dataclass(frozen=True)
class Area:
    name: str
    time: str
    pb: str
    delta: str
    tone: Tone  # good (ahead of the PB), bad (behind), muted (no PB)


@dataclass(frozen=True)
class Frame:
    mode: Literal["campaign", "mapping", "waiting"]
    moving: bool = False
    hot: bool = False  # the cursor is over the Move button
    # campaign
    timer: str = ""
    paused: bool = False
    meta: str = ""
    pb: str = ""
    areas: tuple[Area, ...] = ()
    # mapping
    avg_chaos: str = ""
    avg_divine: str = ""
    total_chaos: str = ""
    total_divine: str = ""
    maps: str = ""
    map_name: str = ""
    map_time: str = ""


def duration(sec: float | None) -> str:
    if sec is None:
        return "—"
    s = max(0, int(sec))
    h, m, s = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02}:{s:02}" if h else f"{m:02}:{s:02}"


def signed(sec: float | None) -> tuple[str, Tone]:
    if sec is None:
        return "—", "muted"
    tone: Tone = "good" if sec < 0 else "bad" if sec > 0 else "text"
    return (MINUS if sec < 0 else "+") + duration(abs(sec)), tone


def chaos(v: float) -> str:
    if abs(v) >= 100:
        return f"{v:,.0f}c"
    text = f"{v:,.1f}"
    return (text[:-2] if text.endswith(".0") else text) + "c"


def divine(v: float | None) -> str:
    return "" if v is None else f"{v:,.2f}d"


def frame(data: Json | None, elapsed: float, moving: bool = False, hot: bool = False) -> Frame:
    """`elapsed`: seconds since `data` was fetched (timers tick between fetches)."""
    if not data:
        return Frame("waiting", moving, hot)
    if data.get("mode") == "campaign":
        act = data.get("act")
        meta = [("Epilogue" if act > 10 else f"Act {act}") if act else None, data.get("class")]
        if data.get("pb_class"):
            pb = f"vs your best {data['pb_class']} run"
        elif data.get("base_class"):
            pb = f"no finished {data['base_class']} run yet"
        else:
            pb = "PB once your class is known"
        areas = []
        for a in data.get("areas") or []:
            delta, tone = signed(a.get("delta"))
            pb_rta = a.get("pb_rta")
            areas.append(
                Area(
                    str(a.get("name") or ""),
                    duration(a.get("rta")),
                    "—" if pb_rta is None else duration(pb_rta),
                    delta,
                    tone,
                )
            )
        paused = bool(data.get("paused"))
        rta = float(data.get("rta") or 0) + (0 if paused else elapsed)
        return Frame(
            "campaign", moving, hot, timer=duration(rta), paused=paused,
            meta=" · ".join(str(m) for m in meta if m), pb=pb, areas=tuple(areas),
        )  # fmt: skip
    maps = int(data.get("maps") or 0)
    current = data.get("current")
    map_name, map_time = "Not in a map", ""
    if current:
        active = current.get("status") == "active"
        map_name = str(current.get("name") or "Map")
        t = float(current.get("seconds") or 0) + (elapsed if active else 0)
        map_time = duration(t) + ("" if active else " (hideout)")
    avg = data.get("avg_chaos")
    total = data.get("chaos")
    return Frame(
        "mapping", moving, hot,
        avg_chaos="—" if avg is None else chaos(avg),
        avg_divine="" if avg is None else divine(data.get("avg_divine")),
        total_chaos="" if not maps else "—" if total is None else chaos(total),
        total_divine="" if not maps else divine(data.get("divine")),
        maps=f"{maps} map{'' if maps == 1 else 's'}" if maps else "No maps yet",
        map_name=map_name, map_time=map_time,
    )  # fmt: skip


# --- fonts -------------------------------------------------------------------------------

_WINDOWS_FONTS = {"regular": "segoeui.ttf", "semibold": "seguisb.ttf", "bold": "segoeuib.ttf"}
_OTHER_FONTS = {  # for running from source elsewhere, and the tests
    "regular": ["DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
    "semibold": ["DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    "bold": ["DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
}


@cache
def font(weight: Weight, px: int) -> Font:
    candidates: list[str] = []
    if sys.platform == "win32":
        fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        candidates.append(str(fonts / _WINDOWS_FONTS[weight]))
        candidates.append(str(fonts / _WINDOWS_FONTS["regular"]))
    candidates += _OTHER_FONTS[weight]
    for name in candidates:
        try:
            return ImageFont.truetype(name, px)
        except OSError:
            continue
    return ImageFont.load_default(px)


# --- drawing -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Rendered:
    image: Image.Image
    button: tuple[int, int, int, int]  # the Move / Lock button, px from the top-left


class _Canvas:
    def __init__(self, scale: float, height: int) -> None:
        self.s = scale
        self.text = Image.new("RGBA", (self.px(WIDTH), self.px(height)), (0, 0, 0, 0))
        self.draw = ImageDraw.Draw(self.text)

    def px(self, v: float) -> int:
        return round(v * self.s)

    def font(self, size: float, weight: Weight = "regular") -> Font:
        return font(weight, max(6, self.px(size)))

    def width(self, text: str, f: Font) -> float:
        return float(f.getlength(text)) / self.s

    def fit(self, text: str, f: Font, max_w: float) -> str:
        if self.width(text, f) <= max_w:
            return text
        while text and self.width(text + "…", f) > max_w:
            text = text[:-1]
        return text.rstrip() + "…" if text else ""

    def put(self, x: float, baseline: float, text: str, f: Font, tone: Tone,
            alpha: int = 255) -> float:  # fmt: skip
        """Draw at (x, baseline) in logical px; returns the x after it."""
        if text:
            xy = (self.px(x), self.px(baseline))
            shadow = (xy[0], xy[1] + max(1, self.px(1)))
            self.draw.text(shadow, text, font=f, fill=(0, 0, 0, alpha * 5 // 8), anchor="ls")
            self.draw.text(xy, text, font=f, fill=(*COLORS[tone], alpha), anchor="ls")
        return x + self.width(text, f)

    def put_right(self, right: float, baseline: float, parts: list[tuple[str, Font, Tone]]) -> None:
        """Right-aligned run of differently styled texts."""
        x = right - sum(self.width(t, f) for t, f, _ in parts)
        for t, f, tone in parts:
            x = self.put(x, baseline, t, f, tone)


SUPERSAMPLE = 3  # Pillow doesn't anti-alias shapes: draw them bigger and scale down


def _smooth_rounded_rect(
    size: tuple[int, int],
    box: tuple[int, int, int, int],
    radius: int,
    fill: tuple[int, ...],
    outline: tuple[int, ...] | None,
    width: int,
) -> Image.Image:
    """A layer of `size` with an anti-aliased rounded rectangle."""
    k = SUPERSAMPLE
    x0, y0, x1, y1 = box
    pad = 2  # only the rectangle's own area is supersampled
    w, h = x1 - x0 + 1 + 2 * pad, y1 - y0 + 1 + 2 * pad
    big = Image.new("RGBA", (w * k, h * k), (0, 0, 0, 0))
    ImageDraw.Draw(big).rounded_rectangle(
        (pad * k, pad * k, (w - pad) * k - 1, (h - pad) * k - 1), radius=radius * k,
        fill=fill, outline=outline, width=max(1, width) * k if outline else 0,
    )  # fmt: skip
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    layer.paste(big.resize((w, h), Image.Resampling.LANCZOS), (x0 - pad, y0 - pad))
    return layer


def render(fr: Frame, scale: float = 1.0, opacity: int = 90) -> Rendered:
    """The panel for `fr` at `scale` (DPI / 96); `opacity` (percent) is the background's."""
    c = _Canvas(scale, 400)
    left, right = MARGIN + PAD_X, WIDTH - MARGIN - PAD_X
    y = MARGIN + PAD_TOP  # top of the next line
    reserve = 72  # keep the first line clear of the button

    # the Move / Lock button, top-right
    bf = c.font(11, "semibold")
    label = "Lock" if fr.moving else "Move"
    bw = c.width(label, bf) + 18
    bx1, by0 = WIDTH - MARGIN - 8, MARGIN + 6
    bx0, by1 = bx1 - bw, by0 + BUTTON_H

    if fr.moving:
        c.put(left, y + 11, "Drag to move it, then Lock", c.font(11), "accent")
        y += 17

    if fr.mode == "campaign":
        tf = c.font(24, "bold")
        base = y + 23
        x = c.put(left, base, fr.timer, tf, "muted" if fr.paused else "accent")
        mf = c.font(12)
        meta = " · ".join(m for m in ("paused" if fr.paused else "", fr.meta) if m)
        c.put(x + 8, base - 1, c.fit(meta, mf, right - reserve - x - 8), mf, "muted")
        y = base + 6
        sf = c.font(11)
        c.put(left, y + 11, c.fit(fr.pb, sf, right - left), sf, "muted")
        y += 17
        af, afb = c.font(12.5), c.font(12.5, "semibold")
        col_d, col_p, col_t = right, right - 58, right - 58 - 62
        name_w = col_t - 62 - left - 4
        if not fr.areas:
            c.put(left, y + 14, "No areas yet", af, "muted")
            y += 19
        for i, a in enumerate(fr.areas):
            f = afb if i == 0 else af
            base = y + 14
            c.put(left, base, c.fit(a.name, f, name_w), f, "text")
            c.put_right(col_t, base, [(a.time, f, "text")])
            c.put_right(col_p, base, [(a.pb, af, "muted")])
            c.put_right(col_d, base, [(a.delta, f, a.tone)])
            y += 19
    elif fr.mode == "mapping":
        kf, big, small = c.font(13), c.font(22, "bold"), c.font(12)
        base = y + 22
        c.put(left, base, "Avg / map", kf, "muted")
        parts = [(fr.avg_chaos, big, "chaos" if fr.avg_chaos != "—" else "muted")]
        if fr.avg_divine:
            parts.append(("  " + fr.avg_divine, small, "divine"))
        c.put_right(right - reserve, base, parts)  # type: ignore[arg-type]
        y = base + 7
        base = y + 14
        c.put(left, base, "Session", kf, "muted")
        sparts: list[tuple[str, Font, Tone]] = []
        if fr.total_chaos:
            sparts.append((fr.total_chaos, kf, "chaos" if fr.total_chaos != "—" else "muted"))
        if fr.total_divine:
            sparts.append(("  " + fr.total_divine, small, "divine"))
        sparts.append(((" · " if sparts else "") + fr.maps, kf, "text"))
        c.put_right(right, base, sparts)
        y += 19
        base = y + 14
        time_w = c.width(fr.map_time, kf) + 10 if fr.map_time else 0
        c.put(left, base, c.fit(fr.map_name, kf, right - left - time_w), kf, "muted")
        c.put_right(right, base, [(fr.map_time, kf, "text")])
        y += 19
    else:
        c.put(left, y + 12, "poefire", c.font(11), "muted")
        y += 18
    bottom = max(y + PAD_BOTTOM, by1 + 6)
    height = bottom + MARGIN

    # background: soft shadow, panel, border; then the button and the text on top
    size = (c.px(WIDTH), c.px(height))
    bg = round(255 * max(0, min(100, opacity)) / 100)
    panel_box = (c.px(MARGIN), c.px(MARGIN), c.px(WIDTH - MARGIN) - 1, c.px(bottom) - 1)
    out = Image.new("RGBA", size, (0, 0, 0, 0))
    if bg:
        shadow = Image.new("RGBA", size, (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle(
            (panel_box[0], panel_box[1] + c.px(2), panel_box[2], panel_box[3] + c.px(2)),
            radius=c.px(RADIUS), fill=(0, 0, 0, bg * 2 // 5),
        )  # fmt: skip
        out = Image.alpha_composite(out, shadow.filter(ImageFilter.GaussianBlur(c.px(3))))
    border = (*COLORS["accent"], 255) if fr.moving else (*BORDER, max(bg, 60))
    out = Image.alpha_composite(
        out, _smooth_rounded_rect(size, panel_box, c.px(RADIUS), (*PANEL, bg), border, c.px(1))
    )

    box = (c.px(bx0), c.px(by0), c.px(bx1), c.px(by1))
    if fr.moving:
        fill, line = (*COLORS["accent"], 255), None
        tone_rgb, t_alpha = (26, 16, 6), 255
    else:
        on = fr.hot
        fill, line = (255, 255, 255, 40 if on else 22), (255, 255, 255, 70 if on else 40)
        tone_rgb, t_alpha = COLORS["text" if on else "muted"], 255 if on else 170
    btn = _smooth_rounded_rect(size, box, c.px(6), fill, line, c.px(1))
    bd = ImageDraw.Draw(btn)
    bd.text(
        (c.px((bx0 + bx1) / 2), c.px(by0 + BUTTON_H / 2 + 0.5)), label, font=bf,
        fill=(*tone_rgb, t_alpha), anchor="mm",
    )  # fmt: skip
    out = Image.alpha_composite(out, btn)
    out = Image.alpha_composite(out, c.text.crop((0, 0, *size)))
    return Rendered(out, box)
