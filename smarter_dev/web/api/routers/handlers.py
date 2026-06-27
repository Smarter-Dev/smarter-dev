"""API router for the agentic handler system (bot integration).

The Discord bot owns the live creation pipeline (author + judge) but has no DB
access, so it reaches the data layer through these endpoints:

- ``POST /handlers`` install an authored+approved script (event = single-listener
  upsert; time = insert + schedule the first fire).
- ``GET /handlers`` list a channel's handlers.
- ``DELETE /handlers/{id}`` remove any handler and cancel its queued job.
- ``POST /handlers/dispatch`` event dispatch — windowed fire cap then enqueue.
- ``GET /handlers/active-channels`` the (channel, trigger) set for the bot's
  cheap in-memory guard, so it doesn't call the API on every message.

These run inside the Skrift ASGI app, where the worker runtime is configured, so
``worker_submit`` works here. The web tier stays pydantic-ai-free: handler
*execution* happens in the worker (``handlers.fire``); this only dispatches.
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
from smarter_dev.shared.redis_client import get_redis_client
from smarter_dev.web.api.dependencies import verify_api_key
from smarter_dev.web.admin_handlers_jobs import AdminHandlerFirePayload
from smarter_dev.web.handler_caps import (
    ADMIN_FIRES_PER_MIN,
    WindowedLimiter,
    fires_per_min_for_trigger,
    handler_fire_key,
)
from smarter_dev.web.handler_schedule import (
    ScheduleError,
    first_fire_at,
    validate_interval,
)
from smarter_dev.web.handlers_jobs import HandlerFirePayload
from smarter_dev.web.models import (
    HANDLER_EVENT_TRIGGERS,
    HANDLER_TRIGGER_TYPES,
    AdminHandler,
    ChannelHandler,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/handlers", tags=["handlers"])


class CreateHandlerRequest(BaseModel):
    guild_id: str
    channel_id: str
    trigger_type: str
    settings: dict = Field(default_factory=dict)
    description: str
    script: str
    created_by: str


class HandlerResponse(BaseModel):
    handler_id: str
    guild_id: str
    channel_id: str
    trigger_type: str
    settings: dict
    description: str
    enabled: bool


class HandlerDetailResponse(HandlerResponse):
    script: str


class DispatchRequest(BaseModel):
    guild_id: str
    channel_id: str
    trigger_type: str
    trigger_context: dict = Field(default_factory=dict)


def _to_response(record: ChannelHandler) -> HandlerResponse:
    return HandlerResponse(
        handler_id=str(record.id),
        guild_id=record.guild_id,
        channel_id=record.channel_id,
        trigger_type=record.trigger_type,
        settings=record.settings or {},
        description=record.description,
        enabled=record.enabled,
    )


async def _schedule_first_fire(record: ChannelHandler) -> None:
    """For a time trigger, validate the floor and enqueue the first fire."""
    validate_interval(record.settings or {}, uses_agent="spawn_agent" in record.script)
    fire_at = first_fire_at(
        record.trigger_type, record.settings or {}, datetime.now(timezone.utc)
    )
    job_id = uuid4().hex
    await worker_submit(
        HandlerFirePayload(
            handler_id=str(record.id),
            trigger_context={"trigger_type": record.trigger_type},
        ),
        scheduled_for=fire_at,
        job_id=job_id,
    )
    record.scheduled_job_id = job_id


@router.post("", response_model=HandlerResponse, status_code=status.HTTP_201_CREATED)
async def create_handler(
    body: CreateHandlerRequest,
    session: AsyncSession = Depends(get_skrift_db_session),
    _: Any = Depends(verify_api_key),
) -> HandlerResponse:
    if body.trigger_type not in HANDLER_TRIGGER_TYPES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "unknown trigger_type")

    if body.trigger_type in HANDLER_EVENT_TRIGGERS:
        # Single-listener: replace any existing handler for this channel+trigger.
        existing = (
            await session.execute(
                select(ChannelHandler).where(
                    ChannelHandler.channel_id == body.channel_id,
                    ChannelHandler.trigger_type == body.trigger_type,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.script = body.script
            existing.description = body.description
            existing.settings = body.settings
            existing.enabled = True
            await session.commit()
            await session.refresh(existing)
            return _to_response(existing)

    record = ChannelHandler(
        guild_id=body.guild_id,
        channel_id=body.channel_id,
        trigger_type=body.trigger_type,
        settings=body.settings,
        description=body.description,
        script=body.script,
        created_by=body.created_by,
    )
    session.add(record)
    await session.flush()  # assign id before scheduling

    if body.trigger_type not in HANDLER_EVENT_TRIGGERS:
        try:
            await _schedule_first_fire(record)
        except ScheduleError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))

    await session.commit()
    await session.refresh(record)
    return _to_response(record)


@router.get("", response_model=list[HandlerResponse])
async def list_handlers(
    channel_id: str,
    session: AsyncSession = Depends(get_skrift_db_session),
    _: Any = Depends(verify_api_key),
) -> list[HandlerResponse]:
    rows = (
        await session.execute(
            select(ChannelHandler).where(ChannelHandler.channel_id == channel_id)
        )
    ).scalars().all()
    return [_to_response(r) for r in rows]


@router.delete("/{handler_id}", status_code=status.HTTP_200_OK)
async def delete_handler(
    handler_id: UUID,
    session: AsyncSession = Depends(get_skrift_db_session),
    _: Any = Depends(verify_api_key),
) -> dict:
    record = await session.get(ChannelHandler, handler_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "handler not found")
    if record.scheduled_job_id:
        try:
            await get_handle(record.scheduled_job_id).cancel()
        except Exception:  # noqa: BLE001 — best-effort; the chain also self-stops
            logger.warning("could not cancel job %s", record.scheduled_job_id)
    await session.delete(record)
    await session.commit()
    return {"deleted": str(handler_id)}


@router.post("/dispatch", status_code=status.HTTP_200_OK)
async def dispatch_event(
    body: DispatchRequest,
    session: AsyncSession = Depends(get_skrift_db_session),
    _: Any = Depends(verify_api_key),
) -> dict:
    limiter = WindowedLimiter(redis=get_redis_client())
    dispatched: list[str] = []

    # Standard tier: at most one handler per (channel, trigger).
    standard = (
        await session.execute(
            select(ChannelHandler).where(
                ChannelHandler.channel_id == body.channel_id,
                ChannelHandler.trigger_type == body.trigger_type,
                ChannelHandler.enabled.is_(True),
            )
        )
    ).scalar_one_or_none()
    if standard is not None:
        if await limiter.hit(
            handler_fire_key(str(standard.id)),
            fires_per_min_for_trigger(standard.trigger_type),
        ):
            await worker_submit(
                HandlerFirePayload(
                    handler_id=str(standard.id), trigger_context=body.trigger_context
                )
            )
            dispatched.append(str(standard.id))

    # Admin tier: every enabled admin handler for this guild+trigger whose scope
    # includes this channel ([] / null = all channels).
    admin_rows = (
        await session.execute(
            select(AdminHandler).where(
                AdminHandler.guild_id == body.guild_id,
                AdminHandler.trigger_type == body.trigger_type,
                AdminHandler.enabled.is_(True),
            )
        )
    ).scalars().all()
    for ah in admin_rows:
        scope = ah.channel_ids or []
        if scope and body.channel_id not in scope:
            continue
        if not await limiter.hit(handler_fire_key(str(ah.id)), ADMIN_FIRES_PER_MIN):
            continue
        await worker_submit(
            AdminHandlerFirePayload(
                admin_handler_id=str(ah.id),
                channel_id=body.channel_id,
                trigger_context=body.trigger_context,
            )
        )
        dispatched.append(str(ah.id))

    return {"dispatched": bool(dispatched), "handler_ids": dispatched}


@router.get("/active-channels", status_code=status.HTTP_200_OK)
async def active_channels(
    session: AsyncSession = Depends(get_skrift_db_session),
    _: Any = Depends(verify_api_key),
) -> dict:
    """The bot's cheap dispatch guard.

    ``channels``: [channel_id, trigger] for standard handlers AND admin handlers
    scoped to specific channels. ``guild_triggers``: [guild_id, trigger] for
    admin handlers scoped to ALL channels (so every channel in that guild fires).
    """
    rows = (
        await session.execute(
            select(ChannelHandler.channel_id, ChannelHandler.trigger_type).where(
                ChannelHandler.enabled.is_(True),
                ChannelHandler.trigger_type.in_(HANDLER_EVENT_TRIGGERS),
            )
        )
    ).all()
    channels = [[c, t] for c, t in rows]

    admin_rows = (
        await session.execute(
            select(
                AdminHandler.guild_id,
                AdminHandler.trigger_type,
                AdminHandler.channel_ids,
            ).where(
                AdminHandler.enabled.is_(True),
                AdminHandler.trigger_type.in_(HANDLER_EVENT_TRIGGERS),
            )
        )
    ).all()
    guild_triggers: list[list[str]] = []
    for guild_id, trigger, channel_ids in admin_rows:
        if channel_ids:
            channels.extend([cid, trigger] for cid in channel_ids)
        else:
            guild_triggers.append([guild_id, trigger])

    return {"channels": channels, "guild_triggers": guild_triggers}


@router.get("/{handler_id}", response_model=HandlerDetailResponse)
async def get_handler(
    handler_id: UUID,
    session: AsyncSession = Depends(get_skrift_db_session),
    _: Any = Depends(verify_api_key),
) -> HandlerDetailResponse:
    record = await session.get(ChannelHandler, handler_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "handler not found")
    return HandlerDetailResponse(**_to_response(record).model_dump(), script=record.script)
