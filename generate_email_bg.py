#!/usr/bin/env python3
"""
Tileable Hex Grid Email Background & Logo Generator

Generates a seamlessly tileable PNG based on the hex-warp animation
from the Smarter Dev website and Discord branding assets,
plus a transparent "SMARTER Dev" logo image using brand fonts.

Output:
  resources/email-hex-bg.png
  resources/email-logo.png
"""

import math
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SEED = 91
HEX_SIZE = 18
H_SPACING = HEX_SIZE * math.sqrt(3)
V_SPACING = HEX_SIZE * 1.5

# Tile dimensions: must be exact multiples for seamless tiling
# Width = cols * H_SPACING, Height = (even rows) * V_SPACING
TILE_COLS = 10
TILE_ROWS = 10  # must be even for stagger pattern to repeat

TILE_W = round(TILE_COLS * H_SPACING)
TILE_H = round(TILE_ROWS * V_SPACING)

# Brand colors
BG = (2, 4, 8)
CYAN = (0, 212, 255)
WHITE = (212, 224, 236)

# Visual tuning — alpha values (0–255) for the RGBA overlay
DOT_ALPHA = 18             # all dots same brightness
TRACE_COUNT = 15
TRACE_PEAK_ALPHA = 65      # brightest segment alpha
TRACE_SEGMENTS = 5
TRACE_WIDTH = 2
DOT_RADIUS = 1.2

# Paths
PROJECT_ROOT = Path(__file__).parent.resolve()
FONTS_DIR = PROJECT_ROOT / "resources" / "fonts"
OUTPUT_BG = PROJECT_ROOT / "resources" / "email-hex-bg.png"
OUTPUT_LOGO = PROJECT_ROOT / "resources" / "email-logo.png"


def _hex_verts(size):
    """Pointy-top hex vertex offsets."""
    return [
        (size * math.cos(math.radians(60 * i - 30)),
         size * math.sin(math.radians(60 * i - 30)))
        for i in range(6)
    ]


def _dot_position(row, col):
    """Get the (x, y) center of a hex cell, with stagger for odd rows."""
    is_odd = row % 2 == 1
    x = col * H_SPACING + (H_SPACING * 0.5 if is_odd else 0)
    y = row * V_SPACING
    return x, y


def _wrap_positions(x, y):
    """Yield (x, y) plus wrapped copies so drawing near edges tiles correctly."""
    for dx in (-TILE_W, 0, TILE_W):
        for dy in (-TILE_H, 0, TILE_H):
            wx, wy = x + dx, y + dy
            if -HEX_SIZE * 2 < wx < TILE_W + HEX_SIZE * 2 and \
               -HEX_SIZE * 2 < wy < TILE_H + HEX_SIZE * 2:
                yield wx, wy



def _all_dot_positions():
    """Build a dict mapping (row, col, vert_index) → (x, y) for every dot.

    vert_index=-1 means center dot, 0-5 are the hex vertex dots.
    """
    verts = _hex_verts(HEX_SIZE)
    dots = {}
    for row in range(TILE_ROWS):
        for col in range(TILE_COLS):
            cx, cy = _dot_position(row, col)
            dots[(row, col, -1)] = (cx, cy)
            for vi in range(6):
                dots[(row, col, vi)] = (cx + verts[vi][0], cy + verts[vi][1])
    return dots


def _build_adjacency(dots):
    """Build adjacency list: two dots are neighbors if within one hex side length."""
    max_dist = HEX_SIZE * 1.05  # slight tolerance
    keys = list(dots.keys())
    adj = {k: [] for k in keys}
    for i, k1 in enumerate(keys):
        x1, y1 = dots[k1]
        for k2 in keys[i + 1:]:
            x2, y2 = dots[k2]
            if abs(x2 - x1) > max_dist or abs(y2 - y1) > max_dist:
                continue
            dist = math.hypot(x2 - x1, y2 - y1)
            if dist < max_dist:
                adj[k1].append(k2)
                adj[k2].append(k1)
    return adj


def _build_trace_route(dots, adj):
    """Build a route of exactly TRACE_SEGMENTS+1 points by random-walking.

    Only moves laterally or downward (never upward).
    Returns None if the walk dead-ends before reaching full length.
    """
    keys = list(dots.keys())
    current = random.choice(keys)
    cur_pos = dots[current]
    route = [cur_pos]
    visited = {current}

    for _ in range(TRACE_SEGMENTS):
        # Filter: not visited, y >= current y (no upward moves)
        candidates = [
            k for k in adj[current]
            if k not in visited and dots[k][1] >= cur_pos[1]
        ]
        if not candidates:
            return None
        current = random.choice(candidates)
        cur_pos = dots[current]
        visited.add(current)
        route.append(cur_pos)

    return route


