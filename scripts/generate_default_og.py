#!/usr/bin/env python3
"""Default site OG preview image generator.

Produces ``static/og-default.png`` (1200x630), served at
``/static/site/og-default.png``. Used as the social-share image fallback for
any page that does not set its own ``og_meta.image``.

Adapted from ``generate_sudo_og.py``: same cyberpunk/terminal aesthetic
(tiled hex background, circuit traces, corner brackets, cyan glow) so the
default share card stays visually consistent with the per-page ones.

Run from the repo root:
    .venv/bin/python scripts/generate_default_og.py
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESOURCE_DIR = PROJECT_ROOT / "resources"
FONT_DIR = RESOURCE_DIR / "fonts"

FONT_BUNGEE = str(FONT_DIR / "Bungee Hairline" / "BungeeHairline-Regular.ttf")
FONT_BRUNO = str(FONT_DIR / "Bruno_Ace_SC" / "BrunoAceSC-Regular.ttf")
EMAIL_HEX_BG = RESOURCE_DIR / "email-hex-bg.png"

OUTPUT = PROJECT_ROOT / "static" / "og-default.png"

# ---------------------------------------------------------------------------
# Dimensions & colors
# ---------------------------------------------------------------------------
W, H = 1200, 630

BG = (2, 4, 8)
CYAN = (0, 212, 255)
WHITE = (212, 224, 236)
AMBER = (234, 179, 8)
MUTED = (128, 152, 176)

# Wordmark + subtitle. The wordmark mirrors the masthead's "SMARTER.dev"
# split so the cyan accent lands on .DEV.
WORDMARK_LEFT = "SMARTER"
WORDMARK_RIGHT = ".DEV"
SUBTITLE = "BECOME THE TRUST LAYER"
TERMINAL_PROMPT = "$ smarter-dev --start"


def load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        print(f"  Warning: Could not load {path}, using default")
        return ImageFont.load_default()


def tile_background(img: Image.Image, scale: float = 2.6) -> None:
    """Tile the hex background, scaled up so it reads at preview size.

    Social cards display the OG image at ~500-600px wide; at the source
    tile's native size the hex pattern compresses into noise. Scaling the
    tile up (2-3x) keeps the cell count low enough to stay legible when
    the image is shown small.
    """
    tile = Image.open(EMAIL_HEX_BG).convert("RGBA")
    tw, th = tile.size
    tile = tile.resize((int(tw * scale), int(th * scale)), Image.Resampling.LANCZOS)
    tw, th = tile.size
    for y in range(0, H, th):
        for x in range(0, W, tw):
            img.paste(tile, (x, y), tile)


def draw_circuit_lines(draw: ImageDraw.ImageDraw) -> None:
    alpha_color = tuple(int(BG[i] + (CYAN[i] - BG[i]) * 0.08) for i in range(3))
    # Left
    draw.line([(0, 315), (80, 315), (120, 275), (200, 275)], fill=alpha_color, width=1)
    draw.ellipse([197, 272, 203, 278], fill=alpha_color)
    draw.line([(0, 340), (60, 340), (100, 380), (160, 380)], fill=alpha_color, width=1)
    draw.ellipse([157, 377, 163, 383], fill=alpha_color)
    draw.line([(0, 290), (40, 290), (70, 260), (140, 260)], fill=alpha_color, width=1)
    draw.ellipse([137, 257, 143, 263], fill=alpha_color)
    # Right
    draw.line([(W, 315), (W - 80, 315), (W - 120, 275), (W - 200, 275)], fill=alpha_color, width=1)
    draw.ellipse([W - 203, 272, W - 197, 278], fill=alpha_color)
    draw.line([(W, 340), (W - 60, 340), (W - 100, 380), (W - 200, 380)], fill=alpha_color, width=1)
    draw.ellipse([W - 203, 377, W - 197, 383], fill=alpha_color)
    draw.line([(W, 290), (W - 40, 290), (W - 70, 260), (W - 140, 260)], fill=alpha_color, width=1)
    draw.ellipse([W - 143, 257, W - 137, 263], fill=alpha_color)


def draw_corner_brackets(draw: ImageDraw.ImageDraw) -> None:
    bracket_color = tuple(int(BG[i] + (CYAN[i] - BG[i]) * 0.1) for i in range(3))
    m, l = 30, 30
    for cx, cy, sx, sy in [
        (m, m, 1, 1),
        (W - m, m, -1, 1),
        (m, H - m, 1, -1),
        (W - m, H - m, -1, -1),
    ]:
        draw.line([(cx, cy), (cx, cy + l * sy)], fill=bracket_color, width=1)
        draw.line([(cx, cy), (cx + l * sx, cy)], fill=bracket_color, width=1)


def generate():
    print("Generating default OG preview (1200x630)...")

    img = Image.new("RGBA", (W, H), BG + (255,))
    tile_background(img)
    draw = ImageDraw.Draw(img)
    draw_circuit_lines(draw)
    draw_corner_brackets(draw)

    font_wordmark = load_font(FONT_BUNGEE, 130)
    font_subtitle = load_font(FONT_BRUNO, 24)
    font_terminal = load_font(FONT_BRUNO, 15)

    # Wordmark sizing — measure the two halves so we can flow them together.
    wl_bbox = font_wordmark.getbbox(WORDMARK_LEFT)
    wr_bbox = font_wordmark.getbbox(WORDMARK_RIGHT)
    wl_w = wl_bbox[2] - wl_bbox[0]
    wr_w = wr_bbox[2] - wr_bbox[0]
    word_h = max(wl_bbox[3] - wl_bbox[1], wr_bbox[3] - wr_bbox[1])
    word_total_w = wl_w + wr_w

    # Layout: wordmark, accent line, subtitle, terminal box.
    accent_gap = 18
    accent_to_sub = 24
    sub_to_terminal = 30
    terminal_h = 48
    sub_bbox = font_subtitle.getbbox(SUBTITLE)
    sub_h = sub_bbox[3] - sub_bbox[1]
    total_content_h = (
        word_h + accent_gap + 1 + accent_to_sub + sub_h + sub_to_terminal + terminal_h
    )
    content_top = (H - total_content_h) // 2

    # Wordmark — SMARTER (white) + .DEV (cyan), with cyan glow on both.
    word_x = (W - word_total_w) // 2
    word_y = content_top - min(wl_bbox[1], wr_bbox[1])

    glow_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow_layer)
    glow_draw.text((word_x - wl_bbox[0], word_y), WORDMARK_LEFT, font=font_wordmark, fill=CYAN + (45,))
    glow_draw.text((word_x + wl_w - wr_bbox[0], word_y), WORDMARK_RIGHT, font=font_wordmark, fill=CYAN + (100,))
    for radius in [40, 20, 8]:
        img = Image.alpha_composite(img, glow_layer.filter(ImageFilter.GaussianBlur(radius=radius)))

    sharp_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sharp_draw = ImageDraw.Draw(sharp_layer)
    sharp_draw.text((word_x - wl_bbox[0], word_y), WORDMARK_LEFT, font=font_wordmark, fill=WHITE + (255,))
    sharp_draw.text((word_x + wl_w - wr_bbox[0], word_y), WORDMARK_RIGHT, font=font_wordmark, fill=CYAN + (255,))
    img = Image.alpha_composite(img, sharp_layer)

    # Accent line — bell-curve fade across the middle.
    accent_y = content_top + word_h + accent_gap
    line_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    line_draw = ImageDraw.Draw(line_layer)
    line_x1, line_x2 = 240, W - 240
    line_w = line_x2 - line_x1
    for x in range(line_x1, line_x2):
        t = (x - line_x1) / line_w
        alpha = int(230 * (1 - abs(t - 0.5) * 2) ** 0.5)
        line_draw.line([(x, accent_y), (x + 1, accent_y)], fill=CYAN + (alpha,))
    img = Image.alpha_composite(img, line_layer)

    # Subtitle
    sub_y_top = accent_y + 1 + accent_to_sub
    sub_w = sub_bbox[2] - sub_bbox[0]
    sub_x = (W - sub_w) // 2 - sub_bbox[0]
    sub_y = sub_y_top - sub_bbox[1]
    sub_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(sub_layer).text((sub_x, sub_y), SUBTITLE, font=font_subtitle, fill=WHITE + (230,))
    img = Image.alpha_composite(img, sub_layer)

    # Terminal box
    term_y_top = sub_y_top + sub_h + sub_to_terminal
    term_w = 480
    term_x = (W - term_w) // 2
    term_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    term_draw = ImageDraw.Draw(term_layer)
    term_draw.rounded_rectangle(
        [term_x, term_y_top, term_x + term_w, term_y_top + terminal_h],
        radius=6, fill=(8, 12, 20, 255), outline=CYAN + (40,), width=1,
    )
    text_left = term_x + 20
    text_y = term_y_top + (terminal_h - 15) // 2
    term_draw.text((text_left, text_y), "$", font=font_terminal, fill=AMBER + (230,))
    dollar_w = font_terminal.getbbox("$ ")[2]
    cmd_rest = TERMINAL_PROMPT[2:]  # drop "$ "
    term_draw.text(
        (text_left + dollar_w, text_y), cmd_rest, font=font_terminal, fill=MUTED + (210,),
    )
    # Blinking cursor block
    cursor_x = text_left + dollar_w + font_terminal.getbbox(cmd_rest + " ")[2]
    term_draw.rectangle(
        [cursor_x, text_y + 1, cursor_x + 8, text_y + 17], fill=CYAN + (200,),
    )
    img = Image.alpha_composite(img, term_layer)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(OUTPUT, "PNG")
    print(f"  Saved: {OUTPUT}")
    print(f"  Size:  {OUTPUT.stat().st_size / 1024:.1f} KB")


if __name__ == "__main__":
    generate()
