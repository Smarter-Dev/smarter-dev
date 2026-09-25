"""The memory budget of one media read: download, prepare, model call (#25).

The bot's container idles ~180 MiB under its 612Mi limit (425Mi in use, one
sample on 2026-09-25: nominal headroom, not a per-read budget), and a read
holds its bytes for its whole life: the download (briefly twice, as chunks
and joined), the input through the model call, any converted parts, and the
SDK's request. Measured 2026-09-24 through pydantic-ai's OpenAI Responses
model, the request costs ~5.2x the media bytes it carries (base64 data URL,
JSON body, httpx) — a 20 MB image grew peak memory by 102 MiB. So:

- ``MAX_DOWNLOAD_BYTES`` (10 MiB) caps what a read holds in memory, and any
  file that is not an image (rejected from its Content-Length when the
  server sends one, otherwise at the cap mid-stream);
- an image over it is downsampled rather than refused: its download streams
  on to a temp file on disk (``SpooledImage``), up to
  ``MAX_IMAGE_DOWNLOAD_BYTES``, and is decoded from there in a bounded child
  process (``media_images``), so its bytes are never held in memory;
- ``MAX_SEND_BYTES`` caps the media bytes one model call carries
  (``media_images`` downscales or refuses what is over it), ~16 MiB of SDK
  copies at the cap;
- ``media_read_slot`` lets one read run at a time per process, from download
  to model reply, so concurrent reads (parallel tool calls, other channels)
  queue instead of adding up, and returns what each read freed to the OS
  before the next starts. Waiting is bounded; a read that cannot get the
  slot in time answers that the reader is busy.

Used by the bot's ``web_read`` and the web worker's ``media_read``, each in its
own process.
"""

from __future__ import annotations

import asyncio
import ctypes
import gc
import os
import tempfile
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

# Sizes are in MiB throughout, as the repo's other byte caps are.
MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024
# Discord's upload limit on a level-2 boosted server; streamed to disk.
MAX_IMAGE_DOWNLOAD_BYTES = 50 * 1024 * 1024
MAX_SEND_BYTES = 3 * 1024 * 1024
SLOT_WAIT_SECONDS = 120.0

# One slot per event loop (the bot and the worker each run one); keyed so a
# semaphore is never shared across loops, which asyncio refuses.
_slots: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


def _slot() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    slot = _slots.get(loop)
    if slot is None:
        slot = _slots[loop] = asyncio.Semaphore(1)
    return slot


def _load_malloc_trim():
    # By soname: ctypes.util.find_library would spawn ldconfig (or gcc) to look.
    try:
        return ctypes.CDLL("libc.so.6").malloc_trim
    except (OSError, AttributeError):  # not glibc: nothing to trim
        return None


_malloc_trim = _load_malloc_trim()


def release_freed_memory() -> None:
    """Hand the heap a read freed back to the OS.

    glibc keeps large freed blocks in the heap once its dynamic mmap threshold
    has risen, so without this each process stayed ~31 MiB larger after its
    first read (measured 2026-09-24; 6 MiB with it).
    """
    gc.collect()
    if _malloc_trim is not None:
        _malloc_trim(0)


class MediaReaderBusy(RuntimeError):
    """Another read held the slot for longer than a caller may wait."""


@asynccontextmanager
async def media_read_slot(wait: float | None = None) -> AsyncIterator[None]:
    """Hold the process's one media-read slot for the body of the block."""
    slot = _slot()
    try:
        async with asyncio.timeout(SLOT_WAIT_SECONDS if wait is None else wait):
            await slot.acquire()
    except TimeoutError as timed_out:
        raise MediaReaderBusy(
            "Another file is being read; try again in a moment."
        ) from timed_out
    try:
        yield
    finally:
        release_freed_memory()
        slot.release()


def too_large_to_send(size: int, what: str) -> str:
    """The answer a read gives for media over ``MAX_SEND_BYTES``."""
    return (
        f"The {what} is {size / 1_048_576:.1f} MB, too large to read "
        f"(the limit is {MAX_SEND_BYTES // 1_048_576} MB)."
    )


@dataclass(frozen=True)
class SpooledImage:
    """An image download over ``MAX_DOWNLOAD_BYTES``, held on disk, not in memory.

    Whoever ends the read deletes the file (``discard``); ``media_images``
    does once it has decoded it.
    """

    path: str
    size: int

    def head(self, count: int = 64) -> bytes:
        with open(self.path, "rb") as file:
            return file.read(count)

    def discard(self) -> None:
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass


def spool_image(data: bytes) -> SpooledImage:
    """Write ``data`` to a private temp file, for a child process to decode."""
    handle, path = tempfile.mkstemp(suffix=".image")
    with os.fdopen(handle, "wb") as file:
        file.write(data)
    return SpooledImage(path, len(data))


async def read_body(
    chunks: AsyncIterator[bytes],
    declared: str,
    *,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
    spill_image: bool = False,
) -> bytes | SpooledImage | None:
    """Stream a response body within the caps; ``None`` when it is over them.

    Held in memory up to ``max_bytes``. With ``spill_image`` (the response is
    an image) a longer body continues to a temp file, up to
    ``MAX_IMAGE_DOWNLOAD_BYTES``, and comes back as a ``SpooledImage``. A
    ``declared`` Content-Length over the applicable cap is refused unread.
    """
    ceiling = MAX_IMAGE_DOWNLOAD_BYTES if spill_image else max_bytes
    if declared.isdigit() and int(declared) > ceiling:
        return None
    held: list[bytes] = []
    received = 0
    spool = None
    path = ""
    try:
        async for chunk in chunks:
            received += len(chunk)
            if received > ceiling:
                return None
            if spool is None and received > max_bytes:
                handle, path = tempfile.mkstemp(suffix=".image")
                spool = os.fdopen(handle, "wb")
                spool.writelines(held)
                held.clear()
            if spool is not None:
                spool.write(chunk)
            else:
                held.append(chunk)
        if spool is None:
            return b"".join(held)
        spool.close()
        spooled, path = SpooledImage(path, received), ""
        return spooled
    finally:
        if spool is not None:
            spool.close()
        if path:  # abandoned: over the ceiling, failed or cancelled
            os.unlink(path)
