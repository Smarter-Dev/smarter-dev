"""One person's opt-out from the AI assistant (#92), on the purge's block list.

Opting out puts the person on the block list with ``source='opt_out'``: both
runtimes then read their messages as ``[BLOCKED BY USER]`` from their next
refresh (60 seconds), exactly as for a purged user. It deletes nothing the
assistant already holds; that is a deletion request.

Opting back in removes only an ``opt_out`` row and records the moment in
:class:`~smarter_dev.web.models.ChatBotOptIn`, so it applies to new messages
only: the runtimes keep hiding the person's messages written before then. A
``purge`` row is never removed here; a deleted user stays opted out.

Every change bumps the block list's revision. Only the Discord user id and
timestamps are stored, never a reason.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.chat_bot_purge import BLOCK_SOURCE_PURGE
from smarter_dev.web.chat_bot_purge import _insert
from smarter_dev.web.chat_bot_purge import add_blocked_user
from smarter_dev.web.chat_bot_purge import block_list_revision
from smarter_dev.web.chat_bot_purge import bump_block_list_revision
from smarter_dev.web.models import ChatBotBlockedUser
from smarter_dev.web.models import ChatBotOptIn

BLOCK_SOURCE_OPT_OUT = "opt_out"


class OptOutState(BaseModel):
    """What ``/privacy`` shows: whether the person is opted out, and why.

    ``source`` is ``opt_out`` when they can opt back in, ``purge`` when the
    opt-out comes from a deletion and stays, and None when not opted out.
    """

    opted_out: bool
    source: Literal["opt_out", "purge"] | None
    revision: int


async def read_opt_out(session: AsyncSession, discord_user_id: str) -> OptOutState:
    source = await session.scalar(
        select(ChatBotBlockedUser.source).where(
            ChatBotBlockedUser.discord_user_id == discord_user_id
        )
    )
    revision = await block_list_revision(session)
    if source is None:
        return OptOutState(opted_out=False, source=None, revision=revision)
    # Any other source keeps the person blocked for good, like a purge.
    kind = BLOCK_SOURCE_OPT_OUT if source == BLOCK_SOURCE_OPT_OUT else BLOCK_SOURCE_PURGE
    return OptOutState(opted_out=True, source=kind, revision=revision)


async def opt_out(session: AsyncSession, discord_user_id: str) -> OptOutState:
    """Opt the person out; a no-op when they already are, for either reason."""
    await add_blocked_user(session, discord_user_id, source=BLOCK_SOURCE_OPT_OUT)
    return await read_opt_out(session, discord_user_id)


async def opt_in(
    session: AsyncSession, discord_user_id: str, *, now: datetime | None = None
) -> OptOutState:
    """Opt the person back in, for messages written from now on.

    Removes only an ``opt_out`` row. A purged person stays opted out and the
    returned state says so; someone not opted out is left as they are.
    """
    removed = await session.execute(
        delete(ChatBotBlockedUser).where(
            ChatBotBlockedUser.discord_user_id == discord_user_id,
            ChatBotBlockedUser.source == BLOCK_SOURCE_OPT_OUT,
        )
    )
    if removed.rowcount:
        read_from = now or datetime.now(UTC)
        insert = _insert(session)
        await session.execute(
            insert(ChatBotOptIn)
            .values(discord_user_id=discord_user_id, read_from=read_from)
            .on_conflict_do_update(
                index_elements=["discord_user_id"], set_={"read_from": read_from}
            )
        )
        await bump_block_list_revision(session)
    return await read_opt_out(session, discord_user_id)
