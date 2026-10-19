"""Switching the proactive bot on or off for a channel from the web.

Every web page that toggles a channel goes through
:func:`set_proactive_channel_enabled`, so switching one on wakes the guild
agent to get its bearings, as ``/proactive on`` does.
"""

from __future__ import annotations

import logging
from datetime import UTC
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.exception_logging import log_exception
from smarter_dev.shared.proactive_channel_wake import request_channel_enabled_wake
from smarter_dev.web.crud import get_proactive_channel_settings
from smarter_dev.web.crud import upsert_proactive_channel_settings

logger = logging.getLogger(__name__)


async def set_proactive_channel_enabled(
    session: AsyncSession,
    redis,
    *,
    guild_id: str,
    channel_id: str,
    enabled: bool,
) -> bool:
    """Save the channel's on/off switch and commit the session.

    Only a change from off (or no row) to on wakes the agent, and only after
    the commit, so the bot reads the channel as enabled when the wake lands.
    Returns whether a wake was requested. A failed request is logged, not
    raised: the setting is saved, and the agent still finds the channel on its
    next message there.
    """
    existing = await get_proactive_channel_settings(session, guild_id, channel_id)
    was_enabled = existing is not None and existing.enabled
    await upsert_proactive_channel_settings(
        session,
        guild_id=guild_id,
        channel_id=channel_id,
        enabled=enabled,
        # The agent owns its watch instructions; a toggle never touches them.
        watch_addendum=existing.watch_addendum if existing else "",
    )
    await session.commit()
    if not enabled or was_enabled:
        return False
    try:
        await request_channel_enabled_wake(
            redis, guild_id=guild_id, channel_id=channel_id, now=datetime.now(UTC)
        )
    except Exception:  # noqa: BLE001 — the saved setting must stand
        log_exception(
            logger,
            "proactive channel-enabled wake request failed guild=%s channel=%s",
            guild_id,
            channel_id,
        )
        return False
    return True