def generate():
    random.seed(SEED)
    print(f"Generating tileable email background ({TILE_W}x{TILE_H})...")

    cr, cg, cb = CYAN

    # Solid background
    img = Image.new("RGBA", (TILE_W, TILE_H), BG + (255,))

    # Draw all cyan elements on a transparent overlay using the accent color
    # with varying alpha — this avoids sub-pixel RGB fringing
    overlay = Image.new("RGBA", (TILE_W, TILE_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Build all dot positions
    dots = _all_dot_positions()

    # --- All dots (centers + vertices, same style) ---
    dot_fill = (cr, cg, cb, DOT_ALPHA)
    for x, y in dots.values():
        for wx, wy in _wrap_positions(x, y):
            draw.ellipse(
                [wx - DOT_RADIUS, wy - DOT_RADIUS,
                 wx + DOT_RADIUS, wy + DOT_RADIUS],
                fill=dot_fill,
            )

    # --- Frozen trace segments ---
    # Collect all segments with their alpha, then draw dimmest first
    adj = _build_adjacency(dots)
    all_segments = []  # (alpha, x1, y1, dx, dy)

    attempts = 0
    traces_built = 0
    while traces_built < TRACE_COUNT and attempts < TRACE_COUNT * 5:
        attempts += 1
        route = _build_trace_route(dots, adj)
        if route is None:
            continue
        traces_built += 1

        for i in range(len(route) - 1):
            x1, y1 = route[i]
            x2, y2 = route[i + 1]

            # Wrap delta to shortest path across tile boundary
            dx = x2 - x1
            dy = y2 - y1
            if dx > TILE_W / 2: dx -= TILE_W
            elif dx < -TILE_W / 2: dx += TILE_W
            if dy > TILE_H / 2: dy -= TILE_H
            elif dy < -TILE_H / 2: dy += TILE_H

            # Fade out: last segment (lowest) brightest, first (highest) dimmest
            progress = i / max(1, TRACE_SEGMENTS - 1)
            alpha = int(TRACE_PEAK_ALPHA * (0.15 + 0.85 * progress))
            all_segments.append((alpha, x1, y1, dx, dy))

    # Draw dimmest segments first so brighter ones paint on top
    all_segments.sort(key=lambda s: s[0])
    for alpha, x1, y1, dx, dy in all_segments:
        seg_fill = (cr, cg, cb, alpha)
        for wx1, wy1 in _wrap_positions(x1, y1):
            wx2, wy2 = wx1 + dx, wy1 + dy
            draw.line([(wx1, wy1), (wx2, wy2)], fill=seg_fill, width=TRACE_WIDTH)

    # Composite overlay onto background
    img = Image.alpha_composite(img, overlay)

    # Save as RGB PNG (no need for alpha in the final tile)
    img = img.convert("RGB")
    OUTPUT_BG.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUTPUT_BG, "PNG", optimize=True)
    print(f"  Saved: {OUTPUT_BG}")
    print(f"  Size: {OUTPUT_BG.stat().st_size / 1024:.1f} KB")
    print(f"  Dimensions: {TILE_W}x{TILE_H}")


def generate_logo():
    """Generate a transparent PNG logo: 'SMARTER Dev' using brand fonts."""
    print("Generating email logo...")

    font_smarter = ImageFont.truetype(
        str(FONTS_DIR / "Bungee Hairline" / "BungeeHairline-Regular.ttf"), 42
    )
    font_dev = ImageFont.truetype(
        str(FONTS_DIR / "Bruno_Ace_SC" / "BrunoAceSC-Regular.ttf"), 30
    )

    # Measure text
    tmp = Image.new("RGBA", (1, 1))
    tmp_draw = ImageDraw.Draw(tmp)
    smarter_bbox = tmp_draw.textbbox((0, 0), "SMARTER", font=font_smarter)
    dev_bbox = tmp_draw.textbbox((0, 0), "Dev", font=font_dev)

    smarter_w = smarter_bbox[2] - smarter_bbox[0]
    smarter_h = smarter_bbox[3] - smarter_bbox[1]
    dev_w = dev_bbox[2] - dev_bbox[0]
    dev_h = dev_bbox[3] - dev_bbox[1]

    gap = 14  # space between words
    glow_pad = 20  # padding for glow effect
    total_w = smarter_w + gap + dev_w + glow_pad * 2
    total_h = max(smarter_h, dev_h) + glow_pad * 2

    # Draw on transparent canvas
    img = Image.new("RGBA", (total_w, total_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Align baselines: position SMARTER at top of content area,
    # then align Dev's baseline to match SMARTER's baseline
    smarter_y = glow_pad - smarter_bbox[1]
    smarter_baseline = smarter_y + smarter_bbox[3]
    dev_x = glow_pad + smarter_w + gap
    dev_baseline = dev_bbox[3]
    dev_y = smarter_baseline - dev_baseline - 5

    # Draw "SMARTER" in white
    draw.text((glow_pad, smarter_y), "SMARTER", font=font_smarter, fill=WHITE + (255,))

    # Draw "Dev" in cyan with glow
    # Glow layer: draw cyan text on separate layer, blur, composite
    glow_layer = Image.new("RGBA", (total_w, total_h), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow_layer)
    glow_draw.text((dev_x, dev_y), "Dev", font=font_dev, fill=CYAN + (100,))
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(radius=8))
    img = Image.alpha_composite(img, glow_layer)

    # Sharp "Dev" text on top
    draw = ImageDraw.Draw(img)
    draw.text((dev_x, dev_y), "Dev", font=font_dev, fill=CYAN + (255,))

    # Trim transparent edges
    bbox = img.getbbox()
    if bbox:
        img = img.crop(bbox)

    img.save(OUTPUT_LOGO, "PNG", optimize=True)
    print(f"  Saved: {OUTPUT_LOGO}")
    print(f"  Size: {OUTPUT_LOGO.stat().st_size / 1024:.1f} KB")
    print(f"  Dimensions: {img.width}x{img.height}")
    return img.width, img.height


if __name__ == "__main__":
    generate()
    generate_logo()
