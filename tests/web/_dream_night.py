"""One guild's dream, driven the way the agent-run dream drives it.

The service reads the inputs, runs its model and posts the result; this runs a
stub in the model's place so tests can drive ``prepare_guild_dream`` and
``commit_guild_dream`` in one call.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.chat_memory_dream import GuildDreamResult
from smarter_dev.web.chat_memory_dream import commit_guild_dream
from smarter_dev.web.chat_memory_dream import prepare_guild_dream


async def dream_one_night(
    session: AsyncSession,
    *,
    guild_id: str,
    cutoff: datetime,
    dreamed_at: datetime,
    agent: Any,
) -> GuildDreamResult:
    inputs = await prepare_guild_dream(
        session, guild_id=guild_id, cutoff=cutoff, dreamed_at=dreamed_at
    )
    if isinstance(inputs, GuildDreamResult):
        return inputs
    result = await agent.run(user_prompt=inputs.user_message, deps=inputs.context)
    return await commit_guild_dream(
        session, inputs, result.output, dreamed_at=dreamed_at
    )
