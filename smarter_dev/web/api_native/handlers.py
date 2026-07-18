"""Native Litestar port of the agentic channel-handler bot API.

Ports the legacy FastAPI ``routers/handlers.py`` (prefix ``/handlers``) — part
of unit U9 in docs/v2/legacy-sunset/04-api-rewrite.md. Preserves the exact
paths, verbs, status codes, and request/response shapes of the FastAPI
implementation so ``smarter_dev/bot/agents/handler_tools.py`` and
``smarter_dev/bot/plugins/handler_events.py`` (and any external caller) need
zero changes:

- ``POST   /api/handlers`` → 201, install an authored+approved script.
- ``PUT    /api/handlers/{handler_id}`` → edit (rename optional), reschedule.
- ``GET    /api/handlers?channel_id=...`` → list (``include_scripts`` adds bodies).
- ``DELETE /api/handlers/{handler_id}`` → 200 ``{"deleted": id}``.
- ``POST   /api/handlers/dispatch`` → fan out an event to every enabled handler.
- ``GET    /api/handlers/active-channels`` → the bot's cheap dispatch guard.
- ``GET    /api/handlers/{handler_id}`` → detail incl. script.

Auth parity: every legacy route required only a valid API key
(``verify_api_key``) — no admin scope — so every route takes
:data:`BOT_API_GUARDS` (``Permission("bot-api")``).

Error-shape parity: legacy 404/409/422s came from bare ``HTTPException``s —
plain ``{"detail": "<string>"}`` bodies — reproduced via :func:`errors.plain_error`.
Malformed ``handler_id`` path segments answer 422 (FastAPI ``UUID`` path-param
validation), reproduced via :func:`errors.parse_uuid_path`.

These endpoints already used the Skrift session (``get_skrift_db_session``) and
run inside the Skrift ASGI app where the worker runtime is configured, so the
Litestar-injected ``db_session`` and ``skrift.workers`` submit/cancel calls are
unchanged.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID, uuid4

from litestar import Controller, delete, get, post, put
from litestar.status_codes import HTTP_200_OK, HTTP_201_CREATED
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.guards import APIKeyOnly, Permission
from skrift.workers import get_handle
from skrift.workers import submit as worker_submit

from smarter_dev.shared.redis_client import get_redis_client
from smarter_dev.web.admin_handlers_jobs import AdminHandlerFirePayload
from smarter_dev.web.api_native.auth import bot_api_auth_guard
from smarter_dev.web.api_native.errors import (
    BOT_API_EXCEPTION_HANDLERS,
    parse_uuid_path,
    plain_error,
)
from smarter_dev.web.handler_caps import (
    ADMIN_FIRES_PER_MIN,
    DM_FIRES_PER_AUTHOR_PER_MIN,
    GUILD_MEMBER_EVENTS_PER_MIN,
    MAX_HANDLERS_PER_CHANNEL,
    WindowedLimiter,
    dm_trigger_author_key,
    fires_per_min_for_trigger,
    guild_member_events_key,
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
    ADMIN_HANDLER_EVENT_TRIGGERS,
    ADMIN_ONLY_TRIGGER_TYPES,
    ADMIN_SYNTHETIC_TRIGGER_TYPES,
    HANDLER_EVENT_TRIGGERS,
    HANDLER_TRIGGER_TYPES,
    AdminHandler,
    ChannelHandler,
)

logger = logging.getLogger(__name__)

# Admin-only triggers that are NOT guild-shaped member lifecycle events, so the
# per-guild raid window must not gate them: thread_create and message_edit
# (both dispatched with a real home channel — the thread's parent / the edited
# message's channel) and dm_message (its own per-author window). Excluded from
# MEMBER_EVENT_TRIGGERS.
_NON_MEMBER_ADMIN_TRIGGERS = ("thread_create", "dm_message", "message_edit")

# The guild-shaped member lifecycle triggers: dispatched with ``channel_id=""``
# (a member event has no channel), matched admin-only by guild + trigger, and
# gated by the per-guild ``GUILD_MEMBER_EVENTS_PER_MIN`` raid window. dm_message is
# deliberately NOT here — it is guild-scoped in dispatch but has its OWN
# per-(handler, author) window (see GUILD_SCOPED_ADMIN_TRIGGERS), not the raid gate.
MEMBER_EVENT_TRIGGERS = tuple(
    trigger
    for trigger in ADMIN_ONLY_TRIGGER_TYPES
    if trigger not in _NON_MEMBER_ADMIN_TRIGGERS
)

# Admin triggers dispatched with NO home channel (``channel_id=""``), so the
# admin scope check is bypassed and the handler surfaces as a (guild_id, trigger)
# guild-trigger in active-channels: the member_* events, dm_message (a DM has no
# guild channel to scope against), and the synthetic mod_action trigger (fired
# guild-wide after a ModerationAction commit; NOT under the member-events raid
# gate — MEMBER_EVENT_TRIGGERS excludes it — so a mass-ban wave is bounded only by
# the per-handler ADMIN_FIRES_PER_MIN window).
GUILD_SCOPED_ADMIN_TRIGGERS = (
    MEMBER_EVENT_TRIGGERS + ("dm_message",) + ADMIN_SYNTHETIC_TRIGGER_TYPES
)

# Permission granted to the bot's Skrift service key (see roles.py `bot-service`
# role and the phase-01 key-mint runbook).
BOT_API_PERMISSION = "bot-api"

# Guards are declared PER ROUTE (not only on the controller) because Skrift's
# ``auth_guard`` inspects ``route_handler.guards`` to find the ``APIKeyOnly``
# marker — controller-level guards do not populate that attribute. See the bytes
# controller and docs/v2/legacy-sunset/04-api-rewrite.md ("Auth model").
BOT_API_GUARDS = [bot_api_auth_guard, APIKeyOnly(), Permission(BOT_API_PERMISSION)]


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


def _reject_bot_optin_on_non_message(settings: dict, trigger_type: str) -> None:
    """422 when ``include_bot_messages`` is set on a non-message-trigger handler.

    The opt-in changes which bot/webhook messages fire the handler, so it only
    means anything on a ``message`` trigger — allowing it elsewhere would be a
    silent no-op the author might rely on. Enforced host-side at create/update.
    """
    if settings.get("include_bot_messages") and trigger_type != "message":
        raise plain_error(
            422, "include_bot_messages is only valid on message-trigger handlers"
        )


def _normalized_name(raw: str) -> str:
    """Validate and normalize a handler name; 422 on blank/oversized."""
    name = raw.strip()
    if not name:
        raise plain_error(422, "name is required")
    if len(name) > 64:
        raise plain_error(422, "name is too long (max 64)")
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


async def _cancel_scheduled_job(record: ChannelHandler | AdminHandler) -> None:
    """Best-effort cancel of a handler's pending fire (the chain also self-stops)."""
    try:
        await get_handle(record.scheduled_job_id).cancel()
    except Exception:  # noqa: BLE001 — best-effort; mirrors the legacy router
        logger.warning("could not cancel job %s", record.scheduled_job_id)


