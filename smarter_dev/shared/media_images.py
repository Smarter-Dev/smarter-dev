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
model. It runs inside the bot's container, which idles ~180 MiB under its 612Mi
limit (425Mi in use, one sample on 2026-09-25), so memory is bounded before any
pixel data is decoded:

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

Images the bot's own process should not decode go to a bounded child
instead of being refused (#25): one over ``MAX_DOWNLOAD_BYTES`` (10 MiB),
which arrives on disk as a ``SpooledImage``, and one over the in-process caps
above (``NeedsIsolation``: a 4K PNG screenshot, a large progressive JPEG, a
BMP or GIF canvas over ``MAX_DECODE_PIXELS``). ``image_child`` decodes it from
the file with its address space capped ``IMAGE_CHILD_EXTRA_BYTES`` over its
imports, after the same kind of header checks, so a small compressed bomb is
still refused unread.

Measured peaks are in PRs #99 and #101 and ``tests/bot/agents``.
"""

from __future__ import annotations

import asyncio
import io
import logging
import struct

from PIL import Image

from smarter_dev.shared import bounded_child
from smarter_dev.shared.media_reads import MAX_SEND_BYTES
from smarter_dev.shared.media_reads import SpooledImage
from smarter_dev.shared.media_reads import spool_image
from smarter_dev.shared.media_reads import too_large_to_send

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
# A JPEG over the send cap is decoded at 1/2 to 1/8 scale (``draft``), so its
# header may be this large and still decode near 2048 px.
MAX_JPEG_DRAFT_PIXELS = 64_000_000
# A PNG over the send cap is decoded whole to be scaled down: ~16 MB
# as RGBA at this size.
MAX_DOWNSCALE_PIXELS = 4_096_000
# WebP is not among them: Pillow decodes it at ~17 bytes a pixel, so one over
# the send cap always goes to ``image_child``.
_DOWNSCALABLE_FORMATS = {"image/png": "PNG"}
_MODEL_SHORT_SIDE = 768
# What a drafted JPEG may decode to: ~12 MB as RGB.
_MAX_DRAFTED_PIXELS = 4_000_000
# A progressive JPEG, or one whose first scan lacks a component, makes libjpeg
# hold every DCT coefficient of the whole image at once (2 bytes each, at full
# size whatever the draft scale): an 8000 x 8000 one grew a read by 199 MiB
# (measured 2026-09-25). Such a JPEG is refused over this buffer size, which
# admits a 7 MP 4:2:0 photo.
MAX_JPEG_BUFFERED_BYTES = 20 * 1024 * 1024
# What the bounded child (``image_child``) may decode, checked from the header.
# Whole, for PNG, BMP and a GIF's first frame: 4096 x 4096 (a 5K screenshot
# fits, 8K does not). WebP costs ~17 bytes a pixel to decode through Pillow
# (measured 2026-09-25: +200 MiB for 12 MP), so less of it. A JPEG's
# coefficient buffer: a 16 MP 4:2:0 progressive photo.
MAX_CHILD_DECODE_PIXELS = 4096 * 4096
MAX_CHILD_WEBP_PIXELS = 5_000_000
MAX_CHILD_JPEG_BUFFERED_BYTES = 48 * 1024 * 1024
# The child's address space over its size after importing Pillow (~23 MiB
# resident), so the child stays under ~120 MiB. Measured decodes within the
# caps: RGB PNG 4096 x 4096 +70 MiB, 16 MP progressive JPEG +52, 48 MP
# baseline JPEG +25. Anything needing more fails in the child: "too large".
IMAGE_CHILD_EXTRA_BYTES = 96 * 1024 * 1024
# Modes Pillow can reduce(); others (palette, 1-bit, 16-bit) convert first.
REDUCIBLE_MODES = frozenset({"L", "LA", "RGB", "RGBA", "CMYK", "PA"})
CHILD_TIMEOUT_SECONDS = 60.0
# Start-of-frame markers; the rest of 0xC0..0xCF are tables (DHT, JPG, DAC).
_JPEG_SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
# Frames libjpeg can decode one MCU row at a time: baseline, extended
# sequential and arithmetic sequential, when the first scan has every component.
_JPEG_SEQUENTIAL_SOF = frozenset({0xC0, 0xC1, 0xC9})

# The one type the readers receive that the model refuses (the attachment
# sniffer and URL extensions admit PNG, JPEG, GIF, WebP and BMP).
_CONVERTIBLE_FORMATS = {"image/bmp": "BMP"}

# One decode at a time per process, shared by every caller in it, so two
# images arriving together cannot add their decode peaks. What each read keeps
# for its model call (input, PNG parts, request body) is not bounded here.
_conversions = asyncio.Semaphore(1)


class ImageTooLarge(ValueError):
    """An image that would have to be decoded to be read, and is too big to."""


class NeedsIsolation(ImageTooLarge):
    """Too big to decode in this process; ``image_child`` may still read it."""


async def prepare_image_bounded(
    data: bytes | SpooledImage, media_type: str
) -> tuple[list[tuple[bytes, str]], str]:
    """``prepare_image`` in a worker thread, one conversion at a time.

    An image on disk, or one ``prepare_image`` finds too big to decode here,
    is downsampled in ``image_child`` instead. A ``SpooledImage`` is deleted
    before this returns or raises.
    """
    if isinstance(data, SpooledImage):
        try:
            async with _conversions:
                return await _prepare_in_child(data, media_type)
        finally:
            data.discard()
    if not _needs_work(data, media_type):
        return [(data, media_type)], ""
    async with _conversions:
        try:
            return await asyncio.to_thread(prepare_image, data, media_type)
        except NeedsIsolation:
            pass
        # The caller still holds ``data`` (at most MAX_DOWNLOAD_BYTES) while
        # the child runs.
        spooled = spool_image(data)
        try:
            return await _prepare_in_child(spooled, media_type)
        finally:
            spooled.discard()


async def _prepare_in_child(
    image: SpooledImage, media_type: str
) -> tuple[list[tuple[bytes, str]], str]:
    try:
        code, out = await bounded_child.run(
            "smarter_dev.shared.image_child",
            image.path,
            media_type,
            str(IMAGE_CHILD_EXTRA_BYTES),
            timeout=CHILD_TIMEOUT_SECONDS,
        )
    except bounded_child.ChildTimedOut as timed_out:
        raise ImageTooLarge("The image took too long to read.") from timed_out
    if code == 2:
        raise ImageTooLarge(out.decode("utf-8", "replace"))
    if code != 0:
        raise ImageTooLarge(
            f"The image is {image.size / 1_048_576:.1f} MB, too large to decode "
            "within the memory limit."
        )
    sent_type, note, data = out.split(b"\n", 2)
    if len(data) > MAX_SEND_BYTES:
        raise ImageTooLarge(too_large_to_send(len(data), "image"))
    return [(data, sent_type.decode())], note.decode()


def prepare_image(data: bytes, media_type: str) -> tuple[list[tuple[bytes, str]], str]:
    """Return the ``(bytes, media_type)`` parts to send and a note for the prompt.

    The note is empty unless the parts need explaining (sampled GIF frames).
    Bytes Pillow cannot read are passed through unchanged, so the model's own
    refusal still surfaces as it did before. Raises ``ImageTooLarge`` for an
    image that cannot be sent within the caps, with a message fit to show as
    the read's result. The parts never add up to more than ``MAX_SEND_BYTES``.
    """
    if not _needs_work(data, media_type):
        return [(data, media_type)], ""
    if media_type == "image/gif":
        parts, note = _prepare_gif(data)
    elif media_type in _CONVERTIBLE_FORMATS:
        parts, note = _prepare_convertible(data, media_type)
    elif media_type == "image/jpeg":
        parts, note = [(_downscaled_jpeg(data), "image/jpeg")], ""
    elif media_type in _DOWNSCALABLE_FORMATS:
        parts, note = [_downscaled(data, _DOWNSCALABLE_FORMATS[media_type])], ""
    elif media_type == "image/webp":
        raise NeedsIsolation(too_large_to_send(len(data), "image"))
    else:
        raise ImageTooLarge(too_large_to_send(len(data), "image"))
    size = sum(len(part) for part, _ in parts)
    if size > MAX_SEND_BYTES:
        raise NeedsIsolation(too_large_to_send(size, "image"))
    return parts, note


def _prepare_convertible(data: bytes, media_type: str) -> tuple[list[tuple[bytes, str]], str]:
    try:
        with Image.open(io.BytesIO(data), formats=[_CONVERTIBLE_FORMATS[media_type]]) as image:
            width, height = image.size
            if width * height > MAX_DECODE_PIXELS:
                raise NeedsIsolation(
                    f"The image is {width}x{height} pixels, too large to read "
                    f"(the limit is {MAX_DECODE_PIXELS:,} pixels)."
                )
            return [(_png(image), "image/png")], ""
    except ImageTooLarge:
        raise
    except Exception:  # noqa: BLE001 — undecodable: let the model refuse it
        logger.info("could not re-encode %s image; sending it as is", media_type)
        return [(data, media_type)], ""


def _needs_work(data: bytes, media_type: str) -> bool:
    return (
        media_type == "image/gif"
        or media_type in _CONVERTIBLE_FORMATS
        or len(data) > MAX_SEND_BYTES
    )


def _downscaled(data: bytes, pillow_format: str) -> tuple[bytes, str]:
    """A PNG over the send cap, decoded whole and scaled down.

    PNG cannot be decoded at reduced scale, so the header must be within
    ``MAX_DOWNSCALE_PIXELS``: a 2560 x 1600 screenshot is, a 4K one is not.
    """
    try:
        with Image.open(io.BytesIO(data), formats=[pillow_format]) as image:
            width, height = image.size
            if width * height > MAX_DOWNSCALE_PIXELS:
                raise NeedsIsolation(
                    f"The image is {width}x{height} pixels and "
                    f"{len(data) / 1_048_576:.1f} MB, too large to read."
                )
            if image.mode not in REDUCIBLE_MODES:
                # A palette screenshot would have to convert whole first.
                raise NeedsIsolation(too_large_to_send(len(data), "image"))
            has_alpha = image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info
            image.load()
            # OpenAI scales an image's short side to 768 px before the model
            # sees it, so an integer reduce that keeps it at least that loses
            # nothing, and costs far less than convert + thumbnail (measured
            # 23 against 56 MiB on a 2560 x 1600 PNG).
            factor = max(1, min(width, height) // _MODEL_SHORT_SIDE)
            reduced = image.reduce(factor) if factor > 1 else image
            frame = reduced.convert("RGBA" if has_alpha else "RGB")
            del reduced
    except ImageTooLarge:
        raise
    except Exception as unreadable:  # noqa: BLE001
        raise ImageTooLarge(too_large_to_send(len(data), "image")) from unreadable
    frame.thumbnail((MAX_SIDE, MAX_SIDE))
    out = io.BytesIO()
    if has_alpha:
        frame.save(out, "PNG")
        return out.getvalue(), "image/png"
    frame.save(out, "JPEG", quality=90)
    return out.getvalue(), "image/jpeg"


def _downscaled_jpeg(data: bytes) -> bytes:
    """A JPEG over the send cap, decoded at reduced scale and re-encoded."""
    try:
        with Image.open(io.BytesIO(data), formats=["JPEG"]) as image:
            width, height = image.size
            if width * height > MAX_JPEG_DRAFT_PIXELS:
                raise ImageTooLarge(
                    f"The image is {width}x{height} pixels, too large to read."
                )
            # Progressive and multi-scan JPEGs are decoded from a buffer of
            # the whole image's coefficients, whatever the draft scale.
            buffered = jpeg_coefficient_buffer(data)
            if buffered is None:
                raise ImageTooLarge(too_large_to_send(len(data), "image"))
            if buffered > MAX_JPEG_BUFFERED_BYTES:
                raise NeedsIsolation(
                    f"The image is {width}x{height} pixels and "
                    f"{len(data) / 1_048_576:.1f} MB, too large to read."
                )
            # draft() makes the decoder itself scale by 1/2..1/8, so the full
            # pixels are never allocated. It keeps both sides at least the size
            # asked for, so asking for the model's short side loses nothing.
            image.draft("RGB", (_MODEL_SHORT_SIDE, _MODEL_SHORT_SIDE))
            if image.size[0] * image.size[1] > _MAX_DRAFTED_PIXELS:
                raise NeedsIsolation(
                    f"The image is {width}x{height} pixels, too large to read."
                )
            frame = image.convert("RGB")
    except ImageTooLarge:
        raise
    except Exception as unreadable:  # noqa: BLE001
        raise ImageTooLarge(too_large_to_send(len(data), "image")) from unreadable
    frame.thumbnail((MAX_SIDE, MAX_SIDE))
    out = io.BytesIO()
    frame.save(out, "JPEG", quality=85)
    return out.getvalue()


def jpeg_coefficient_buffer(data: bytes) -> int | None:
    """Bytes of whole-image coefficient buffer libjpeg will allocate to decode.

    0 for a JPEG it decodes one MCU row at a time; ``None`` when the markers up
    to the first scan cannot be read. Follows libjpeg: the buffer is used when
    the frame is progressive (or lossless/hierarchical) or the first scan has
    fewer components than the frame, and holds every component's blocks,
    rounded up to its sampling factors, at 64 two-byte coefficients each.
    """
    if data[:2] != b"\xff\xd8":
        return None
    frame: tuple[int, int, int, list[tuple[int, int]]] | None = None
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            return None
        marker = data[pos + 1]
        if marker == 0xFF:  # fill byte
            pos += 1
            continue
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:  # no length
            pos += 2
            continue
        length = int.from_bytes(data[pos + 2 : pos + 4], "big")
        segment = data[pos + 4 : pos + 2 + length]
        if length < 2 or len(segment) != length - 2:
            return None
        if marker in _JPEG_SOF:
            if len(segment) < 6:
                return None
            height = int.from_bytes(segment[1:3], "big")
            width = int.from_bytes(segment[3:5], "big")
            count = segment[5]
            if len(segment) < 6 + 3 * count or count == 0:
                return None
            sampling = [
                (segment[6 + 3 * i + 1] >> 4, segment[6 + 3 * i + 1] & 0x0F)
                for i in range(count)
            ]
            frame = (marker, width, height, sampling)
        elif marker == 0xDA:  # first scan: everything needed is known
            if frame is None or not segment:
                return None
            sof, width, height, sampling = frame
            if sof in _JPEG_SEQUENTIAL_SOF and segment[0] == len(sampling):
                return 0
            if not all(h and v for h, v in sampling):
                return None
            h_max = max(h for h, _ in sampling)
            v_max = max(v for _, v in sampling)
            total = 0
            for h, v in sampling:
                blocks_wide = _ceil_div(_ceil_div(width * h, h_max), 8)
                blocks_high = _ceil_div(_ceil_div(height * v, v_max), 8)
                padded = _ceil_div(blocks_wide, h) * h * _ceil_div(blocks_high, v) * v
                total += padded * 64 * 2
            return total
        pos += 2 + length
    return None


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(numerator // -denominator)


def _prepare_gif(data: bytes) -> tuple[list[tuple[bytes, str]], str]:
    scan = scan_gif(data)
    if len(data) > MAX_GIF_BYTES or scan is None:
        return _gif_as_is(data, scan)
    total, canvas = scan
    # The last frame is always sampled, and seek() decodes every frame before
    # it onto the (grown) canvas, so all ``total`` frames are decoded at that
    # size.
    if total < 2 or canvas > MAX_DECODE_PIXELS or total * canvas > MAX_GIF_DECODE_PIXELS:
        return _gif_as_is(data, scan)
    try:
        with Image.open(io.BytesIO(data), formats=["GIF"]) as image:
            count = min(MAX_GIF_FRAMES, total)
            # First and last always included; the rest spread between them.
            indices = sorted({round(i * (total - 1) / (count - 1)) for i in range(count)})
            shown = []
            sent = 0
            for index in indices:
                image.seek(index)
                png = _png(image)
                sent += len(png)
                if sent > MAX_SEND_BYTES:
                    break  # the frames so far still fit one request
                shown.append((index, png))
    except Exception:  # noqa: BLE001 — undecodable: send it as it arrived
        return _gif_as_is(data, scan)
    if len(shown) < 2:
        del shown
        return _gif_as_is(data, scan)
    note = (
        f"This is an animated GIF of {total} frames. {len(shown)} evenly "
        "spaced frames are attached in playback order (frames "
        f"{', '.join(str(i + 1) for i, _ in shown)}); motion between them "
        "is not visible."
    )
    return [(png, "image/png") for _, png in shown], note


def _gif_as_is(
    data: bytes, scan: tuple[int, int] | None
) -> tuple[list[tuple[bytes, str]], str]:
    """A GIF not sampled: sent as it arrived, or its first frame if too big.

    The model reads a GIF's first frame only, so a GIF over the send cap loses
    nothing by going as that frame, decoded on the canvas it is drawn on.
    """
    if len(data) <= MAX_SEND_BYTES:
        return [(data, "image/gif")], ""
    if scan is None or scan[1] > MAX_DECODE_PIXELS:
        raise NeedsIsolation(too_large_to_send(len(data), "image"))
    try:
        with Image.open(io.BytesIO(data), formats=["GIF"]) as image:
            first = _png(image)
    except Exception as unreadable:  # noqa: BLE001
        raise ImageTooLarge(too_large_to_send(len(data), "image")) from unreadable
    return [(first, "image/png")], (
        "This GIF was too large to send whole; only its first frame is attached."
    )


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
