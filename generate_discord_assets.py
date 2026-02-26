#!/usr/bin/env python3
"""
Discord Banner + Animated Server Icon Generator

Generates:
  1. resources/discord-banner.png  (960x540 static PNG)
  2. resources/discord-icon.gif    (256x256 looping GIF)

Uses the hex grid animation style from the sudo teaser video with the
Smarter Dev brand identity (SD brain logo, Bungee Hairline + Bruno Ace SC
fonts, cyan/dark color palette).
"""

import math
import random
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter

BANNER_SEED = 56
ICON_SEED = 42

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).parent.resolve()
RESOURCE_DIR = PROJECT_ROOT / "resources"
FONT_DIR = RESOURCE_DIR / "fonts"

BRAIN_GLOW_PATH = RESOURCE_DIR / "smarter-dev-brain.png"
BRAIN_NO_GLOW_PATH = RESOURCE_DIR / "smarter-dev-brain-no-glow.png"
FONT_BUNGEE = str(FONT_DIR / "Bungee Hairline" / "BungeeHairline-Regular.ttf")
FONT_BRUNO = str(FONT_DIR / "Bruno_Ace_SC" / "BrunoAceSC-Regular.ttf")

BANNER_OUTPUT = RESOURCE_DIR / "discord-banner.png"
ICON_OUTPUT = RESOURCE_DIR / "discord-icon.gif"

# ---------------------------------------------------------------------------
# Brand colors
# ---------------------------------------------------------------------------
BG_COLOR = (2, 4, 8)          # --bg  #020408
CYAN = (0, 212, 255)          # --cyan #00d4ff
PRIMARY_CYAN = (0, 225, 255)  # #00E1FF (used for "Dev" text)
WHITE = (212, 224, 236)       # --white #d4e0ec

# ---------------------------------------------------------------------------
# Hex grid constants (matching sudo teaser)
# ---------------------------------------------------------------------------
BANNER_HEX_SIZE = 36
BANNER_H_SPACING = BANNER_HEX_SIZE * math.sqrt(3)
BANNER_V_SPACING = BANNER_HEX_SIZE * 1.5
BANNER_PATH_COUNT = 25
PATH_SPEED = 1.2
TURN_CHANCE = 0.2
SEGMENT_FADE = 0.001
SEGMENT_PEAK_ALPHA = 0.12
AMBIENT_AMPLITUDE = 1.0
AMBIENT_SPEED = 0.0008


def _hex_verts(hex_size):
    """Pre-compute hex vertex offsets (pointy-top)."""
    verts = []
    for i in range(6):
        angle = math.radians(60 * i - 30)
        verts.append((hex_size * math.cos(angle), hex_size * math.sin(angle)))
    return verts


# ===================================================================
# Banner Hex Grid (adapted from sudo teaser — dots + outlines + traces)
# ===================================================================

