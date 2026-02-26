#!/usr/bin/env python3
"""
Smarter Dev — sudo Membership Teaser Video (30s)
Renders a terminal/hacker narrative teaser using Pillow for frame rendering
and MoviePy for video/audio composition.

Output: videos/sudo-teaser/output/sudo_teaser.mp4 (1920x1080, 30fps)
"""

import math
import random
import numpy as np
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
WIDTH, HEIGHT = 1920, 1080
FPS = 30
DURATION = 30  # seconds
TOTAL_FRAMES = FPS * DURATION  # 900

# Colors (RGB tuples)
BG_COLOR = (2, 4, 8)
CYAN = (0, 212, 255)
WHITE = (212, 224, 236)
ROSE = (239, 68, 68)
AMBER = (234, 179, 8)
GREEN = (34, 197, 94)
GRAY = (74, 96, 120)

# Paths (relative to this script)
BASE_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = BASE_DIR.parent.parent
FONT_DIR = PROJECT_ROOT / "resources" / "fonts"
AUDIO_DIR = PROJECT_ROOT / "videos" / "rebrand-reveal" / "audio"
ASSETS_DIR = PROJECT_ROOT / "resources"
OUTPUT_DIR = BASE_DIR / "output"

FONT_BUNGEE = str(FONT_DIR / "Bungee Hairline" / "BungeeHairline-Regular.ttf")
FONT_BRUNO = str(FONT_DIR / "Bruno_Ace_SC" / "BrunoAceSC-Regular.ttf")
FONT_ANTA = str(FONT_DIR / "Anta" / "Anta-Regular.ttf")

# ---------------------------------------------------------------------------
# Hex Grid Constants (ported from mockup-42)
# ---------------------------------------------------------------------------
HEX_SIZE = 18
H_SPACING = HEX_SIZE * math.sqrt(3)  # ~31.18
V_SPACING = HEX_SIZE * 1.5  # 27
PATH_COUNT = 60
PATH_SPEED = 1.2
TURN_CHANCE = 0.2
SEGMENT_FADE = 0.001
SEGMENT_PEAK_ALPHA = 0.12
AMBIENT_AMPLITUDE = 1.0
AMBIENT_SPEED = 0.0008

# Pre-compute hex vertex offsets (pointy-top)
HEX_VERTS = []
for i in range(6):
    angle = math.radians(60 * i - 30)
    HEX_VERTS.append((HEX_SIZE * math.cos(angle), HEX_SIZE * math.sin(angle)))


