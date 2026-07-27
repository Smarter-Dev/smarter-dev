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

from smarter_dev.bot.plugins.handler_events import _dispatch

# The context shape lives in the web tier's handler_dispatch because the worker's
# handler-issued ``warn_user`` builds the same context for the same trigger, and
# the web package must never import from the bot. Re-exported here so the bot-side
# writers (and their tests) keep importing it from the module they always have.
from smarter_dev.web.handler_dispatch import build_mod_action_context
from smarter_dev.web.models import ModerationAction

logger = logging.getLogger(__name__)

__all__ = ["build_mod_action_context", "dispatch_mod_action"]


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
