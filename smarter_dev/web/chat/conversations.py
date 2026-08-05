"""Naming rules for a Chat conversation, shared by the API and the agent tool.

A conversation's title is written from three directions — the first message's
opening words, the agent's own name for it, and the owner renaming it in the
rail — so the rules for what a title may be live in one place rather than in
whichever of the three happened to be written first.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import update

from smarter_dev.web.models import WebChatConversation

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

MAX_TITLE_CHARS = 120
# The rail truncates with an ellipsis, so a long title is not broken, just
# unhelpful. This is the point past which it stops being a name at all.
AUTO_TITLE_CHARS = 79
UNNAMED_TITLE = "New Chat"


class ConversationTitleError(ValueError):
    """A proposed title is empty or otherwise unusable."""


def normalize_title(value: str | None) -> str:
    """Collapse a proposed title to a single clean line, or reject it.

    Newlines and control characters would break the rail's single-line rows and
    the page title, so they are folded to spaces rather than refused — the only
    hard failure is a title with nothing in it.
    """
    stripped = "".join(
        " " if ord(char) < 32 or ord(char) == 127 else char for char in (value or "")
    )
    cleaned = " ".join(stripped.split())
    if not cleaned:
        raise ConversationTitleError("A title is required.")
    if len(cleaned) > MAX_TITLE_CHARS:
        cleaned = cleaned[:MAX_TITLE_CHARS].rstrip() + "…"
    return cleaned


async def name_if_unnamed(
    session: AsyncSession, *, conversation_id: UUID, title: str
) -> bool:
    """Give an unnamed conversation a name; report whether it took.

    The owner can rename a conversation from the rail while a turn is still
    running, so the agent's name is only ever applied to a conversation nobody
    has named — the check and the write are one statement so the two cannot
    interleave. Returns False when it was already named.
    """
    applied = await session.execute(
        update(WebChatConversation)
        .where(
            WebChatConversation.id == conversation_id,
            WebChatConversation.title_is_custom.is_(False),
        )
        .values(title=title, title_is_custom=True)
    )
    await session.commit()
    return bool(applied.rowcount)


def derive_title(content: str) -> str:
    """The stand-in title: the opening of what the user actually asked."""
    opening = " ".join((content or "").split())
    if len(opening) > AUTO_TITLE_CHARS:
        return opening[:AUTO_TITLE_CHARS] + "…"
    return opening or UNNAMED_TITLE
