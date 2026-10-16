"""Say once, per guild, that a moderation subsystem is skipping that guild.

The content filters and the spam engine both return early when a guild has no
``moderation_filter_configs`` row or has the subsystem switched off. That early
return used to be silent, so a guild that had never been configured looked
exactly like a guild with no spam (#168). Logging it on every message would
drown the bot's log during a raid, so each subsystem keeps one
:class:`ModerationConfigSkipLog` that logs a guild's skip reason only when it
changes, and forgets the guild once the subsystem runs for it again.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

SKIP_REASON_NO_CONFIG = "has no moderation_filter_configs row"


def skip_reason_disabled(toggle_name: str) -> str:
    return f"has {toggle_name} switched off"


class ModerationConfigSkipLog:
    """Remembers which guilds a subsystem has already reported skipping."""

    def __init__(self, subsystem: str) -> None:
        self._subsystem = subsystem
        self._reported_reason_by_guild: dict[str, str] = {}

    def skipped(self, guild_id: str, reason: str) -> None:
        """Log at INFO unless this guild was already reported for ``reason``."""
        if self._reported_reason_by_guild.get(guild_id) == reason:
            return
        self._reported_reason_by_guild[guild_id] = reason
        logger.info(
            "%s: skipping guild %s, which %s.",
            self._subsystem,
            guild_id,
            reason,
        )

    def running(self, guild_id: str) -> None:
        """The subsystem ran for this guild, so a later skip is news again."""
        self._reported_reason_by_guild.pop(guild_id, None)

    def reset(self) -> None:
        self._reported_reason_by_guild.clear()
