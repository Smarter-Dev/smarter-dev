"""API router for admin handlers — the privileged, script-based handler tier.

Separate from `/handlers` (member tier): created only via the admin slash command
(author-written script with moderation powers), and never editable by members.
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
from smarter_dev.web.admin_handlers_jobs import AdminHandlerFirePayload
from smarter_dev.web.api.dependencies import verify_api_key
from smarter_dev.web.handler_schedule import ScheduleError, first_fire_at
from smarter_dev.web.models import HANDLER_TRIGGER_TYPES, AdminHandler

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/handlers", tags=["admin-handlers"])

_TIME_TRIGGERS = ("schedule", "timer")


class CreateAdminHandlerRequest(BaseModel):
    guild_id: str
    trigger_type: str
    settings: dict = Field(default_factory=dict)
    channel_ids: list[str] = Field(default_factory=list)
    description: str
    script: str
    created_by_admin: str


class AdminHandlerResponse(BaseModel):
    handler_id: str
    guild_id: str
    trigger_type: str
    channel_ids: list[str]
    settings: dict
    description: str
    enabled: bool


class AdminHandlerDetail(AdminHandlerResponse):
    script: str


def _to_response(r: AdminHandler) -> AdminHandlerResponse:
    return AdminHandlerResponse(
        handler_id=str(r.id),
        guild_id=r.guild_id,
        trigger_type=r.trigger_type,
        channel_ids=list(r.channel_ids or []),
        settings=r.settings or {},
        description=r.description,
        enabled=r.enabled,
    )


@router.post("", response_model=AdminHandlerResponse, status_code=status.HTTP_201_CREATED)
async def create_admin_handler(
    body: CreateAdminHandlerRequest,
    session: AsyncSession = Depends(get_skrift_db_session),
    _: Any = Depends(verify_api_key),
) -> AdminHandlerResponse:
    if body.trigger_type not in HANDLER_TRIGGER_TYPES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "unknown trigger_type")

    record = AdminHandler(
        guild_id=body.guild_id,
        trigger_type=body.trigger_type,
        settings=body.settings,
        channel_ids=body.channel_ids,
        description=body.description,
        script=body.script,
        created_by_admin=body.created_by_admin,
    )
    session.add(record)
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
            AdminHandlerFirePayload(
                admin_handler_id=str(record.id),
                channel_id=(body.channel_ids[0] if body.channel_ids else ""),
                trigger_context={"trigger_type": body.trigger_type},
            ),
            scheduled_for=fire_at,
            job_id=job_id,
        )
        record.scheduled_job_id = job_id

    await session.commit()
    await session.refresh(record)
    return _to_response(record)


@router.get("", response_model=list[AdminHandlerResponse])
async def list_admin_handlers(
    guild_id: str,
    session: AsyncSession = Depends(get_skrift_db_session),
    _: Any = Depends(verify_api_key),
) -> list[AdminHandlerResponse]:
    rows = (
        await session.execute(
            select(AdminHandler).where(AdminHandler.guild_id == guild_id)
        )
    ).scalars().all()
    return [_to_response(r) for r in rows]


@router.delete("/{handler_id}", status_code=status.HTTP_200_OK)
async def delete_admin_handler(
    handler_id: UUID,
    session: AsyncSession = Depends(get_skrift_db_session),
    _: Any = Depends(verify_api_key),
) -> dict:
    record = await session.get(AdminHandler, handler_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "admin handler not found")
    if record.scheduled_job_id:
        try:
            await get_handle(record.scheduled_job_id).cancel()
        except Exception:  # noqa: BLE001
            logger.warning("could not cancel job %s", record.scheduled_job_id)
    await session.delete(record)
    await session.commit()
    return {"deleted": str(handler_id)}