class BannerHexGrid:
    """Full hex grid with dots, outlines, and flowing traces for the banner."""

    def __init__(self, width, height, hex_size=BANNER_HEX_SIZE,
                 path_count=BANNER_PATH_COUNT):
        self.width = width
        self.height = height
        self.hex_size = hex_size
        self.h_spacing = hex_size * math.sqrt(3)
        self.v_spacing = hex_size * 1.5
        self.hex_verts = _hex_verts(hex_size)
        self.path_count = path_count

        self.cols = int(math.ceil(width / self.h_spacing)) + 3
        self.rows = int(math.ceil(height / self.v_spacing)) + 3
        self.dots = []
        self.traces = []
        self.recent_spawn_cols: list[int] = []

        for row in range(self.rows):
            for col in range(self.cols):
                is_odd = row % 2 == 1
                bx = col * self.h_spacing + (self.h_spacing * 0.5 if is_odd else 0) - self.h_spacing
                by = row * self.v_spacing - self.v_spacing
                self.dots.append({
                    "baseX": bx, "baseY": by,
                    "offsetX": 0.0, "offsetY": 0.0,
                    "col": col, "row": row,
                    "phaseX": random.random() * math.pi * 2,
                    "phaseY": random.random() * math.pi * 2,
                })

        self._init_traces()

    def _get_dot(self, row, col):
        if 0 <= row < self.rows and 0 <= col < self.cols:
            return self.dots[row * self.cols + col]
        return None

    def _down_neighbors(self, row, col):
        if row % 2 == 1:
            return [(row + 1, col), (row + 1, col + 1)]
        return [(row + 1, col - 1), (row + 1, col)]

    def _build_route(self, start_col):
        route = []
        col = start_col
        for row in range(self.rows):
            route.append((row, col))
            if random.random() < TURN_CHANCE and row < self.rows - 1:
                d = random.choice([-1, 1])
                nc = col + d
                if 0 <= nc < self.cols:
                    route.append((row, nc))
                    col = nc
            if row < self.rows - 1:
                dns = self._down_neighbors(row, col)
                pick = dns[random.randint(0, 1)]
                if 0 <= pick[1] < self.cols:
                    col = pick[1]
                else:
                    col = dns[0][1] if 0 <= dns[0][1] < self.cols else dns[1][1]
        return route

    def _pick_spawn_col(self):
        MIN_DIST = 8
        for _ in range(200):
            col = random.randint(0, self.cols - 1)
            if not any(abs(col - p) < MIN_DIST for p in self.recent_spawn_cols):
                self.recent_spawn_cols.append(col)
                if len(self.recent_spawn_cols) > 3:
                    self.recent_spawn_cols.pop(0)
                return col
        col = random.randint(0, self.cols - 1)
        self.recent_spawn_cols.append(col)
        if len(self.recent_spawn_cols) > 3:
            self.recent_spawn_cols.pop(0)
        return col

    def _spawn_trace(self):
        col = self._pick_spawn_col()
        return {
            "route": self._build_route(col),
            "step": 0,
            "progress": 0.0,
            "speed": PATH_SPEED * (0.5 + random.random() * 0.8),
            "segments": [],
        }

    def _init_traces(self):
        self.recent_spawn_cols = []
        self.traces = []
        for _ in range(self.path_count):
            t = self._spawn_trace()
            t["step"] = random.randint(0, int(len(t["route"]) * 0.8))
            self.traces.append(t)

    def update(self, time_ms):
        for dot in self.dots:
            ax = math.sin(time_ms * AMBIENT_SPEED + dot["phaseX"]) * AMBIENT_AMPLITUDE
            ay = math.cos(time_ms * AMBIENT_SPEED * 0.7 + dot["phaseY"]) * AMBIENT_AMPLITUDE
            dot["offsetX"] += (ax - dot["offsetX"]) * 0.08
            dot["offsetY"] += (ay - dot["offsetY"]) * 0.08

        for i in range(len(self.traces)):
            t = self.traces[i]
            if t["step"] < len(t["route"]) - 1:
                fr = t["route"][t["step"]]
                to = t["route"][t["step"] + 1]
                d1 = self._get_dot(fr[0], fr[1])
                d2 = self._get_dot(to[0], to[1])
                step_dist = self.h_spacing
                if d1 and d2:
                    dx = d2["baseX"] - d1["baseX"]
                    dy = d2["baseY"] - d1["baseY"]
                    step_dist = math.sqrt(dx * dx + dy * dy) or self.h_spacing
                t["progress"] += t["speed"] / step_dist
                if t["progress"] >= 1:
                    t["segments"].append({
                        "fromRow": fr[0], "fromCol": fr[1],
                        "toRow": to[0], "toCol": to[1],
                        "alpha": SEGMENT_PEAK_ALPHA,
                    })
                    t["progress"] -= 1
                    t["step"] += 1
            else:
                self.traces[i] = self._spawn_trace()
                t = self.traces[i]

            j = len(t["segments"]) - 1
            while j >= 0:
                t["segments"][j]["alpha"] -= SEGMENT_FADE
                if t["segments"][j]["alpha"] <= 0:
                    t["segments"].pop(j)
                j -= 1

    def draw(self, img, trace_color=CYAN, brightness_mult=1.0):
        draw = ImageDraw.Draw(img)
        tr, tg, tb = trace_color[:3]
        br, bg_g, bb = BG_COLOR

        # Hex outlines and center dots
        base_hex_alpha = 0.025
        base_dot_alpha = 0.12
        for dot in self.dots:
            x = dot["baseX"] + dot["offsetX"]
            y = dot["baseY"] + dot["offsetY"]

            ha = base_hex_alpha
            hex_color = (
                int(br + (tr - br) * ha),
                int(bg_g + (tg - bg_g) * ha),
                int(bb + (tb - bb) * ha), 255,
            )
            pts = [(x + vx, y + vy) for vx, vy in self.hex_verts]
            draw.polygon(pts, outline=hex_color)

            da = base_dot_alpha
            dot_color = (
                int(br + (tr - br) * da),
                int(bg_g + (tg - bg_g) * da),
                int(bb + (tb - bb) * da), 255,
            )
            dr_ = 1.5
            draw.ellipse([x - dr_, y - dr_, x + dr_, y + dr_], fill=dot_color)

        # Build per-trace point lists with alpha for gradient drawing.
        # Each trace becomes [(x, y, alpha), ...] ordered tail to head.
        trace_paths = []  # (max_alpha, [(x, y, a), ...])

        for t in self.traces:
            points = []
            for seg in t["segments"]:
                d1 = self._get_dot(seg["fromRow"], seg["fromCol"])
                d2 = self._get_dot(seg["toRow"], seg["toCol"])
                if not d1 or not d2:
                    continue
                a = min(1.0, seg["alpha"] * brightness_mult)
                x1 = d1["baseX"] + d1["offsetX"]
                y1 = d1["baseY"] + d1["offsetY"]
                x2 = d2["baseX"] + d2["offsetX"]
                y2 = d2["baseY"] + d2["offsetY"]
                if not points:
                    points.append((x1, y1, a))
                points.append((x2, y2, a))

            if t["step"] < len(t["route"]) - 1:
                fr = t["route"][t["step"]]
                to = t["route"][t["step"] + 1]
                d1 = self._get_dot(fr[0], fr[1])
                d2 = self._get_dot(to[0], to[1])
                if d1 and d2:
                    x1 = d1["baseX"] + d1["offsetX"]
                    y1 = d1["baseY"] + d1["offsetY"]
                    x2 = d2["baseX"] + d2["offsetX"]
                    y2 = d2["baseY"] + d2["offsetY"]
                    px = x1 + (x2 - x1) * t["progress"]
                    py = y1 + (y2 - y1) * t["progress"]
                    a = min(1.0, SEGMENT_PEAK_ALPHA * brightness_mult)
                    if not points:
                        points.append((x1, y1, a))
                    points.append((px, py, a))

            if len(points) >= 2:
                max_a = max(p[2] for p in points)
                trace_paths.append((max_a, points))

        # Collect ALL gradient sub-segments across all traces,
        # then sort globally by alpha so dim pieces always draw before bright.
        all_subseg = []  # (alpha, sx1, sy1, sx2, sy2)
        num_subs = 8
        for _max_a, points in trace_paths:
            for i in range(1, len(points)):
                x1, y1, a1 = points[i - 1]
                x2, y2, a2 = points[i]
                for s in range(num_subs):
                    t0 = s / num_subs
                    t1 = (s + 1) / num_subs
                    sx1 = x1 + (x2 - x1) * t0
                    sy1 = y1 + (y2 - y1) * t0
                    sx2 = x1 + (x2 - x1) * t1
                    sy2 = y1 + (y2 - y1) * t1
                    sa = a1 + (a2 - a1) * (t0 + t1) / 2
                    all_subseg.append((sa, sx1, sy1, sx2, sy2))

        all_subseg.sort(key=lambda s: s[0])

        for sa, sx1, sy1, sx2, sy2 in all_subseg:
            vis_a = min(1.0, sa * 3)
            seg_color = (
                int(br + (tr - br) * vis_a),
                int(bg_g + (tg - bg_g) * vis_a),
                int(bb + (tb - bb) * vis_a), 255,
            )
            draw.line([(sx1, sy1), (sx2, sy2)], fill=seg_color, width=8)


