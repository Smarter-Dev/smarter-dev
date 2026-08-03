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
from skrift.workers import RetryPolicy, WorkerContext, handler
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
    claim_fire_attempt,
)
from smarter_dev.web.handler_emitter import DiscordEmitter
from smarter_dev.web.handler_guild_memory import (
    load_guild_memory,
    persist_guild_memory,
)
from smarter_dev.web.crud import GuildRulesConfigOperations, ModerationActionOperations
from smarter_dev.web.guild_rules import parse_guild_rules
from smarter_dev.web.handler_notify import notify_handler_error
from smarter_dev.web.handler_schedule import next_fire_at
from smarter_dev.web.models import AdminHandler, HandlerRun, ModerationAction

logger = logging.getLogger(__name__)

_mod_action_ops = ModerationActionOperations()
_guild_rules_ops = GuildRulesConfigOperations()


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
    # How many handler fires deep this fire is (0 = caused by a gateway event).
    # An explicit FIELD, never a trigger_context key: context goes to the Monty
    # sandbox verbatim, so a depth in there would be script-readable and
    # script-forgeable. Defaulted so an omitted field means "chain root", not a
    # crash — schedule re-arms are roots, and so is any already-enqueued job.
    chain_depth: int = 0


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

    budget = admin_budget()
    # Loop rail (§3.5, HARD): a mod_action-triggered handler formats and posts an
    # audit row into the mod-log — it must NEVER ban/kick/timeout/delete, or a
    # handler action would write an audit row that re-fires it. Forcing the
    # mod-action budget to 0 makes that loop structurally impossible. The chain
    # depth counter is defense in DEPTH behind this, never a replacement for it:
    # depth would still permit three generations of a ban wave, this permits zero.
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
                # Caused BY this fire, so it descends one generation. The re-fire
                # is still enqueued (depth is enforced at the dispatch choke
                # point, and the 3600s arming window bounds a self-deferring
                # handler); carrying the depth is what makes anything that
                # re-fire DISPATCHES get refused past MAX_CHAIN_DEPTH.
                chain_depth=payload.chain_depth + 1,
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

    async def record_warn(
        target_user_id: str, reason: str, warn_channel_id: str
    ) -> int:
        # guild_id is bound host-side from THIS fire's guild, like the reader —
        # the script supplies only the target, the reason, and the channel it
        # already had to be in scope for.
        target_user_id = str(target_user_id)
        # The username lands in a PERMANENT audit record, so it is resolved from
        # Discord rather than trusted from the script (a script-supplied name
        # could impersonate anyone in the log). One UNMETERED fetch — it is a
        # host rail, not a script-visible read — and a failure degrades to the
        # raw id rather than failing a warn that already posted its notice.
        target_username = target_user_id
        try:
            info = await actor.get_member_info(target_user_id)
            target_username = info.get("username") or target_user_id
        except Exception:  # noqa: BLE001 — a name lookup must never fail the warn
            logger.debug(
                "warn_user could not resolve username for %s", target_user_id,
                exc_info=True,
            )
        async with get_db_session_context() as writer_session:
            action = await _mod_action_ops.create_action(
                writer_session,
                guild_id=guild_id,
                target_user_id=target_user_id,
                target_username=target_username,
                moderator_user_id=None,
                moderator_username=f"handler:{handler_name}",
                action_type="warn",
                reason=reason,
                source="handler",
                channel_id=warn_channel_id or None,
            )
            await writer_session.commit()
            # Counted host-side so the script gets an AUTHORITATIVE escalation
            # counter. A script tallying warns itself from list_mod_actions would
            # silently undercount: that read is clamped to 50 rows and returns
            # every action type, so a heavy user's warn history truncates and the
            # "third strike" escalation quietly never fires.
            warn_count = await _mod_action_ops.count_warns_for_user(
                writer_session, guild_id, target_user_id
            )
            # Fire the synthetic mod_action trigger so a handler-issued warn
            # reaches mod-log formatter handlers exactly like /warn does. Best
            # effort, mirroring mod_action_dispatch's discipline: a dispatch
            # failure is logged, NEVER propagated into the warn, whose notice and
            # audit row have both already landed. Imported here because
            # handler_dispatch imports this module for AdminHandlerFirePayload.
            try:
                from smarter_dev.web.handler_dispatch import (
                    build_mod_action_context,
                    dispatch_handler_event,
                )

                await dispatch_handler_event(
                    writer_session,
                    guild_id=guild_id,
                    channel_id="",
                    trigger_type="mod_action",
                    trigger_context=build_mod_action_context(action),
                    # This dispatch is caused BY the running fire, so it descends
                    # one generation: warn -> mod-log handler -> whatever THAT
                    # warns is a real chain, and past MAX_CHAIN_DEPTH the choke
                    # point cuts it. Sits behind the max_mod_actions=0 rail above,
                    # which already forbids a mod_action fire from warning at all.
                    chain_depth=payload.chain_depth + 1,
                )
            except Exception:  # noqa: BLE001 — dispatch never breaks the warn
                logger.debug("handler warn mod_action dispatch failed", exc_info=True)
        return warn_count

    async def read_rules() -> list[dict]:
        # guild_id bound host-side, same as the mod-action reader. Parsing is
        # guild_rules.parse_guild_rules — the SAME function /rule uses — so a
        # handler and the command number the rules identically. A guild with no
        # rules row parses None into [].
        async with get_db_session_context() as reader_session:
            config = await _guild_rules_ops.get_config(reader_session, guild_id)
        markdown = config.rules_markdown if config is not None else None
        return [
            {"number": rule.index, "title": rule.title, "text": rule.body}
            for rule in parse_guild_rules(markdown)
        ]

    # At-most-once side effects across retries — claimed as late as possible so
    # only failures that reach the script suppress a re-run. See handler_caps.
    if not await claim_fire_attempt(redis, context.job.id):
        logger.warning(
            "admin handler fire job %s retried after an earlier attempt already "
            "entered the script; skipping execution so actions aren't duplicated",
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
        channel_ids=channel_ids,
        allowed_role_ids=list(handler_settings.get("allowed_role_ids") or []),
        emitter=emitter,
        limiter=limiter,
        agent_runner=run_gathering_agent,
        mod_action_reader=read_mod_actions,
        mod_action_recorder=record_warn,
        rules_reader=read_rules,
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
    """Audit a retry that declined to re-run an already-started admin script."""
    async with get_db_session_context() as session:
        session.add(
            HandlerRun(
                handler_id=handler_id,
                handler_kind="admin",
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
