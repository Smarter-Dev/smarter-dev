"""PDF text, extracted in a child process that cannot outgrow its budget (#25).

pdfplumber builds an object per character it lays out and has no memory
bound: a 10 MB PDF whose one page draws text ops grew a process by 9.5 GiB
(measured 2026-09-24). In the bot, which idles ~70 MiB under its limit, that
is an OOM kill for any member who posts one. So the parse runs in a child
Python with an address-space limit (``RLIMIT_AS``) set just above what
importing pdfplumber takes: past it, allocation fails inside the child and
the read answers that the PDF is too complex, while the parent is untouched.

The PDF is handed over as a file, so the caller can drop its bytes, and the
parent returns them to the OS, before the child starts. The child costs the
container ~35 MiB once pdfplumber is imported (bare Python is 12 of it) and
at most ~57 MiB when a parse hits the limit (measured 2026-09-25); +20 MiB is
the least that reads a real 6.6 MB, 75k-character manual (+16 failed it).
One read at a time is enforced by the callers' ``media_read_slot``, and the
child is killed and reaped before a read returns, however it ends.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import resource
import signal
import sys
import tempfile

from smarter_dev.shared.media_reads import release_freed_memory

logger = logging.getLogger(__name__)

# Headroom over the child's own address space once pdfplumber is imported.
CHILD_EXTRA_BYTES = 20 * 1024 * 1024
CHILD_TIMEOUT_SECONDS = 60.0
# How long a killed child may take to be reaped before the read gives up on it.
REAP_TIMEOUT_SECONDS = 10.0


class PdfUnreadable(RuntimeError):
    """The PDF could not be read within the child's memory or time budget."""


def spool(data: bytes) -> str:
    """Write ``data`` to a private temp file and return its path."""
    handle, path = tempfile.mkstemp(suffix=".pdf")
    with os.fdopen(handle, "wb") as file:
        file.write(data)
    return path


async def pdf_text_from_file(path: str, max_chars: int) -> str:
    """Up to ``max_chars`` of the PDF at ``path``; deletes the file after.

    The child is killed and reaped before this returns or raises, including
    when the read is cancelled (again, even, while the kill is in progress),
    so it never outlives the caller's ``media_read_slot``.
    """
    release_freed_memory()  # the caller's dropped download, before the child
    child = None
    try:
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "smarter_dev.shared.pdf_text",
            path,
            str(max_chars),
            str(CHILD_EXTRA_BYTES),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            out, _ = await asyncio.wait_for(child.communicate(), CHILD_TIMEOUT_SECONDS)
        except TimeoutError as timed_out:
            raise PdfUnreadable("The PDF took too long to read.") from timed_out
    finally:
        try:
            if child is not None and child.returncode is None:
                await _kill_and_reap(child)
        finally:
            os.unlink(path)
    if child.returncode != 0:
        raise PdfUnreadable(
            "The PDF is too large or complex to read within the memory limit."
        )
    return out.decode("utf-8", errors="replace")[:max_chars]


async def _kill_and_reap(child: asyncio.subprocess.Process) -> None:
    """SIGKILL ``child`` and wait for it to exit, through further cancellation.

    A cancel that arrives while waiting is held back until the child is gone,
    then re-raised, so the caller's slot is not released with a child alive.
    """
    try:
        child.kill()
    except ProcessLookupError:
        pass
    reaped = asyncio.ensure_future(child.wait())
    cancelled = False
    loop = asyncio.get_running_loop()
    deadline = loop.time() + REAP_TIMEOUT_SECONDS
    while not reaped.done():
        remaining = deadline - loop.time()
        if remaining <= 0:
            logger.error("pdf_text: child %s not reaped after SIGKILL", child.pid)
            break
        try:
            await asyncio.wait_for(asyncio.shield(reaped), remaining)
        except asyncio.CancelledError:
            cancelled = True
        except TimeoutError:
            pass
    if cancelled:
        raise asyncio.CancelledError


def _child(path: str, max_chars: int, extra: int) -> int:
    import pdfplumber

    with open("/proc/self/status") as status:
        size = next(int(line.split()[1]) for line in status if line.startswith("VmSize"))
    limit = size * 1024 + extra
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    # Backstops if the parent cannot kill us: die with it, and stop on CPU.
    ctypes.CDLL(None).prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG
    cpu = int(CHILD_TIMEOUT_SECONDS) + 5
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    try:
        pages: list[str] = []
        count = 0
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                page.close()  # drop the page's layout objects before the next
                pages.append(text)
                count += len(text)
                if count >= max_chars:
                    break
        sys.stdout.write("\n\n".join(pages)[:max_chars])
        return 0
    except BaseException:  # noqa: BLE001 — MemoryError surfaces as many types
        return 1


if __name__ == "__main__":
    raise SystemExit(_child(sys.argv[1], int(sys.argv[2]), int(sys.argv[3])))