# ===================================================================
# Icon Hex Grid (traces only, fixed routes for perfect looping)
# ===================================================================

ICON_SIZE = 256
ICON_HEX_SIZE = 20
ICON_TRACE_COUNT = 12    # more traces visible
ICON_SPEED = 9.0         # pixels per frame
ICON_TRAIL_PX = 120      # visible trail length in pixels
ICON_FPS = 10
ICON_TURN_CHANCE = 0.35


class IconTraceSystem:
    """Simple trace system for the icon — few large traces, no wrapping glitches."""

    def __init__(self):
        self.size = ICON_SIZE
        self.h_spacing = ICON_HEX_SIZE * math.sqrt(3)
        self.v_spacing = ICON_HEX_SIZE * 1.5
        self.cols = int(math.ceil(self.size / self.h_spacing)) + 3
        self.rows = int(math.ceil(self.size / self.v_spacing)) + 3

        # Build grid positions
        self.grid = []  # grid[row][col] = (x, y)
        for row in range(self.rows):
            row_pos = []
            for col in range(self.cols):
                is_odd = row % 2 == 1
                x = col * self.h_spacing + (self.h_spacing * 0.5 if is_odd else 0) - self.h_spacing
                y = row * self.v_spacing - self.v_spacing
                row_pos.append((x, y))
            self.grid.append(row_pos)

        # Build routes as pixel paths (top to bottom through the hex grid)
        self.routes = []
        self.route_cum_dists = []  # cumulative distances for each route
        for _ in range(ICON_TRACE_COUNT):
            route = self._build_route()
            self.routes.append(route)
            # Compute cumulative distance
            cum = [0.0]
            for j in range(1, len(route)):
                dx = route[j][0] - route[j - 1][0]
                dy = route[j][1] - route[j - 1][1]
                cum.append(cum[-1] + math.sqrt(dx * dx + dy * dy))
            self.route_cum_dists.append(cum)

        # All routes have the same length (top-to-bottom through same # of rows)
        # so use the max for the loop period
        self.route_length = max(c[-1] for c in self.route_cum_dists)

        # Total frames = time for a trace to travel the full route + trail
        travel_dist = self.route_length + ICON_TRAIL_PX
        self.total_frames = int(travel_dist / ICON_SPEED)

        # Stagger offsets evenly so traces are spread across the loop
        self.offsets = [i * self.total_frames / ICON_TRACE_COUNT
                        for i in range(ICON_TRACE_COUNT)]

    def _down_neighbors(self, row, col):
        if row % 2 == 1:
            return [(row + 1, col), (row + 1, col + 1)]
        return [(row + 1, col - 1), (row + 1, col)]

    def _build_route(self):
        """Build a pixel-coordinate route top-to-bottom."""
        col = random.randint(0, self.cols - 1)
        indices = []
        for row in range(self.rows):
            indices.append((row, col))
            if random.random() < ICON_TURN_CHANCE and row < self.rows - 1:
                d = random.choice([-1, 1])
                nc = col + d
                if 0 <= nc < self.cols:
                    indices.append((row, nc))
                    col = nc
            if row < self.rows - 1:
                dns = self._down_neighbors(row, col)
                pick = dns[random.randint(0, 1)]
                if 0 <= pick[1] < self.cols:
                    col = pick[1]
                else:
                    col = dns[0][1] if 0 <= dns[0][1] < self.cols else dns[1][1]
        # Convert to pixel positions
        return [self.grid[r][c] for r, c in indices
                if 0 <= r < self.rows and 0 <= c < self.cols]

    def _point_at_distance(self, route_idx, dist):
        """Interpolate point along a route. Returns None if dist is out of range."""
        route = self.routes[route_idx]
        cum = self.route_cum_dists[route_idx]
        total = cum[-1]

        if dist < 0 or dist > total:
            return None

        # Find segment
        for i in range(1, len(route)):
            if cum[i] >= dist:
                seg_len = cum[i] - cum[i - 1]
                t = (dist - cum[i - 1]) / seg_len if seg_len > 0 else 0
                x = route[i - 1][0] + (route[i][0] - route[i - 1][0]) * t
                y = route[i - 1][1] + (route[i][1] - route[i - 1][1]) * t
                return (x, y)
        return route[-1]

    def draw_frame(self, img, frame_num, trace_color=CYAN):
        """Draw traces for a single frame."""
        draw = ImageDraw.Draw(img)
        br, bg_g, bb = BG_COLOR
        tr, tg, tb = trace_color[:3]

        # Sort traces so the most-faded (furthest along) draw first,
        # and the freshest (just appeared) draw last on top.
        # "progress" = how far along the route the head is (0..1)
        trace_order = []
        for i in range(ICON_TRACE_COUNT):
            raw_frame = (frame_num + self.offsets[i]) % self.total_frames
            progress = raw_frame / self.total_frames
            trace_order.append((progress, i))
        trace_order.sort(reverse=True)  # most progressed (fading) first

        for _progress, i in trace_order:
            raw_frame = (frame_num + self.offsets[i]) % self.total_frames
            head_dist = raw_frame * ICON_SPEED - ICON_TRAIL_PX

            # Draw trail as series of short segments with fade
            num_segs = 16
            seg_step = ICON_TRAIL_PX / num_segs
            prev_pt = None
            for s in range(num_segs + 1):
                d = head_dist + ICON_TRAIL_PX - s * seg_step
                pt = self._point_at_distance(i, d)

                if pt is not None and prev_pt is not None:
                    alpha = (1.0 - s / num_segs) * 0.5
                    color = (
                        int(br + (tr - br) * alpha),
                        int(bg_g + (tg - bg_g) * alpha),
                        int(bb + (tb - bb) * alpha),
                        255,
                    )
                    draw.line([prev_pt, pt], fill=color, width=5)

                prev_pt = pt


