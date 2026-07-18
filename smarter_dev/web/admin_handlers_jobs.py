"""Admin-handler firing as a worker job.

Mirrors ``handlers_jobs`` but runs an admin handler with moderation powers: the
runtime gets an :class:`AdminActor` (enabling ban/kick/timeout/delete and
cross-channel send) and a looser :func:`admin_budget`. Audited in ``handler_runs``
with ``handler_kind="admin"``.

Import-clean of pydantic-ai/Monty (lazy inside the job) so the web tier can
import ``AdminHandlerFirePayload`` to dispatch without the inference stack.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID, uuid4

from pydantic import BaseModel
from skrift.workers import handler
from skrift.workers import submit as worker_submit

from smarter_dev.shared.config import get_settings
from smarter_dev.shared.database import get_db_session_context
from smarter_dev.shared.redis_client import get_redis_client
from smarter_dev.web.handler_budget import admin_budget
from smarter_dev.web.handler_caps import (
    DM_USER_WINDOW_SECONDS,
    ERROR_NOTICE_WINDOW_SECONDS,
    TIMER_ARMING_WINDOW_SECONDS,
    WindowedLimiter,
)
from smarter_dev.web.handler_emitter import DiscordEmitter
from smarter_dev.web.handler_guild_memory import (
    load_guild_memory,
    persist_guild_memory,
)
from smarter_dev.web.crud import ModerationActionOperations
from smarter_dev.web.handler_notify import notify_handler_error
from smarter_dev.web.handler_schedule import next_fire_at
from smarter_dev.web.models import AdminHandler, HandlerRun, ModerationAction

logger = logging.getLogger(__name__)

_mod_action_ops = ModerationActionOperations()


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


class AdminHandlerFirePayload(BaseModel):
    """Job payload for one admin-handler firing."""

    admin_handler_id: str
    channel_id: str = ""
    trigger_context: dict = {}


@handler(
    "admin_handlers.fire",
    queue="agents",
    max_attempts=1,
    visibility_timeout=180.0,
)
async def run_admin_handler_fire(payload: AdminHandlerFirePayload) -> dict:
    """Load, run (with moderation powers), audit one admin-handler firing."""
    settings = get_settings()
    if not settings.handlers_enabled:
        return {"status": "disabled"}

    handler_id = UUID(payload.admin_handler_id)
    async with get_db_session_context() as session:
        record = await session.get(AdminHandler, handler_id)
        if record is None or not record.enabled:
            return {"status": "missing"}
        script = record.script
        guild_id = record.guild_id
        trigger_type = record.trigger_type
        channel_ids = list(record.channel_ids or [])
        handler_settings = dict(record.settings or {})
        memory = dict(record.memory or {})
        # Guild-shared store: snapshotted before the fire so guild_memory_* reads
        # see a consistent view; changed keys are persisted per key after.
        guild_memory = await load_guild_memory(session, guild_id)

    # For time triggers there's no triggering channel; default to the first
    # scoped channel (the script should target channels explicitly for "all").
    channel_id = payload.channel_id or (channel_ids[0] if channel_ids else "")

    from smarter_dev.web.admin_actions import AdminActor
    from smarter_dev.web.handler_agent import run_gathering_agent
    from smarter_dev.web.handler_runtime import run_handler_script

    budget = admin_budget()
    # Loop rail (§3.5, HARD): a mod_action-triggered handler formats and posts an
    # audit row into the mod-log — it must NEVER ban/kick/timeout/delete, or a
    # handler action would write an audit row that re-fires it. Forcing the
    # mod-action budget to 0 makes that loop structurally impossible.
    if trigger_type == "mod_action":
        budget.max_mod_actions = 0
    # The emitter carries the fire's guild so list_threads() can hit the
    # guild-scoped active-threads endpoint; without it the URL is malformed.
    emitter = DiscordEmitter(bot_token=settings.discord_bot_token, guild_id=guild_id)
    redis = get_redis_client()
    limiter = WindowedLimiter(redis=redis)
    actor = AdminActor(bot_token=settings.discord_bot_token, guild_id=guild_id)
    # schedule_timer arms a durable one-shot re-fire of THIS admin handler. Same
    # closure discipline as the standard job, with AdminHandlerFirePayload; the
    # timer limiter is a separate 3600s window (self.limiter is fixed at 60s).
    timer_limiter = WindowedLimiter(
        redis=redis, window_seconds=TIMER_ARMING_WINDOW_SECONDS
    )
    # send_dm's per-recipient cap is a 3600s window; the shared 60s limiter above
    # carries only its global per-minute cap (same separate-instance pattern as
    # the timer window).
    dm_user_limiter = WindowedLimiter(
        redis=redis, window_seconds=DM_USER_WINDOW_SECONDS
    )

    async def schedule_timer(fire_at: datetime, refire_context: dict) -> None:
        await worker_submit(
            AdminHandlerFirePayload(
                admin_handler_id=str(handler_id),
                channel_id=channel_id,
                trigger_context=refire_context,
            ),
            scheduled_for=fire_at,
            job_id=uuid4().hex,
        )

    async def read_mod_actions(target_user_id: str, limit: int) -> list[dict]:
        # guild_id is bound host-side from THIS fire's guild — a script passes only
        # the target user and limit, so it can never read another guild's history.
        async with get_db_session_context() as reader_session:
            actions = await _mod_action_ops.get_actions_for_user(
                reader_session, guild_id, str(target_user_id), limit=int(limit)
            )
            return [_mod_action_row(action) for action in actions]

    result = await run_handler_script(
        script,
        payload.trigger_context,
        channel_id=channel_id,
        guild_id=guild_id,
        channel_ids=channel_ids,
        allowed_role_ids=list(handler_settings.get("allowed_role_ids") or []),
        emitter=emitter,
        limiter=limiter,
        agent_runner=run_gathering_agent,
        mod_action_reader=read_mod_actions,
        handler_id=str(handler_id),
        timer_scheduler=schedule_timer,
        timer_limiter=timer_limiter,
        dm_user_limiter=dm_user_limiter,
        budget=budget,
        actor=actor,
        memory=memory,
        guild_memory=guild_memory,
    )

    async with get_db_session_context() as session:
        session.add(
            HandlerRun(
                handler_id=handler_id,
                handler_kind="admin",
                trigger_context=payload.trigger_context,
                outcome=result.outcome,
                cap=result.cap,
                error=result.error,
                messages_sent=result.usage["messages_sent"],
                web_searches=result.usage["web_searches"],
                web_reads=result.usage["web_reads"],
                agent_calls=result.usage["agent_calls"],
                mod_actions=result.usage.get("mod_actions", 0),
                discord_reads=result.usage.get("discord_reads", 0),
                thread_ops=result.usage.get("thread_ops", 0),
                role_changes=result.usage.get("role_changes", 0),
                timers_scheduled=result.usage.get("timers_scheduled", 0),
                lookups=result.usage.get("lookups", 0),
                duration_ms=result.duration_ms,
                finished_at=datetime.now(timezone.utc),
            )
        )
        if result.memory_changed:
            record = await session.get(AdminHandler, handler_id)
            if record is not None:
                record.memory = result.memory
        # Guild-shared memory persists per changed key regardless of outcome
        # (emitted effects stay): a bind target set before a later script error
        # must survive, matching how per-handler memory is persisted above.
        if result.guild_memory_changed:
            await persist_guild_memory(
                session,
                guild_id,
                result.guild_memory_writes,
                result.guild_memory_deletes,
            )
        await session.commit()

    # On an error (not a cap breach), tell the triggering channel so it can be
    # fixed. Skipped when there's no channel (e.g. a time trigger with no scope).
    if result.outcome == "error":
        await notify_handler_error(
            emitter=emitter,
            limiter=WindowedLimiter(
                redis=redis, window_seconds=ERROR_NOTICE_WINDOW_SECONDS
            ),
            handler_id=str(handler_id),
            channel_id=channel_id,
            error=result.error,
        )

    # Re-arm the recurring chain only on a genuine scheduled fire. A schedule
    # handler that self-arms a schedule_timer re-fires with trigger_type "timer"
    # in its context; that re-fire must NOT re-enter _reschedule or it forks a
    # duplicate perpetual chain and clobbers scheduled_job_id (orphaning the
    # original chain's job so disable/update can no longer cancel it).
    if trigger_type == "schedule" and (
        payload.trigger_context.get("trigger_type") != "timer"
    ):
        await _reschedule(handler_id, handler_settings)

    return {"status": result.outcome, "cap": result.cap}


async def _reschedule(handler_id: UUID, handler_settings: dict) -> None:
    """Enqueue the next occurrence of a recurring admin schedule, if still enabled."""
    nxt = next_fire_at(handler_settings, datetime.now(timezone.utc))
    if nxt is None:
        return
    job_id = uuid4().hex
    await worker_submit(
        AdminHandlerFirePayload(
            admin_handler_id=str(handler_id),
            trigger_context={"trigger_type": "schedule"},
        ),
        scheduled_for=nxt,
        job_id=job_id,
    )
    async with get_db_session_context() as session:
        record = await session.get(AdminHandler, handler_id)
        if record is not None and record.enabled:
            record.scheduled_job_id = job_id
            await session.commit()
