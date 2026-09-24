"""Shape image bytes into what the OpenAI image reader accepts.

Images go to GPT-6 Luna (OpenAI Responses), which takes PNG, JPEG, WebP and
GIF. Checked against the live API on 2026-09-24:

- BMP is refused outright (400 "does not represent a valid image"), so it is
  re-encoded as PNG.
- An animated GIF is accepted but only its first frame is read — by Gemini 3.8
  Flash as well — so a reaction GIF or screen recording would be described from
  one still. It goes as up to ``MAX_GIF_FRAMES`` evenly spaced PNG frames
  instead, with a note saying which frames they are. Motion between the chosen
  frames is still unseen, and frame timing is not conveyed.
- Animated WebP and APNG are passed through untouched: the model reads a single
  frame of them, as it did before.

Used by both the bot's and the web app's media readers; nothing here calls a
model. It runs inside the bot's container, which idles ~70 MiB under its 512Mi
limit, so memory is bounded before any pixel data is decoded:

1. The header alone decides. An image over ``MAX_DECODE_PIXELS``, or a GIF
   whose canvas is once grown to fit every frame's position and size, is
   never decoded. A GIF is also refused over
   ``MAX_GIF_BYTES``, over ``MAX_GIF_COUNT`` frames, or when every frame up to
   the last sampled one (``seek`` decodes them in order) would pass
   ``MAX_GIF_DECODE_PIXELS``. Frames are counted from the GIF's block
   structure; no pixel data is read to count them.
2. A refused GIF goes as it arrived, and the model reads its first frame as it
   always did. A refused BMP cannot (the model refuses BMP), so it raises
   ``ImageTooLarge`` and the reader answers with that message.
3. Only BMP and GIF ever reach a Pillow decoder (``formats=``), one decode at
   a time per process (``prepare_image_bounded``). That serializes only the
   decode: the input, the PNG parts and the request copies stay held through
   each model call, so concurrent reads still add their retained bytes (#25).

Measured peaks are in PR #99 and ``tests/bot/agents/test_media_image_formats``.
"""

from __future__ import annotations

import asyncio
import io
import logging
import struct

from PIL import Image

logger = logging.getLogger(__name__)

OPENAI_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})
MAX_GIF_FRAMES = 4
# 1000 x 1000. Measured with the 20 MB input and the request body held, the
# worst conversion at the caps grows peak memory by 38 MiB (PR #99).
MAX_DECODE_PIXELS = 1_000_000
MAX_GIF_BYTES = 8 * 1024 * 1024
MAX_GIF_COUNT = 500
MAX_GIF_DECODE_PIXELS = 50_000_000
# OpenAI first scales an image to fit 2048 x 2048; larger adds only bytes.
MAX_SIDE = 2048

# The one type the readers receive that the model refuses (the attachment
# sniffer and URL extensions admit PNG, JPEG, GIF, WebP and BMP).
_CONVERTIBLE_FORMATS = {"image/bmp": "BMP"}

# One decode at a time per process, shared by every caller in it, so two
# images arriving together cannot add their decode peaks. What each read keeps
# for its model call (input, PNG parts, request body) is not bounded here.
_conversions = asyncio.Semaphore(1)


class ImageTooLarge(ValueError):
    """An image that would have to be decoded to be read, and is too big to."""


async def prepare_image_bounded(
    data: bytes, media_type: str
) -> tuple[list[tuple[bytes, str]], str]:
    """``prepare_image`` in a worker thread, one conversion at a time."""
    if not _needs_work(media_type):
        return [(data, media_type)], ""
    async with _conversions:
        return await asyncio.to_thread(prepare_image, data, media_type)