# ===================================================================
# Font loading helper
# ===================================================================

def load_font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        print(f"  Warning: Could not load font {path}, using default")
        return ImageFont.load_default()


# ===================================================================
# Banner generation
# ===================================================================

def generate_banner():
    """Generate the 960x540 Discord banner PNG."""
    random.seed(BANNER_SEED)
    W, H = 960, 540
    print("Generating banner (960x540)...")

    # 1. Background
    img = Image.new("RGBA", (W, H), BG_COLOR + (255,))

    # 2. Hex grid — warm up 300 frames, render one
    print("  Building hex grid and warming up 300 frames...")
    grid = BannerHexGrid(W, H)
    dt = 33  # ~30fps timestep in ms
    for frame in range(300):
        grid.update(frame * dt)
    grid.draw(img, trace_color=CYAN, brightness_mult=1.0)

    # 3. Load brain logo — smaller, 65% opacity
    brain = Image.open(BRAIN_GLOW_PATH).convert("RGBA")
    brain_h = 150
    aspect = brain.width / brain.height
    brain_w = int(brain_h * aspect)
    brain = brain.resize((brain_w, brain_h), Image.Resampling.LANCZOS)
    # Apply 65% opacity
    r, g, b, a = brain.split()
    a = a.point(lambda x: int(x * 0.50))
    brain = Image.merge("RGBA", (r, g, b, a))

    brain_x = 80
    brain_y = (H - brain_h) // 2
    img.paste(brain, (brain_x, brain_y), brain)

    # 4. Render text
    text_x = brain_x + brain_w + 30

    # "Smarter" — Bungee Hairline, white with glow
    smarter_size = 72
    font_smarter = load_font(FONT_BUNGEE, smarter_size)

    # "Dev" — Bruno Ace SC, scaled down to visually match hairline weight
    dev_size = 57
    font_dev = load_font(FONT_BRUNO, dev_size)

    # Measure both words' bounding boxes (getbbox returns (left, top, right, bottom)
    # relative to the (0,0) origin passed to draw.text)
    s_bbox = font_smarter.getbbox("Smarter")  # (left, top, right, bottom)
    d_bbox = font_dev.getbbox("Dev")
    gap = 16  # horizontal gap between words

    # Align by visual vertical center of each word.
    # Visual center of "Smarter" glyphs = midpoint of its bbox top/bottom
    s_vcenter = (s_bbox[1] + s_bbox[3]) / 2
    d_vcenter = (d_bbox[1] + d_bbox[3]) / 2
    # dev_y offset so both visual centers match
    dev_y_offset = s_vcenter - d_vcenter

    # Combined visual bounds (with dev offset applied)
    combined_top = min(s_bbox[1], d_bbox[1] + dev_y_offset)
    combined_bottom = max(s_bbox[3], d_bbox[3] + dev_y_offset)
    combined_h = combined_bottom - combined_top

    # Local Y positions within the combined block
    smarter_local_y = -combined_top
    dev_local_y = dev_y_offset - combined_top

    # dev X position (after smarter)
    dev_local_x = s_bbox[2] + gap

    # Place the combined text block vertically centered on the banner
    block_top_y = (H - combined_h) / 2

    smarter_y = int(block_top_y + smarter_local_y)
    dev_y = int(block_top_y + dev_local_y) - 3

    dev_x = text_x + dev_local_x

    # Dark BG glow behind both text words to mask traces underneath
    dark_glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dark_draw = ImageDraw.Draw(dark_glow)
    dark_color = BG_COLOR + (180,)
    for ox in range(-6, 7):
        for oy in range(-6, 7):
            dark_draw.text((text_x + ox, smarter_y + oy), "Smarter",
                           font=font_smarter, fill=dark_color)
            dark_draw.text((dev_x + ox, dev_y + oy), "Dev",
                           font=font_dev, fill=dark_color)
    dark_glow = dark_glow.filter(ImageFilter.GaussianBlur(radius=10))
    img = Image.alpha_composite(img, dark_glow)

    # White glow behind "Smarter"
    glow_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow_layer)
    glow_color = WHITE + (60,)
    for ox in range(-2, 3):
        for oy in range(-2, 3):
            glow_draw.text((text_x + ox, smarter_y + oy), "Smarter",
                           font=font_smarter, fill=glow_color)
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(radius=4))
    img = Image.alpha_composite(img, glow_layer)

    # Draw "Smarter"
    main_draw = ImageDraw.Draw(img)
    main_draw.text((text_x, smarter_y), "Smarter",
                   font=font_smarter, fill=WHITE + (255,))

    # Draw "Dev" — cyan at 75% opacity
    dev_color = PRIMARY_CYAN + (int(255 * 0.75),)
    dev_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dev_draw = ImageDraw.Draw(dev_layer)
    dev_draw.text((dev_x, dev_y), "Dev", font=font_dev, fill=dev_color)
    img = Image.alpha_composite(img, dev_layer)

    # 5. Save
    img = img.convert("RGB")
    img.save(BANNER_OUTPUT, "PNG")
    print(f"  Saved banner: {BANNER_OUTPUT}")
    print(f"  Size: {BANNER_OUTPUT.stat().st_size / 1024:.1f} KB")


