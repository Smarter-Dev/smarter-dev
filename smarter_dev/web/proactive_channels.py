"""Switching the proactive bot on or off for a channel from the web.

Every web page that toggles a channel goes through
:func:`set_proactive_channel_enabled`, so switching one on wakes the guild
agent to get its bearings, as ``/proactive on`` does.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.exception_logging import log_exception
from smarter_dev.shared.proactive_channel_wake import request_channel_enabled_wake
from smarter_dev.web.crud import set_proactive_channel_enabled_flag

logger = logging.getLogger(__name__)

# The shared Redis client has no socket timeout; a Redis that accepts the
# connection but never answers must not hold the page after the commit.
WAKE_REQUEST_TIMEOUT_SECONDS = 2.0


async def set_proactive_channel_enabled(
    session: AsyncSession,
    redis,
    *,
    guild_id: str,
    channel_id: str,
    enabled: bool,
) -> bool:
    """Save the channel's on/off switch and commit the session.

    Only the caller whose write turned the channel on wakes the agent (the
    database decides, so a double-click wakes once), and only after the
    commit, so the bot reads the channel as enabled when the wake lands.
    Returns whether a wake was requested. A failed or stalled request is
    logged, not raised: the setting is saved, and the agent still finds the
    channel on its next message there.
    """
    turned_on = await set_proactive_channel_enabled_flag(
        session, guild_id=guild_id, channel_id=channel_id, enabled=enabled
    )
    await session.commit()
    if not turned_on:
        return False
    try:
        await asyncio.wait_for(
            request_channel_enabled_wake(
                redis, guild_id=guild_id, channel_id=channel_id, now=datetime.now(UTC)
            ),
            timeout=WAKE_REQUEST_TIMEOUT_SECONDS,
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