def prepare_image(data: bytes, media_type: str) -> tuple[list[tuple[bytes, str]], str]:
    """Return the ``(bytes, media_type)`` parts to send and a note for the prompt.

    The note is empty unless the parts need explaining (sampled GIF frames).
    Bytes Pillow cannot read are passed through unchanged, so the model's own
    refusal still surfaces as it did before. Raises ``ImageTooLarge`` for a BMP
    over the pixel cap, whose message is fit to show as the read's result.
    """
    if not _needs_work(media_type):
        return [(data, media_type)], ""
    if media_type == "image/gif":
        return _prepare_gif(data)
    try:
        with Image.open(io.BytesIO(data), formats=[_CONVERTIBLE_FORMATS[media_type]]) as image:
            width, height = image.size
            if width * height > MAX_DECODE_PIXELS:
                raise ImageTooLarge(
                    f"The image is {width}x{height} pixels, too large to read "
                    f"(the limit is {MAX_DECODE_PIXELS:,} pixels)."
                )
            return [(_png(image), "image/png")], ""
    except ImageTooLarge:
        raise
    except Exception:  # noqa: BLE001 — undecodable: let the model refuse it
        logger.info("could not re-encode %s image; sending it as is", media_type)
        return [(data, media_type)], ""


def _needs_work(media_type: str) -> bool:
    return media_type == "image/gif" or media_type in _CONVERTIBLE_FORMATS


def _prepare_gif(data: bytes) -> tuple[list[tuple[bytes, str]], str]:
    as_is = [(data, "image/gif")], ""
    if len(data) > MAX_GIF_BYTES:
        return as_is
    scan = scan_gif(data)
    if scan is None:
        return as_is
    total, canvas = scan
    # The last frame is always sampled, and seek() decodes every frame before
    # it onto the (grown) canvas, so all ``total`` frames are decoded at that
    # size.
    if total < 2 or canvas > MAX_DECODE_PIXELS or total * canvas > MAX_GIF_DECODE_PIXELS:
        return as_is
    try:
        with Image.open(io.BytesIO(data), formats=["GIF"]) as image:
            count = min(MAX_GIF_FRAMES, total)
            # First and last always included; the rest spread between them.
            indices = sorted({round(i * (total - 1) / (count - 1)) for i in range(count)})
            shown = []
            for index in indices:
                image.seek(index)
                shown.append((index, _png(image)))
    except Exception:  # noqa: BLE001 — undecodable: send it as it arrived
        return as_is
    note = (
        f"This is an animated GIF of {total} frames. {len(shown)} evenly "
        "spaced frames are attached in playback order (frames "
        f"{', '.join(str(i + 1) for i, _ in shown)}); motion between them "
        "is not visible."
    )
    return [(png, "image/png") for _, png in shown], note


def scan_gif(data: bytes) -> tuple[int, int] | None:
    """``(frames, canvas in pixels)`` from a GIF's blocks, decoding none.

    The canvas is the one Pillow will decode into: the logical screen, grown to
    reach past any frame placed beyond it (``GifImagePlugin`` enlarges it to
    ``x0 + width`` by ``y0 + height`` while seeking, and only warns). Returns
    None for anything that is not a well-formed GIF up to its trailer, and for
    one of more than ``MAX_GIF_COUNT`` frames (counting stops there).
    """
    if len(data) < 13 or data[:6] not in (b"GIF87a", b"GIF89a"):
        return None
    canvas_w = data[6] | data[7] << 8
    canvas_h = data[8] | data[9] << 8
    pos = 13
    if data[10] & 0x80:  # global colour table
        pos += 3 << ((data[10] & 0x07) + 1)
    frames = 0
    try:
        while True:
            block = data[pos]
            if block == 0x3B:  # trailer
                return frames, canvas_w * canvas_h
            if block == 0x21:  # extension: label, then sub-blocks
                pos += 2
            elif block == 0x2C:  # image descriptor, then optional local table
                frames += 1
                if frames > MAX_GIF_COUNT:
                    return None
                x0, y0, width, height = struct.unpack_from("<HHHH", data, pos + 1)
                canvas_w = max(canvas_w, x0 + width)
                canvas_h = max(canvas_h, y0 + height)
                flags = data[pos + 9]
                pos += 10
                if flags & 0x80:
                    pos += 3 << ((flags & 0x07) + 1)
                pos += 1  # LZW minimum code size
            else:
                return None
            while (size := data[pos]) != 0:  # sub-blocks up to the terminator
                pos += size + 1
            pos += 1
    except (IndexError, struct.error):
        return None


def _png(image: Image.Image) -> bytes:
    has_alpha = image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info
    frame = image.convert("RGBA" if has_alpha else "RGB")
    frame.thumbnail((MAX_SIDE, MAX_SIDE))
    out = io.BytesIO()
    frame.save(out, "PNG")
    return out.getvalue()
