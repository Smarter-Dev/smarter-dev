"""Shape image bytes into what the OpenAI image reader accepts.

Images go to GPT-6 Luna (OpenAI Responses), which takes PNG, JPEG, WebP and
GIF. Checked against the live API on 2026-09-24:

- BMP is refused outright (400 "does not represent a valid image"), so it and
  any other type Pillow can open is re-encoded as PNG.
- An animated GIF is accepted but only its first frame is read — by Gemini 3.8
  Flash as well — so a reaction GIF or screen recording would be described from
  one still. It goes as up to ``MAX_GIF_FRAMES`` evenly spaced PNG frames
  instead, with a note saying which frames they are. Motion between the chosen
  frames is still unseen, and frame timing is not conveyed.

Used by both the bot's and the web app's media readers; nothing here calls a
model.
"""

from __future__ import annotations

import io
import logging

from PIL import Image
from PIL import UnidentifiedImageError

logger = logging.getLogger(__name__)

OPENAI_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})
MAX_GIF_FRAMES = 4


def prepare_image(data: bytes, media_type: str) -> tuple[list[tuple[bytes, str]], str]:
    """Return the ``(bytes, media_type)`` parts to send and a note for the prompt.

    The note is empty unless the parts need explaining (sampled GIF frames).
    Bytes Pillow cannot read are passed through unchanged, so the model's own
    refusal still surfaces as it did before.
    """
    if media_type == "image/gif":
        frames = _sampled_gif_frames(data)
        if frames is None:
            return [(data, media_type)], ""
        shown, total = frames
        note = (
            f"This is an animated GIF of {total} frames. {len(shown)} evenly "
            "spaced frames are attached in playback order (frames "
            f"{', '.join(str(i + 1) for i, _ in shown)}); motion between them "
            "is not visible."
        )
        return [(png, "image/png") for _, png in shown], note
    if media_type in OPENAI_IMAGE_TYPES:
        return [(data, media_type)], ""
    try:
        with Image.open(io.BytesIO(data)) as image:
            return [(_png(image), "image/png")], ""
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        logger.info("could not re-encode %s image; sending it as is", media_type)
        return [(data, media_type)], ""


def _sampled_gif_frames(data: bytes) -> tuple[list[tuple[int, bytes]], int] | None:
    """Evenly spaced frames of an animated GIF as PNGs, or None if it is still."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            total = getattr(image, "n_frames", 1)
            if total < 2:
                return None
            count = min(MAX_GIF_FRAMES, total)
            # First and last always included; the rest spread between them.
            indices = sorted({round(i * (total - 1) / (count - 1)) for i in range(count)})
            frames = []
            for index in indices:
                image.seek(index)
                frames.append((index, _png(image)))
            return frames, total
    except (UnidentifiedImageError, OSError, ValueError, EOFError, Image.DecompressionBombError):
        return None


def _png(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.convert("RGBA").save(out, "PNG")
    return out.getvalue()
