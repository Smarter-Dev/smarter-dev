"""Handler firing as a Skrift worker job (agent-worker tier).

A trigger (event dispatch or a scheduled time) enqueues ``HandlerFirePayload``;
this job loads the handler, runs its script under all the rails, writes a durable
:class:`~smarter_dev.web.models.HandlerRun`, and — for recurring schedules —
enqueues the next occurrence.

Kept import-clean of pydantic-ai and Monty at module load (they are imported
lazily inside the job) so the web tier can import ``HandlerFirePayload`` to
dispatch jobs without pulling in the inference stack — the same discipline as
``resources_jobs``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID, uuid4

from pydantic import BaseModel
from skrift.workers import handler
from skrift.workers import submit as worker_submit

from smarter_dev.shared.config import get_settings
from smarter_dev.shared.database import get_skrift_db_session_context
from smarter_dev.shared.redis_client import get_redis_client
from smarter_dev.web.handler_budget import HandlerBudget
from smarter_dev.web.handler_caps import WindowedLimiter
from smarter_dev.web.handler_emitter import DiscordEmitter
from smarter_dev.web.handler_schedule import next_fire_at
from smarter_dev.web.models import ChannelHandler, HandlerRun

logger = logging.getLogger(__name__)


class HandlerFirePayload(BaseModel):
    """Job payload for one handler firing."""

    handler_id: str
    trigger_context: dict = {}


@handler(
    "handlers.fire",
    queue="agents",
    max_attempts=1,
    # A fire may spawn an agent that web-searches/reads; keep a generous claim.
    visibility_timeout=180.0,
)
async def run_handler_fire(payload: HandlerFirePayload) -> dict:
    """Load, run, audit one handler firing; reschedule recurring schedules."""
    settings = get_settings()
    if not settings.handlers_enabled:
        return {"status": "disabled"}

    handler_id = UUID(payload.handler_id)
    async with get_skrift_db_session_context() as session:
        record = await session.get(ChannelHandler, handler_id)
        if record is None or not record.enabled:
            # Missing/disabled also breaks any recurring schedule chain.
            return {"status": "missing"}
        script = record.script
        channel_id = record.channel_id
        guild_id = record.guild_id
        trigger_type = record.trigger_type
        handler_settings = dict(record.settings or {})
        memory = dict(record.memory or {})

    # Lazy: these pull pydantic-ai / Monty, kept out of web-tier import.
    from smarter_dev.web.handler_agent import run_gathering_agent
    from smarter_dev.web.handler_runtime import run_handler_script

    budget = HandlerBudget()
    emitter = DiscordEmitter(bot_token=settings.discord_bot_token)
    limiter = WindowedLimiter(redis=get_redis_client())

    result = await run_handler_script(
        script,
        payload.trigger_context,
        channel_id=channel_id,
        guild_id=guild_id,
        emitter=emitter,
        limiter=limiter,
        agent_runner=run_gathering_agent,
        budget=budget,
        memory=memory,
    )

    async with get_skrift_db_session_context() as session:
        session.add(
            HandlerRun(
                handler_id=handler_id,
                trigger_context=payload.trigger_context,
                outcome=result.outcome,
                cap=result.cap,
                error=result.error,
                messages_sent=result.usage["messages_sent"],
                web_searches=result.usage["web_searches"],
                web_reads=result.usage["web_reads"],
                agent_calls=result.usage["agent_calls"],
                duration_ms=result.duration_ms,
                finished_at=datetime.now(timezone.utc),
            )
        )
        # Persist memory only when the script changed it (the common message-handler
        # path leaves it untouched and skips the write).
        if result.memory_changed:
            record = await session.get(ChannelHandler, handler_id)
            if record is not None:
                record.memory = result.memory
        await session.commit()

    if trigger_type == "schedule":
        await _reschedule(handler_id, handler_settings)

    return {"status": result.outcome, "cap": result.cap}


async def _reschedule(handler_id: UUID, handler_settings: dict) -> None:
    """Enqueue the next occurrence of a recurring schedule, if still enabled."""
    nxt = next_fire_at(handler_settings, datetime.now(timezone.utc))
    if nxt is None:
        return
    job_id = uuid4().hex
    await worker_submit(
        HandlerFirePayload(
            handler_id=str(handler_id),
            trigger_context={"trigger_type": "schedule"},
        ),
        scheduled_for=nxt,
        job_id=job_id,
    )
    async with get_skrift_db_session_context() as session:
        record = await session.get(ChannelHandler, handler_id)
        if record is not None and record.enabled:
            record.scheduled_job_id = job_id
            await session.commit()
