"""API router for the agentic handler system (bot integration).

The Discord bot owns the live creation pipeline (author + judge) but has no DB
access, so it reaches the data layer through these endpoints:

- ``POST /handlers`` install an authored+approved script under a channel-unique
  name (time triggers also schedule the first fire).
- ``PUT /handlers/{id}`` edit an existing handler (script/description/settings,
  optional rename); time triggers are rescheduled.
- ``GET /handlers`` list a channel's handlers (``include_scripts`` for bodies).
- ``DELETE /handlers/{id}`` remove any handler and cancel its queued job.
- ``POST /handlers/dispatch`` event dispatch — every enabled handler for the
  (channel, trigger) fires, each behind its own windowed fire cap.
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
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from skrift.workers import get_handle
from skrift.workers import submit as worker_submit

from smarter_dev.shared.database import get_skrift_db_session
from smarter_dev.shared.redis_client import get_redis_client
from smarter_dev.web.api.dependencies import verify_api_key
from smarter_dev.web.admin_handlers_jobs import AdminHandlerFirePayload
from smarter_dev.web.handler_caps import (
    ADMIN_FIRES_PER_MIN,
    MAX_HANDLERS_PER_CHANNEL,
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
from smarter_dev.web.member_activity import (
    activity_facts,
    get_activity,
    record_activity,
)
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
    name: str
    trigger_type: str
    settings: dict = Field(default_factory=dict)
    description: str
    script: str
    created_by: str


class UpdateHandlerRequest(BaseModel):
    description: str
    script: str
    settings: dict = Field(default_factory=dict)
    # Optional rename; omitted = keep the current name.
    name: str | None = None


class HandlerResponse(BaseModel):
    handler_id: str
    guild_id: str
    channel_id: str
    name: str
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
        name=record.name,
        trigger_type=record.trigger_type,
        settings=record.settings or {},
        description=record.description,
        enabled=record.enabled,
    )


def _normalized_name(raw: str) -> str:
    """Validate and normalize a handler name; 422 on blank/oversized."""
    name = raw.strip()
    if not name:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "name is required")
    if len(name) > 64:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "name is too long (max 64)")
    return name


async def _name_taken(
    session: AsyncSession, channel_id: str, name: str, exclude_id: UUID | None = None
) -> bool:
    query = select(ChannelHandler.id).where(
        ChannelHandler.channel_id == channel_id, ChannelHandler.name == name
    )
    if exclude_id is not None:
        query = query.where(ChannelHandler.id != exclude_id)
    return (await session.execute(query)).first() is not None


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
    name = _normalized_name(body.name)

    if await _name_taken(session, body.channel_id, name):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"a handler named {name!r} already exists in this channel — edit it instead",
        )
    channel_count = (
        await session.execute(
            select(func.count())
            .select_from(ChannelHandler)
            .where(ChannelHandler.channel_id == body.channel_id)
        )
    ).scalar_one()
    if channel_count >= MAX_HANDLERS_PER_CHANNEL:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"this channel already has {MAX_HANDLERS_PER_CHANNEL} handlers — "
            "delete or edit one instead",
        )

    record = ChannelHandler(
        guild_id=body.guild_id,
        channel_id=body.channel_id,
        name=name,
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


@router.put("/{handler_id}", response_model=HandlerResponse)
async def update_handler(
    handler_id: UUID,
    body: UpdateHandlerRequest,
    session: AsyncSession = Depends(get_skrift_db_session),
    _: Any = Depends(verify_api_key),
) -> HandlerResponse:
    record = await session.get(ChannelHandler, handler_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "handler not found")

    if body.name is not None:
        name = _normalized_name(body.name)
        if await _name_taken(session, record.channel_id, name, exclude_id=record.id):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"a handler named {name!r} already exists in this channel",
            )
        record.name = name

    record.description = body.description
    record.script = body.script
    record.settings = body.settings
    record.enabled = True

    if record.trigger_type not in HANDLER_EVENT_TRIGGERS:
        # Timing may have changed: cancel the pending fire and schedule afresh.
        if record.scheduled_job_id:
            try:
                await get_handle(record.scheduled_job_id).cancel()
            except Exception:  # noqa: BLE001 — best-effort; the chain also self-stops
                logger.warning("could not cancel job %s", record.scheduled_job_id)
            record.scheduled_job_id = None
        try:
            await _schedule_first_fire(record)
        except ScheduleError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))

    await session.commit()
    await session.refresh(record)
    return _to_response(record)


@router.get("", response_model=list[HandlerResponse] | list[HandlerDetailResponse])
async def list_handlers(
    channel_id: str,
    include_scripts: bool = False,
    session: AsyncSession = Depends(get_skrift_db_session),
    _: Any = Depends(verify_api_key),
) -> list[HandlerResponse] | list[HandlerDetailResponse]:
    """List a channel's handlers; ``include_scripts`` adds the script bodies
    (used by the authoring agent to decide edit-vs-create)."""
    rows = (
        await session.execute(
            select(ChannelHandler).where(ChannelHandler.channel_id == channel_id)
        )
    ).scalars().all()
    if include_scripts:
        return [
            HandlerDetailResponse(**_to_response(r).model_dump(), script=r.script)
            for r in rows
        ]
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

    # Message triggers carry the author: enrich the context with activity facts
    # ("first message ever", "days since last message") read BEFORE recording
    # this message, so scripts get platform truth instead of tracking users in
    # their size-capped memory.
    trigger_context = dict(body.trigger_context)
    author_id = trigger_context.get("author_id")
    if body.trigger_type == "message" and author_id:
        now = datetime.now(timezone.utc)
        row = await get_activity(session, body.guild_id, str(author_id))
        trigger_context.update(activity_facts(row, now))
        await record_activity(session, body.guild_id, str(author_id), now)
        await session.commit()

    # Standard tier: every enabled handler for this (channel, trigger) fires,
    # each behind its own windowed cap.
    standard_rows = (
        await session.execute(
            select(ChannelHandler).where(
                ChannelHandler.channel_id == body.channel_id,
                ChannelHandler.trigger_type == body.trigger_type,
                ChannelHandler.enabled.is_(True),
            )
        )
    ).scalars().all()
    for standard in standard_rows:
        if not await limiter.hit(
            handler_fire_key(str(standard.id)),
            fires_per_min_for_trigger(standard.trigger_type),
        ):
            continue
        await worker_submit(
            HandlerFirePayload(
                handler_id=str(standard.id), trigger_context=trigger_context
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
                trigger_context=trigger_context,
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
