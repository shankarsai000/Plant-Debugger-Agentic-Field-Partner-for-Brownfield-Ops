"""Simulated field camera.

Renders what an operator's phone camera would capture at F-17 or BV-104 given the
current *physical* state. The PNG carries a `pd:sim-reading` text chunk so the
offline (local) vision engine can work without Gemini; the Gemini engine ignores it
and reads the pixels.
"""

from __future__ import annotations

import io
import json
import math
import random

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from PIL.PngImagePlugin import PngInfo

W, H = 960, 720
DP_MAX = 2.5
DP_NORMAL_MAX = 0.6
DP_HIGH_MIN = 1.0


def _font(size: int) -> ImageFont.ImageFont:
    for name in ("arialbd.ttf", "DejaVuSans-Bold.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _background(seed: int) -> Image.Image:
    rnd = random.Random(seed)
    img = Image.new("RGB", (W, H), (58, 62, 66))
    d = ImageDraw.Draw(img)
    # concrete wall / floor gradient + grime
    for y in range(H):
        shade = int(70 + 30 * (y / H))
        d.line([(0, y), (W, y)], fill=(shade, shade + 2, shade + 4))
    for _ in range(900):
        x, y = rnd.randrange(W), rnd.randrange(H)
        c = rnd.randint(40, 110)
        d.point((x, y), fill=(c, c, c))
    # floor line
    d.rectangle([0, int(H * 0.82), W, H], fill=(84, 86, 80))
    d.line([(0, int(H * 0.82)), (W, int(H * 0.82))], fill=(230, 190, 40), width=6)
    return img


def _pipe(d: ImageDraw.ImageDraw, x0: int, y: int, x1: int, r: int = 34) -> None:
    d.rectangle([x0, y - r, x1, y + r], fill=(120, 128, 135), outline=(40, 40, 40), width=3)
    d.rectangle([x0, y - r + 6, x1, y - r + 14], fill=(170, 176, 182))


def _tag(d: ImageDraw.ImageDraw, x: int, y: int, text: str) -> None:
    f = _font(40)
    d.rounded_rectangle([x, y, x + 170, y + 62], radius=6, fill=(250, 250, 245), outline=(20, 20, 20), width=3)
    d.text((x + 18, y + 9), text, fill=(15, 15, 15), font=f)


def render_f17(dp_bar: float, seed: int = 17) -> Image.Image:
    img = _background(seed)
    d = ImageDraw.Draw(img)
    _pipe(d, 0, 470, W)
    # strainer body (Y-strainer style)
    d.rounded_rectangle([330, 380, 600, 560], radius=24, fill=(105, 110, 118), outline=(30, 30, 30), width=4)
    d.polygon([(430, 560), (520, 560), (560, 690), (470, 690)], fill=(95, 100, 108), outline=(30, 30, 30))
    for bx in range(345, 600, 42):
        d.ellipse([bx, 388, bx + 14, 402], fill=(60, 60, 60))
    _tag(d, 385, 420, "F-17")
    # impulse lines to gauge
    d.line([(370, 380), (370, 300), (440, 300)], fill=(190, 160, 70), width=8)
    d.line([(560, 380), (560, 300), (520, 300)], fill=(190, 160, 70), width=8)
    # differential pressure gauge
    cx, cy, r = 480, 190, 135
    d.ellipse([cx - r - 12, cy - r - 12, cx + r + 12, cy + r + 12], fill=(35, 35, 38))
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(245, 245, 238))
    start, sweep = 225.0, 270.0  # degrees, clockwise from top-left

    def ang(v: float) -> float:
        return math.radians(start - sweep * (v / DP_MAX))

    def arc_band(v0: float, v1: float, color: tuple[int, int, int]) -> None:
        a0 = start - sweep * (v0 / DP_MAX)
        a1 = start - sweep * (v1 / DP_MAX)
        # PIL arcs are clockwise from 3 o'clock in screen coords
        d.arc([cx - r + 14, cy - r + 14, cx + r - 14, cy + r - 14], start=-a0, end=-a1, fill=color, width=18)

    arc_band(0.0, DP_NORMAL_MAX, (40, 160, 70))
    arc_band(DP_NORMAL_MAX, DP_HIGH_MIN, (230, 180, 30))
    arc_band(DP_HIGH_MIN, DP_MAX, (210, 40, 35))
    f_small = _font(22)
    for i in range(0, 6):
        v = i * 0.5
        a = ang(v)
        x0, y0 = cx + (r - 34) * math.cos(a), cy - (r - 34) * math.sin(a)
        x1, y1 = cx + (r - 52) * math.cos(a), cy - (r - 52) * math.sin(a)
        d.line([(x0, y0), (x1, y1)], fill=(20, 20, 20), width=4)
        tx, ty = cx + (r - 76) * math.cos(a), cy - (r - 76) * math.sin(a)
        d.text((tx - 16, ty - 12), f"{v:.1f}", fill=(20, 20, 20), font=f_small)
    d.text((cx - 36, cy + 82), "ΔP bar", fill=(20, 20, 20), font=f_small)
    v = max(0.0, min(DP_MAX, dp_bar))
    a = ang(v)
    d.line([(cx, cy), (cx + (r - 30) * math.cos(a), cy - (r - 30) * math.sin(a))], fill=(15, 15, 15), width=7)
    d.ellipse([cx - 14, cy - 14, cx + 14, cy + 14], fill=(15, 15, 15))
    return img.filter(ImageFilter.GaussianBlur(0.8))


