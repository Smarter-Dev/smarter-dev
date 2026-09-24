"""The memory budget of one media read: download, prepare, model call (#25).

The bot's container idles ~70 MiB under its 512Mi limit, and a read holds its
bytes for its whole life: the download (briefly twice, as chunks and joined),
the input through the model call, any converted parts, and the SDK's request.
Measured 2026-09-24 through pydantic-ai's OpenAI Responses model, the request
costs ~5.2x the media bytes it carries (base64 data URL, JSON body, httpx) —
a 20 MB image grew peak memory by 102 MiB. So:

- ``MAX_DOWNLOAD_BYTES`` caps any file a read fetches (rejected from its
  Content-Length when the server sends one, otherwise at the cap mid-stream);
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
import ctypes.util
import gc
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024
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
    try:
        return ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6").malloc_trim
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
