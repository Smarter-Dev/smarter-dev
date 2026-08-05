"""Validation and durable storage for Chat-created markdown documents.

Documents are written by forking the agent's own history: the model asks for a
file, the fork tells it the file exists and to emit the body as its next turn,
and that turn streams straight into the row created here. So a document row has
a life cycle — it is visible while it is still being written — and the body
arrives in pieces rather than all at once.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.chat.runtime import RunSuperseded
from smarter_dev.web.models import WebChatAttachment
from smarter_dev.web.models import WebChatDocument
from smarter_dev.web.models import WebChatMessage
from smarter_dev.web.models import WebChatTurn

MAX_DOCUMENT_TITLE_CHARS = 200
MAX_DOCUMENT_FILENAME_CHARS = 255
MAX_DOCUMENT_MARKDOWN_CHARS = 100_000
# What a read hands back to the model in one tool result. Larger than any
# document the writer is allowed to finish is pointless; smaller keeps a reread
# from swallowing the context window whole.
MAX_DOCUMENT_READ_CHARS = 60_000

# Terminal states. Only "complete" is safe to download or reread verbatim.
# "superseded" is the one a write never produces: it is applied to the row a
# successful overwrite replaced, so the old file stops being part of the
# conversation without being destroyed.
DOCUMENT_STATUSES = (
    "streaming",
    "complete",
    "truncated",
    "stopped",
    "failed",
    "superseded",
)
READABLE_STATUSES = ("complete", "truncated", "stopped")
# What an overwrite has to reach before it is allowed to replace what was there.
# A stopped or failed write is half a file, and half a file must never be the
# thing that displaces a whole one.
REPLACING_STATUSES = ("complete", "truncated")

# One edit call may carry several patches, but not an unbounded number: they
# are real tool arguments and every one of them costs context.
MAX_DOCUMENT_PATCHES = 20


class MarkdownDocumentError(ValueError):
    """The model supplied an invalid markdown document."""


@dataclass(frozen=True, slots=True)
class ValidatedDocumentRequest:
    title: str
    filename: str


def validate_document_request(*, title: str, filename: str) -> ValidatedDocumentRequest:
    """Check the parts the model supplies as tool arguments.

    The body is not checked here: it does not exist yet when the file is
    created, and it arrives as a stream afterwards.
    """
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
        raise MarkdownDocumentError(
            "Document filename is too long (max 255 characters)."
        )
    return ValidatedDocumentRequest(title=clean_title, filename=clean_filename)


def document_body_bytes(markdown: str) -> int:
    return len(markdown.encode("utf-8"))


def clean_document_body(markdown: str) -> str:
    """Normalize a streamed body into what belongs in the file.

    The fork asks for the file and nothing else, and models mostly comply — but
    a wrapping ```markdown fence is the one habit they fall back into, and it
    would otherwise be stored and downloaded as part of the document.
    """
    body = (markdown or "").strip()
    if not body.startswith("```") or not body.endswith("```") or len(body) < 6:
        return body
    first_newline = body.find("\n")
    if first_newline == -1:
        return body
    opener = body[3:first_newline].strip().lower()
    if opener and opener not in {"markdown", "md"}:
        return body
    return body[first_newline + 1 : -3].strip()


def document_word_count(markdown: str) -> int:
    return len((markdown or "").split())


def apply_document_patches(body: str, patches: Sequence[tuple[str, str]]) -> str:
    """Apply ordered find/replace patches to a document body.

    Each patch's target must appear exactly once in the text *as it stands* —
    zero matches means the model is working from a stale idea of the file, and
    several means it has not said which one it meant. Both are mistakes worth
    naming rather than guessing at, so either aborts the whole call: the result
    is a new string, and nothing is written until every patch has landed.

    Patches apply in order and each sees the previous one's result, which is what
    lets a single call make several related changes to the same region.
    """
    if not patches:
        raise MarkdownDocumentError("At least one patch is required.")
    if len(patches) > MAX_DOCUMENT_PATCHES:
        raise MarkdownDocumentError(
            f"Too many patches in one edit (max {MAX_DOCUMENT_PATCHES}). "
            "Split the change across several calls."
        )
    working = body or ""
    for index, (old, new) in enumerate(patches, start=1):
        if not old:
            raise MarkdownDocumentError(
                f"Patch {index}: the text to replace is required. To add content "
                "to the file, anchor the patch on the line it follows."
            )
        if old == new:
            raise MarkdownDocumentError(
                f"Patch {index}: the replacement is identical to the original, "
                "so this patch would do nothing."
            )
        found = working.count(old)
        if found == 0:
            raise MarkdownDocumentError(
                f"Patch {index}: that text is not in the file. Read it back with "
                "read_document to see its current contents, then patch what is "
                "actually there. No patch in this call was applied."
            )
        if found > 1:
            raise MarkdownDocumentError(
                f"Patch {index}: that text appears {found} times, so it is "
                "ambiguous. Include more surrounding lines to make it unique. "
                "No patch in this call was applied."
            )
        working = working.replace(old, new, 1)
    if not working.strip():
        raise MarkdownDocumentError(
            "That edit would empty the file. Overwrite it with write_document if "
            "it should be replaced outright."
        )
    if len(working) > MAX_DOCUMENT_MARKDOWN_CHARS:
        raise MarkdownDocumentError(
            f"That edit would push the file past {MAX_DOCUMENT_MARKDOWN_CHARS:,} "
            "characters. Make it shorter, or split the content into a second file."
        )
    return working


async def _assert_lease(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    turn_id: UUID,
    worker_lease_token: str,
) -> None:
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


async def begin_document(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    turn_id: UUID,
    assistant_message_id: UUID,
    worker_lease_token: str,
    tool_call_id: str,
    title: str,
    filename: str,
) -> tuple[WebChatDocument, bool]:
    """Open a document for streaming.

    Returns ``(document, needs_body)``. A finished document short-circuits on
    tool redelivery, but an unfinished one is reset and rewritten: half a file
    from a worker that died mid-stream is not something to resume from, and the
    fork that produces the body is deterministic enough to simply run again.
    """
    await _assert_lease(
        session,
        conversation_id=conversation_id,
        turn_id=turn_id,
        worker_lease_token=worker_lease_token,
    )
    validated = validate_document_request(title=title, filename=filename)

    existing = await session.scalar(
        select(WebChatDocument).where(
            WebChatDocument.turn_id == turn_id,
            WebChatDocument.tool_call_id == tool_call_id,
        )
    )
    if existing is not None:
        if existing.status == "complete":
            return existing, False
        existing.title = validated.title
        existing.filename = validated.filename
        existing.markdown_content = ""
        existing.size_bytes = 0
        existing.status = "streaming"
        await session.flush()
        return existing, True

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

    document = WebChatDocument(
        conversation_id=conversation_id,
        turn_id=turn_id,
        assistant_message_id=assistant_message_id,
        tool_call_id=tool_call_id,
        title=validated.title,
        filename=validated.filename,
        markdown_content="",
        size_bytes=0,
        status="streaming",
    )
    session.add(document)
    await session.flush()
    return document, True


async def append_document_body(
    session: AsyncSession,
    *,
    document_id: UUID,
    turn_id: UUID,
    worker_lease_token: str,
    chunk: str,
    size_bytes: int,
) -> None:
    """Append one flushed chunk of the streaming body.

    Appends rather than rewrites: a finished document can run to a hundred
    thousand characters, and rewriting the whole column on every flush would
    make the write volume quadratic in the length of the file.
    """
    updated = await session.execute(
        update(WebChatDocument)
        .where(
            WebChatDocument.id == document_id,
            WebChatDocument.status == "streaming",
            select(WebChatTurn.id)
            .where(
                WebChatTurn.id == turn_id,
                WebChatTurn.worker_lease_token == worker_lease_token,
            )
            .exists(),
        )
        .values(
            markdown_content=WebChatDocument.markdown_content + chunk,
            size_bytes=size_bytes,
        )
    )
    if not updated.rowcount:
        raise RunSuperseded()


async def finish_document(
    session: AsyncSession,
    *,
    document_id: UUID,
    turn_id: UUID,
    worker_lease_token: str,
    markdown: str,
    status: str,
) -> WebChatDocument:
    """Write the whole body one last time and settle the status.

    The streamed appends exist for the live preview; this is the authoritative
    write, so a dropped or double-applied chunk cannot leave the stored file
    different from what the model actually produced.
    """
    if status not in DOCUMENT_STATUSES or status == "streaming":
        raise ValueError(f"invalid terminal document status: {status}")
    updated = await session.execute(
        update(WebChatDocument)
        .where(
            WebChatDocument.id == document_id,
            select(WebChatTurn.id)
            .where(
                WebChatTurn.id == turn_id,
                WebChatTurn.worker_lease_token == worker_lease_token,
            )
            .exists(),
        )
        .values(
            markdown_content=markdown,
            size_bytes=document_body_bytes(markdown),
            status=status,
        )
    )
    if not updated.rowcount:
        raise RunSuperseded()
    document = await session.get(WebChatDocument, document_id)
    if document is None:
        raise RunSuperseded()
    await session.refresh(document)
    return document


async def edit_document_body(
    session: AsyncSession,
    *,
    document_id: UUID,
    turn_id: UUID,
    worker_lease_token: str,
    markdown: str,
) -> WebChatDocument:
    """Replace a settled document's body with the patched version.

    The lease checked here is the *editing* turn's, not the one that wrote the
    file: an edit happens turns later, and the write that produced the document
    is long finished. The status is left as it was — patching a truncated file
    does not make it whole, and saying otherwise would be a lie the reader acts
    on.
    """
    updated = await session.execute(
        update(WebChatDocument)
        .where(
            WebChatDocument.id == document_id,
            WebChatDocument.status.in_(READABLE_STATUSES),
            select(WebChatTurn.id)
            .where(
                WebChatTurn.id == turn_id,
                WebChatTurn.worker_lease_token == worker_lease_token,
            )
            .exists(),
        )
        .values(markdown_content=markdown, size_bytes=document_body_bytes(markdown))
    )
    if not updated.rowcount:
        raise RunSuperseded()
    document = await session.get(WebChatDocument, document_id)
    if document is None:
        raise RunSuperseded()
    await session.refresh(document)
    return document


async def supersede_replaced_documents(
    session: AsyncSession, *, conversation_id: UUID, filename: str, keep_id: UUID
) -> list[UUID]:
    """Retire every other readable document sharing this name.

    Called once an overwrite has actually produced a file, never before — the
    point of the whole arrangement is that a write which dies half way through
    leaves the previous version standing. Returns what it retired so the panel
    can drop those rows without waiting for a reload.
    """
    retired = list(
        (
            await session.execute(
                select(WebChatDocument.id).where(
                    WebChatDocument.conversation_id == conversation_id,
                    WebChatDocument.id != keep_id,
                    WebChatDocument.status.in_(READABLE_STATUSES),
                    func.lower(WebChatDocument.filename) == filename.lower(),
                )
            )
        ).scalars()
    )
    if not retired:
        return []
    await session.execute(
        update(WebChatDocument)
        .where(WebChatDocument.id.in_(retired))
        .values(status="superseded")
    )
    return retired


async def retire_failed_overwrite(
    session: AsyncSession, *, document_id: UUID
) -> None:
    """Discard the half-written replacement, leaving the original in place.

    Without this an abandoned overwrite would leave two readable files with one
    name — exactly the silent shadowing the overwrite flag exists to prevent.
    """
    await session.execute(
        update(WebChatDocument)
        .where(WebChatDocument.id == document_id)
        .values(status="superseded")
    )


async def readable_documents(
    session: AsyncSession, *, conversation_id: UUID
) -> list[WebChatDocument]:
    """Every document in the conversation the model may read back, oldest first.

    Not filtered to the current branch: a document the model wrote is a durable
    artifact of the conversation, and it survives compaction of the history
    that produced it. Rereading one is how the model gets its own words back.
    """
    return list(
        (
            await session.execute(
                select(WebChatDocument)
                .where(
                    WebChatDocument.conversation_id == conversation_id,
                    WebChatDocument.status.in_(READABLE_STATUSES),
                )
                .order_by(WebChatDocument.created_at)
            )
        ).scalars()
    )


async def find_name_clash(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    filename: str,
    turn_id: UUID,
    tool_call_id: str,
) -> WebChatDocument | None:
    """The readable document this write would shadow, if there is one.

    A retried tool call must not collide with the document it created on its
    first delivery, so the row belonging to this exact call is excluded — it is
    the same write finishing, not a second one.
    """
    wanted = (filename or "").strip().lower()
    if not wanted:
        return None
    candidates = await readable_documents(session, conversation_id=conversation_id)
    matches = [
        document
        for document in candidates
        if document.filename.lower() == wanted
        and not (
            document.turn_id == turn_id and document.tool_call_id == tool_call_id
        )
    ]
    return matches[-1] if matches else None


async def find_readable_document(
    session: AsyncSession, *, conversation_id: UUID, filename: str
) -> WebChatDocument | None:
    """Resolve a filename to its newest readable document.

    Matching is case-insensitive and tolerates a missing ``.md``, because the
    model is quoting a name back from an earlier tool result rather than
    handling an identifier.
    """
    wanted = (filename or "").strip().lower()
    if not wanted:
        return None
    candidates = await readable_documents(session, conversation_id=conversation_id)
    for name in (wanted, f"{wanted}.md"):
        matches = [
            document
            for document in candidates
            if document.filename.lower() == name
        ]
        if matches:
            return matches[-1]
    return None


# ── Artifacts ────────────────────────────────────────────────────────────────
# A conversation holds files from two directions: ones the agent wrote, and ones
# the user uploaded. They are one shelf to the reader and one namespace to the
# model, but two tables underneath — an upload keeps its bytes in storage and its
# own lifecycle, so it is referenced here, never copied into a document row.

ARTIFACT_ORIGIN_CREATED = "created"
ARTIFACT_ORIGIN_UPLOAD = "upload"


def attachment_kind(media_type: str) -> str:
    """Classify an upload for display and for how it can be read."""
    media_type = (media_type or "").lower()
    if media_type.startswith("image/"):
        return "image"
    if media_type == "application/pdf":
        return "pdf"
    return "text"


@dataclass(frozen=True, slots=True)
class Artifact:
    """One file in the conversation, whichever side of it produced the file."""

    origin: str
    kind: str
    id: UUID
    title: str
    filename: str
    size_bytes: int
    status: str
    turn_id: UUID | None
    assistant_message_id: UUID | None
    media_type: str
    created_at: object = None

    @property
    def is_upload(self) -> bool:
        return self.origin == ARTIFACT_ORIGIN_UPLOAD

    @property
    def readable(self) -> bool:
        return self.status in READABLE_STATUSES or self.is_upload

    def as_dict(self) -> dict:
        return {
            "id": str(self.id),
            "origin": self.origin,
            "kind": self.kind,
            "title": self.title,
            "filename": self.filename,
            "size_bytes": self.size_bytes,
            "status": self.status,
            "media_type": self.media_type,
            "turn_id": str(self.turn_id) if self.turn_id else None,
            "assistant_message_id": (
                str(self.assistant_message_id) if self.assistant_message_id else None
            ),
        }


def document_artifact(document: WebChatDocument) -> Artifact:
    return Artifact(
        origin=ARTIFACT_ORIGIN_CREATED,
        kind="markdown",
        id=document.id,
        title=document.title,
        filename=document.filename,
        size_bytes=document.size_bytes,
        status=document.status,
        turn_id=document.turn_id,
        assistant_message_id=document.assistant_message_id,
        media_type="text/markdown",
        created_at=document.created_at,
    )


def attachment_artifact(attachment: WebChatAttachment) -> Artifact:
    return Artifact(
        origin=ARTIFACT_ORIGIN_UPLOAD,
        kind=attachment_kind(attachment.media_type),
        id=attachment.id,
        title=attachment.original_name,
        filename=attachment.original_name,
        size_bytes=attachment.size_bytes,
        status=attachment.status,
        turn_id=attachment.turn_id,
        assistant_message_id=None,
        media_type=attachment.media_type,
        created_at=attachment.created_at,
    )


async def conversation_artifacts(
    session: AsyncSession, *, conversation_id: UUID
) -> list[Artifact]:
    """Every file in the conversation, oldest first, uploads included.

    Staged uploads — rows with no turn yet — are left out: until the message is
    sent, the file is a composer draft rather than part of the conversation.
    """
    documents = list(
        (
            await session.execute(
                select(WebChatDocument).where(
                    WebChatDocument.conversation_id == conversation_id,
                    # A superseded row is bookkeeping for an overwrite that
                    # happened, not a file in the conversation.
                    WebChatDocument.status != "superseded",
                )
            )
        ).scalars()
    )
    uploads = list(
        (
            await session.execute(
                select(WebChatAttachment).where(
                    WebChatAttachment.conversation_id == conversation_id,
                    WebChatAttachment.turn_id.is_not(None),
                    WebChatAttachment.status == "ready",
                )
            )
        ).scalars()
    )
    artifacts = [document_artifact(row) for row in documents]
    artifacts.extend(attachment_artifact(row) for row in uploads)
    artifacts.sort(key=lambda artifact: (artifact.created_at is None, artifact.created_at))
    return artifacts


async def load_upload(
    session: AsyncSession, *, conversation_id: UUID, attachment_id: UUID
) -> WebChatAttachment | None:
    """Fetch one ready upload of this conversation, by id."""
    return await session.scalar(
        select(WebChatAttachment).where(
            WebChatAttachment.id == attachment_id,
            WebChatAttachment.conversation_id == conversation_id,
            WebChatAttachment.status == "ready",
        )
    )


async def readable_artifacts(
    session: AsyncSession, *, conversation_id: UUID
) -> list[Artifact]:
    return [
        artifact
        for artifact in await conversation_artifacts(
            session, conversation_id=conversation_id
        )
        if artifact.readable
    ]


async def find_artifact(
    session: AsyncSession, *, conversation_id: UUID, filename: str
) -> Artifact | None:
    """Resolve one name against both kinds of file.

    The model is quoting a name back from a manifest or an earlier tool result,
    so matching is case-insensitive and tolerates a missing ``.md``. A document
    the agent wrote wins a tie against an upload of the same name: it is the
    newer artifact in every case where both exist.
    """
    wanted = (filename or "").strip().lower()
    if not wanted:
        return None
    candidates = await readable_artifacts(session, conversation_id=conversation_id)
    for name in (wanted, f"{wanted}.md"):
        matches = [
            artifact for artifact in candidates if artifact.filename.lower() == name
        ]
        if matches:
            written = [artifact for artifact in matches if not artifact.is_upload]
            return (written or matches)[-1]
    return None
