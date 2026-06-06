#!/usr/bin/env python3
"""Static hex-warp background image for `prefers-reduced-motion: reduce`.

Renders one "moment in time" frame of the hex-warp canvas so the browser
can paint it as a plain ``background-image`` instead of running JS. Output
lives at ``static/hex-warp-static.png`` (served at
``/static/site/hex-warp-static.png``).

Run from the repo root:
    .venv/bin/python scripts/generate_hex_warp_static.py
"""

from __future__ import annotations

import math
import random
from pathlib import Path

from PIL import Image, ImageDraw

# ---------------------------------------------------------------------------
# Paths + canvas
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT = PROJECT_ROOT / "static" / "hex-warp-static.png"

# Wide enough to cover desktops with `background-size: cover` and still look
# crisp on tall portrait mobile when stretched.
W, H = 1920, 1080

BG = (2, 4, 8, 255)
ACCENT = (0, 212, 255)  # site cyan

# Mirrors hex-warp.js constants so the static image matches the live look.
HEX_SIZE = 18
H_SPACING = HEX_SIZE * math.sqrt(3)
V_SPACING = HEX_SIZE * 1.5
DOT_RADIUS = 1.6
DOT_REST_ALPHA = 0.07         # visible-but-soft dot field; quieter than the paths
SEGMENT_PEAK_ALPHA = 0.30     # subdued; must not compete with headline / accent text
TURN_CHANCE = 0.2

# Length of the fading tail behind each trace's head (in route steps).
# Shorter trails make the comet head + fade read at a glance.
TAIL_LEN_MIN = 7
TAIL_LEN_MAX = 12
# How many sub-segments to break each route step into for a smooth gradient
# fade. PIL doesn't gradient-stroke, so we fake it with N short segments.
SUB_SEGMENTS = 6

# Density of "in-flight" traces baked into the static frame. Tuned to feel
# similar to the canvas after ~10s of activity at default settings.
TRACE_DENSITY = 0.6  # traces per column

# Deterministic so re-running produces the same output (lets us commit it
# and review diffs).
random.seed(20260606001)


def hex_verts(cx: float, cy: float) -> list[tuple[float, float]]:
    pts = []
    for i in range(6):
        a = math.radians(60 * i - 30)
        pts.append((cx + HEX_SIZE * math.cos(a), cy + HEX_SIZE * math.sin(a)))
    return pts


def accent_alpha(a: float) -> tuple[int, int, int, int]:
    return (ACCENT[0], ACCENT[1], ACCENT[2], max(0, min(255, int(round(a * 255)))))


def build_dots() -> tuple[list[list[tuple[float, float]]], int, int]:
    cols = math.ceil(W / H_SPACING) + 3
    rows = math.ceil(H / V_SPACING) + 3
    grid: list[list[tuple[float, float]]] = []
    for row in range(rows):
        line: list[tuple[float, float]] = []
        is_odd = row % 2 == 1
        for col in range(cols):
            x = col * H_SPACING + (H_SPACING * 0.5 if is_odd else 0) - H_SPACING
            y = row * V_SPACING - V_SPACING
            line.append((x, y))
        grid.append(line)
    return grid, rows, cols


def down_neighbors(row: int, col: int) -> list[tuple[int, int]]:
    if row % 2 == 1:
        return [(row + 1, col), (row + 1, col + 1)]
    return [(row + 1, col - 1), (row + 1, col)]


def build_route(start_col: int, rows: int, cols: int) -> list[tuple[int, int]]:
    route: list[tuple[int, int]] = []
    col = start_col
    for row in range(rows):
        route.append((row, col))
        if random.random() < TURN_CHANCE and row < rows - 1:
            new_col = col + (-1 if random.random() < 0.5 else 1)
            if 0 <= new_col < cols:
                route.append((row, new_col))
                col = new_col
        if row < rows - 1:
            dns = down_neighbors(row, col)
            pick = dns[0 if random.random() < 0.5 else 1]
            col = pick[1] if 0 <= pick[1] < cols else (
                dns[0][1] if 0 <= dns[0][1] < cols else dns[1][1]
            )
    return route


def draw_dots(draw: ImageDraw.ImageDraw, dots: list[list[tuple[float, float]]]) -> None:
    """Draw just the dot field. The hex outlines are intentionally omitted —
    the dot grid alone gives the canvas its pattern, and traces carry the
    visual energy on top."""
    dot_color = accent_alpha(DOT_REST_ALPHA)
    for row in dots:
        for (x, y) in row:
            if x < -HEX_SIZE or x > W + HEX_SIZE or y < -HEX_SIZE or y > H + HEX_SIZE:
                continue
            draw.ellipse(
                [x - DOT_RADIUS, y - DOT_RADIUS, x + DOT_RADIUS, y + DOT_RADIUS],
                fill=dot_color,
            )


