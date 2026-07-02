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
from smarter_dev.web.handler_caps import MAX_ADMIN_HANDLERS_PER_GUILD
from smarter_dev.web.handler_schedule import ScheduleError, first_fire_at
from smarter_dev.web.models import HANDLER_TRIGGER_TYPES, AdminHandler

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/handlers", tags=["admin-handlers"])

_TIME_TRIGGERS = ("schedule", "timer")


class CreateAdminHandlerRequest(BaseModel):
    guild_id: str
    name: str
    trigger_type: str
    settings: dict = Field(default_factory=dict)
    channel_ids: list[str] = Field(default_factory=list)
    description: str
    script: str
    created_by_admin: str


class UpdateAdminHandlerRequest(BaseModel):
    description: str
    script: str
    settings: dict = Field(default_factory=dict)
    channel_ids: list[str] = Field(default_factory=list)
    # Optional rename; omitted = keep the current name.
    name: str | None = None


class AdminHandlerResponse(BaseModel):
    handler_id: str
    guild_id: str
    name: str
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
        name=r.name,
        trigger_type=r.trigger_type,
        channel_ids=list(r.channel_ids or []),
        settings=r.settings or {},
        description=r.description,
        enabled=r.enabled,
    )


def _normalized_name(raw: str) -> str:
    name = raw.strip()
    if not name:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "name is required")
    if len(name) > 64:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "name is too long (max 64)")
    return name


async def _name_taken(
    session: AsyncSession, guild_id: str, name: str, exclude_id: UUID | None = None
) -> bool:
    query = select(AdminHandler.id).where(
        AdminHandler.guild_id == guild_id, AdminHandler.name == name
    )
    if exclude_id is not None:
        query = query.where(AdminHandler.id != exclude_id)
    return (await session.execute(query)).first() is not None


async def _reschedule(record: AdminHandler) -> None:
    """Cancel any pending fire and schedule the first fire from current settings."""
    if record.scheduled_job_id:
        try:
            await get_handle(record.scheduled_job_id).cancel()
        except Exception:  # noqa: BLE001 — best-effort; the chain also self-stops
            logger.warning("could not cancel job %s", record.scheduled_job_id)
        record.scheduled_job_id = None
    fire_at = first_fire_at(
        record.trigger_type, record.settings or {}, datetime.now(timezone.utc)
    )
    job_id = uuid4().hex
    await worker_submit(
        AdminHandlerFirePayload(
            admin_handler_id=str(record.id),
            channel_id=(record.channel_ids[0] if record.channel_ids else ""),
            trigger_context={"trigger_type": record.trigger_type},
        ),
        scheduled_for=fire_at,
        job_id=job_id,
    )
    record.scheduled_job_id = job_id


@router.post("", response_model=AdminHandlerResponse, status_code=status.HTTP_201_CREATED)
async def create_admin_handler(
    body: CreateAdminHandlerRequest,
    session: AsyncSession = Depends(get_skrift_db_session),
    _: Any = Depends(verify_api_key),
) -> AdminHandlerResponse:
    if body.trigger_type not in HANDLER_TRIGGER_TYPES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "unknown trigger_type")
    name = _normalized_name(body.name)

    if await _name_taken(session, body.guild_id, name):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"an admin handler named {name!r} already exists in this guild — edit it instead",
        )
    guild_count = len(
        (
            await session.execute(
                select(AdminHandler.id).where(AdminHandler.guild_id == body.guild_id)
            )
        ).all()
    )
    if guild_count >= MAX_ADMIN_HANDLERS_PER_GUILD:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"this guild already has {MAX_ADMIN_HANDLERS_PER_GUILD} admin handlers — "
            "delete or edit one instead",
        )

    record = AdminHandler(
        guild_id=body.guild_id,
        name=name,
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
            await _reschedule(record)
        except ScheduleError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))

    await session.commit()
    await session.refresh(record)
    return _to_response(record)


@router.put("/{handler_id}", response_model=AdminHandlerResponse)
async def update_admin_handler(
    handler_id: UUID,
    body: UpdateAdminHandlerRequest,
    session: AsyncSession = Depends(get_skrift_db_session),
    _: Any = Depends(verify_api_key),
) -> AdminHandlerResponse:
    record = await session.get(AdminHandler, handler_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "admin handler not found")

    if body.name is not None:
        name = _normalized_name(body.name)
        if await _name_taken(session, record.guild_id, name, exclude_id=record.id):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"an admin handler named {name!r} already exists in this guild",
            )
        record.name = name

    record.description = body.description
    record.script = body.script
    record.settings = body.settings
    record.channel_ids = body.channel_ids
    record.enabled = True

    if record.trigger_type in _TIME_TRIGGERS:
        try:
            await _reschedule(record)
        except ScheduleError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))

    await session.commit()
    await session.refresh(record)
    return _to_response(record)


@router.get("", response_model=list[AdminHandlerResponse] | list[AdminHandlerDetail])
async def list_admin_handlers(
    guild_id: str,
    include_scripts: bool = False,
    session: AsyncSession = Depends(get_skrift_db_session),
    _: Any = Depends(verify_api_key),
) -> list[AdminHandlerResponse] | list[AdminHandlerDetail]:
    """List a guild's admin handlers; ``include_scripts`` adds the script bodies
    (used by the admin author to decide edit-vs-create)."""
    rows = (
        await session.execute(
            select(AdminHandler).where(AdminHandler.guild_id == guild_id)
        )
    ).scalars().all()
    if include_scripts:
        return [
            AdminHandlerDetail(**_to_response(r).model_dump(), script=r.script)
            for r in rows
        ]
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