# ---------------------------------------------------------------------------
# Hex Grid State
# ---------------------------------------------------------------------------
class HexGrid:
    """Manages the hex grid dots and flowing traces."""

    def __init__(self, width: int, height: int):
        self.width = width
        self.height = height
        self.cols = int(math.ceil(width / H_SPACING)) + 3
        self.rows = int(math.ceil(height / V_SPACING)) + 3
        self.dots = []
        self.traces = []
        self.recent_spawn_cols: list[int] = []

        # Create dots
        for row in range(self.rows):
            for col in range(self.cols):
                is_odd = row % 2 == 1
                bx = col * H_SPACING + (H_SPACING * 0.5 if is_odd else 0) - H_SPACING
                by = row * V_SPACING - V_SPACING
                self.dots.append({
                    "baseX": bx,
                    "baseY": by,
                    "offsetX": 0.0,
                    "offsetY": 0.0,
                    "col": col,
                    "row": row,
                    "phaseX": random.random() * math.pi * 2,
                    "phaseY": random.random() * math.pi * 2,
                })

        # Init traces
        self._init_traces()

    def _get_dot(self, row: int, col: int):
        if 0 <= row < self.rows and 0 <= col < self.cols:
            return self.dots[row * self.cols + col]
        return None

    def _down_neighbors(self, row: int, col: int):
        if row % 2 == 1:
            return [(row + 1, col), (row + 1, col + 1)]
        else:
            return [(row + 1, col - 1), (row + 1, col)]

    def _build_route(self, start_col: int):
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
        for _ in range(PATH_COUNT):
            t = self._spawn_trace()
            t["step"] = random.randint(0, int(len(t["route"]) * 0.8))
            self.traces.append(t)

    def update(self, time_ms: float):
        """Advance simulation by one tick at the given time in ms."""
        # Update dot ambient offsets
        for dot in self.dots:
            ax = math.sin(time_ms * AMBIENT_SPEED + dot["phaseX"]) * AMBIENT_AMPLITUDE
            ay = math.cos(time_ms * AMBIENT_SPEED * 0.7 + dot["phaseY"]) * AMBIENT_AMPLITUDE
            dot["offsetX"] += (ax - dot["offsetX"]) * 0.08
            dot["offsetY"] += (ay - dot["offsetY"]) * 0.08

        # Update traces
        for i in range(len(self.traces)):
            t = self.traces[i]
            if t["step"] < len(t["route"]) - 1:
                fr = t["route"][t["step"]]
                to = t["route"][t["step"] + 1]
                d1 = self._get_dot(fr[0], fr[1])
                d2 = self._get_dot(to[0], to[1])
                step_dist = H_SPACING
                if d1 and d2:
                    dx = d2["baseX"] - d1["baseX"]
                    dy = d2["baseY"] - d1["baseY"]
                    step_dist = math.sqrt(dx * dx + dy * dy) or H_SPACING
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

    def draw(self, img: Image.Image, trace_color: tuple = CYAN,
             brightness_mult: float = 1.0):
        """Draw hex grid onto the given Pillow Image."""
        draw = ImageDraw.Draw(img)

        # Draw hex outlines and center dots
        # Pre-blend against BG since draw calls overwrite pixels
        base_hex_alpha = 0.025
        base_dot_alpha = 0.12
        tr, tg, tb = trace_color[:3]
        br, bg_g, bb = BG_COLOR
        for dot in self.dots:
            x = dot["baseX"] + dot["offsetX"]
            y = dot["baseY"] + dot["offsetY"]

            # Hex outline — pre-blended against background (constant)
            ha = base_hex_alpha
            hex_color = (
                int(br + (tr - br) * ha),
                int(bg_g + (tg - bg_g) * ha),
                int(bb + (tb - bb) * ha),
                255,
            )
            pts = [(x + vx, y + vy) for vx, vy in HEX_VERTS]
            draw.polygon(pts, outline=hex_color)

            # Center dot — pre-blended against background (constant)
            da = base_dot_alpha
            dot_color = (
                int(br + (tr - br) * da),
                int(bg_g + (tg - bg_g) * da),
                int(bb + (tb - bb) * da),
                255,
            )
            dr = 1.5
            draw.ellipse([x - dr, y - dr, x + dr, y + dr], fill=dot_color)

        # Draw trace segments
        for t in self.traces:
            for seg in t["segments"]:
                d1 = self._get_dot(seg["fromRow"], seg["fromCol"])
                d2 = self._get_dot(seg["toRow"], seg["toCol"])
                if not d1 or not d2:
                    continue
                a = min(1.0, seg["alpha"] * brightness_mult)
                vis_a = min(1.0, a * 3)
                seg_color = (
                    int(br + (tr - br) * vis_a),
                    int(bg_g + (tg - bg_g) * vis_a),
                    int(bb + (tb - bb) * vis_a),
                    255,
                )
                x1 = d1["baseX"] + d1["offsetX"]
                y1 = d1["baseY"] + d1["offsetY"]
                x2 = d2["baseX"] + d2["offsetX"]
                y2 = d2["baseY"] + d2["offsetY"]
                draw.line([(x1, y1), (x2, y2)], fill=seg_color, width=2)

            # Active partial segment
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
                    vis_a = min(1.0, a * 3)
                    ac = (
                        int(br + (tr - br) * vis_a),
                        int(bg_g + (tg - bg_g) * vis_a),
                        int(bb + (tb - bb) * vis_a),
                        255,
                    )
                    draw.line([(x1, y1), (px, py)], fill=ac, width=2)


