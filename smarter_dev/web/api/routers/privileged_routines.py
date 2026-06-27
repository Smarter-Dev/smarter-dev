"""API router for the privileged admin tier — separate from member handlers.

Deliberately isolated from ``/handlers``: a different prefix, a different table
(``privileged_routines``), and no exposure to the chatbot tools. Only the admin
slash command (which gates on Discord ADMINISTRATOR) calls these endpoints.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from skrift.workers import get_handle
from skrift.workers import submit as worker_submit

from smarter_dev.shared.database import get_skrift_db_session
from smarter_dev.web.api.dependencies import verify_api_key
from smarter_dev.web.handler_schedule import ScheduleError, first_fire_at
from smarter_dev.web.models import HANDLER_TRIGGER_TYPES, PrivilegedRoutine
from smarter_dev.web.privileged_actions import PrivilegedActionError, validate_action
from smarter_dev.web.privileged_jobs import PrivilegedFirePayload

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/routines", tags=["privileged-routines"])

_TIME_TRIGGERS = ("schedule", "timer")


class CreateRoutineRequest(BaseModel):
    guild_id: str
    channel_id: str | None = None
    trigger_type: str
    settings: dict = Field(default_factory=dict)
    action: dict
    created_by_admin: str


class RoutineResponse(BaseModel):
    routine_id: str
    guild_id: str
    trigger_type: str
    action: dict
    enabled: bool


def _to_response(r: PrivilegedRoutine) -> RoutineResponse:
    return RoutineResponse(
        routine_id=str(r.id),
        guild_id=r.guild_id,
        trigger_type=r.trigger_type,
        action=r.action or {},
        enabled=r.enabled,
    )


@router.post("", response_model=RoutineResponse, status_code=status.HTTP_201_CREATED)
async def create_routine(
    body: CreateRoutineRequest,
    session: AsyncSession = Depends(get_skrift_db_session),
    _: Any = Depends(verify_api_key),
) -> RoutineResponse:
    if body.trigger_type not in HANDLER_TRIGGER_TYPES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "unknown trigger_type")
    try:
        validate_action(body.action)
    except PrivilegedActionError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))

    routine = PrivilegedRoutine(
        guild_id=body.guild_id,
        channel_id=body.channel_id,
        trigger_type=body.trigger_type,
        settings=body.settings,
        action=body.action,
        created_by_admin=body.created_by_admin,
    )
    session.add(routine)
    await session.flush()

    if body.trigger_type in _TIME_TRIGGERS:
        try:
            fire_at = first_fire_at(
                body.trigger_type, body.settings, datetime.now(timezone.utc)
            )
        except ScheduleError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
        job_id = uuid4().hex
        await worker_submit(
            PrivilegedFirePayload(routine_id=str(routine.id)),
            scheduled_for=fire_at,
            job_id=job_id,
        )
        routine.scheduled_job_id = job_id

    await session.commit()
    await session.refresh(routine)
    return _to_response(routine)


@router.get("", response_model=list[RoutineResponse])
async def list_routines(
    guild_id: str,
    session: AsyncSession = Depends(get_skrift_db_session),
    _: Any = Depends(verify_api_key),
) -> list[RoutineResponse]:
    rows = (
        await session.execute(
            select(PrivilegedRoutine).where(PrivilegedRoutine.guild_id == guild_id)
        )
    ).scalars().all()
    return [_to_response(r) for r in rows]


@router.delete("/{routine_id}", status_code=status.HTTP_200_OK)
async def delete_routine(
    routine_id: UUID,
    session: AsyncSession = Depends(get_skrift_db_session),
    _: Any = Depends(verify_api_key),
) -> dict:
    routine = await session.get(PrivilegedRoutine, routine_id)
    if routine is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "routine not found")
    if routine.scheduled_job_id:
        try:
            await get_handle(routine.scheduled_job_id).cancel()
        except Exception:  # noqa: BLE001
            logger.warning("could not cancel job %s", routine.scheduled_job_id)
    await session.delete(routine)
    await session.commit()
    return {"deleted": str(routine_id)}
