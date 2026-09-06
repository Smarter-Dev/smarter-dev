"""The durable trail a handler fire leaves behind, and what may be kept in it.

A fire runs its script against the verbatim trigger context — a handler is
allowed to read the message it is reacting to — while every row that outlives
the fire keeps the redacted copy. This module takes the context a caller has
and redacts it itself, so no call site can store what a member actually said.

Both fire jobs share it: the member one in ``handlers_jobs`` and the admin one
in ``admin_handlers_jobs`` differ only in which tier they name.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from uuid import UUID

from smarter_dev.shared.database import get_db_session_context
from smarter_dev.shared.message_content import redact_trigger_context
from smarter_dev.web.models import HandlerRun

_SKIPPED_RETRY_ERROR = (
    "retry of a fire whose script had already started; skipped to "
    "avoid duplicate side effects"
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


def is_schedule_fire(trigger_type: str, trigger_context: dict) -> bool:
    """Whether this fire is the one that owns re-arming the recurring chain.

    Only a genuine scheduled fire re-arms. A schedule handler that self-arms a
    schedule_timer re-fires with trigger_type "timer" in its context; that
    re-fire must NOT re-enter the rescheduler or it forks a duplicate perpetual
    chain and clobbers scheduled_job_id (orphaning the original chain's job so
    disable/update can no longer cancel it).
    """
    return trigger_type == "schedule" and trigger_context.get("trigger_type") != "timer"