# ---------------------------------------------------------------------------
# Text rendering helpers
# ---------------------------------------------------------------------------
def load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        print(f"  Warning: Could not load font {path}, using default")
        return ImageFont.load_default()


def composite_text(img: Image.Image, text: str, pos: tuple,
                   font: ImageFont.FreeTypeFont, color: tuple,
                   alpha: float = 1.0):
    """Draw text onto img using proper alpha compositing (tight bbox)."""
    if alpha < 0.01:
        return
    r, g, b = color[:3]
    # Measure text bounds to create a minimal layer
    tmp_draw = ImageDraw.Draw(img)
    bbox = tmp_draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0] + 4  # small padding
    th = bbox[3] - bbox[1] + 4
    if tw <= 0 or th <= 0:
        return
    # Create small layer and draw text at origin-adjusted position
    layer = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    ld.text((-bbox[0] + 2, -bbox[1] + 2), text, font=font,
            fill=(r, g, b, int(255 * alpha)))
    # Paste with alpha mask at the target position
    px, py = int(pos[0]), int(pos[1])
    img.paste(layer, (px + bbox[0] - 2, py + bbox[1] - 2), layer)


def draw_text_centered(img: Image.Image, text: str, y: int,
                       font: ImageFont.FreeTypeFont, color: tuple,
                       alpha: float = 1.0, x_offset: int = 0):
    """Draw text horizontally centered on the canvas with proper compositing."""
    if alpha < 0.01:
        return 0, 0
    draw = ImageDraw.Draw(img)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    x = (WIDTH - tw) // 2 + x_offset
    composite_text(img, text, (x, y), font, color, alpha)
    return x, tw


def draw_text_with_glow(img: Image.Image, text: str, y: int,
                        font: ImageFont.FreeTypeFont, color: tuple,
                        alpha: float = 1.0, glow_color: tuple | None = None,
                        glow_radius: int = 8):
    """Draw text with a soft glow behind it."""
    if alpha < 0.01:
        return
    if glow_color is None:
        glow_color = color

    draw = ImageDraw.Draw(img)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    x = (WIDTH - tw) // 2

    # Draw glow layer
    glow_layer = Image.new("RGBA", (tw + glow_radius * 4, th + glow_radius * 4), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow_layer)
    gr, gg, gb = glow_color[:3]
    glow_draw.text((glow_radius * 2, glow_radius * 2), text, font=font,
                   fill=(gr, gg, gb, int(120 * alpha)))
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(glow_radius))
    img.paste(glow_layer, (x - glow_radius * 2, y - glow_radius * 2), glow_layer)

    # Draw text on top via compositing
    composite_text(img, text, (x, y), font, color, alpha)


def typing_progress(text: str, start_t: float, t: float,
                    chars_per_sec: float = 12.0) -> str:
    """Return the portion of text typed at time t."""
    elapsed = t - start_t
    if elapsed <= 0:
        return ""
    num_chars = int(elapsed * chars_per_sec)
    return text[:min(num_chars, len(text))]


def cursor_visible(t: float, blink_rate: float = 0.5) -> bool:
    """Blinking cursor: on for half the cycle, off for the other."""
    return (t % (blink_rate * 2)) < blink_rate


