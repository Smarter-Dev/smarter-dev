"""Private Chat attachment validation and bounded text extraction."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import multiprocessing
import warnings
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

from PIL import Image
from PIL import UnidentifiedImageError
from sqlalchemy import or_
from sqlalchemy import select

from smarter_dev.web.chat.safety import safe_filename

MAX_ATTACHMENTS_PER_TURN = 5
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_EXTRACTED_CHARS = 100_000
MAX_IMAGE_PIXELS = 40_000_000
MAX_PDF_PAGES = 50
MAX_PDF_BYTES = MAX_ATTACHMENT_BYTES

IMAGE_MIMES = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp"}
)
TEXT_MIMES = frozenset(
    {
        "text/plain",
        "text/markdown",
        "text/csv",
        "application/json",
        "application/xml",
        "text/xml",
        "text/x-python",
        "text/javascript",
        "application/javascript",
        "text/css",
        "text/x-c",
        "text/x-c++",
        "text/x-java-source",
        "application/toml",
        "application/x-yaml",
        "text/yaml",
    }
)
PDF_MIME = "application/pdf"
ALLOWED_MIMES = TEXT_MIMES | IMAGE_MIMES | {PDF_MIME}
DENIED_SUFFIXES = frozenset(
    {
        ".zip",
        ".tar",
        ".gz",
        ".bz2",
        ".7z",
        ".rar",
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".app",
        ".msi",
        ".apk",
        ".deb",
        ".rpm",
        ".jar",
        ".class",
        ".com",
        ".bat",
        ".cmd",
        ".ps1",
        ".sh",
        ".bin",
        ".iso",
        ".dmg",
    }
)
TEXT_SUFFIXES = frozenset(
    {
        ".txt",
        ".md",
        ".markdown",
        ".json",
        ".csv",
        ".log",
        ".cfg",
        ".conf",
        ".ini",
        ".toml",
        ".yaml",
        ".yml",
        ".xml",
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".css",
        ".html",
        ".htm",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".java",
        ".go",
        ".rs",
        ".rb",
        ".php",
        ".sql",
        ".env",
        ".properties",
    }
)
IMAGE_SUFFIX_MIMES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


class AttachmentError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ValidatedAttachment:
    filename: str
    media_type: str
    size_bytes: int
    sha256: str
    kind: str


def _verify_image(data: bytes, media_type: str) -> None:
    expected = media_type.split("/", 1)[1].upper().replace("JPEG", "JPEG")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise AttachmentError("image dimensions are too large")
                actual = (image.format or "").upper()
                if actual == "JPG":
                    actual = "JPEG"
                if actual != expected:
                    raise AttachmentError(
                        "attachment contents do not match the declared image type"
                    )
                image.verify()
    except AttachmentError:
        raise
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
        SyntaxError,
        ValueError,
    ) as exc:
        raise AttachmentError("image is malformed or unsafe") from exc


def validate_attachment(
    filename: str, media_type: str, data: bytes
) -> ValidatedAttachment:
    try:
        filename = safe_filename(filename)
    except ValueError as exc:
        raise AttachmentError(str(exc)) from exc
    if len(filename) > 255:
        raise AttachmentError("attachment filename is too long")
    suffix = Path(filename).suffix.lower()
    media_type = (media_type or "").split(";", 1)[0].strip().lower()
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise AttachmentError("attachment exceeds the 10 MB limit")
    if not data:
        raise AttachmentError("attachment is empty")
    if suffix in DENIED_SUFFIXES:
        raise AttachmentError("archives and executable files are not supported")
    if media_type not in ALLOWED_MIMES:
        if media_type == "application/octet-stream" and suffix in TEXT_SUFFIXES:
            media_type = "text/plain"
        else:
            raise AttachmentError(
                f"unsupported attachment type: {media_type or 'unknown'}"
            )
    if media_type in IMAGE_MIMES:
        if IMAGE_SUFFIX_MIMES.get(suffix) != media_type:
            raise AttachmentError("filename extension does not match the image type")
        _verify_image(data, media_type)
        kind = "image"
    elif media_type == PDF_MIME:
        if len(data) > MAX_PDF_BYTES:
            raise AttachmentError("PDF attachments are limited to 10 MB")
        if suffix != ".pdf" or not data.startswith(b"%PDF-"):
            raise AttachmentError("attachment contents do not match PDF")
        kind = "pdf"
    else:
        if suffix and suffix not in TEXT_SUFFIXES:
            raise AttachmentError("filename extension is not supported for text")
        if b"\x00" in data:
            raise AttachmentError("binary files cannot be uploaded as text")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AttachmentError("text attachments must be UTF-8") from exc
        kind = "text"
    return ValidatedAttachment(
        filename=filename,
        media_type=media_type,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        kind=kind,
    )


def extract_text(data: bytes, media_type: str) -> str | None:
    """Extract bounded text and normalize all parser failures."""
    if media_type in IMAGE_MIMES:
        return None
    try:
        if media_type == PDF_MIME:
            import pdfplumber

            chunks: list[str] = []
            total = 0
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                if len(pdf.pages) > MAX_PDF_PAGES:
                    raise AttachmentError(
                        f"PDF attachments are limited to {MAX_PDF_PAGES} pages"
                    )
                for index in range(len(pdf.pages)):
                    page = pdf.pages[index]
                    text = page.extract_text() or ""
                    chunks.append(text)
                    total += len(text)
                    if total >= MAX_EXTRACTED_CHARS:
                        break
            return "\n".join(chunks)[:MAX_EXTRACTED_CHARS]
        text = data.decode("utf-8")
        if media_type == "application/json":
            json.loads(text)
        elif media_type == "text/csv":
            list(csv.reader(io.StringIO(text[:MAX_EXTRACTED_CHARS]), strict=True))
        return text[:MAX_EXTRACTED_CHARS]
    except AttachmentError:
        raise
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        csv.Error,
        OSError,
        ValueError,
    ) as exc:
        raise AttachmentError("attachment content is malformed") from exc
    except Exception as exc:
        # PDF parsers use several library-specific exception classes.  Never
        # leak one as a 500 from the upload endpoint.
        raise AttachmentError(
            "attachment content could not be safely extracted"
        ) from exc


def _pdf_extract_process(data: bytes, connection) -> None:
    try:
        try:
            import resource

            resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024,) * 2)
            resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
        except (ImportError, OSError, ValueError):
            pass
        connection.send((True, extract_text(data, PDF_MIME)))
    except BaseException as exc:
        connection.send((False, f"{type(exc).__name__}: {exc}"))
    finally:
        connection.close()


async def extract_text_bounded(
    data: bytes, media_type: str, *, timeout: float = 15
) -> str | None:
    """Extract PDF text in a killable resource-bounded subprocess."""
    if media_type != PDF_MIME:
        return extract_text(data, media_type)
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_pdf_extract_process, args=(data, child))
    process.start()
    child.close()
    try:
        ready = await asyncio.to_thread(parent.poll, timeout)
        if not ready:
            raise AttachmentError("Attachment extraction timed out.")
        ok, payload = parent.recv()
        if not ok:
            raise AttachmentError("attachment content could not be safely extracted")
        return payload
    finally:
        parent.close()
        if process.is_alive():
            process.terminate()
        await asyncio.to_thread(process.join, 2)
        if process.is_alive():
            process.kill()
            await asyncio.to_thread(process.join, 2)


async def cleanup_orphan_attachments(
    *, max_age: timedelta = timedelta(hours=24)
) -> int:
    """Delete abandoned/staged objects and rows; safe to retry after any failure."""
    from skrift.config import get_settings as get_skrift_settings
    from skrift.storage import StorageManager

    from smarter_dev.shared.database import get_db_session_context
    from smarter_dev.web.models import WebChatAttachment

    cutoff = datetime.now(UTC) - max_age
    async with get_db_session_context() as session:
        rows = list(
            (
                await session.execute(
                    select(WebChatAttachment)
                    .where(
                        or_(
                            WebChatAttachment.status == "deleting",
                            (
                                WebChatAttachment.turn_id.is_(None)
                                & (WebChatAttachment.created_at < cutoff)
                                & WebChatAttachment.status.in_(("ready", "uploading"))
                            ),
                        )
                    )
                    .with_for_update(skip_locked=True)
                )
            ).scalars()
        )
        # Claim cleanup before releasing row locks. Turn submission only accepts
        # ready rows under its own lock, so it cannot attach a stale snapshot.
        for row in rows:
            row.status = "deleting"
        await session.commit()
    if not rows:
        return 0
    manager = StorageManager(get_skrift_settings().storage)
    deleted_ids = []
    try:
        backend = await manager.get("chat_attachments")
        for row in rows:
            try:
                await backend.delete(row.storage_key)
            except Exception:
                continue
            deleted_ids.append(row.id)
    finally:
        await manager.close()
    if deleted_ids:
        async with get_db_session_context() as session:
            durable = list(
                (
                    await session.execute(
                        select(WebChatAttachment).where(
                            WebChatAttachment.id.in_(deleted_ids),
                            WebChatAttachment.status == "deleting",
                        )
                    )
                ).scalars()
            )
            for row in durable:
                await session.delete(row)
            await session.commit()
    return len(deleted_ids)


def require_attachment_count(ids: list[object]) -> None:
    if len(ids) > MAX_ATTACHMENTS_PER_TURN:
        raise AttachmentError("a turn may include at most 5 attachments")
    if len(set(ids)) != len(ids):
        raise AttachmentError("attachment IDs must be unique")
