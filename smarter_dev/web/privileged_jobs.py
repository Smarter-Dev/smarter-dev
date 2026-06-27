"""Privileged routine firing as a worker job (admin tier).

A completely separate code path from ``handlers.fire``: no sandbox, no budget,
no agents — a privileged routine runs a single structured moderation action.
Recurring schedules re-enqueue themselves, mirroring the member-handler chain.
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
from smarter_dev.web.handler_schedule import next_fire_at
from smarter_dev.web.models import PrivilegedRoutine
from smarter_dev.web.privileged_actions import PrivilegedActor

logger = logging.getLogger(__name__)


class PrivilegedFirePayload(BaseModel):
    """Job payload for one privileged-routine firing."""

    routine_id: str


@handler(
    "privileged.routine.fire",
    queue="agents",
    max_attempts=1,
    visibility_timeout=60.0,
)
async def run_privileged_fire(payload: PrivilegedFirePayload) -> dict:
    """Load and execute a privileged routine; reschedule recurring schedules."""
    settings = get_settings()
    routine_id = UUID(payload.routine_id)
    async with get_skrift_db_session_context() as session:
        routine = await session.get(PrivilegedRoutine, routine_id)
        if routine is None or not routine.enabled:
            return {"status": "missing"}
        action = dict(routine.action or {})
        guild_id = routine.guild_id
        trigger_type = routine.trigger_type
        routine_settings = dict(routine.settings or {})

    actor = PrivilegedActor(bot_token=settings.discord_bot_token)
    try:
        outcome = await actor.execute(action, guild_id)
        status = "ok"
    except Exception as exc:  # noqa: BLE001 — record and stop, never crash the worker
        logger.exception("privileged routine %s failed", routine_id)
        outcome = str(exc)
        status = "error"

    if trigger_type == "schedule":
        nxt = next_fire_at(routine_settings, datetime.now(timezone.utc))
        if nxt is not None:
            job_id = uuid4().hex
            await worker_submit(
                PrivilegedFirePayload(routine_id=str(routine_id)),
                scheduled_for=nxt,
                job_id=job_id,
            )
            async with get_skrift_db_session_context() as session:
                routine = await session.get(PrivilegedRoutine, routine_id)
                if routine is not None and routine.enabled:
                    routine.scheduled_job_id = job_id
                    await session.commit()

    return {"status": status, "outcome": outcome}
