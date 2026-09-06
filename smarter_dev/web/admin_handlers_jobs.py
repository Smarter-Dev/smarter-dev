"""Admin-handler firing as a worker job.

Mirrors ``handlers_jobs`` but runs an admin handler with moderation powers: the
runtime gets an :class:`AdminActor` (enabling ban/kick/timeout/delete and
cross-channel send), the looser :func:`admin_budget`, and the host services in
``handler_script_services``. Audited in ``handler_runs`` with
``handler_kind="admin"``.

Import-clean of pydantic-ai/Monty (lazy inside the job) so the web tier can
import ``AdminHandlerFirePayload`` to dispatch without the inference stack.
"""

from __future__ import annotations

import logging
from uuid import UUID

from skrift.workers import RetryPolicy, WorkerContext, handler

from smarter_dev.shared.config import get_settings
from smarter_dev.shared.database import get_db_session_context
from smarter_dev.shared.message_content import redact_trigger_context
from smarter_dev.shared.redis_client import get_redis_client
from smarter_dev.web.handler_budget import admin_budget
from smarter_dev.web.handler_caps import (
    DM_USER_WINDOW_SECONDS,
    ERROR_NOTICE_WINDOW_SECONDS,
    TIMER_ARMING_WINDOW_SECONDS,
    WindowedLimiter,
    claim_fire_attempt,
)
from smarter_dev.web.handler_emitter import DiscordEmitter
from smarter_dev.web.handler_fire_payloads import AdminHandlerFirePayload
from smarter_dev.web.handler_guild_memory import (
    load_guild_memory,
    persist_guild_memory,
)
from smarter_dev.web.handler_memory import persist_handler_memory
from smarter_dev.web.handler_notify import notify_handler_error
from smarter_dev.web.handler_recurrence import RECURRING_CHAINS
from smarter_dev.web.handler_run_audit import record_completed_run, record_skipped_run
from smarter_dev.web.handler_script_services import (
    AdminScriptServices,
    HandlerTimerScheduler,
)
from smarter_dev.web.models import AdminHandler

__all__ = ["AdminHandlerFirePayload", "run_admin_handler_fire"]

logger = logging.getLogger(__name__)

HANDLER_KIND = "admin"

_recurring_chain = RECURRING_CHAINS[HANDLER_KIND]


@handler(
    "admin_handlers.fire",
    queue="agents",
    # See handlers_jobs.run_handler_fire: retries keep a transient failure from
    # dead-lettering a fire and ending a recurring chain, and the
    # claim_fire_attempt marker keeps script side effects at-most-once so a
    # retry can never repeat a ban/kick/send.
    retry_policy=RetryPolicy(max_attempts=3, backoff_seconds=30.0, jitter_seconds=10.0),
    visibility_timeout=180.0,
)
async def run_admin_handler_fire(
    payload: AdminHandlerFirePayload, context: WorkerContext
) -> dict:
    """Load, run (with moderation powers), audit one admin-handler firing."""
    settings = get_settings()
    if not settings.handlers_enabled:
        return {"status": "disabled"}

    handler_id = UUID(payload.admin_handler_id)
    audit_trigger_context = redact_trigger_context(payload.trigger_context)
    async with get_db_session_context() as session:
        record = await session.get(AdminHandler, handler_id)
        if record is None or not record.enabled:
            return {"status": "missing"}
        script = record.script
        guild_id = record.guild_id
        # Read here (not from the row later) because the recorder stamps it into
        # every ModerationAction as "handler:<name>" — a permanent audit field.
        handler_name = record.name
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

    budget = admin_budget(trigger_type)
    # The emitter carries the fire's guild so list_threads() can hit the
    # guild-scoped active-threads endpoint; without it the URL is malformed.
    emitter = DiscordEmitter(bot_token=settings.discord_bot_token, guild_id=guild_id)
    redis = get_redis_client()
    limiter = WindowedLimiter(redis=redis)
    actor = AdminActor(bot_token=settings.discord_bot_token, guild_id=guild_id)
    services = AdminScriptServices(
        handler_id=handler_id,
        handler_name=handler_name,
        guild_id=guild_id,
        channel_id=channel_id,
        chain_depth=payload.chain_depth,
        actor=actor,
    )
    timer_scheduler = HandlerTimerScheduler(
        chain_depth=payload.chain_depth,
        build_refire_payload=lambda refire_context, chain_depth: AdminHandlerFirePayload(
            admin_handler_id=str(handler_id),
            channel_id=channel_id,
            trigger_context=refire_context,
            chain_depth=chain_depth,
        ),
    )
    # The timer limiter is a separate 3600s window (self.limiter is fixed at
    # 60s), and send_dm's per-recipient cap is a third window — same
    # separate-instance pattern, so one cap can never consume another's budget.
    timer_limiter = WindowedLimiter(
        redis=redis, window_seconds=TIMER_ARMING_WINDOW_SECONDS
    )
    dm_user_limiter = WindowedLimiter(
        redis=redis, window_seconds=DM_USER_WINDOW_SECONDS
    )

    # At-most-once side effects across retries — claimed as late as possible so
    # only failures that reach the script suppress a re-run. See handler_caps.
    if not await claim_fire_attempt(redis, context.job.id):
        logger.warning(
            "admin handler fire job %s retried after an earlier attempt already "
            "entered the script; skipping execution so actions aren't duplicated",
            context.job.id,
        )
        async with get_db_session_context() as session:
            record_skipped_run(
                session,
                handler_id=handler_id,
                handler_kind=HANDLER_KIND,
                trigger_context=audit_trigger_context,
            )
            await session.commit()
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
        channel_ids=channel_ids,
        allowed_role_ids=list(handler_settings.get("allowed_role_ids") or []),
        emitter=emitter,
        limiter=limiter,
        agent_runner=run_gathering_agent,
        mod_action_reader=services.read_mod_actions,
        mod_action_recorder=services.record_warn,
        rules_reader=services.read_rules,
        handler_id=str(handler_id),
        timer_scheduler=timer_scheduler.schedule_timer,
        timer_limiter=timer_limiter,
        dm_user_limiter=dm_user_limiter,
        budget=budget,
        actor=actor,
        memory=memory,
        guild_memory=guild_memory,
    )

    async with get_db_session_context() as session:
        record_completed_run(
            session,
            handler_id=handler_id,
            handler_kind=HANDLER_KIND,
            trigger_context=audit_trigger_context,
            result=result,
        )
        await persist_handler_memory(
            session,
            AdminHandler,
            handler_id,
            result.memory,
            changed=result.memory_changed,
        )
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

    await _recurring_chain.rearm_after_fire(
        handler_id=handler_id,
        trigger_type=trigger_type,
        trigger_context=payload.trigger_context,
        handler_settings=handler_settings,
    )

    return {"status": result.outcome, "cap": result.cap}
