"""Downsample an image file in a bounded child process (#25).

Run by ``media_images.prepare_image_bounded`` as
``python -m smarter_dev.shared.image_child <path> <media_type> <extra>`` for an
image the bot cannot decode in its own process: one downloaded to disk because
it is over ``MAX_DOWNLOAD_BYTES``, or one whose pixels are over the in-process
caps (a 4K PNG screenshot). The child's address space is capped ``extra``
bytes above its size after imports (``bounded_child.limit_memory``), so a
decode that would need more fails in here and the read answers "too large".

The header is still checked before any pixel data is read, since a few KB of
PNG or GIF can declare a canvas of billions of pixels: over
``MAX_CHILD_DECODE_PIXELS`` (decoded whole; ``MAX_CHILD_WEBP_PIXELS`` for
WebP) or, for a JPEG,
``MAX_JPEG_DRAFT_PIXELS`` (decoded at 1/2 to 1/8 scale) or a coefficient
buffer over ``MAX_CHILD_JPEG_BUFFERED_BYTES``, it is refused unread. A GIF goes
as its first frame. The result is one JPEG (or PNG, if it has transparency)
within ``MAX_SEND_BYTES``, short side kept at 768 px or more where it can be.

Protocol: exit 0 with ``<media type>\\n<note>\\n<bytes>`` on stdout; exit 2
with a message for the reader on stdout; anything else is a failure (most
often the memory cap).
"""

from __future__ import annotations

import io
import sys

from PIL import Image

from smarter_dev.shared import bounded_child
from smarter_dev.shared.media_images import CHILD_TIMEOUT_SECONDS
from smarter_dev.shared.media_images import MAX_CHILD_DECODE_PIXELS
from smarter_dev.shared.media_images import MAX_CHILD_JPEG_BUFFERED_BYTES
from smarter_dev.shared.media_images import MAX_CHILD_WEBP_PIXELS
from smarter_dev.shared.media_images import MAX_JPEG_DRAFT_PIXELS
from smarter_dev.shared.media_images import MAX_SIDE
from smarter_dev.shared.media_images import REDUCIBLE_MODES
from smarter_dev.shared.media_images import ImageTooLarge
from smarter_dev.shared.media_images import jpeg_coefficient_buffer
from smarter_dev.shared.media_reads import MAX_SEND_BYTES

_FORMATS = {
    "image/jpeg": "JPEG",
    "image/png": "PNG",
    "image/webp": "WEBP",
    "image/gif": "GIF",
    "image/bmp": "BMP",
}
_MODEL_SHORT_SIDE = 768
# The markers before a JPEG's first scan (EXIF, ICC, tables) fit well within this.
_JPEG_HEAD_BYTES = 4 * 1024 * 1024
# Counting a GIF's frames (n_frames) would decode every one of them.
_GIF_NOTE = "Only this GIF's first frame is attached; it was too large to send whole."


def webp_size(head: bytes) -> tuple[int, int] | None:
    """A WebP's canvas from its RIFF header (VP8, VP8L or VP8X), or None."""
    if len(head) < 30 or head[:4] != b"RIFF" or head[8:12] != b"WEBP":
        return None
    chunk = head[12:16]
    if chunk == b"VP8 ":
        width = int.from_bytes(head[26:28], "little") & 0x3FFF
        height = int.from_bytes(head[28:30], "little") & 0x3FFF
        return width, height
    if chunk == b"VP8L":
        bits = int.from_bytes(head[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8X":
        width = int.from_bytes(head[24:27], "little") + 1
        height = int.from_bytes(head[27:30], "little") + 1
        return width, height
    return None


def downsample(path: str, media_type: str) -> tuple[bytes, str, str]:
    """``(bytes, media type, note)`` for the image at ``path``, within the caps."""
    pillow_format = _FORMATS.get(media_type)
    if pillow_format is None:
        raise ImageTooLarge("This image format cannot be read.")
    if pillow_format == "WEBP":
        # Pillow sets up libwebp's decoder (and its buffers) in open(), so the
        # size must be known before that.
        with open(path, "rb") as file:
            size = webp_size(file.read(32))
        if size is None:
            raise ImageTooLarge("This WebP image cannot be read.")
        if size[0] * size[1] > MAX_CHILD_WEBP_PIXELS:
            raise ImageTooLarge(
                f"The image is {size[0]}x{size[1]} pixels, too large to read."
            )
    try:
        image = Image.open(path, formats=[pillow_format])
    except Image.DecompressionBombError as bomb:
        # Pillow's own guard: over twice its MAX_IMAGE_PIXELS, from the header.
        raise ImageTooLarge("The image declares too many pixels to read.") from bomb
    with image:
        width, height = image.size
        too_large = ImageTooLarge(f"The image is {width}x{height} pixels, too large to read.")
        if pillow_format == "JPEG":
            if width * height > MAX_JPEG_DRAFT_PIXELS:
                raise too_large
            with open(path, "rb") as file:
                buffered = jpeg_coefficient_buffer(file.read(_JPEG_HEAD_BYTES))
            if buffered is None or buffered > MAX_CHILD_JPEG_BUFFERED_BYTES:
                raise too_large
            image.draft("RGB", (_MODEL_SHORT_SIDE, _MODEL_SHORT_SIDE))
        elif width * height > (
            MAX_CHILD_WEBP_PIXELS if pillow_format == "WEBP" else MAX_CHILD_DECODE_PIXELS
        ):
            raise too_large
        note = _GIF_NOTE if pillow_format == "GIF" else ""
        has_alpha = image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info
        image.load()
        frame = image
        if frame.mode not in REDUCIBLE_MODES:  # palette, 1-bit, 16-bit
            frame = frame.convert("RGBA" if has_alpha else "RGB")
        # Integer reduce keeps the model's short side and costs far less than
        # resampling the whole decode.
        factor = max(1, min(frame.size) // _MODEL_SHORT_SIDE)
        if factor > 1:
            frame = frame.reduce(factor)
        frame = frame.convert("RGBA" if has_alpha else "RGB")
    frame.thumbnail((MAX_SIDE, MAX_SIDE))
    if has_alpha:
        out = io.BytesIO()
        frame.save(out, "PNG")
        if out.tell() <= MAX_SEND_BYTES:
            return out.getvalue(), "image/png", note
        # Too large as PNG: flatten onto white and send as JPEG.
        flat = Image.new("RGB", frame.size, "white")
        flat.paste(frame, mask=frame.getchannel("A"))
        frame = flat
    for side, quality in ((MAX_SIDE, 85), (MAX_SIDE, 70), (1536, 70), (1024, 70)):
        if max(frame.size) > side:
            frame.thumbnail((side, side))
        out = io.BytesIO()
        frame.save(out, "JPEG", quality=quality)
        if out.tell() <= MAX_SEND_BYTES:
            return out.getvalue(), "image/jpeg", note
    raise ImageTooLarge("The image is too detailed to send within the size limit.")


def _main(path: str, media_type: str, extra: int) -> int:
    bounded_child.limit_memory(extra, cpu_seconds=int(CHILD_TIMEOUT_SECONDS) + 5)
    try:
        data, sent_type, note = downsample(path, media_type)
    except ImageTooLarge as refused:
        sys.stdout.buffer.write(str(refused).encode())
        return 2
    except BaseException:  # noqa: BLE001 — MemoryError surfaces as many types
        return 1
    sys.stdout.buffer.write(f"{sent_type}\n{note}\n".encode() + data)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1], sys.argv[2], int(sys.argv[3])))
