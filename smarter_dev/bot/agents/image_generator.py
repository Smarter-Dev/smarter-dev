"""Generate a single image with Gemini's image model (google-genai).

The chat agent's ``generate_image`` tool hands an approved, technical prompt
here; we call ``gemini-3.1-flash-lite-image`` and return the raw image bytes so
the engine can attach them to the reply. We talk to google-genai directly (not
pydantic-ai) because we want the image bytes, not a text/tool-call response.
"""

from __future__ import annotations

import logging
import os

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-3.1-flash-lite-image"
MODEL_ENV_VAR = "IMAGE_GENERATOR_MODEL"


class ImageGenerationError(RuntimeError):
    """Raised when the model returns no usable image."""


# Fixed neon palette every generated image must use. The dark base + cyan and
# the green/yellow/red accents are pulled straight from the site theme
# (themes/smarterdev/static/css/framework.css: --bg, --surface, --cyan, --green,
# --amber, --rose, --white/--light). Blue, orange, and magenta aren't defined as
# neons in the theme, so they're gap-filled with saturated neons chosen to sit
# alongside the cyan on the near-black background. Baking this in (rather than
# telling the model to "match the website") keeps every diagram on-brand.
PALETTE: dict[str, str] = {
    "background": "#020408",   # --bg
    "card": "#0D141E",         # elevated panel/card surface
    "text": "#D4E0EC",         # --white (primary text/labels)
    "muted_text": "#8098B0",   # --light (secondary text)
    "cyan": "#00D4FF",         # --cyan (primary accent)
    "blue": "#2B8BFF",         # neon gap-fill
    "green": "#22C55E",        # --green
    "yellow": "#EAB308",       # --amber
    "orange": "#FF8A3D",       # neon gap-fill
    "red": "#EF4444",          # --rose
    "magenta": "#FF2EC4",      # neon gap-fill
    "purple": "#8B5CF6",       # theme accent (used in charts)
}

STYLE_PREAMBLE = (
    "Render the following as a clean technical diagram on a dark background, "
    "using ONLY this fixed color palette (do not introduce any other colors):\n"
    f"- Background: {PALETTE['background']} (near-black)\n"
    f"- Panels / cards / boxes: {PALETTE['card']}\n"
    f"- Primary text and labels: {PALETTE['text']}; "
    f"secondary/muted text: {PALETTE['muted_text']}\n"
    "- Accent colors (neon, glowing on the dark background — use for strokes, "
    "shapes, arrows, and highlights):\n"
    f"    cyan {PALETTE['cyan']} (primary — prefer it), blue {PALETTE['blue']}, "
    f"green {PALETTE['green']}, yellow {PALETTE['yellow']}, "
    f"orange {PALETTE['orange']}, red {PALETTE['red']}, "
    f"magenta {PALETTE['magenta']}, purple {PALETTE['purple']}.\n"
    "Aesthetic: an engineering schematic / terminal-HUD look, cyan-forward. "
    "Build shapes from STRAIGHT LINES and RIGHT ANGLES — square-cornered boxes "
    "(never rounded corners), orthogonal connectors that bend at 90 degrees, "
    "and angular arrowheads. Avoid decorative curves, blobs, and organic "
    "shapes; only draw a curve when the concept itself is a curve (a plotted "
    "function, or a circle/arc in a geometry figure). Use thin 1-2px neon "
    "strokes with a subtle outer glow on the flat near-black background, and "
    "monospaced, slightly letter-spaced technical labels in the text color, "
    "aligned to an implicit grid with generous spacing. Keep it flat and "
    "minimal: no photorealism, no 3D or isometric extrusion, no drop shadows "
    "or gradients beyond the neon glow, no background scenery, no watermarks, "
    "and no colors outside this palette.\n"
    "IMPORTANT: never draw, print, or label any of these hex codes, color "
    "names, or these style instructions inside the image — the ONLY text in "
    "the image is the diagram's own content described below.\n\n"
    "Diagram to draw:\n"
)


def apply_palette(prompt: str) -> str:
    """Prefix a diagram prompt with the fixed brand-palette style instructions."""
    return f"{STYLE_PREAMBLE}{prompt}"


_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
        _client = genai.Client(api_key=api_key)
    return _client


async def generate_image(prompt: str) -> tuple[bytes, str]:
    """Generate an image for ``prompt``; return ``(data, mime_type)``.

    Raises :class:`ImageGenerationError` when the model responds without inline
    image data (e.g. it refused, or returned text only).
    """
    model_id = os.getenv(MODEL_ENV_VAR, DEFAULT_MODEL)
    logger.info("generate_image: model=%s prompt=%r", model_id, prompt)
    response = await _get_client().aio.models.generate_content(
        model=model_id,
        contents=apply_palette(prompt),
        config=types.GenerateContentConfig(response_modalities=["IMAGE"]),
    )
    for candidate in response.candidates or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            inline = getattr(part, "inline_data", None)
            if inline is not None and inline.data:
                return inline.data, (inline.mime_type or "image/png")
    raise ImageGenerationError("model returned no image data")