def _draw_fading_line(
    draw: ImageDraw.ImageDraw,
    x1: float, y1: float,
    x2: float, y2: float,
    alpha_start: float,
    alpha_end: float,
    width: int = 2,
) -> None:
    """Draw a line that fades linearly from alpha_start (at x1,y1) to
    alpha_end (at x2,y2) by chopping it into ``SUB_SEGMENTS`` short pieces
    and drawing each at the interpolated alpha. PIL has no native gradient
    stroke, so this is the readable fallback."""
    for s in range(SUB_SEGMENTS):
        t0 = s / SUB_SEGMENTS
        t1 = (s + 1) / SUB_SEGMENTS
        sx = x1 + (x2 - x1) * t0
        sy = y1 + (y2 - y1) * t0
        ex = x1 + (x2 - x1) * t1
        ey = y1 + (y2 - y1) * t1
        # Use the midpoint alpha so the gradient steps don't band.
        tm = (t0 + t1) / 2
        a = alpha_start + (alpha_end - alpha_start) * tm
        draw.line([(sx, sy), (ex, ey)], fill=accent_alpha(a), width=width)


def draw_traces(
    draw: ImageDraw.ImageDraw,
    dots: list[list[tuple[float, float]]],
    rows: int,
    cols: int,
) -> None:
    """Bake a handful of trace tails that fade smoothly from a bright head
    down to nothing. Mirrors the look of the live canvas after ~10s of
    activity, but with a continuous gradient instead of stepwise per-segment
    alpha."""
    trace_count = max(8, int(cols * TRACE_DENSITY))
    recent_cols: list[int] = []
    for _ in range(trace_count):
        # Spread spawn columns out (same idea as pickSpawnCol in the JS).
        for _attempt in range(40):
            c = random.randint(0, cols - 1)
            if all(abs(c - rc) >= 6 for rc in recent_cols[-3:]):
                recent_cols.append(c)
                break
        else:
            c = random.randint(0, cols - 1)
            recent_cols.append(c)
        route = build_route(c, rows, cols)
        if len(route) < 4:
            continue

        head_step = random.randint(3, max(4, len(route) - 2))
        tail_len = random.randint(TAIL_LEN_MIN, TAIL_LEN_MAX)
        # Alpha at position p along the tail (0 = oldest, 1 = at head). Use a
        # squared curve so the brightest part is concentrated near the head
        # and the tail fades out gently rather than holding visible alpha for
        # half its length.
        def tail_alpha(p: float) -> float:
            return SEGMENT_PEAK_ALPHA * (p * p)

        steps = list(range(head_step - tail_len, head_step))
        for i, step in enumerate(steps):
            if step < 0 or step + 1 >= len(route):
                continue
            r1, c1 = route[step]
            r2, c2 = route[step + 1]
            if not (0 <= r1 < rows and 0 <= r2 < rows):
                continue
            if not (0 <= c1 < cols and 0 <= c2 < cols):
                continue
            x1, y1 = dots[r1][c1]
            x2, y2 = dots[r2][c2]
            a_start = tail_alpha(i / tail_len)
            a_end = tail_alpha((i + 1) / tail_len)
            _draw_fading_line(draw, x1, y1, x2, y2, a_start, a_end)

        # The head's in-progress sub-segment, at peak alpha.
        if 0 <= head_step < len(route) - 1:
            r1, c1 = route[head_step]
            r2, c2 = route[head_step + 1]
            if 0 <= r1 < rows and 0 <= r2 < rows and 0 <= c1 < cols and 0 <= c2 < cols:
                x1, y1 = dots[r1][c1]
                x2, y2 = dots[r2][c2]
                t = random.random() * 0.8 + 0.1
                hx = x1 + (x2 - x1) * t
                hy = y1 + (y2 - y1) * t
                # The head segment is at peak; fade the very tip slightly so it
                # doesn't look chopped off.
                _draw_fading_line(draw, x1, y1, hx, hy, SEGMENT_PEAK_ALPHA, SEGMENT_PEAK_ALPHA * 0.6)


def generate() -> None:
    print(f"Generating static hex-warp ({W}x{H})...")
    # Draw onto a fully transparent overlay. PIL's ImageDraw doesn't alpha-blend
    # against the underlying pixels — it just writes the color including its
    # alpha channel. If we draw on an opaque BG, low-alpha strokes look fine
    # in RGBA but convert("RGB") drops the alpha and the strokes come through
    # at full color. By drawing on a transparent layer and then alpha-
    # compositing onto an opaque BG, the alpha actually mixes.
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay, "RGBA")
    dots, rows, cols = build_dots()
    draw_dots(draw, dots)
    draw_traces(draw, dots, rows, cols)

    bg_img = Image.new("RGB", (W, H), BG[:3])
    bg_img.paste(overlay, (0, 0), overlay)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    bg_img.save(OUTPUT, "PNG", optimize=True)
    print(f"  Saved: {OUTPUT}")
    print(f"  Size:  {OUTPUT.stat().st_size / 1024:.1f} KB")


if __name__ == "__main__":
    generate()
