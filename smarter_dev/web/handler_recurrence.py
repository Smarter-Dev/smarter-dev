"""Keeping a recurring schedule's job chain alive.

A recurring schedule has no cron daemon behind it: the fire that just ran is
the only thing that enqueues its successor, and the stalled-schedule sweep
revives a chain whose successor never came. Both enqueue the same tier payload
and stamp the same job id on the handler row, so both go through one
:class:`RecurringFireChain` per tier, looked up in :data:`RECURRING_CHAINS` by
the ``handler_kind`` the audit row names.

The time arithmetic stays in ``handler_schedule``; this module is the worker
and database side of it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from uuid import UUID
from uuid import uuid4

from pydantic import BaseModel
from skrift.workers import submit as worker_submit
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.database import get_db_session_context
from smarter_dev.web.handler_fire_payloads import AdminHandlerFirePayload
from smarter_dev.web.handler_fire_payloads import HandlerFirePayload
from smarter_dev.web.handler_schedule import next_fire_at
from smarter_dev.web.models import AdminHandler
from smarter_dev.web.models import ChannelHandler

SCHEDULED_FIRE_CONTEXT = {"trigger_type": "schedule"}


def _fire_owns_rearming(trigger_type: str, trigger_context: dict) -> bool:
    """Whether this fire is the one that enqueues the next occurrence.

    Only a genuine scheduled fire re-arms. A schedule handler that self-arms a
    ``schedule_timer`` re-fires with trigger_type "timer" in its context; that
    re-fire must NOT re-arm, or it forks a duplicate perpetual chain and
    clobbers ``scheduled_job_id`` (orphaning the original chain's job, so
    disable/update can no longer cancel it).
    """
    return trigger_type == "schedule" and trigger_context.get("trigger_type") != "timer"


@dataclass(frozen=True)
class RecurringFireChain:
    """One handler tier's self-perpetuating schedule chain.

    The tier supplies its ORM model and how to build its fire payload once; the
    chain then owns enqueueing an occurrence and stamping its job id, whether a
    fire or the sweep asked for it.
    """

    handler_model: type
    build_fire_payload: Callable[[str], BaseModel]

    async def arm_next(
        self, session: AsyncSession, handler_id: UUID, next_occurrence: datetime
    ) -> str:
        """Enqueue the occurrence and stamp its job id on the handler row.

        Returns the job id. The stamp is what lets a later disable or edit
        cancel the occurrence; a row that is gone or disabled is left alone.
        Joins the caller's session and commits nothing.
        """
        job_id = uuid4().hex
        await worker_submit(
            self.build_fire_payload(str(handler_id)),
            scheduled_for=next_occurrence,
            job_id=job_id,
        )
        record = await session.get(self.handler_model, handler_id)
        if record is not None and record.enabled:
            record.scheduled_job_id = job_id
        return job_id

    async def rearm_after_fire(
        self,
        *,
        handler_id: UUID,
        trigger_type: str,
        trigger_context: dict,
        handler_settings: dict,
    ) -> None:
        """Enqueue this handler's next occurrence, if this fire owns the chain."""
        if not _fire_owns_rearming(trigger_type, trigger_context):
            return
        next_occurrence = next_fire_at(handler_settings, datetime.now(UTC))
        if next_occurrence is None:
            return
        async with get_db_session_context() as session:
            await self.arm_next(session, handler_id, next_occurrence)
            await session.commit()


RECURRING_CHAINS: dict[str, RecurringFireChain] = {
    "standard": RecurringFireChain(
        handler_model=ChannelHandler,
        build_fire_payload=lambda handler_id: HandlerFirePayload(
            handler_id=handler_id, trigger_context=dict(SCHEDULED_FIRE_CONTEXT)
        ),
    ),
    "admin": RecurringFireChain(
        handler_model=AdminHandler,
        build_fire_payload=lambda handler_id: AdminHandlerFirePayload(
            admin_handler_id=handler_id, trigger_context=dict(SCHEDULED_FIRE_CONTEXT)
        ),
    ),
}