# ===================================================================
# Icon GIF generation
# ===================================================================

def generate_icon():
    """Generate the 256x256 animated Discord icon GIF."""
    random.seed(ICON_SEED)
    print("Generating icon GIF (256x256)...")

    # Build trace system
    traces = IconTraceSystem()
    total_frames = traces.total_frames
    print(f"  Route length: {traces.route_length:.0f}px, "
          f"frames: {total_frames}, traces: {ICON_TRACE_COUNT}")

    # Load brain logo (with glow) — 85% of icon size, 80% opacity
    brain = Image.open(BRAIN_GLOW_PATH).convert("RGBA")
    brain_size = int(ICON_SIZE * 0.85)
    brain = brain.resize((brain_size, brain_size), Image.Resampling.LANCZOS)
    # Apply 80% opacity
    r, g, b, a = brain.split()
    a = a.point(lambda x: int(x * 0.90))
    brain = Image.merge("RGBA", (r, g, b, a))
    brain_layer = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    bx = (ICON_SIZE - brain_size) // 2
    by = (ICON_SIZE - brain_size) // 2
    brain_layer.paste(brain, (bx, by), brain)

    # Render frames
    frames = []
    for f in range(total_frames):
        frame = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), BG_COLOR + (255,))
        traces.draw_frame(frame, f, trace_color=CYAN)
        frame = Image.alpha_composite(frame, brain_layer)
        frames.append(frame.convert("RGB"))
        if (f + 1) % 20 == 0 or f == total_frames - 1:
            print(f"  Frame {f + 1}/{total_frames}")

    # Quantize frames to a reduced palette for smaller file size
    quantized = [fr.quantize(colors=64, method=Image.Quantize.MEDIANCUT)
                 for fr in frames]

    # Save GIF
    frame_duration = int(1000 / ICON_FPS)
    quantized[0].save(
        ICON_OUTPUT,
        save_all=True,
        append_images=quantized[1:],
        duration=frame_duration,
        loop=0,
        optimize=True,
    )
    print(f"  Saved icon: {ICON_OUTPUT}")
    print(f"  Size: {ICON_OUTPUT.stat().st_size / 1024:.1f} KB")


# ===================================================================
# Main
# ===================================================================

def main():
    print("=== Discord Asset Generator ===\n")
    RESOURCE_DIR.mkdir(parents=True, exist_ok=True)

    generate_banner()
    print()
    generate_icon()

    print("\nDone! Generated:")
    print(f"  Banner: {BANNER_OUTPUT}")
    print(f"  Icon:   {ICON_OUTPUT}")


if __name__ == "__main__":
    main()