class HandlerController(Controller):
    """Member-tier channel handlers: install, edit, list, dispatch."""

    path = "/api/handlers"
    exception_handlers = BOT_API_EXCEPTION_HANDLERS

    @post("", status_code=HTTP_201_CREATED, guards=BOT_API_GUARDS)
    async def create_handler(
        self,
        db_session: AsyncSession,
        data: CreateHandlerRequest,
    ) -> HandlerResponse:
        if data.trigger_type not in HANDLER_TRIGGER_TYPES:
            raise plain_error(422, "unknown trigger_type")
        _reject_bot_optin_on_non_message(data.settings, data.trigger_type)
        name = _normalized_name(data.name)

        if await _name_taken(db_session, data.channel_id, name):
            raise plain_error(
                409,
                f"a handler named {name!r} already exists in this channel — edit it instead",
            )
        channel_count = (
            await db_session.execute(
                select(func.count())
                .select_from(ChannelHandler)
                .where(ChannelHandler.channel_id == data.channel_id)
            )
        ).scalar_one()
        if channel_count >= MAX_HANDLERS_PER_CHANNEL:
            raise plain_error(
                422,
                f"this channel already has {MAX_HANDLERS_PER_CHANNEL} handlers — "
                "delete or edit one instead",
            )

        record = ChannelHandler(
            guild_id=data.guild_id,
            channel_id=data.channel_id,
            name=name,
            trigger_type=data.trigger_type,
            settings=data.settings,
            description=data.description,
            script=data.script,
            created_by=data.created_by,
        )
        db_session.add(record)
        await db_session.flush()  # assign id before scheduling

        if data.trigger_type not in HANDLER_EVENT_TRIGGERS:
            try:
                await _schedule_first_fire(record)
            except ScheduleError as exc:
                raise plain_error(422, str(exc))

        await db_session.commit()
        await db_session.refresh(record)
        return _to_response(record)

    @put("/{handler_id:str}", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def update_handler(
        self,
        db_session: AsyncSession,
        handler_id: str,
        data: UpdateHandlerRequest,
    ) -> HandlerResponse:
        parsed_handler_id = parse_uuid_path(handler_id, "handler_id")
        record = await db_session.get(ChannelHandler, parsed_handler_id)
        if record is None:
            raise plain_error(404, "handler not found")
        # trigger_type is immutable on edit, so validate against the stored one.
        _reject_bot_optin_on_non_message(data.settings, record.trigger_type)

        if data.name is not None:
            name = _normalized_name(data.name)
            if await _name_taken(db_session, record.channel_id, name, exclude_id=record.id):
                raise plain_error(
                    409,
                    f"a handler named {name!r} already exists in this channel",
                )
            record.name = name

        record.description = data.description
        record.script = data.script
        record.settings = data.settings
        record.enabled = True

        if record.trigger_type not in HANDLER_EVENT_TRIGGERS:
            # Timing may have changed: cancel the pending fire and schedule afresh.
            if record.scheduled_job_id:
                await _cancel_scheduled_job(record)
                record.scheduled_job_id = None
            try:
                await _schedule_first_fire(record)
            except ScheduleError as exc:
                raise plain_error(422, str(exc))

        await db_session.commit()
        await db_session.refresh(record)
        return _to_response(record)

    @get("", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def list_handlers(
        self,
        db_session: AsyncSession,
        channel_id: str,
        include_scripts: bool = False,
    ) -> list[HandlerResponse] | list[HandlerDetailResponse]:
        """List a channel's handlers; ``include_scripts`` adds the script bodies
        (used by the authoring agent to decide edit-vs-create)."""
        rows = (
            await db_session.execute(
                select(ChannelHandler).where(ChannelHandler.channel_id == channel_id)
            )
        ).scalars().all()
        if include_scripts:
            return [
                HandlerDetailResponse(**_to_response(r).model_dump(), script=r.script)
                for r in rows
            ]
        return [_to_response(r) for r in rows]

    @delete("/{handler_id:str}", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def delete_handler(
        self,
        db_session: AsyncSession,
        handler_id: str,
    ) -> dict:
        parsed_handler_id = parse_uuid_path(handler_id, "handler_id")
        record = await db_session.get(ChannelHandler, parsed_handler_id)
        if record is None:
            raise plain_error(404, "handler not found")
        if record.scheduled_job_id:
            await _cancel_scheduled_job(record)
        await db_session.delete(record)
        await db_session.commit()
        return {"deleted": str(parsed_handler_id)}

    @post("/dispatch", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def dispatch_event(
        self,
        db_session: AsyncSession,
        data: DispatchRequest,
    ) -> dict:
        limiter = WindowedLimiter(redis=get_redis_client())
        dispatched: list[str] = []

        is_member_event = data.trigger_type in MEMBER_EVENT_TRIGGERS
        is_guild_scoped = data.trigger_type in GUILD_SCOPED_ADMIN_TRIGGERS
        # mod_action is admin-only (synthetic), never in the standard vocabulary,
        # so the standard-tier query is skipped for it exactly like the member
        # events — no ChannelHandler can carry it.
        is_admin_only = (
            data.trigger_type in ADMIN_ONLY_TRIGGER_TYPES
            or data.trigger_type in ADMIN_SYNTHETIC_TRIGGER_TYPES
        )

        # Member lifecycle events are gated by a per-guild raid window BEFORE any
        # fire is enqueued, so a raid + ban wave degrades to declined dispatches
        # rather than a fire-queue explosion (all four member_* triggers share the
        # window). thread_create is not under this gate.
        if is_member_event and not await limiter.hit(
            guild_member_events_key(data.guild_id), GUILD_MEMBER_EVENTS_PER_MIN
        ):
            return {"dispatched": False, "handler_ids": []}

        # Message triggers carry the author: enrich the context with activity
        # facts ("first message ever", "days since last message") read BEFORE
        # recording this message, so scripts get platform truth instead of
        # tracking users in their size-capped memory.
        trigger_context = dict(data.trigger_context)
        # A bot/webhook-authored message (author_is_bot, set bot-side after the
        # own-bot anti-loop guard) fires ONLY handlers that opted in via
        # settings["include_bot_messages"]; a plain message handler in the same
        # channel must not react to bot traffic. Human messages fire every
        # message handler unchanged.
        author_is_bot = bool(trigger_context.get("author_is_bot"))
        author_id = trigger_context.get("author_id")
        if data.trigger_type == "message" and author_id:
            now = datetime.now(timezone.utc)
            row = await get_activity(db_session, data.guild_id, str(author_id))
            trigger_context.update(activity_facts(row, now))
            await record_activity(db_session, data.guild_id, str(author_id), now)
            await db_session.commit()

        # Standard tier: every enabled handler for this (channel, trigger) fires,
        # each behind its own windowed cap. The five admin-only member/thread
        # triggers are never in the standard vocabulary, so skip the query for
        # them (no ChannelHandler can match).
        if not is_admin_only:
            standard_rows = (
                await db_session.execute(
                    select(ChannelHandler).where(
                        ChannelHandler.channel_id == data.channel_id,
                        ChannelHandler.trigger_type == data.trigger_type,
                        ChannelHandler.enabled.is_(True),
                    )
                )
            ).scalars().all()
            for standard in standard_rows:
                if author_is_bot and not (standard.settings or {}).get(
                    "include_bot_messages"
                ):
                    continue
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

        # Admin tier: every enabled admin handler for this guild+trigger. For
        # member_* events (channel_id="") the scope check is bypassed — the event
        # has no channel for a scope to mean anything, so they match by guild
        # alone. Every other trigger (including thread_create, dispatched with the
        # parent channel) matches when its scope includes the channel ([] = all).
        admin_rows = (
            await db_session.execute(
                select(AdminHandler).where(
                    AdminHandler.guild_id == data.guild_id,
                    AdminHandler.trigger_type == data.trigger_type,
                    AdminHandler.enabled.is_(True),
                )
            )
        ).scalars().all()
        for admin_handler in admin_rows:
            if author_is_bot and not (admin_handler.settings or {}).get(
                "include_bot_messages"
            ):
                continue
            if not is_guild_scoped:
                scope = admin_handler.channel_ids or []
                if scope and data.channel_id not in scope:
                    continue
            # dm_message: a per-(handler, author) minute window so a user spamming
            # DMs burns their OWN window (a declined dispatch) rather than the
            # handler's global fire budget. Enforced before the fire cap below,
            # which still applies on top. A DM always carries author_id.
            if data.trigger_type == "dm_message" and author_id:
                if not await limiter.hit(
                    dm_trigger_author_key(str(admin_handler.id), str(author_id)),
                    DM_FIRES_PER_AUTHOR_PER_MIN,
                ):
                    continue
            if not await limiter.hit(
                handler_fire_key(str(admin_handler.id)), ADMIN_FIRES_PER_MIN
            ):
                continue
            await worker_submit(
                AdminHandlerFirePayload(
                    admin_handler_id=str(admin_handler.id),
                    channel_id=data.channel_id,
                    trigger_context=trigger_context,
                )
            )
            dispatched.append(str(admin_handler.id))

        return {"dispatched": bool(dispatched), "handler_ids": dispatched}

    @get("/active-channels", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def active_channels(
        self,
        db_session: AsyncSession,
    ) -> dict:
        """The bot's cheap dispatch guard.

        ``channels``: [channel_id, trigger] for standard handlers AND admin
        handlers scoped to specific channels. ``guild_triggers``: [guild_id,
        trigger] for admin handlers scoped to ALL channels (so every channel in
        that guild fires).

        ``bot_message_channels`` / ``bot_message_guild_triggers``: the channel
        ids (and guild ids for guild-wide admin handlers) that have a
        message-trigger handler with ``include_bot_messages`` — so the bot only
        POSTs a bot/webhook message to /dispatch when some handler there opted in.
        """
        rows = (
            await db_session.execute(
                select(ChannelHandler.channel_id, ChannelHandler.trigger_type).where(
                    ChannelHandler.enabled.is_(True),
                    ChannelHandler.trigger_type.in_(HANDLER_EVENT_TRIGGERS),
                )
            )
        ).all()
        channels = [[channel, trigger] for channel, trigger in rows]

        admin_rows = (
            await db_session.execute(
                select(
                    AdminHandler.guild_id,
                    AdminHandler.trigger_type,
                    AdminHandler.channel_ids,
                ).where(
                    AdminHandler.enabled.is_(True),
                    AdminHandler.trigger_type.in_(ADMIN_HANDLER_EVENT_TRIGGERS),
                )
            )
        ).all()
        guild_triggers: list[list[str]] = []
        for guild_id, trigger, channel_ids in admin_rows:
            # Guild-scoped admin triggers (member_* and dm_message) always surface
            # as (guild_id, trigger) — their dispatch guard is per-guild regardless
            # of channel_ids (a member event / DM has no channel to scope against).
            # Everything else (message/reaction/thread_create/message_edit)
            # follows the scoped/guild-wide split: listed channels become channel
            # entries, empty scope = guild.
            if trigger in GUILD_SCOPED_ADMIN_TRIGGERS:
                guild_triggers.append([guild_id, trigger])
            elif channel_ids:
                channels.extend([channel_id, trigger] for channel_id in channel_ids)
            else:
                guild_triggers.append([guild_id, trigger])

        # Bot-message opt-in sets: message-trigger handlers with
        # include_bot_messages. Standard + channel-scoped admin -> channel ids;
        # guild-wide admin -> guild ids. Only the message trigger can opt in.
        bot_message_channels: list[str] = []
        bot_message_guild_triggers: list[str] = []
        std_bot_rows = (
            await db_session.execute(
                select(ChannelHandler.channel_id, ChannelHandler.settings).where(
                    ChannelHandler.enabled.is_(True),
                    ChannelHandler.trigger_type == "message",
                )
            )
        ).all()
        for channel_id, settings in std_bot_rows:
            if (settings or {}).get("include_bot_messages"):
                bot_message_channels.append(channel_id)
        admin_bot_rows = (
            await db_session.execute(
                select(
                    AdminHandler.guild_id,
                    AdminHandler.channel_ids,
                    AdminHandler.settings,
                ).where(
                    AdminHandler.enabled.is_(True),
                    AdminHandler.trigger_type == "message",
                )
            )
        ).all()
        for guild_id, channel_ids, settings in admin_bot_rows:
            if not (settings or {}).get("include_bot_messages"):
                continue
            if channel_ids:
                bot_message_channels.extend(channel_ids)
            else:
                bot_message_guild_triggers.append(guild_id)

        return {
            "channels": channels,
            "guild_triggers": guild_triggers,
            "bot_message_channels": bot_message_channels,
            "bot_message_guild_triggers": bot_message_guild_triggers,
        }

    @get("/{handler_id:str}", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def get_handler(
        self,
        db_session: AsyncSession,
        handler_id: str,
    ) -> HandlerDetailResponse:
        parsed_handler_id = parse_uuid_path(handler_id, "handler_id")
        record = await db_session.get(ChannelHandler, parsed_handler_id)
        if record is None:
            raise plain_error(404, "handler not found")
        return HandlerDetailResponse(
            **_to_response(record).model_dump(), script=record.script
        )
