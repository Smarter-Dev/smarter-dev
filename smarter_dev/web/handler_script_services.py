"""The host services one fire lends to its sandboxed script.

A script never reaches Discord, the worker queue or the database itself: it
calls external functions the fire injects, and each of those is bound host-side
to THIS fire's guild, handler and channel. That binding is the security
property — a script supplies a target user and a reason, never the guild whose
history it reads or the moderator name that lands in a permanent audit row.

:class:`HandlerTimerScheduler` is the one service both tiers share: a
script-armed timer re-fires the same handler one generation deeper, and the
tier's :class:`~smarter_dev.web.handler_recurrence.RecurringFireChain` says what
that fire's payload is. :class:`AdminScriptServices` is what the admin fire adds
on top. Gathering them into objects is what keeps the fire jobs
readable: a job builds these once from the facts it just loaded and hands the
methods to the runtime, instead of carrying closures over the same variables.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID
from uuid import uuid4

from skrift.workers import submit as worker_submit

from smarter_dev.shared.database import get_db_session_context
from smarter_dev.web.crud import GuildRulesConfigOperations
from smarter_dev.web.crud import ModerationActionOperations
from smarter_dev.web.guild_rules import parse_guild_rules
from smarter_dev.web.models import ModerationAction
from smarter_dev.web.moderation_gateway.config import load_incident_report_channel

if TYPE_CHECKING:
    from smarter_dev.web.admin_actions import AdminActor
    from smarter_dev.web.handler_recurrence import RecurringFireChain

logger = logging.getLogger(__name__)

_mod_action_ops = ModerationActionOperations()
_guild_rules_ops = GuildRulesConfigOperations()


@dataclass(frozen=True)
class HandlerTimerScheduler:
    """Arms durable one-shot re-fires of the handler that is currently firing.

    Holds only which handler is firing, where, and how deep the current fire
    is; the tier's chain owns the payload that carries those facts.
    """

    chain: RecurringFireChain
    handler_id: str
    channel_id: str = ""
    chain_depth: int = 0

    async def schedule_timer(self, fire_at: datetime, refire_context: dict) -> None:
        """Arm a durable one-shot re-fire of this handler.

        The re-fire is caused BY this fire, so it descends one generation. It is
        still enqueued (depth is enforced at the dispatch choke point, and the
        arming window bounds a self-deferring handler); carrying the depth is
        what makes anything that re-fire DISPATCHES get refused past
        MAX_CHAIN_DEPTH.
        """
        await worker_submit(
            self.chain.build_fire_payload(
                self.handler_id,
                refire_context,
                chain_depth=self.chain_depth + 1,
                channel_id=self.channel_id,
            ),
            scheduled_for=fire_at,
            job_id=uuid4().hex,
        )


def _mod_action_row(action: ModerationAction) -> dict:
    """Map a ModerationAction to the list_mod_actions row (the §3.5/§3.7 shape).

    channel_id/trigger_message_id come straight off the row (either may be None)
    so a script can build "Jump To Action" links; created_at is ISO-8601."""
    return {
        "action_type": action.action_type,
        "reason": action.reason,
        "source": action.source,
        "moderator_username": action.moderator_username,
        "duration_seconds": action.duration_seconds,
        "channel_id": action.channel_id,
        "trigger_message_id": action.trigger_message_id,
        "created_at": action.created_at.isoformat() if action.created_at else None,
    }


@dataclass(frozen=True)
class AdminScriptServices:
    """Everything one admin fire's script may reach, bound to that fire."""

    handler_id: UUID
    handler_name: str
    guild_id: str
    channel_id: str
    chain_depth: int
    actor: AdminActor

    async def read_mod_actions(self, target_user_id: str, limit: int) -> list[dict]:
        """This guild's recent moderation history for one member."""
        async with get_db_session_context() as session:
            actions = await _mod_action_ops.get_actions_for_user(
                session, self.guild_id, str(target_user_id), limit=int(limit)
            )
            return [_mod_action_row(action) for action in actions]

    async def count_warns(self, target_user_id: str) -> int:
        """This member's authoritative warn count in this guild.

        The count is host-side because a script tallying warns itself from
        ``read_mod_actions`` would silently undercount: that read is clamped to
        50 rows and returns every action type, so a heavy user's warn history
        truncates and the "third strike" escalation quietly never fires. The
        warn row itself is written by the moderation gateway.
        """
        async with get_db_session_context() as session:
            return await _mod_action_ops.count_warns_for_user(
                session, self.guild_id, str(target_user_id)
            )

    async def incident_report_channel(self) -> str | None:
        """Where this guild's moderation incident reports go."""
        async with get_db_session_context() as session:
            return await load_incident_report_channel(session, self.guild_id)

    async def read_rules(self) -> list[dict]:
        """This guild's numbered rules, parsed exactly as ``/rule`` parses them.

        Sharing ``parse_guild_rules`` is what makes a handler and the command
        number the rules identically. A guild with no rules row reads as [].
        """
        async with get_db_session_context() as session:
            config = await _guild_rules_ops.get_config(session, self.guild_id)
        markdown = config.rules_markdown if config is not None else None
        return [
            {"number": rule.index, "title": rule.title, "text": rule.body}
            for rule in parse_guild_rules(markdown)
        ]
