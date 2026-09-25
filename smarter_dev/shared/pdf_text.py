"""PDF text, extracted in a child process that cannot outgrow its budget (#25).

pdfplumber builds an object per character it lays out and has no memory
bound: a 10 MB PDF whose one page draws text ops grew a process by 9.5 GiB
(measured 2026-09-24): far past the bot's memory limit, so an OOM kill for
any member who posts one. So the parse runs in a ``bounded_child``: a child
Python with an address-space limit (``RLIMIT_AS``) set just above what
importing pdfplumber takes. Past it, allocation fails inside the child and
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

import os
import sys
import tempfile

from smarter_dev.shared import bounded_child

# Headroom over the child's own address space once pdfplumber is imported.
CHILD_EXTRA_BYTES = 20 * 1024 * 1024
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
    """Up to ``max_chars`` of the PDF at ``path``; deletes the file after.

    The child is killed and reaped before this returns or raises, including
    when the read is cancelled, so it never outlives the caller's slot.
    """
    try:
        code, out = await bounded_child.run(
            "smarter_dev.shared.pdf_text",
            path,
            str(max_chars),
            str(CHILD_EXTRA_BYTES),
            timeout=CHILD_TIMEOUT_SECONDS,
        )
    except bounded_child.ChildTimedOut as timed_out:
        raise PdfUnreadable("The PDF took too long to read.") from timed_out
    finally:
        os.unlink(path)
    if code != 0:
        raise PdfUnreadable(
            "The PDF is too large or complex to read within the memory limit."
        )
    return out.decode("utf-8", errors="replace")[:max_chars]


def _child(path: str, max_chars: int, extra: int) -> int:
    import pdfplumber

    bounded_child.limit_memory(extra, cpu_seconds=int(CHILD_TIMEOUT_SECONDS) + 5)
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
