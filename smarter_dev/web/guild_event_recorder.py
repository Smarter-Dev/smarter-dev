"""Worker/web-tier entry point for the short-term guild event log.

The handler runtime's own moderation actions (``web/admin_actions.py``) and the
messages and DMs a handler emits (``web/handler_emitter.py``) are things the
bot's account did just as much as a slash command is, so they belong in the same
rolling hour the chat agent reads. This tier has no bot object; it uses the
process-wide :func:`~smarter_dev.shared.redis_client.get_redis_client` handle,
which is ``decode_responses=True`` — the other half of the mismatch
:mod:`smarter_dev.shared.guild_event_log` decodes defensively for.

Like the bot-side adapter, this NEVER raises: a handler's send must not fail
because the bot couldn't write down that it sent it.
"""

from __future__ import annotations

import logging
from datetime import datetime

from smarter_dev.shared.guild_event_log import GuildEvent
from smarter_dev.shared.guild_event_log import append_event
from smarter_dev.shared.redis_client import get_redis_client

logger = logging.getLogger(__name__)

__all__ = ["record_guild_event"]


async def record_guild_event(event: GuildEvent, *, now: datetime | None = None) -> None:
    """Best-effort write of ``event`` to its guild's rolling hour of memory."""
    try:
        await append_event(get_redis_client(), event, now=now)
    except Exception:  # noqa: BLE001 — recording must never break the action
        logger.debug("guild event recording failed", exc_info=True)
