"""The single fan-out point for a handler trigger, callable from anywhere.

Every handler fire — standard tier and admin tier — is enqueued here, behind the
same gates: the per-guild member-events raid window, the message-activity
enrichment, the per-handler fire windows, and the bot-message opt-in. Keeping
ONE implementation is what makes those rails real rails; a second fan-out path
that skipped a window would quietly widen every cap.

This used to live inline in ``api_native/handlers.py``'s ``dispatch_event``, so
the ONLY way to fire a trigger was an HTTP POST from the bot. The worker needs
it too: a handler-issued ``warn_user`` records a ``ModerationAction`` and must
fire the synthetic ``mod_action`` trigger exactly like ``/warn`` does, from
inside the fire job with no HTTP round trip. ``dispatch_event`` is now a thin
wrapper over :func:`dispatch_handler_event`.
"""

from __future__ import annotations

import logging
from datetime import UTC
from datetime import datetime
from typing import Any

from skrift.workers import submit as worker_submit
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.redis_client import get_redis_client
from smarter_dev.web.admin_handlers_jobs import AdminHandlerFirePayload
from smarter_dev.web.handler_caps import ADMIN_FIRES_PER_MIN
from smarter_dev.web.handler_caps import DM_FIRES_PER_AUTHOR_PER_MIN
from smarter_dev.web.handler_caps import GUILD_MEMBER_EVENTS_PER_MIN
from smarter_dev.web.handler_caps import MAX_CHAIN_DEPTH
from smarter_dev.web.handler_caps import WindowedLimiter
from smarter_dev.web.handler_caps import dm_trigger_author_key
from smarter_dev.web.handler_caps import fires_per_min_for_trigger
from smarter_dev.web.handler_caps import guild_member_events_key
from smarter_dev.web.handler_caps import handler_fire_key
from smarter_dev.web.handlers_jobs import HandlerFirePayload
from smarter_dev.web.member_activity import activity_facts
from smarter_dev.web.member_activity import get_activity
from smarter_dev.web.member_activity import record_activity
from smarter_dev.web.models import ADMIN_ONLY_TRIGGER_TYPES
from smarter_dev.web.models import ADMIN_SYNTHETIC_TRIGGER_TYPES
from smarter_dev.web.models import AdminHandler
from smarter_dev.web.models import ChannelHandler
from smarter_dev.web.models import ModerationAction

logger = logging.getLogger(__name__)

# Admin-only triggers that are NOT guild-shaped member lifecycle events, so the
# per-guild raid window must not gate them: thread_create and message_edit
# (both dispatched with a real home channel — the thread's parent / the edited
# message's channel) and dm_message (its own per-author window). Excluded from
# MEMBER_EVENT_TRIGGERS.
_NON_MEMBER_ADMIN_TRIGGERS = ("thread_create", "dm_message", "message_edit")

# The guild-shaped member lifecycle triggers: dispatched with ``channel_id=""``
# (a member event has no channel), matched admin-only by guild + trigger, and
# gated by the per-guild ``GUILD_MEMBER_EVENTS_PER_MIN`` raid window. dm_message is
# deliberately NOT here — it is guild-scoped in dispatch but has its OWN
# per-(handler, author) window (see GUILD_SCOPED_ADMIN_TRIGGERS), not the raid gate.
MEMBER_EVENT_TRIGGERS = tuple(
    trigger
    for trigger in ADMIN_ONLY_TRIGGER_TYPES
    if trigger not in _NON_MEMBER_ADMIN_TRIGGERS
)

# Admin triggers dispatched with NO home channel (``channel_id=""``), so the
# admin scope check is bypassed and the handler surfaces as a (guild_id, trigger)
# guild-trigger in active-channels: the member_* events, dm_message (a DM has no
# guild channel to scope against), and the synthetic mod_action trigger (fired
# guild-wide after a ModerationAction commit; NOT under the member-events raid
# gate — MEMBER_EVENT_TRIGGERS excludes it — so a mass-ban wave is bounded only by
# the per-handler ADMIN_FIRES_PER_MIN window).
GUILD_SCOPED_ADMIN_TRIGGERS = (
    MEMBER_EVENT_TRIGGERS + ("dm_message",) + ADMIN_SYNTHETIC_TRIGGER_TYPES
)