# ---------------------------------------------------------------------------
# Frame renderer
# ---------------------------------------------------------------------------
def render_frame(frame_idx: int, grid: HexGrid,
                 fonts: dict) -> np.ndarray:
    """Render a single frame and return as a numpy array (H, W, 3)."""
    t = frame_idx / FPS  # time in seconds
    time_ms = t * 1000

    # Update hex grid
    grid.update(time_ms)

    # Create frame
    img = Image.new("RGBA", (WIDTH, HEIGHT), (*BG_COLOR, 255))

    # --- Determine hex grid visual parameters ---
    trace_color = CYAN
    brightness_mult = 1.0

    # Phase 2: Red flash at 6.5-7.5s
    if 6.5 <= t < 7.5:
        flash_t = (t - 6.5) / 1.0
        # Blend from cyan toward rose then back
        blend = math.sin(flash_t * math.pi)
        trace_color = (
            int(CYAN[0] + (ROSE[0] - CYAN[0]) * blend),
            int(CYAN[1] + (ROSE[1] - CYAN[1]) * blend),
            int(CYAN[2] + (ROSE[2] - CYAN[2]) * blend),
        )

    # Phase 2: Intensify at 8-9.5s
    if 8.0 <= t < 9.5:
        brightness_mult = 1.0 + 0.5 * ((t - 8.0) / 1.5)

    # Phase 3: Sharp grid spike on reveal (13-14.5s)
    if 13.0 <= t < 13.15:
        # Instant spike
        brightness_mult = 3.5
    elif 13.15 <= t < 14.5:
        # Decay back to normal
        decay = 1.0 - ((t - 13.15) / 1.35)
        brightness_mult = 1.0 + 2.5 * decay

    # Draw hex grid
    grid.draw(img, trace_color=trace_color, brightness_mult=brightness_mult)

    # --- Compute global fade (0-1s fade in, 29.5-30s fade out) ---
    global_alpha = 1.0

    # ===================================================================
    # PHASES 1-2: Terminal console (0-11s)
    # Left-aligned, lines persist like a real terminal
    # ===================================================================
    TERM_LINE_H = 50  # line height
    TERM_COLOR = (180, 195, 210)  # muted light gray for all terminal text
    # Compute left edge so the widest line ("$ sudo elevate-access") is centered
    _widest = "$ sudo elevate-access"
    _wb = ImageDraw.Draw(img).textbbox((0, 0), _widest, font=fonts["anta_36"])
    TERM_X = (WIDTH - (_wb[2] - _wb[0])) // 2

    if 0 <= t < 13.0:
        term_y = HEIGHT // 2 - 125  # top of terminal block (5 lines)
        line = 0

        # Fade everything out together at 11-13s
        term_alpha = 1.0
        if t >= 11.0:
            term_alpha = max(0, 1.0 - (t - 11.0) / 2.0)
        ta = term_alpha * global_alpha

        # Line 0: "$ whoami" (types 1-3s)
        if t >= 1.0 and ta > 0.01:
            text_0 = typing_progress("$ whoami", 1.0, t, chars_per_sec=8)
            if text_0:
                composite_text(img, text_0, (TERM_X, term_y + line * TERM_LINE_H),
                               fonts["anta_36"], TERM_COLOR, ta)
        line += 1

        # Line 1: "developer" (appears instantly at 3s — it's command output)
        if t >= 3.0 and ta > 0.01:
            composite_text(img, "developer", (TERM_X, term_y + line * TERM_LINE_H),
                           fonts["anta_36"], TERM_COLOR, ta)
        line += 1

        # Line 2: "$ access --level elite" (types 5-6.5s)
        if t >= 5.0 and ta > 0.01:
            text_2 = typing_progress("$ access --level elite", 5.0, t, chars_per_sec=14)
            if text_2:
                composite_text(img, text_2, (TERM_X, term_y + line * TERM_LINE_H),
                               fonts["anta_36"], TERM_COLOR, ta)
        line += 1

        # Line 3: "permission denied" (appears instantly at 6.5s — it's command output)
        if t >= 6.5 and ta > 0.01:
            composite_text(img, "permission denied", (TERM_X, term_y + line * TERM_LINE_H),
                           fonts["anta_36"], ROSE, ta)
        line += 1

        # Line 4: "$ sudo elevate-access" (types 8-11s, slower for drama)
        if t >= 8.0 and ta > 0.01:
            text_4 = typing_progress("$ sudo elevate-access", 8.0, t, chars_per_sec=7)
            if text_4:
                composite_text(img, text_4, (TERM_X, term_y + line * TERM_LINE_H),
                               fonts["anta_36"], TERM_COLOR, ta)
        line += 1

        # Blinking cursor on the active line
        if ta > 0.01 and cursor_visible(t):
            if t < 3.0:
                cur_text = typing_progress("$ whoami", 1.0, t, chars_per_sec=8)
                cur_line = 0
            elif t < 5.0:
                cur_text = "developer"
                cur_line = 1
            elif t < 6.5:
                cur_text = typing_progress("$ access --level elite", 5.0, t, chars_per_sec=14)
                cur_line = 2
            elif t < 8.0:
                cur_text = "permission denied"
                cur_line = 3
            else:
                cur_text = typing_progress("$ sudo elevate-access", 8.0, t, chars_per_sec=7)
                cur_line = 4
            # Measure text width for cursor position
            tmp = ImageDraw.Draw(img)
            bbox = tmp.textbbox((0, 0), cur_text, font=fonts["anta_36"])
            cx = TERM_X + (bbox[2] - bbox[0]) + 4
            composite_text(img, "_", (cx, term_y + cur_line * TERM_LINE_H),
                           fonts["anta_36"], TERM_COLOR, ta)

    # ===================================================================
    # PHASE 3: "sudo" reveal (13-18s)
    # ===================================================================
    if 13.0 <= t < 18.0:
        # White flash overlay (quick burst, decays fast)
        if 13.0 <= t < 13.25:
            flash_alpha = int(100 * (1.0 - (t - 13.0) / 0.25))
            overlay = Image.new("RGBA", (WIDTH, HEIGHT), (255, 255, 255, flash_alpha))
            img = Image.alpha_composite(img, overlay)

        reveal_alpha = min(1.0, (t - 13.0) / 0.3)  # snap in fast
        # Glow burst: starts huge and decays to steady
        if t < 14.0:
            glow_burst = 10 + int(30 * max(0, 1.0 - (t - 13.0) / 1.0))
        else:
            glow_burst = 10
        draw_text_with_glow(img, "sudo", HEIGHT // 2 - 80,
                            fonts["bruno_100"], WHITE,
                            alpha=reveal_alpha * global_alpha,
                            glow_color=CYAN, glow_radius=glow_burst)


    # ===================================================================
    # PHASE 4: "The Tease" (18-26s)
    # ===================================================================
    if 18.0 <= t < 26.0:
        # sudo + MEMBERSHIP persist
        phase4_alpha = 1.0
        if t >= 22.0:
            # Ambient pulse
            phase4_alpha = 0.9 + 0.1 * math.sin(t * 3)

        draw_text_with_glow(img, "sudo", HEIGHT // 2 - 80,
                            fonts["bruno_100"], WHITE,
                            alpha=phase4_alpha * global_alpha,
                            glow_color=CYAN, glow_radius=10)
        # Feature chips (18-20s, staggered) — centered under "sudo"
        chip_y_base = HEIGHT // 2 + 50
        chips = [
            ("PRIORITY CHALLENGES", 18.0),
            ("EXCLUSIVE TOOLS", 18.5),
            ("ELEVATED ACCESS", 19.0),
        ]
        # Measure widest chip to center the block
        _chip_tmp = ImageDraw.Draw(img)
        _chip_widths = [_chip_tmp.textbbox((0, 0), l, font=fonts["anta_24"])[2] for l, _ in chips]
        _max_chip_w = max(_chip_widths) + 20  # dot + gap
        _chip_block_x = (WIDTH - _max_chip_w) // 2
        for chip_idx, (label, chip_start) in enumerate(chips):
            if t >= chip_start:
                chip_alpha = min(1.0, (t - chip_start) / 0.5)
                if t >= 25.5:
                    chip_alpha *= max(0, 1.0 - (t - 25.5) / 0.5)
                cy = chip_y_base + chip_idx * 40
                ca = chip_alpha * global_alpha
                if ca > 0.01:
                    dot_r = 4
                    dot_x = _chip_block_x
                    dot_layer = Image.new("RGBA", (dot_r * 2 + 2, dot_r * 2 + 2), (0, 0, 0, 0))
                    dot_draw = ImageDraw.Draw(dot_layer)
                    dot_draw.ellipse([1, 1, dot_r * 2 + 1, dot_r * 2 + 1],
                                     fill=(*CYAN, int(255 * ca)))
                    img.paste(dot_layer, (dot_x - dot_r, cy + 6 - dot_r), dot_layer)
                    composite_text(img, label, (dot_x + 15, cy - 6),
                                   fonts["anta_24"], GRAY, ca)


    # ===================================================================
    # PHASE 5: "Coming Soon" (26-30s)
    # ===================================================================
    if t >= 26.0:
        draw_text_with_glow(img, "sudo", HEIGHT // 2 - 80,
                            fonts["bruno_100"], WHITE,
                            alpha=global_alpha,
                            glow_color=CYAN, glow_radius=10)

        # "COMING SOON" types in (26-28s)
        if t >= 26.0:
            cs_text = typing_progress("COMING SOON", 26.0, t, chars_per_sec=10)
            if cs_text:
                draw_text_centered(img, cs_text, HEIGHT // 2 + 130,
                                   fonts["bruno_30"], WHITE,
                                   alpha=global_alpha * 0.7)

        # "smarter.dev/sudo" fades in (28-29.5s)
        if t >= 28.0:
            url_alpha = min(1.0, (t - 28.0) / 1.0)
            draw_text_centered(img, "smarter.dev/sudo", HEIGHT // 2 + 180,
                               fonts["anta_24"], GRAY,
                               alpha=url_alpha * global_alpha)

    # Convert to RGB numpy array
    rgb = img.convert("RGB")
    return np.array(rgb)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print("=" * 60)
    print("  Smarter Dev — sudo Membership Teaser (30s)")
    print("=" * 60)

    # Ensure output dir exists
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Load fonts
    print("\nLoading fonts...")
    fonts = {
        "bruno_100": load_font(FONT_BRUNO, 100),
        "bruno_60": load_font(FONT_BRUNO, 60),
        "bruno_40": load_font(FONT_BRUNO, 40),
        "bruno_30": load_font(FONT_BRUNO, 30),
        "anta_36": load_font(FONT_ANTA, 36),
        "anta_24": load_font(FONT_ANTA, 24),
    }
    print("  Fonts loaded.")

    # Initialize hex grid
    print("\nInitializing hex grid...")
    grid = HexGrid(WIDTH, HEIGHT)
    print(f"  Grid: {grid.cols}x{grid.rows} = {len(grid.dots)} dots, {len(grid.traces)} traces")

    # Warm up the grid so traces are already flowing at frame 0
    WARMUP_FRAMES = 300  # 10 seconds at 30fps
    print(f"\nWarming up grid ({WARMUP_FRAMES} frames)...")
    for i in range(WARMUP_FRAMES):
        grid.update(-(WARMUP_FRAMES - i) * (1000 / FPS))

    # Render all frames
    print(f"\nRendering {TOTAL_FRAMES} frames...")
    frames = []
    for i in range(TOTAL_FRAMES):
        frame = render_frame(i, grid, fonts)
        frames.append(frame)
        if (i + 1) % FPS == 0:
            sec = (i + 1) // FPS
            print(f"  {sec}/{DURATION}s rendered ({i + 1}/{TOTAL_FRAMES} frames)")

    print("\nAll frames rendered. Building video with MoviePy...")

    # Import MoviePy
    from moviepy import (
        VideoClip, AudioFileClip, CompositeAudioClip,
    )
    from moviepy.audio.fx import AudioFadeIn, AudioFadeOut

    # Create video clip from frames
    def make_frame(t):
        idx = min(int(t * FPS), TOTAL_FRAMES - 1)
        return frames[idx]

    video = VideoClip(make_frame, duration=DURATION)

    # --- Audio ---
    print("\nBuilding audio mix...")
    audio_clips = []

    # Bass rumble (0-11s, fade in 0-1s, fade out 9.5-11s)
    bass_path = AUDIO_DIR / "bass-rumble.wav"
    if bass_path.exists():
        bass = (AudioFileClip(str(bass_path))
                .subclipped(0, 11)
                .with_volume_scaled(0.7)
                .with_effects([AudioFadeIn(1.0), AudioFadeOut(1.5)])
                .with_start(0))
        audio_clips.append(bass)
        print("  + bass-rumble.wav (0-11s)")
    else:
        print(f"  ! bass-rumble.wav not found at {bass_path}")

    # Glitch at 6.5s
    glitch_path = AUDIO_DIR / "glitch.mp3"
    if glitch_path.exists():
        glitch = (AudioFileClip(str(glitch_path))
                  .with_start(6.25)
                  .with_volume_scaled(0.5))
        audio_clips.append(glitch)
        print("  + glitch.mp3 (6.5s, 50% vol)")

    # Piano notes for s, u, d, o in "$ sudo elevate-access" (typed at 7 chars/sec from 8s)
    # char indices: $ =0, ' '=1, s=2, u=3, d=4, o=5 → times: 8+idx/7
    piano_notes = ["piano-note-do.mp3", "piano-note-re.mp3",
                   "piano-note-mi.mp3", "piano-note-fa.mp3"]
    piano_times = [8.0, 8.2, 8.4, 8.6]  # s, u, d, o — spaced 0.2s apart, shifted 0.25s early
    for note_file, note_time in zip(piano_notes, piano_times):
        note_path = AUDIO_DIR / note_file
        if note_path.exists():
            note = (AudioFileClip(str(note_path))
                    .with_start(note_time)
                    .with_volume_scaled(0.6))
            audio_clips.append(note)
            print(f"  + {note_file} ({note_time}s)")

    # Epic hit at 14s
    epic_path = AUDIO_DIR / "epic-hit.mp3"
    if epic_path.exists():
        epic = (AudioFileClip(str(epic_path))
                .with_start(13.0)
                .with_volume_scaled(1.0))
        audio_clips.append(epic)
        print("  + epic-hit.mp3 (14s)")

    # Background music (15.5-29.5s, fade in/out, 40% vol)
    bgm_path = AUDIO_DIR / "background-music.mp3"
    if bgm_path.exists():
        bgm_dur = 14.0  # 15.5 to 29.5
        bgm = (AudioFileClip(str(bgm_path))
               .subclipped(0, min(bgm_dur, 30))
               .with_volume_scaled(0.4)
               .with_effects([AudioFadeIn(1.5), AudioFadeOut(1.5)])
               .with_start(15.25))
        audio_clips.append(bgm)
        print(f"  + background-music.mp3 (15.5-29.5s, 40% vol)")

    # Compose audio
    if audio_clips:
        final_audio = CompositeAudioClip(audio_clips)
        video = video.with_audio(final_audio)
        print(f"\n  Audio mixed: {len(audio_clips)} clips")
    else:
        print("\n  Warning: No audio files found, video will be silent")

    # Export
    output_file = OUTPUT_DIR / "sudo_teaser.mp4"
    print(f"\nExporting to {output_file}...")
    video.write_videofile(
        str(output_file),
        fps=FPS,
        codec="libx264",
        audio_codec="aac",
        temp_audiofile=str(OUTPUT_DIR / "temp-audio.m4a"),
        remove_temp=True,
        logger="bar",
    )

    print(f"\nDone! Output: {output_file}")
    print(f"  Duration: {DURATION}s | Resolution: {WIDTH}x{HEIGHT} | FPS: {FPS}")


if __name__ == "__main__":
    main()
