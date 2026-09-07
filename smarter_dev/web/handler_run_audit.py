"""The durable trail a handler fire leaves behind, and what may be kept in it.

A fire runs its script against the verbatim trigger context — a handler is
allowed to read the message it is reacting to — while every row that outlives
the fire keeps the redacted copy. This module takes the context a caller has
and redacts it itself, so no call site can store what a member actually said.

Three rows, one owner: a completed fire with its outcome and spend, a retry
that declined to re-run an already-started script, and a sweep that re-armed a
chain whose fire never came. Every function joins the caller's session and
commits nothing, so the row lands in the same transaction as whatever the
caller persists beside it.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.message_content import redact_trigger_context
from smarter_dev.web.models import HandlerRun

if TYPE_CHECKING:
    from smarter_dev.web.handler_runtime import HandlerResult

_SKIPPED_RETRY_ERROR = (
    "retry of a fire whose script had already started; skipped to "
    "avoid duplicate side effects"
)

_SWEEP_TRIGGER_CONTEXT = {"trigger_type": "sweep"}


def record_completed_run(
    session: AsyncSession,
    *,
    handler_id: UUID,
    handler_kind: str,
    trigger_context: dict,
    result: HandlerResult,
) -> None:
    """Audit a fire that reached its script, whatever the script did.

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


def record_skipped_run(
    session: AsyncSession,
    *,
    handler_id: UUID,
    handler_kind: str,
    trigger_context: dict,
) -> None:
    """Audit a retry that declined to re-run an already-started script."""
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


def record_rearmed_run(
    session: AsyncSession,
    *,
    handler_id: UUID,
    handler_kind: str,
    last_fired_at: datetime | None,
    overdue_by: timedelta,
    next_occurrence: datetime,
    now: datetime,
) -> None:
    """Audit the sweep reviving a recurring chain that had stopped firing.

    Its explanation is host-authored and names no member, which is why the
    retention sweep leaves a ``rearmed`` row's ``error`` in place.
    """
    last_fire = last_fired_at.isoformat() if last_fired_at else "never"
    session.add(
        HandlerRun(
            handler_id=handler_id,
            handler_kind=handler_kind,
            trigger_context=dict(_SWEEP_TRIGGER_CONTEXT),
            outcome="rearmed",
            error=(
                f"schedule chain had stopped firing (last fire {last_fire}, "
                f"overdue by {overdue_by}); re-armed for {next_occurrence.isoformat()}"
            ),
            fired_at=now,
            finished_at=now,
        )
    )