# The name reported to the bot when a dispatch is cut for depth. A silently
# declined dispatch reads as "my handler randomly stopped working", so the cut
# is both logged at warning level and named in the endpoint's JSON.
CHAIN_DEPTH_DECLINE_REASON = "chain_depth_exceeded"


def chain_depth_exceeded(chain_depth: int) -> bool:
    """Whether a dispatch arriving at ``chain_depth`` must be cut.

    Enforcement lives in :func:`dispatch_handler_event` (the single choke point
    ahead of BOTH tiers' ``worker_submit``). This predicate exists so the HTTP
    endpoint can NAME the reason in its response without re-deriving the rule.
    """
    return chain_depth > MAX_CHAIN_DEPTH


def build_mod_action_context(action: ModerationAction) -> dict[str, Any]:
    """Map a ``ModerationAction`` row to the §3.5 ``mod_action`` trigger context.

    ``channel_id`` / ``trigger_message_id`` come straight off the row (either may
    be None) so a mod-log-formatter can build "Jump To Action" links; ``created_at``
    is ISO-8601 (None only for an unflushed row).

    Lives here, next to the fan-out, because BOTH writers need it: the bot's
    ``mod_action_dispatch`` (for /warn, /timeout, the AI tools, the audit-log
    backfill) and the worker's ``warn_user`` recorder. The web package must never
    import from the bot, so the shared shape belongs on the web side.
    """
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


