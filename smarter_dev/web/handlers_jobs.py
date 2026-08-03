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
from skrift.workers import RetryPolicy, WorkerContext, handler
from skrift.workers import submit as worker_submit

from smarter_dev.shared.config import get_settings
from smarter_dev.shared.database import get_db_session_context
from smarter_dev.shared.redis_client import get_redis_client
from smarter_dev.web.handler_budget import HandlerBudget
from smarter_dev.web.handler_caps import (
    ERROR_NOTICE_WINDOW_SECONDS,
    TIMER_ARMING_WINDOW_SECONDS,
    WindowedLimiter,
    claim_fire_attempt,
)
from smarter_dev.web.handler_emitter import DiscordEmitter
from smarter_dev.web.handler_notify import notify_handler_error
from smarter_dev.web.handler_schedule import next_fire_at
from smarter_dev.web.models import ChannelHandler, HandlerRun

logger = logging.getLogger(__name__)


class HandlerFirePayload(BaseModel):
    """Job payload for one handler firing."""

    handler_id: str
    trigger_context: dict = {}
    # How many handler fires deep this fire is (0 = caused by a gateway event).
    # An explicit FIELD, never a trigger_context key: context goes to the sandbox
    # verbatim, so a depth in there would be script-readable and script-forgeable.
    # Defaulted so an omitted field means "chain root", not a crash — schedule
    # re-arms and any older enqueued job read as roots, which is what they are.
    chain_depth: int = 0


@handler(
    "handlers.fire",
    queue="agents",
    # Retries exist so a transient failure can't dead-letter a fire — which for
    # a recurring schedule ends the chain outright, since the running fire is
    # the only thing that enqueues its successor. Script side effects are made
    # at-most-once by the claim_fire_attempt marker below, so a retry can never
    # double-post. Backoff is generous: the failures worth retrying (a deploy
    # rolling, a DB blip) resolve on the order of tens of seconds.
    retry_policy=RetryPolicy(max_attempts=3, backoff_seconds=30.0, jitter_seconds=10.0),
    # A fire may spawn an agent that web-searches/reads; keep a generous claim.
    visibility_timeout=180.0,
)
async def run_handler_fire(payload: HandlerFirePayload, context: WorkerContext) -> dict:
    """Load, run, audit one handler firing; reschedule recurring schedules."""
    settings = get_settings()
    if not settings.handlers_enabled:
        return {"status": "disabled"}

    handler_id = UUID(payload.handler_id)
    async with get_db_session_context() as session:
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
    # The emitter carries the fire's guild so list_threads() can hit the
    # guild-scoped active-threads endpoint; without it the URL is malformed.
    emitter = DiscordEmitter(bot_token=settings.discord_bot_token, guild_id=guild_id)
    redis = get_redis_client()
    limiter = WindowedLimiter(redis=redis)
    # schedule_timer arms a durable one-shot re-fire of THIS handler. The closure
    # owns the payload class + handler_id, keeping the runtime import-clean; the
    # timer limiter is a separate 3600s window (self.limiter is fixed at 60s).
    timer_limiter = WindowedLimiter(
        redis=redis, window_seconds=TIMER_ARMING_WINDOW_SECONDS
    )

    async def schedule_timer(fire_at: datetime, refire_context: dict) -> None:
        await worker_submit(
            HandlerFirePayload(
                handler_id=str(handler_id),
                trigger_context=refire_context,
                # A timer re-fire is caused BY this fire, so it descends one
                # generation. The re-fire itself is still enqueued (the depth
                # check lives at the dispatch choke point, and the arming window
                # is what bounds a self-deferring handler); carrying the depth is
                # what makes anything that re-fire DISPATCHES get refused once
                # the chain has run past MAX_CHAIN_DEPTH.
                chain_depth=payload.chain_depth + 1,
            ),
            scheduled_for=fire_at,
            job_id=uuid4().hex,
        )

    # At-most-once side effects across retries. Claimed as late as possible —
    # everything above (the lazy import, the record load, emitter setup) is
    # side-effect free, so a failure there leaves the claim unset and the retry
    # runs the fire properly.
    if not await claim_fire_attempt(redis, context.job.id):
        logger.warning(
            "handler fire job %s retried after an earlier attempt already entered "
            "the script; skipping execution so emits aren't duplicated",
            context.job.id,
        )
        await _record_skipped_run(handler_id, payload.trigger_context)
        if _is_schedule_fire(trigger_type, payload.trigger_context):
            await _reschedule(handler_id, handler_settings)
        return {"status": "skipped"}

    result = await run_handler_script(
        script,
        payload.trigger_context,
        channel_id=channel_id,
        guild_id=guild_id,
        emitter=emitter,
        limiter=limiter,
        agent_runner=run_gathering_agent,
        handler_id=str(handler_id),
        timer_scheduler=schedule_timer,
        timer_limiter=timer_limiter,
        budget=budget,
        memory=memory,
    )

    async with get_db_session_context() as session:
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
                discord_reads=result.usage.get("discord_reads", 0),
                thread_ops=result.usage.get("thread_ops", 0),
                role_changes=result.usage.get("role_changes", 0),
                timers_scheduled=result.usage.get("timers_scheduled", 0),
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

    # On an error (not a cap breach), tell the channel so it can be fixed.
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

    if _is_schedule_fire(trigger_type, payload.trigger_context):
        await _reschedule(handler_id, handler_settings)

    return {"status": result.outcome, "cap": result.cap}


def _is_schedule_fire(trigger_type: str, trigger_context: dict) -> bool:
    """Whether this fire is the one that owns re-arming the recurring chain.

    Only a genuine scheduled fire re-arms. A schedule handler that self-arms a
    schedule_timer re-fires with trigger_type "timer" in its context; that
    re-fire must NOT re-enter ``_reschedule`` or it forks a duplicate perpetual
    chain and clobbers scheduled_job_id (orphaning the original chain's job so
    disable/update can no longer cancel it).
    """
    return trigger_type == "schedule" and trigger_context.get("trigger_type") != "timer"


async def _record_skipped_run(handler_id: UUID, trigger_context: dict) -> None:
    """Audit a retry that declined to re-run an already-started script."""
    async with get_db_session_context() as session:
        session.add(
            HandlerRun(
                handler_id=handler_id,
                trigger_context=trigger_context,
                outcome="skipped",
                error=(
                    "retry of a fire whose script had already started; skipped to "
                    "avoid duplicate side effects"
                ),
                finished_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()


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
    async with get_db_session_context() as session:
        record = await session.get(ChannelHandler, handler_id)
        if record is not None and record.enabled:
            record.scheduled_job_id = job_id
            await session.commit()
