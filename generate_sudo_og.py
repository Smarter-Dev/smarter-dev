#!/usr/bin/env python3
"""
Sudo OG Preview Image Generator

Generates resources/sudo-og-preview.png (1200x630) for social media previews.
Uses the same cyberpunk/terminal aesthetic as the sudo page with Bungee Hairline
and Bruno Ace SC fonts. Tiles email-hex-bg.png as the background.
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).parent.resolve()
RESOURCE_DIR = PROJECT_ROOT / "resources"
FONT_DIR = RESOURCE_DIR / "fonts"

FONT_BUNGEE = str(FONT_DIR / "Bungee Hairline" / "BungeeHairline-Regular.ttf")
FONT_BRUNO = str(FONT_DIR / "Bruno_Ace_SC" / "BrunoAceSC-Regular.ttf")
EMAIL_HEX_BG = RESOURCE_DIR / "email-hex-bg.png"

OUTPUT = RESOURCE_DIR / "sudo-og-preview.png"

# ---------------------------------------------------------------------------
# Dimensions & colors
# ---------------------------------------------------------------------------
W, H = 1200, 630

BG = (2, 4, 8)
CYAN = (0, 212, 255)
WHITE = (212, 224, 236)
AMBER = (234, 179, 8)
MUTED = (128, 152, 176)


def load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        print(f"  Warning: Could not load {path}, using default")
        return ImageFont.load_default()


def tile_background(img: Image.Image) -> None:
    """Tile the email hex background across the full image."""
    tile = Image.open(EMAIL_HEX_BG).convert("RGBA")
    tw, th = tile.size
    for y in range(0, H, th):
        for x in range(0, W, tw):
            img.paste(tile, (x, y), tile)


def draw_circuit_lines(draw: ImageDraw.ImageDraw) -> None:
    """Decorative circuit traces on left and right sides."""
    alpha_color = tuple(
        int(BG[i] + (CYAN[i] - BG[i]) * 0.08) for i in range(3)
    )

    # Left side
    draw.line([(0, 315), (80, 315), (120, 275), (200, 275)], fill=alpha_color, width=1)
    draw.ellipse([197, 272, 203, 278], fill=alpha_color)
    draw.line([(0, 340), (60, 340), (100, 380), (160, 380)], fill=alpha_color, width=1)
    draw.ellipse([157, 377, 163, 383], fill=alpha_color)
    draw.line([(0, 290), (40, 290), (70, 260), (140, 260)], fill=alpha_color, width=1)
    draw.ellipse([137, 257, 143, 263], fill=alpha_color)

    # Right side
    draw.line([(W, 315), (W - 80, 315), (W - 120, 275), (W - 200, 275)], fill=alpha_color, width=1)
    draw.ellipse([W - 203, 272, W - 197, 278], fill=alpha_color)
    draw.line([(W, 340), (W - 60, 340), (W - 100, 380), (W - 200, 380)], fill=alpha_color, width=1)
    draw.ellipse([W - 203, 377, W - 197, 383], fill=alpha_color)
    draw.line([(W, 290), (W - 40, 290), (W - 70, 260), (W - 140, 260)], fill=alpha_color, width=1)
    draw.ellipse([W - 143, 257, W - 137, 263], fill=alpha_color)


def draw_corner_brackets(draw: ImageDraw.ImageDraw) -> None:
    """Subtle corner bracket decorations."""
    bracket_color = tuple(
        int(BG[i] + (CYAN[i] - BG[i]) * 0.1) for i in range(3)
    )
    m = 30  # margin
    l = 30  # bracket arm length

    # Top left
    draw.line([(m, m), (m, m + l)], fill=bracket_color, width=1)
    draw.line([(m, m), (m + l, m)], fill=bracket_color, width=1)
    # Top right
    draw.line([(W - m, m), (W - m, m + l)], fill=bracket_color, width=1)
    draw.line([(W - m, m), (W - m - l, m)], fill=bracket_color, width=1)
    # Bottom left
    draw.line([(m, H - m), (m, H - m - l)], fill=bracket_color, width=1)
    draw.line([(m, H - m), (m + l, H - m)], fill=bracket_color, width=1)
    # Bottom right
    draw.line([(W - m, H - m), (W - m, H - m - l)], fill=bracket_color, width=1)
    draw.line([(W - m, H - m), (W - m - l, H - m)], fill=bracket_color, width=1)


def generate():
    print("Generating sudo OG preview (1200x630)...")

    # ---- Base image with tiled hex background ----
    img = Image.new("RGBA", (W, H), BG + (255,))
    tile_background(img)

    draw = ImageDraw.Draw(img)

    # ---- Circuit lines ----
    draw_circuit_lines(draw)

    # ---- Corner brackets ----
    draw_corner_brackets(draw)

    # ---- Load fonts ----
    font_sudo = load_font(FONT_BUNGEE, 150)
    font_subtitle = load_font(FONT_BRUNO, 22)
    font_terminal = load_font(FONT_BRUNO, 15)

    # ---- Calculate content block dimensions for vertical centering ----
    # Elements: sudo text, accent line (gap), subtitle, gap, terminal box
    sudo_bbox = font_sudo.getbbox("sudo")
    sudo_h = sudo_bbox[3] - sudo_bbox[1]

    subtitle_text = "UNLOCK MORE FROM SMARTER DEV"
    sub_bbox = font_subtitle.getbbox(subtitle_text)
    sub_h = sub_bbox[3] - sub_bbox[1]

    accent_gap = 16       # gap between sudo bottom and accent line
    accent_to_sub = 22    # gap between accent line and subtitle
    sub_to_terminal = 30  # gap between subtitle and terminal box
    terminal_h = 48       # terminal box height

    total_content_h = (
        sudo_h
        + accent_gap + 1  # accent line
        + accent_to_sub + sub_h
        + sub_to_terminal + terminal_h
    )

    # Center the block vertically
    content_top = (H - total_content_h) // 2

    # ---- "sudo" text with cyan glow ----
    sudo_y = content_top - sudo_bbox[1]  # offset for font metrics
    sudo_w = sudo_bbox[2] - sudo_bbox[0]
    sudo_x = (W - sudo_w) // 2 - sudo_bbox[0]

    # Glow layers
    glow_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow_layer)
    glow_draw.text((sudo_x, sudo_y), "sudo", font=font_sudo, fill=CYAN + (80,))
    for radius in [40, 20, 8]:
        blurred = glow_layer.filter(ImageFilter.GaussianBlur(radius=radius))
        img = Image.alpha_composite(img, blurred)

    # Sharp text on top
    sharp_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sharp_draw = ImageDraw.Draw(sharp_layer)
    sharp_draw.text((sudo_x, sudo_y), "sudo", font=font_sudo, fill=CYAN + (255,))
    img = Image.alpha_composite(img, sharp_layer)

    # ---- Accent line ----
    accent_y = content_top + sudo_h + accent_gap
    # Gradient line: fade in from edges, brightest at center
    line_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    line_draw = ImageDraw.Draw(line_layer)
    line_x1, line_x2 = 200, W - 200
    line_w = line_x2 - line_x1
    for x in range(line_x1, line_x2):
        t = (x - line_x1) / line_w  # 0..1
        # Bell curve: peak at 0.5
        alpha = int(230 * (1 - abs(t - 0.5) * 2) ** 0.5)
        line_draw.line([(x, accent_y), (x + 1, accent_y)], fill=CYAN + (alpha,))
    img = Image.alpha_composite(img, line_layer)

    # ---- Subtitle ----
    sub_y_top = accent_y + 1 + accent_to_sub
    sub_w = sub_bbox[2] - sub_bbox[0]
    sub_x = (W - sub_w) // 2 - sub_bbox[0]
    sub_y = sub_y_top - sub_bbox[1]

    sub_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sub_draw = ImageDraw.Draw(sub_layer)
    sub_draw.text((sub_x, sub_y), subtitle_text, font=font_subtitle, fill=WHITE + (220,))
    img = Image.alpha_composite(img, sub_layer)

    # ---- Terminal box (no traffic light dots) ----
    term_y_top = sub_y_top + sub_h + sub_to_terminal
    term_w = 480
    term_x = (W - term_w) // 2
    term_r = 6  # corner radius

    # Terminal background
    term_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    term_draw = ImageDraw.Draw(term_layer)
    term_draw.rounded_rectangle(
        [term_x, term_y_top, term_x + term_w, term_y_top + terminal_h],
        radius=term_r,
        fill=(8, 12, 20, 255),
        outline=CYAN + (40,),
        width=1,
    )

    # Terminal text: "$ sudo smarter-dev --unlock"
    text_left = term_x + 20
    text_y = term_y_top + (terminal_h - 15) // 2

    # "$ "
    term_draw.text((text_left, text_y), "$", font=font_terminal, fill=AMBER + (230,))
    dollar_w = font_terminal.getbbox("$ ")[2]

    # "sudo"
    term_draw.text(
        (text_left + dollar_w, text_y),
        "sudo",
        font=font_terminal,
        fill=CYAN + (255,),
    )
    sudo_cmd_w = font_terminal.getbbox("sudo ")[2]

    # "smarter-dev --unlock"
    term_draw.text(
        (text_left + dollar_w + sudo_cmd_w, text_y),
        "smarter-dev --unlock",
        font=font_terminal,
        fill=MUTED + (200,),
    )

    # Blinking cursor
    rest_w = font_terminal.getbbox("smarter-dev --unlock ")[2]
    cursor_x = text_left + dollar_w + sudo_cmd_w + rest_w
    cursor_h = 16
    cursor_y = text_y + 1
    term_draw.rectangle(
        [cursor_x, cursor_y, cursor_x + 8, cursor_y + cursor_h],
        fill=CYAN + (200,),
    )

    img = Image.alpha_composite(img, term_layer)

    # ---- Save ----
    final = img.convert("RGB")
    final.save(OUTPUT, "PNG")
    print(f"  Saved: {OUTPUT}")
    print(f"  Size: {OUTPUT.stat().st_size / 1024:.1f} KB")


if __name__ == "__main__":
    generate()
