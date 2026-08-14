"""Thread rules for a Quick chat, shared by the runtime, the agent and the evaluator.

A Quick chat is one perpetual conversation per person. When the subject changes a
boundary is drawn in place: the messages stay in one continuous scroll, but the
agent's context restarts from the boundary. Boundaries are drawn from three
directions — the first turn of the chat, the agent noticing a topic break, and
the idle evaluator judging a returning message to be a new subject — so the rules
for what a boundary is and what it hides live here rather than in whichever of
the three was written first.

Everything a standard conversation does is unchanged, and that is enforced in one
place: ``history_floor`` returns 0 for anything that is not a Quick chat.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import update

from smarter_dev.web.chat.conversations import derive_title
from smarter_dev.web.models import WebChatConversation
from smarter_dev.web.models import WebChatThread

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

QUICK_CHAT_MODE = "quick"
STANDARD_CHAT_MODE = "standard"


async def current_thread(
    session: AsyncSession, *, conversation_id: UUID
) -> WebChatThread | None:
    """The thread the conversation is in right now, or None if it has none.

    Threads never nest and never reopen, so "current" is simply the one that
    starts latest. A standard conversation and a Quick chat before its first
    turn both have none.
    """
    return await session.scalar(
        select(WebChatThread)
        .where(WebChatThread.conversation_id == conversation_id)
        .order_by(WebChatThread.start_sequence.desc())
        .limit(1)
    )


async def history_floor(
    session: AsyncSession, *, conversation: WebChatConversation
) -> int:
    """The lowest ``WebChatMessage.sequence`` the agent is allowed to read.

    This is the whole of the threading rule as the runtime sees it. 0 means "read
    everything", which is what every standard conversation gets — so a chat that
    was never a Quick chat cannot be affected by a stray thread row.
    """
    if conversation.chat_mode != QUICK_CHAT_MODE:
        return 0
    thread = await current_thread(session, conversation_id=conversation.id)
    if thread is None:
        return 0
    return thread.start_sequence


async def open_thread(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    start_sequence: int,
    title: str | None,
    origin: str,
    reason: str | None,
) -> WebChatThread | None:
    """Draw a boundary at ``start_sequence``; return None if one is already there.

    A repeat call at the same sequence is the worker retrying a turn it already
    partly processed, not a mistake, so it is a no-op rather than an error. The
    ``uq_web_chat_thread_start`` constraint is the backstop underneath this
    check; turns are serialized per conversation, so the check itself is what
    normally catches the retry.

    The conversation's context counters are reset through this session rather
    than by mutating a conversation object the caller owns.
    """
    existing = await session.scalar(
        select(WebChatThread).where(
            WebChatThread.conversation_id == conversation_id,
            WebChatThread.start_sequence == start_sequence,
        )
    )
    if existing is not None:
        return None
    highest_sequence = await session.scalar(
        select(func.max(WebChatThread.sequence)).where(
            WebChatThread.conversation_id == conversation_id
        )
    )
    thread = WebChatThread(
        conversation_id=conversation_id,
        sequence=(highest_sequence or 0) + 1,
        start_sequence=start_sequence,
        title=title,
        origin=origin,
        reason=reason,
    )
    session.add(thread)
    # The context meter is rewritten from the real request size on the next
    # primary call, but leaving it stale would make it lie at exactly the moment
    # the person is watching the divider appear. Bumping context_revision also
    # re-keys compaction, so the new thread's fold cannot collide with the
    # reservation key the old thread's fold already used.
    await session.execute(
        update(WebChatConversation)
        .where(WebChatConversation.id == conversation_id)
        .values(
            current_context_tokens=0,
            context_state=[],
            context_revision=WebChatConversation.context_revision + 1,
        )
    )
    await session.commit()
    return thread


def derive_thread_title(content: str) -> str:
    """The divider's label: the opening of the message that started the thread.

    Same rule as a conversation's stand-in title, deliberately — a thread is a
    subject, and a subject is named the way a conversation is.
    """
    return derive_title(content)
