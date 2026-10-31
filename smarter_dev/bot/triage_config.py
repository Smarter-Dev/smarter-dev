"""The staff-ping triage's guild config, read fresh from the database.

The config is the ``triage`` section of the guild's moderation policy
(#241). Nothing is cached: the monitor reads it when a message mentions a
role, and each destructive triage tool reads it again right before it acts
(:func:`tool_refusal`), so switching triage off or removing a tool stops a
run that is already going.

``off`` does not run, ``evaluate`` runs with no destructive tool, ``enforce``
with the policy's ``enabled_tools`` (an empty list is no destructive tool).
The report goes to ``triage.response_channel_id``, or to the channel the ping
came from when that is empty; the protected roles are the policy's. The caps
on each tool (an hour, 50 messages, one delete) are in the tools, not the
policy.

A guild without a policy has no triage and no extra protected roles: the old
``moderation_configs`` table and the filter config's staff roles were dropped
in #242.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from smarter_dev.shared import moderation_policy as mp
from smarter_dev.shared.database import get_db_session_context
from smarter_dev.web.moderation_policy_store import read_policy

logger = logging.getLogger(__name__)

DESTRUCTIVE_TOOLS = ("timeout", "purge", "delete")


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
    """The guild's config when triage is active, else None (always None
    without a policy). Raises on DB errors."""
    async with get_db_session_context() as session:
        stored = await read_policy(session, str(guild_id))
    if stored is None:
        return None
    return triage_from_policy(str(guild_id), stored.document)


def triage_from_policy(
    guild_id: str, document: mp.PolicyDocument
) -> TriageConfig | None:
    """The triage config a policy describes; None when its triage is off."""
    triage = document.triage
    if triage.mode == mp.OFF or not document.controls.evaluate:
        return None
    enforcing = triage.mode == mp.ENFORCE and document.controls.enforce
    return TriageConfig(
        guild_id=guild_id,
        monitored_role_ids=frozenset(triage.monitored_role_ids),
        instructions=triage.instructions,
        enabled_tools=enabled_destructive_tools(triage.enabled_tools)
        if enforcing
        else (),
        context_message_limit=triage.context_message_limit,
        response_channel_id=triage.response_channel_id,
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
    """The guild's extra protected roles (its policy's ``protected_role_ids``,
    none without a policy), on top of the built-in staff rule; raises on DB
    errors."""
    async with get_db_session_context() as session:
        stored = await read_policy(session, str(guild_id))
    if stored is None:
        return ()
    return tuple(stored.document.protected_role_ids)
