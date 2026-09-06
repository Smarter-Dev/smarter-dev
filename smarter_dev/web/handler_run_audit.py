"""The durable trail a handler fire leaves behind, and what may be kept in it.

A fire runs its script against the verbatim trigger context — a handler is
allowed to read the message it is reacting to — while every row that outlives
the fire keeps the redacted copy. This module takes the context a caller has
and redacts it itself, so no call site can store what a member actually said.

Both fire jobs share it: the member one in ``handlers_jobs`` and the admin one
in ``admin_handlers_jobs`` differ only in which tier they name and which
counters their budget lets them spend.

Two rows, two transactions, for one reason each. A completed fire writes into
the caller's session so the run, the handler's memory and the commit land
together. A skipped retry has no such neighbours, so it owns its session.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.database import get_db_session_context
from smarter_dev.shared.message_content import redact_trigger_context
from smarter_dev.web.models import HandlerRun

if TYPE_CHECKING:
    from smarter_dev.web.handler_runtime import HandlerResult

_SKIPPED_RETRY_ERROR = (
    "retry of a fire whose script had already started; skipped to "
    "avoid duplicate side effects"
)


async def record_completed_run(
    session: AsyncSession,
    *,
    handler_id: UUID,
    handler_kind: str,
    trigger_context: dict,
    result: HandlerResult,
) -> None:
    """Audit a fire that reached its script, whatever the script did.

    Added to the caller's session and left uncommitted: the caller persists the
    handler's memory in the same transaction, so a run row never claims a memory
    write that was rolled back.

    The admin-only counters (moderation actions, mod-audit lookups) read zero
    for a standard fire, whose budget forbids them outright.
    """
    usage = result.usage
    session.add(
        HandlerRun(
            handler_id=handler_id,
            handler_kind=handler_kind,
            trigger_context=redact_trigger_context(trigger_context),
            outcome=result.outcome,
            cap=result.cap,
            error=result.error,
            messages_sent=usage["messages_sent"],
            web_searches=usage["web_searches"],
            web_reads=usage["web_reads"],
            agent_calls=usage["agent_calls"],
            mod_actions=usage.get("mod_actions", 0),
            discord_reads=usage.get("discord_reads", 0),
            thread_ops=usage.get("thread_ops", 0),
            role_changes=usage.get("role_changes", 0),
            timers_scheduled=usage.get("timers_scheduled", 0),
            lookups=usage.get("lookups", 0),
            duration_ms=result.duration_ms,
            finished_at=datetime.now(UTC),
        )
    )


async def record_skipped_run(
    handler_id: UUID, handler_kind: str, trigger_context: dict
) -> None:
    """Audit a retry that declined to re-run an already-started script."""
    async with get_db_session_context() as session:
        session.add(
            HandlerRun(
                handler_id=handler_id,
                handler_kind=handler_kind,
                trigger_context=redact_trigger_context(trigger_context),
                outcome="skipped",
                error=_SKIPPED_RETRY_ERROR,
                finished_at=datetime.now(UTC),
            )
        )
        await session.commit()
