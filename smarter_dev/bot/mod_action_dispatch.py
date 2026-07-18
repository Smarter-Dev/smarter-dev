"""Bot-side fire of the synthetic ``mod_action`` handler trigger.

A ``ModerationAction`` row is written from several bot-side sites (the /warn and
/timeout commands, the AI triage tools, and the audit-log backfill). The bot has
no worker/redis access, so — like the member events — it reaches the handler
worker queue only by POSTing ``/handlers/dispatch``. After each action is
committed, its writer calls :func:`dispatch_mod_action`, which builds the §3.5
context and rides the SAME dispatch endpoint + active-channels cache guard the
member events use (guild-scoped, no home channel). See
docs/v2/feature-parity/automated-and-command-moderation.md §3.5.

This is best-effort: a dispatch failure must never break the moderation command
that recorded the action, so :func:`dispatch_mod_action` never raises.
"""

from __future__ import annotations

import logging
from typing import Any

from smarter_dev.bot.plugins.handler_events import _dispatch
from smarter_dev.web.models import ModerationAction

logger = logging.getLogger(__name__)


def build_mod_action_context(action: ModerationAction) -> dict[str, Any]:
    """Map a ``ModerationAction`` row to the §3.5 ``mod_action`` trigger context.

    ``channel_id`` / ``trigger_message_id`` come straight off the row (either may
    be None) so a mod-log-formatter can build "Jump To Action" links; ``created_at``
    is ISO-8601 (None only for an unflushed row)."""
    return {
        "trigger_type": "mod_action",
        "action_type": action.action_type,
        "target_user_id": action.target_user_id,
        "target_username": action.target_username,
        "moderator_user_id": action.moderator_user_id,
        "moderator_username": action.moderator_username,
        "reason": action.reason,
        "duration_seconds": action.duration_seconds,
        "source": action.source,
        "channel_id": action.channel_id,
        "trigger_message_id": action.trigger_message_id,
        "created_at": action.created_at.isoformat() if action.created_at else None,
    }


async def dispatch_mod_action(action: ModerationAction) -> None:
    """Best-effort fire of ``mod_action`` for a just-recorded moderation action.

    Dispatched guild-wide with ``channel_id=""`` (a mod action has no home
    channel), so it matches every guild-wide mod-log handler regardless of scope.
    NEVER raises into the caller: a dispatch failure is logged, not propagated, so
    a mod command's success never depends on the mod-log fire landing.
    """
    try:
        context = build_mod_action_context(action)
        await _dispatch("", str(action.guild_id), "mod_action", context)
    except Exception:  # noqa: BLE001 — dispatch must never break the mod command
        logger.debug("mod_action dispatch failed", exc_info=True)
