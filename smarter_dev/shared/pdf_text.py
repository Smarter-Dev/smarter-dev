"""PDF text, extracted in a child process that cannot outgrow its budget (#25).

pdfplumber builds an object per character it lays out and has no memory
bound: a 10 MB PDF whose one page draws text ops grew a process by 9.5 GiB
(measured 2026-09-24). In the bot, which idles ~70 MiB under its limit, that
is an OOM kill for any member who posts one. So the parse runs in a child
Python with an address-space limit (``RLIMIT_AS``) set just above what
importing pdfplumber takes: past it, allocation fails inside the child and
the read answers that the PDF is too complex, while the parent is untouched.

The PDF is handed over as a file, so the caller can drop its bytes before
the child starts. Measured on real manuals and generated worst cases, the
child peaks at 34 MiB after imports and at most ~56 MiB in all; the limit
reads a 6.5 MB, 75k-character manual that +16 MiB could not. One read at a
time is enforced by the callers' ``media_read_slot``.
"""

from __future__ import annotations

import asyncio
import os
import resource
import sys
import tempfile

# Headroom over the child's own address space once pdfplumber is imported.
CHILD_EXTRA_BYTES = 24 * 1024 * 1024
CHILD_TIMEOUT_SECONDS = 60.0


class PdfUnreadable(RuntimeError):
    """The PDF could not be read within the child's memory or time budget."""


def spool(data: bytes) -> str:
    """Write ``data`` to a private temp file and return its path."""
    handle, path = tempfile.mkstemp(suffix=".pdf")
    with os.fdopen(handle, "wb") as file:
        file.write(data)
    return path


async def pdf_text_from_file(path: str, max_chars: int) -> str:
    """Up to ``max_chars`` of the PDF at ``path``; deletes the file after."""
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
            child.kill()
            await child.wait()
            raise PdfUnreadable("The PDF took too long to read.") from timed_out
    finally:
        os.unlink(path)
    if child.returncode != 0:
        raise PdfUnreadable(
            "The PDF is too large or complex to read within the memory limit."
        )
    return out.decode("utf-8", errors="replace")[:max_chars]


def _child(path: str, max_chars: int, extra: int) -> int:
    import pdfplumber

    with open("/proc/self/status") as status:
        size = next(int(line.split()[1]) for line in status if line.startswith("VmSize"))
    limit = size * 1024 + extra
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
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