async def dispatch_handler_event(
    db_session: AsyncSession,
    *,
    guild_id: str,
    channel_id: str,
    trigger_type: str,
    trigger_context: dict,
    chain_depth: int = 0,
) -> list[str]:
    """Fan one trigger out to every enabled handler, returning the fired ids.

    Enqueues one worker job per handler that passes its gates and returns their
    ids (empty when the raid window declined the event outright, when the chain
    ran too deep, or when nothing matched). ``db_session`` is the caller's
    session: the HTTP wrapper passes Litestar's injected one, the fire job passes
    its own — activity enrichment commits on it, so the caller owns the
    transaction boundary.

    ``chain_depth`` is how many handler fires deep this dispatch is: 0 for a
    gateway event (a chain root), d+1 for anything a fire at depth d caused. It
    is an explicit ARGUMENT and rides the fire payloads as an explicit FIELD —
    deliberately NOT a key in ``trigger_context``. Context is handed to the Monty
    sandbox verbatim, so a depth living there would be both readable and
    forgeable by a script (a runaway handler could reset its own chain to 0), and
    it would change the documented context shape the authoring prompt describes.
    """
    limiter = WindowedLimiter(redis=get_redis_client())

    # Recursion rail, checked BEFORE either tier's worker_submit so one check
    # covers standard and admin handlers alike. Sits behind — never instead of —
    # the mod_action fire's max_mod_actions=0 rail (admin_handlers_jobs): that
    # rail permits ZERO generations of a handler-caused ban wave, this one would
    # still permit three. Defense in depth, in that order.
    if chain_depth_exceeded(chain_depth):
        logger.warning(
            "handler dispatch declined: chain depth %s exceeds max %s "
            "(guild=%s trigger=%s)",
            chain_depth,
            MAX_CHAIN_DEPTH,
            guild_id,
            trigger_type,
        )
        return []

    dispatched: list[str] = []

    is_member_event = trigger_type in MEMBER_EVENT_TRIGGERS
    is_guild_scoped = trigger_type in GUILD_SCOPED_ADMIN_TRIGGERS
    # mod_action is admin-only (synthetic), never in the standard vocabulary,
    # so the standard-tier query is skipped for it exactly like the member
    # events — no ChannelHandler can carry it.
    is_admin_only = (
        trigger_type in ADMIN_ONLY_TRIGGER_TYPES
        or trigger_type in ADMIN_SYNTHETIC_TRIGGER_TYPES
    )

    # Member lifecycle events are gated by a per-guild raid window BEFORE any
    # fire is enqueued, so a raid + ban wave degrades to declined dispatches
    # rather than a fire-queue explosion (all four member_* triggers share the
    # window). thread_create is not under this gate.
    if is_member_event and not await limiter.hit(
        guild_member_events_key(guild_id), GUILD_MEMBER_EVENTS_PER_MIN
    ):
        return []

    # Message triggers carry the author: enrich the context with activity
    # facts ("first message ever", "days since last message") read BEFORE
    # recording this message, so scripts get platform truth instead of
    # tracking users in their size-capped memory.
    context = dict(trigger_context)
    # Every gateway-dispatched fire carries its guild id in context so a
    # script can build cross-channel jump links (mod-log formatters, !history)
    # — the runtime binds guild_id host-side but doesn't expose it to the
    # sandbox, and the prompts document context["guild_id"].
    context["guild_id"] = guild_id
    # A bot/webhook-authored message (author_is_bot, set bot-side after the
    # own-bot anti-loop guard) fires ONLY handlers that opted in via
    # settings["include_bot_messages"]; a plain message handler in the same
    # channel must not react to bot traffic. Human messages fire every
    # message handler unchanged.
    author_is_bot = bool(context.get("author_is_bot"))
    author_id = context.get("author_id")
    # Activity is human-only: a bot/webhook is not a guild member, so an
    # opted-in bot message neither records a MemberActivity row nor derives
    # human-shaped activity facts for it (the bot-side batcher skips them for
    # the same reason).
    if trigger_type == "message" and author_id and not author_is_bot:
        now = datetime.now(UTC)
        row = await get_activity(db_session, guild_id, str(author_id))
        context.update(activity_facts(row, now))
        await record_activity(db_session, guild_id, str(author_id), now)
        await db_session.commit()

    # Standard tier: every enabled handler for this (channel, trigger) fires,
    # each behind its own windowed cap. The five admin-only member/thread
    # triggers are never in the standard vocabulary, so skip the query for
    # them (no ChannelHandler can match).
    if not is_admin_only:
        standard_rows = (
            (
                await db_session.execute(
                    select(ChannelHandler).where(
                        ChannelHandler.channel_id == channel_id,
                        ChannelHandler.trigger_type == trigger_type,
                        ChannelHandler.enabled.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        for standard in standard_rows:
            if author_is_bot and not (standard.settings or {}).get(
                "include_bot_messages"
            ):
                continue
            if not await limiter.hit(
                handler_fire_key(str(standard.id)),
                fires_per_min_for_trigger(standard.trigger_type),
            ):
                continue
            await worker_submit(
                HandlerFirePayload(
                    handler_id=str(standard.id),
                    trigger_context=context,
                    # The fire inherits the dispatch's depth; anything IT causes
                    # is enqueued at depth+1 by the fire job's closures.
                    chain_depth=chain_depth,
                )
            )
            dispatched.append(str(standard.id))

    # Admin tier: every enabled admin handler for this guild+trigger. For
    # member_* events (channel_id="") the scope check is bypassed — the event
    # has no channel for a scope to mean anything, so they match by guild
    # alone. Every other trigger (including thread_create, dispatched with the
    # parent channel) matches when its scope includes the channel ([] = all).
    admin_rows = (
        (
            await db_session.execute(
                select(AdminHandler).where(
                    AdminHandler.guild_id == guild_id,
                    AdminHandler.trigger_type == trigger_type,
                    AdminHandler.enabled.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    for admin_handler in admin_rows:
        if author_is_bot and not (admin_handler.settings or {}).get(
            "include_bot_messages"
        ):
            continue
        if not is_guild_scoped:
            scope = admin_handler.channel_ids or []
            if scope and channel_id not in scope:
                continue
        # dm_message: a per-(handler, author) minute window so a user spamming
        # DMs burns their OWN window (a declined dispatch) rather than the
        # handler's global fire budget. Enforced before the fire cap below,
        # which still applies on top. A DM always carries author_id.
        if trigger_type == "dm_message" and author_id:
            if not await limiter.hit(
                dm_trigger_author_key(str(admin_handler.id), str(author_id)),
                DM_FIRES_PER_AUTHOR_PER_MIN,
            ):
                continue
        if not await limiter.hit(
            handler_fire_key(str(admin_handler.id)), ADMIN_FIRES_PER_MIN
        ):
            continue
        await worker_submit(
            AdminHandlerFirePayload(
                admin_handler_id=str(admin_handler.id),
                channel_id=channel_id,
                trigger_context=context,
                chain_depth=chain_depth,
            )
        )
        dispatched.append(str(admin_handler.id))

    return dispatched