def render_bv104(opening_pct: float, seed: int = 104) -> Image.Image:
    img = _background(seed)
    d = ImageDraw.Draw(img)
    _pipe(d, 0, 500, W)
    # valve body
    d.polygon([(380, 430), (580, 430), (620, 500), (580, 570), (380, 570), (340, 500)],
              fill=(150, 40, 35), outline=(30, 30, 30))
    d.rectangle([462, 300, 498, 430], fill=(150, 150, 150), outline=(30, 30, 30), width=3)  # stem/bonnet
    # handwheel
    d.ellipse([360, 220, 600, 300], outline=(170, 30, 30), width=16)
    d.line([(480, 260), (365, 260)], fill=(170, 30, 30), width=10)
    d.line([(480, 260), (595, 260)], fill=(170, 30, 30), width=10)
    _tag(d, 395, 470, "BV-104")
    # mechanical position indicator plate
    px0, py0, px1, py1 = 640, 300, 900, 420
    d.rectangle([px0, py0, px1, py1], fill=(245, 245, 235), outline=(20, 20, 20), width=4)
    f = _font(28)
    d.text((px0 + 12, py0 + 10), "CLOSED", fill=(20, 20, 20), font=f)
    d.text((px1 - 110, py0 + 10), "OPEN", fill=(20, 120, 40), font=f)
    track_y = py0 + 75
    d.line([(px0 + 30, track_y), (px1 - 30, track_y)], fill=(20, 20, 20), width=4)
    for i in range(0, 11):
        x = px0 + 30 + (px1 - px0 - 60) * i / 10
        d.line([(x, track_y - (14 if i in (0, 10) else 8)), (x, track_y + 8)], fill=(20, 20, 20), width=3)
    open_x = px1 - 30
    d.rectangle([open_x - 10, track_y - 26, open_x + 10, track_y - 16], fill=(20, 140, 50))  # OPEN mark
    x = px0 + 30 + (px1 - px0 - 60) * max(0.0, min(100.0, opening_pct)) / 100
    d.polygon([(x, track_y + 6), (x - 14, track_y + 34), (x + 14, track_y + 34)], fill=(200, 30, 30))
    return img.filter(ImageFilter.GaussianBlur(0.8))


def capture(asset: str, truth: dict) -> bytes:
    if asset == "F-17":
        img = render_f17(truth["f17_dp_bar"])
        reading = {"asset": "F-17", "observable": "dp_gauge", "dp_bar": truth["f17_dp_bar"]}
    elif asset == "BV-104":
        img = render_bv104(truth["bv104_pct"])
        reading = {"asset": "BV-104", "observable": "valve_position", "opening_pct": truth["bv104_pct"]}
    else:
        raise ValueError(f"no camera view for {asset}")
    meta = PngInfo()
    meta.add_text("pd:sim-reading", json.dumps(reading))
    buf = io.BytesIO()
    img.save(buf, format="PNG", pnginfo=meta)
    return buf.getvalue()
