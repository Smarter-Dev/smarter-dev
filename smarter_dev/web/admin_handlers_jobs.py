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
from smarter_dev.web.handler_caps import ERROR_NOTICE_WINDOW_SECONDS, WindowedLimiter
from smarter_dev.web.handler_emitter import DiscordEmitter
from smarter_dev.web.handler_notify import notify_handler_error
from smarter_dev.web.handler_schedule import next_fire_at
from smarter_dev.web.models import AdminHandler, HandlerRun

logger = logging.getLogger(__name__)


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

    # For time triggers there's no triggering channel; default to the first
    # scoped channel (the script should target channels explicitly for "all").
    channel_id = payload.channel_id or (channel_ids[0] if channel_ids else "")

    from smarter_dev.web.admin_actions import AdminActor
    from smarter_dev.web.handler_agent import run_gathering_agent
    from smarter_dev.web.handler_runtime import run_handler_script

    budget = admin_budget()
    emitter = DiscordEmitter(bot_token=settings.discord_bot_token)
    redis = get_redis_client()
    limiter = WindowedLimiter(redis=redis)
    actor = AdminActor(bot_token=settings.discord_bot_token, guild_id=guild_id)

    result = await run_handler_script(
        script,
        payload.trigger_context,
        channel_id=channel_id,
        guild_id=guild_id,
        emitter=emitter,
        limiter=limiter,
        agent_runner=run_gathering_agent,
        budget=budget,
        actor=actor,
        memory=memory,
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
                duration_ms=result.duration_ms,
                finished_at=datetime.now(timezone.utc),
            )
        )
        if result.memory_changed:
            record = await session.get(AdminHandler, handler_id)
            if record is not None:
                record.memory = result.memory
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

    if trigger_type == "schedule":
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
