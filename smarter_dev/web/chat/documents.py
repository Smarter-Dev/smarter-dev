"""Validation and durable storage for Chat-created markdown documents."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.chat.runtime import RunSuperseded
from smarter_dev.web.models import WebChatDocument
from smarter_dev.web.models import WebChatMessage
from smarter_dev.web.models import WebChatTurn

MAX_DOCUMENT_TITLE_CHARS = 200
MAX_DOCUMENT_FILENAME_CHARS = 255
MAX_DOCUMENT_MARKDOWN_CHARS = 100_000


class MarkdownDocumentError(ValueError):
    """The model supplied an invalid markdown document."""


@dataclass(frozen=True, slots=True)
class ValidatedMarkdownDocument:
    title: str
    filename: str
    markdown: str
    size_bytes: int


def validate_markdown_document(
    *, title: str, filename: str, markdown: str
) -> ValidatedMarkdownDocument:
    clean_title = (title or "").strip()
    clean_filename = (filename or "").strip()
    if not clean_title:
        raise MarkdownDocumentError("Document title is required.")
    if len(clean_title) > MAX_DOCUMENT_TITLE_CHARS:
        raise MarkdownDocumentError("Document title is too long (max 200 characters).")
    if any(ord(character) < 32 or ord(character) == 127 for character in clean_title):
        raise MarkdownDocumentError("Document title contains control characters.")
    if not clean_filename:
        raise MarkdownDocumentError("Document filename is required.")
    if clean_filename in {".", ".."} or any(
        separator in clean_filename for separator in ("/", "\\")
    ):
        raise MarkdownDocumentError("Document filename must not contain a path.")
    if any(
        ord(character) < 32 or ord(character) == 127 for character in clean_filename
    ):
        raise MarkdownDocumentError("Document filename contains control characters.")
    if not clean_filename.lower().endswith(".md"):
        clean_filename += ".md"
    if len(clean_filename) > MAX_DOCUMENT_FILENAME_CHARS:
        raise MarkdownDocumentError("Document filename is too long (max 255 characters).")
    if not (markdown or "").strip():
        raise MarkdownDocumentError("Document markdown is required.")
    if len(markdown) > MAX_DOCUMENT_MARKDOWN_CHARS:
        raise MarkdownDocumentError(
            "Document markdown is too long (max 100,000 characters)."
        )
    return ValidatedMarkdownDocument(
        title=clean_title,
        filename=clean_filename,
        markdown=markdown,
        size_bytes=len(markdown.encode("utf-8")),
    )


async def store_markdown_document(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    turn_id: UUID,
    assistant_message_id: UUID,
    worker_lease_token: str,
    tool_call_id: str,
    title: str,
    filename: str,
    markdown: str,
) -> tuple[WebChatDocument, bool]:
    """Store one document, returning the existing row on tool redelivery."""
    lease_owner = await session.scalar(
        select(WebChatTurn.id).where(
            WebChatTurn.id == turn_id,
            WebChatTurn.conversation_id == conversation_id,
            WebChatTurn.worker_lease_token == worker_lease_token,
            WebChatTurn.status.in_(("running", "stopping")),
        )
    )
    if lease_owner is None:
        raise RunSuperseded()

    existing = await session.scalar(
        select(WebChatDocument).where(
            WebChatDocument.turn_id == turn_id,
            WebChatDocument.tool_call_id == tool_call_id,
        )
    )
    if existing is not None:
        return existing, False

    assistant_message = await session.scalar(
        select(WebChatMessage.id).where(
            WebChatMessage.id == assistant_message_id,
            WebChatMessage.conversation_id == conversation_id,
            WebChatMessage.turn_id == turn_id,
            WebChatMessage.role == "assistant",
        )
    )
    if assistant_message is None:
        raise RunSuperseded()

    validated = validate_markdown_document(
        title=title, filename=filename, markdown=markdown
    )
    document = WebChatDocument(
        conversation_id=conversation_id,
        turn_id=turn_id,
        assistant_message_id=assistant_message_id,
        tool_call_id=tool_call_id,
        title=validated.title,
        filename=validated.filename,
        markdown_content=validated.markdown,
        size_bytes=validated.size_bytes,
    )
    session.add(document)
    await session.flush()
    return document, True
