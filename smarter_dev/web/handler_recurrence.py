"""Arming a time-triggered handler's fires, and keeping a recurring chain alive.

A recurring schedule has no cron daemon behind it: the fire that just ran is
the only thing that enqueues its successor, and the stalled-schedule sweep
revives a chain whose successor never came. Installing, editing or re-enabling
a time-triggered handler arms its first fire the same way. Every one of those
enqueues the tier's fire payload and stamps the job id on the handler row, so
all of them go through one :class:`RecurringFireChain` per tier, looked up in
:data:`RECURRING_CHAINS` by the ``handler_kind`` the audit row names.

The chain also owns its tier's handler row: which ORM model it is, how to load
one that may still fire, and how to list the schedules the sweep should watch.
Nothing outside this module names the model. The time arithmetic stays in
``handler_schedule``; this module is the worker and database side of it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC
from datetime import datetime
from uuid import UUID
from uuid import uuid4

from pydantic import BaseModel
from skrift.workers import submit as worker_submit
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.database import get_db_session_context
from smarter_dev.web.handler_fire_payloads import AdminHandlerFirePayload
from smarter_dev.web.handler_fire_payloads import HandlerFirePayload
from smarter_dev.web.handler_schedule import next_fire_at
from smarter_dev.web.models import AdminHandler
from smarter_dev.web.models import ChannelHandler

HandlerRecord = ChannelHandler | AdminHandler


def _fire_owns_rearming(trigger_type: str, trigger_context: dict) -> bool:
    """Whether this fire is the one that enqueues the next occurrence.

    Only a genuine scheduled fire re-arms. A schedule handler that self-arms a
    ``schedule_timer`` re-fires with trigger_type "timer" in its context; that
    re-fire must NOT re-arm, or it forks a duplicate perpetual chain and
    clobbers ``scheduled_job_id`` (orphaning the original chain's job, so
    disable/update can no longer cancel it).
    """
    return trigger_type == "schedule" and trigger_context.get("trigger_type") != "timer"


class RecurringFireChain:
    """One handler tier's time-triggered fires and its self-perpetuating chain.

    The tier supplies its ORM model and how to build its fire payload once. The
    chain then owns loading the row, enqueueing an occurrence and stamping its
    job id, whether an install, a fire or the sweep asked for it.
    """

    def __init__(
        self,
        handler_model: type[HandlerRecord],
        build_fire_payload: Callable[[str, dict], BaseModel],
    ) -> None:
        self._handler_model = handler_model
        self._build_fire_payload = build_fire_payload

    async def load_enabled_handler(
        self, session: AsyncSession, handler_id: UUID
    ) -> HandlerRecord | None:
        """This tier's row for ``handler_id`` if it may still fire, else None.

        A row that is gone or disabled reads as None: nothing downstream should
        arm, stamp or audit a fire for it.
        """
        record = await session.get(self._handler_model, handler_id)
        if record is None or not record.enabled:
            return None
        return record

    async def load_enabled_schedule_handlers(
        self, session: AsyncSession
    ) -> list[HandlerRecord]:
        """Every enabled recurring-schedule row of this tier, for the sweep."""
        rows = await session.execute(
            select(self._handler_model).where(
                self._handler_model.enabled.is_(True),
                self._handler_model.trigger_type == "schedule",
            )
        )
        return list(rows.scalars().all())

    async def arm_occurrence(self, record: HandlerRecord, fire_at: datetime) -> str:
        """Enqueue one fire of ``record`` at ``fire_at`` and stamp its job id.

        Returns the job id. The stamp is what lets a later disable or edit
        cancel the occurrence. The fire's context names the row's trigger type,
        which for a recurring schedule is what makes that fire own re-arming.
        The caller owns the row's session and transaction; nothing is committed.
        """
        job_id = uuid4().hex
        await worker_submit(
            self._build_fire_payload(
                str(record.id), {"trigger_type": record.trigger_type}
            ),
            scheduled_for=fire_at,
            job_id=job_id,
        )
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
        """Enqueue this handler's next occurrence, if this fire owns the chain.

        The row is re-read in a fresh session because the fire may have taken
        long enough for someone to disable or delete the handler meanwhile;
        such a chain ends here rather than leaving an orphan job queued.
        """
        if not _fire_owns_rearming(trigger_type, trigger_context):
            return
        next_occurrence = next_fire_at(handler_settings, datetime.now(UTC))
        if next_occurrence is None:
            return
        async with get_db_session_context() as session:
            record = await self.load_enabled_handler(session, handler_id)
            if record is None:
                return
            await self.arm_occurrence(record, next_occurrence)
            await session.commit()


RECURRING_CHAINS: dict[str, RecurringFireChain] = {
    "standard": RecurringFireChain(
        handler_model=ChannelHandler,
        build_fire_payload=lambda handler_id, trigger_context: HandlerFirePayload(
            handler_id=handler_id, trigger_context=trigger_context
        ),
    ),
    "admin": RecurringFireChain(
        handler_model=AdminHandler,
        build_fire_payload=lambda handler_id, trigger_context: AdminHandlerFirePayload(
            admin_handler_id=handler_id, trigger_context=trigger_context
        ),
    ),
}
