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
from datetime import datetime
from uuid import UUID, uuid4

from pydantic import BaseModel
from skrift.workers import RetryPolicy, WorkerContext, handler
from skrift.workers import submit as worker_submit

from smarter_dev.shared.config import get_settings
from smarter_dev.shared.database import get_db_session_context
from smarter_dev.shared.message_content import redact_trigger_context
from smarter_dev.shared.redis_client import get_redis_client
from smarter_dev.web.handler_budget import HandlerBudget
from smarter_dev.web.handler_caps import (
    ERROR_NOTICE_WINDOW_SECONDS,
    TIMER_ARMING_WINDOW_SECONDS,
    WindowedLimiter,
    claim_fire_attempt,
)
from smarter_dev.web.handler_emitter import DiscordEmitter
from smarter_dev.web.handler_memory import persist_handler_memory
from smarter_dev.web.handler_notify import notify_handler_error
from smarter_dev.web.handler_run_audit import record_completed_run, record_skipped_run
from smarter_dev.web.handler_schedule import RecurringFireChain
from smarter_dev.web.models import ChannelHandler

logger = logging.getLogger(__name__)

# Which tier of handler this job fires, as it is named in the audit row.
HANDLER_KIND = "standard"


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


_recurring_chain = RecurringFireChain(
    handler_model=ChannelHandler,
    build_fire_payload=lambda handler_id: HandlerFirePayload(
        handler_id=handler_id, trigger_context={"trigger_type": "schedule"}
    ),
)


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
    # Snapshotted BEFORE the script runs: the sandbox is handed the live context
    # dict, so a copy taken afterwards could carry text the script wrote into it.
    audit_trigger_context = redact_trigger_context(payload.trigger_context)
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
        await record_skipped_run(handler_id, HANDLER_KIND, audit_trigger_context)
        await _recurring_chain.rearm_after_fire(
            handler_id=handler_id,
            trigger_type=trigger_type,
            trigger_context=payload.trigger_context,
            handler_settings=handler_settings,
        )
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
        await record_completed_run(
            session,
            handler_id=handler_id,
            handler_kind=HANDLER_KIND,
            trigger_context=audit_trigger_context,
            result=result,
        )
        await persist_handler_memory(
            session,
            ChannelHandler,
            handler_id,
            result.memory,
            changed=result.memory_changed,
        )
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

    await _recurring_chain.rearm_after_fire(
        handler_id=handler_id,
        trigger_type=trigger_type,
        trigger_context=payload.trigger_context,
        handler_settings=handler_settings,
    )

    return {"status": result.outcome, "cap": result.cap}
