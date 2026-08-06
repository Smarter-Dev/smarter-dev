"""Bot-tier entry point for the short-term guild event log.

The capture sites (mod-action dispatch, the AI triage tools, the spam and
attachment filters, the announcement services) all have the bot object in scope
and none of them own a Redis handle, so this adapter reads the one the client
stashed at ``bot.d["chat_memory_redis"]`` — the ``decode_responses=False``
handle, which is exactly why :mod:`smarter_dev.shared.guild_event_log` decodes
defensively.

Like :func:`smarter_dev.bot.mod_action_dispatch.dispatch_mod_action`, this NEVER
raises. A moderation command, a filter delete or a scheduled announcement must
not fail because the bot couldn't write down that it happened.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from typing import Protocol

from smarter_dev.shared.guild_event_log import GuildEvent
from smarter_dev.shared.guild_event_log import append_event

logger = logging.getLogger(__name__)

__all__ = ["record_guild_event"]


class EventRecordingBot(Protocol):
    """The one thing this adapter needs of the bot: its service bag."""

    d: dict[str, Any]


async def record_guild_event(
    bot: EventRecordingBot, event: GuildEvent, *, now: datetime | None = None
) -> None:
    """Best-effort write of ``event`` to its guild's rolling hour of memory.

    A no-op when chat memory has no Redis handle: ``client.py`` sets
    ``chat_memory_redis`` to ``None`` when the connection check fails at
    startup, and the bot is expected to keep running without its memory.
    """
    try:
        chat_memory_redis = bot.d.get("chat_memory_redis")
        if chat_memory_redis is None:
            return
        await append_event(chat_memory_redis, event, now=now)
    except Exception:  # noqa: BLE001 — recording must never break the action
        logger.debug("guild event recording failed", exc_info=True)
