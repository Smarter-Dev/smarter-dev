"""The staff-ping triage's guild config, read fresh from the database.

The bot used to cache ``moderation_configs`` at startup, and the admin save
refreshed a cache in the web process, not the bot's, so a change reached the
bot only on restart. Nothing is cached now: the monitor reads the row when a
message mentions a role, and each destructive triage tool reads it again right
before it acts (:func:`tool_refusal`), so switching triage off or removing a
tool stops a run that is already going.

An empty ``enabled_tools`` means no destructive tools. Only the column default
for a new row names all three.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from smarter_dev.shared.database import get_db_session_context
from smarter_dev.web.crud import ModerationConfigOperations
from smarter_dev.web.crud import ModerationFilterConfigOperations

logger = logging.getLogger(__name__)

DESTRUCTIVE_TOOLS = ("timeout", "purge", "delete")
DEFAULT_CONTEXT_MESSAGE_LIMIT = 25

_config_ops = ModerationConfigOperations()
_filter_config_ops = ModerationFilterConfigOperations()


@dataclass(frozen=True, slots=True)
class TriageConfig:
    """One guild's active triage config, as plain values."""

    guild_id: str
    monitored_role_ids: frozenset[str]
    instructions: str
    enabled_tools: tuple[str, ...]
    context_message_limit: int
    response_channel_id: str | None


def enabled_destructive_tools(raw) -> tuple[str, ...]:
    """The destructive tools a stored ``enabled_tools`` value names.

    None, ``[]``, a non-list and unknown names all contribute nothing.
    """
    if not isinstance(raw, list | tuple):
        return ()
    named = {str(tool) for tool in raw}
    return tuple(tool for tool in DESTRUCTIVE_TOOLS if tool in named)


async def load_triage_config(guild_id: str) -> TriageConfig | None:
    """The guild's config when triage is active, else None. Raises on DB errors."""
    async with get_db_session_context() as session:
        config = await _config_ops.get_config(session, str(guild_id))
        if config is None or not config.is_active:
            return None
        return TriageConfig(
            guild_id=str(guild_id),
            monitored_role_ids=frozenset(
                str(role_id) for role_id in (config.monitored_role_ids or [])
            ),
            instructions=config.instructions or "",
            enabled_tools=enabled_destructive_tools(config.enabled_tools),
            context_message_limit=config.context_message_limit
            or DEFAULT_CONTEXT_MESSAGE_LIMIT,
            response_channel_id=config.response_channel_id,
        )


async def tool_refusal(guild_id: str, tool: str) -> str | None:
    """Why ``tool`` may not run for this guild right now, or None when it may.

    Read from the database at call time. A failed read refuses.
    """
    try:
        config = await load_triage_config(guild_id)
    except Exception as error:  # noqa: BLE001 — unknown config never acts
        logger.warning(
            "Could not re-read moderation config for guild %s before %s: %s",
            guild_id,
            tool,
            type(error).__name__,
        )
        return "Moderation config could not be read; no action taken."
    if config is None:
        return "AI moderation has been switched off for this server; no action taken."
    if tool not in config.enabled_tools:
        return f"The {tool} action is not enabled for this server; no action taken."
    return None


async def staff_exempt_role_ids(guild_id: str) -> tuple[str, ...]:
    """The guild's configured extra staff roles; raises on DB errors.

    They live on the filter config (the content filter, spam engine and spam
    ring read the same column), so triage protects the same people they do.
    """
    async with get_db_session_context() as session:
        config = await _filter_config_ops.get_config(session, str(guild_id))
        if config is None:
            return ()
        return tuple(str(role_id) for role_id in config.staff_exempt_role_ids or [])
