"""Native Litestar port of the scheduled- and repeating-message bot API.

Ports the legacy FastAPI ``routers/scheduled_messages.py`` (prefix
``/scheduled-messages``) and ``routers/repeating_messages.py`` (prefix
``/repeating-messages``) — part of unit U6 in
docs/v2/legacy-sunset/04-api-rewrite.md. Preserves the exact paths, verbs,
status codes, and request/response shapes of the FastAPI implementation so
``smarter_dev/bot`` (and any external caller) needs zero changes.

NOT registered in ``app.yaml`` yet — the FastAPI mount still owns ``/api``. This
module exists for isolated parity tests until the atomic switchover.

Session note: post phase-02 the two legacy sessions collapse into the single
injected ``db_session``; both ``ScheduledMessageOperations`` and
``RepeatingMessageOperations`` take that one session.

Error-shape parity: the legacy routers answered every failure with a bare
``HTTPException`` — a plain ``{"detail": "<string>"}`` body — so this port raises
:func:`errors.plain_error` with the identical status codes and detail strings.
Each handler catches :class:`DatabaseOperationError` and re-raises the same
per-endpoint 500 detail the FastAPI handler produced (rather than letting the
flat ``handle_database_error`` shape fire), then falls through to a generic
``"Internal server error"`` 500 — mirroring the legacy try/except structure. A
malformed ``message_id`` answers 422 (the FastAPI ``UUID`` path param validated
before the handler ran), reproduced via :func:`_parse_uuid_path`.

Status-code parity note: FastAPI defaults every verb (including ``POST``) to
200, so ``POST /repeating-messages/`` (create) declares ``HTTP_200_OK`` rather
than Litestar's default 201, and the ``DELETE`` routes declare ``HTTP_200_OK``
so they return the ``{"success": True}`` body instead of a bare 204.

Rate-limiting parity is deferred to the switchover commit (see the plan's
"Rate-limiting parity" section); the FastAPI mount still enforces those windows
in production until switchover.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from litestar import Controller, delete, get, post, put
from litestar.exceptions import ValidationException
from litestar.status_codes import HTTP_200_OK
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.guards import APIKeyOnly, Permission, auth_guard

from smarter_dev.web.api_native.errors import (
    BOT_API_EXCEPTION_HANDLERS,
    BotApiException,
    plain_error,
)
from smarter_dev.web.crud import (
    DatabaseOperationError,
    RepeatingMessageOperations,
    ScheduledMessageOperations,
)

# Permission granted to the bot's Skrift service key (see roles.py `bot-service`
# role and the phase-01 key-mint runbook).
BOT_API_PERMISSION = "bot-api"

# Guards are declared PER ROUTE (not only on the controller) because Skrift's
# ``auth_guard`` inspects ``route_handler.guards`` to find the ``APIKeyOnly``
# marker — controller-level guards do not populate that attribute. See the bytes
# controller and docs/v2/legacy-sunset/04-api-rewrite.md ("Auth model").
BOT_API_GUARDS = [auth_guard, APIKeyOnly(), Permission(BOT_API_PERMISSION)]


def _parse_uuid_path(value: str, field_name: str) -> UUID:
    """Parse a UUID path segment, matching FastAPI's 422 on bad format.

    The FastAPI routes declared their id path params as ``UUID``, so a malformed
    UUID produced a 422 ``RequestValidationError``. Declaring the Litestar param
    as ``str`` and parsing here reproduces that 422 (via
    :func:`errors.handle_validation_exception`) instead of a route-miss 404.
    """
    try:
        return UUID(value)
    except ValueError as parse_error:
        raise ValidationException(
            detail=f"Invalid {field_name} format",
            extra=[{"key": field_name, "message": "value is not a valid uuid"}],
        ) from parse_error


def _scheduled_message_summary(message: Any) -> dict[str, Any]:
    """Serialize a scheduled message for the bot's queue/send lists.

    Matches the ``upcoming``/``pending`` list item shape byte-for-byte.
    """
    campaign = message.campaign
    return {
        "id": str(message.id),
        "title": message.title,
        "description": message.description,
        "announcement_channel_message": message.announcement_channel_message,
        "scheduled_time": message.scheduled_time.isoformat(),
        "guild_id": campaign.guild_id,
        "announcement_channels": campaign.announcement_channels,
        "campaign": {
            "id": str(campaign.id),
            "title": campaign.title,
            "is_active": campaign.is_active,
        },
    }


class CreateRepeatingMessageRequest(BaseModel):
    """Request model for creating a repeating message."""

    guild_id: str = Field(..., description="Discord guild ID")
    channel_id: str = Field(..., description="Discord channel ID")
    message_content: str = Field(..., description="Message content to send")
    role_id: str | None = Field(None, description="Optional role ID to mention")
    start_time: datetime = Field(..., description="UTC datetime when first message is sent")
    interval_minutes: int = Field(..., ge=1, description="Minutes between messages")
    created_by: str = Field(..., description="Username of creator")


class UpdateRepeatingMessageRequest(BaseModel):
    """Request model for updating a repeating message."""

    message_content: str | None = Field(None, description="Message content to send")
    role_id: str | None = Field(None, description="Role ID to mention (null to remove)")
    start_time: datetime | None = Field(None, description="UTC datetime when first message is sent")
    interval_minutes: int | None = Field(None, ge=1, description="Minutes between messages")
    is_active: bool | None = Field(None, description="Whether message is active")


class RepeatingMessageResponse(BaseModel):
    """Response model for repeating message data."""

    id: str
    guild_id: str
    channel_id: str
    message_content: str
    role_id: str | None
    start_time: datetime
    interval_minutes: int
    next_send_time: datetime
    is_active: bool
    total_sent: int
    last_sent_at: datetime | None
    created_by: str
    created_at: datetime
    updated_at: datetime


def _repeating_message_response(message: Any) -> RepeatingMessageResponse:
    """Serialize a repeating message row to its response model."""
    return RepeatingMessageResponse(
        id=str(message.id),
        guild_id=message.guild_id,
        channel_id=message.channel_id,
        message_content=message.message_content,
        role_id=message.role_id,
        start_time=message.start_time,
        interval_minutes=message.interval_minutes,
        next_send_time=message.next_send_time,
        is_active=message.is_active,
        total_sent=message.total_sent,
        last_sent_at=message.last_sent_at,
        created_by=message.created_by,
        created_at=message.created_at,
        updated_at=message.updated_at,
    )


class ScheduledMessageController(Controller):
    """Scheduled-message endpoints — upcoming, pending, mark-sent, detail."""

    path = "/api/scheduled-messages"
    exception_handlers = BOT_API_EXCEPTION_HANDLERS

    @get("/upcoming", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def get_upcoming_scheduled_messages(
        self,
        db_session: AsyncSession,
        seconds: int = 45,
    ) -> dict[str, list[dict[str, Any]]]:
        """Return scheduled messages that will be sent in the next N seconds."""
        try:
            message_ops = ScheduledMessageOperations(db_session)
            upcoming_time = datetime.now(timezone.utc) + timedelta(seconds=seconds)
            scheduled_messages = await message_ops.get_upcoming_scheduled_messages(
                upcoming_time
            )
            return {
                "scheduled_messages": [
                    _scheduled_message_summary(message)
                    for message in scheduled_messages
                ]
            }
        except DatabaseOperationError:
            raise plain_error(500, "Failed to retrieve upcoming scheduled messages")
        except BotApiException:
            raise
        except Exception:
            raise plain_error(500, "Internal server error")

    @get("/pending", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def get_pending_scheduled_messages(
        self,
        db_session: AsyncSession,
    ) -> dict[str, list[dict[str, Any]]]:
        """Return scheduled messages that should be sent but haven't been yet."""
        try:
            message_ops = ScheduledMessageOperations(db_session)
            scheduled_messages = await message_ops.get_pending_scheduled_messages()
            return {
                "scheduled_messages": [
                    _scheduled_message_summary(message)
                    for message in scheduled_messages
                ]
            }
        except DatabaseOperationError:
            raise plain_error(500, "Failed to retrieve pending scheduled messages")
        except BotApiException:
            raise
        except Exception:
            raise plain_error(500, "Internal server error")

    @post("/{message_id:str}/mark-sent", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def mark_scheduled_message_sent(
        self,
        db_session: AsyncSession,
        message_id: str,
    ) -> dict[str, bool]:
        """Mark a scheduled message as sent."""
        parsed_message_id = _parse_uuid_path(message_id, "message_id")
        try:
            message_ops = ScheduledMessageOperations(db_session)
            success = await message_ops.mark_scheduled_message_sent(parsed_message_id)
            if not success:
                raise plain_error(404, "Scheduled message not found")
            return {"success": True}
        except DatabaseOperationError:
            raise plain_error(500, "Failed to mark scheduled message as sent")
        except BotApiException:
            raise
        except Exception:
            raise plain_error(500, "Internal server error")

    @get("/{message_id:str}", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def get_scheduled_message(
        self,
        db_session: AsyncSession,
        message_id: str,
    ) -> dict[str, dict[str, Any]]:
        """Return a scheduled message with its campaign data."""
        parsed_message_id = _parse_uuid_path(message_id, "message_id")
        try:
            message_ops = ScheduledMessageOperations(db_session)
            message = await message_ops.get_scheduled_message_with_campaign(
                parsed_message_id
            )
            if not message:
                raise plain_error(404, "Scheduled message not found")

            campaign = message.campaign
            message_data = {
                "id": str(message.id),
                "title": message.title,
                "description": message.description,
                "scheduled_time": message.scheduled_time.isoformat(),
                "is_sent": message.is_sent,
                "sent_at": message.sent_at.isoformat() if message.sent_at else None,
                "created_at": message.created_at.isoformat(),
                "guild_id": campaign.guild_id,
                "announcement_channels": campaign.announcement_channels,
                "campaign": {
                    "id": str(campaign.id),
                    "title": campaign.title,
                    "is_active": campaign.is_active,
                },
            }
            return {"scheduled_message": message_data}
        except DatabaseOperationError:
            raise plain_error(500, "Failed to retrieve scheduled message")
        except BotApiException:
            raise
        except Exception:
            raise plain_error(500, "Internal server error")


class RepeatingMessageController(Controller):
    """Repeating-message endpoints — bot send flow plus admin CRUD."""

    path = "/api/repeating-messages"
    exception_handlers = BOT_API_EXCEPTION_HANDLERS

    @get("/due", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def get_due_repeating_messages(
        self,
        db_session: AsyncSession,
    ) -> dict[str, list[dict[str, Any]]]:
        """Return repeating messages that are due to be sent."""
        try:
            message_ops = RepeatingMessageOperations(db_session)
            due_messages = await message_ops.get_due_repeating_messages()
            message_list = [
                {
                    "id": str(message.id),
                    "guild_id": message.guild_id,
                    "channel_id": message.channel_id,
                    "message_content": message.get_formatted_message(),
                    "role_id": message.role_id,
                    "interval_minutes": message.interval_minutes,
                    "next_send_time": message.next_send_time.isoformat(),
                    "total_sent": message.total_sent,
                }
                for message in due_messages
            ]
            return {"repeating_messages": message_list}
        except DatabaseOperationError:
            raise plain_error(500, "Failed to retrieve due repeating messages")
        except BotApiException:
            raise
        except Exception:
            raise plain_error(500, "Internal server error")

    @post("/{message_id:str}/mark-sent", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def mark_repeating_message_sent(
        self,
        db_session: AsyncSession,
        message_id: str,
    ) -> dict[str, bool]:
        """Mark a repeating message as sent and update its next send time."""
        parsed_message_id = _parse_uuid_path(message_id, "message_id")
        try:
            message_ops = RepeatingMessageOperations(db_session)
            success = await message_ops.mark_message_sent(parsed_message_id)
            if not success:
                raise plain_error(404, "Repeating message not found")
            return {"success": True}
        except DatabaseOperationError:
            raise plain_error(500, "Failed to mark repeating message as sent")
        except BotApiException:
            raise
        except Exception:
            raise plain_error(500, "Internal server error")

    @post("/", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def create_repeating_message(
        self,
        db_session: AsyncSession,
        data: CreateRepeatingMessageRequest,
    ) -> RepeatingMessageResponse:
        """Create a new repeating message."""
        try:
            message_ops = RepeatingMessageOperations(db_session)
            message = await message_ops.create_repeating_message(
                guild_id=data.guild_id,
                channel_id=data.channel_id,
                message_content=data.message_content,
                start_time=data.start_time,
                interval_minutes=data.interval_minutes,
                created_by=data.created_by,
                role_id=data.role_id,
            )
            return _repeating_message_response(message)
        except DatabaseOperationError:
            raise plain_error(500, "Failed to create repeating message")
        except BotApiException:
            raise
        except Exception:
            raise plain_error(500, "Internal server error")

    @get("/guild/{guild_id:str}", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def get_guild_repeating_messages(
        self,
        db_session: AsyncSession,
        guild_id: str,
        active_only: bool = False,
    ) -> dict[str, list[RepeatingMessageResponse]]:
        """Return all repeating messages for a guild."""
        try:
            message_ops = RepeatingMessageOperations(db_session)
            messages = await message_ops.get_guild_repeating_messages(
                guild_id=guild_id,
                active_only=active_only,
            )
            return {
                "repeating_messages": [
                    _repeating_message_response(message) for message in messages
                ]
            }
        except DatabaseOperationError:
            raise plain_error(500, "Failed to retrieve guild repeating messages")
        except BotApiException:
            raise
        except Exception:
            raise plain_error(500, "Internal server error")

    @get("/{message_id:str}", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def get_repeating_message(
        self,
        db_session: AsyncSession,
        message_id: str,
    ) -> RepeatingMessageResponse:
        """Return a repeating message by ID."""
        parsed_message_id = _parse_uuid_path(message_id, "message_id")
        try:
            message_ops = RepeatingMessageOperations(db_session)
            message = await message_ops.get_repeating_message(parsed_message_id)
            if not message:
                raise plain_error(404, "Repeating message not found")
            return _repeating_message_response(message)
        except DatabaseOperationError:
            raise plain_error(500, "Failed to retrieve repeating message")
        except BotApiException:
            raise
        except Exception:
            raise plain_error(500, "Internal server error")

    @put("/{message_id:str}", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def update_repeating_message(
        self,
        db_session: AsyncSession,
        message_id: str,
        data: UpdateRepeatingMessageRequest,
    ) -> dict[str, bool]:
        """Update a repeating message with the provided non-null fields."""
        parsed_message_id = _parse_uuid_path(message_id, "message_id")
        try:
            message_ops = RepeatingMessageOperations(db_session)
            updates = data.model_dump(exclude_none=True)
            if not updates:
                raise plain_error(400, "No fields to update")

            success = await message_ops.update_repeating_message(
                parsed_message_id, **updates
            )
            if not success:
                raise plain_error(404, "Repeating message not found")
            return {"success": True}
        except DatabaseOperationError:
            raise plain_error(500, "Failed to update repeating message")
        except BotApiException:
            raise
        except Exception:
            raise plain_error(500, "Internal server error")

    @delete("/{message_id:str}", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def delete_repeating_message(
        self,
        db_session: AsyncSession,
        message_id: str,
    ) -> dict[str, bool]:
        """Delete a repeating message."""
        parsed_message_id = _parse_uuid_path(message_id, "message_id")
        try:
            message_ops = RepeatingMessageOperations(db_session)
            success = await message_ops.delete_repeating_message(parsed_message_id)
            if not success:
                raise plain_error(404, "Repeating message not found")
            return {"success": True}
        except DatabaseOperationError:
            raise plain_error(500, "Failed to delete repeating message")
        except BotApiException:
            raise
        except Exception:
            raise plain_error(500, "Internal server error")

    @post("/{message_id:str}/toggle", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def toggle_repeating_message(
        self,
        db_session: AsyncSession,
        message_id: str,
        is_active: bool,
    ) -> dict[str, bool]:
        """Enable or disable a repeating message."""
        parsed_message_id = _parse_uuid_path(message_id, "message_id")
        try:
            message_ops = RepeatingMessageOperations(db_session)
            success = await message_ops.toggle_repeating_message(
                parsed_message_id, is_active
            )
            if not success:
                raise plain_error(404, "Repeating message not found")
            return {"success": True}
        except DatabaseOperationError:
            raise plain_error(500, "Failed to toggle repeating message")
        except BotApiException:
            raise
        except Exception:
            raise plain_error(500, "Internal server error")
