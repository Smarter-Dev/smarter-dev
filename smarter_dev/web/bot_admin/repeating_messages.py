"""Repeating-message management for the Skrift admin panel.

Ports the ``repeating_message_*`` views from the legacy
``smarter_dev.web.admin.views`` onto a Skrift-native Litestar controller under
``/admin/bot/guilds/{guild_id}/repeating-messages``. Repeating messages are
admin-authored announcements that a channel re-posts at a fixed minute interval;
they live in the ``repeating_messages`` table (see
:class:`smarter_dev.web.models.RepeatingMessage`).

Form reading and validation are factored into pure module-level helpers so the
accepted-field contract can be unit-tested without a request or a database. The
legacy views carried redirect-back status in ``?success=created`` style query
params; this controller uses ``skrift.flash`` like the sibling scheduled-message
and campaigns controllers. Guild resolution and the guild error rendering are
shared with :mod:`smarter_dev.web.bot_admin.campaigns`.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from litestar import Controller, Request, get, post
from litestar.response import Redirect, Response, Template as TemplateResponse
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.admin.helpers import get_admin_context
from skrift.auth.guards import Permission, auth_guard
from skrift.flash import flash_error, flash_success, get_flash_messages

from smarter_dev.web.bot_admin.campaigns import (
    fetch_guild_or_error,
    parse_campaign_datetime,
)
from smarter_dev.web.crud import RepeatingMessageOperations
from smarter_dev.web.discord_admin_client import (
    DiscordAdminError,
    get_admin_discord_client,
)
from smarter_dev.web.models import RepeatingMessage

logger = logging.getLogger(__name__)

_CREATED_BY = "admin"
_ACTIVE_PAGE = "repeating_messages"


class FormLike(Protocol):
    """The subset of the submitted-form interface the readers rely on.

    Litestar's ``FormMultiDict`` satisfies this; tests can build one directly.
    """

    def get(self, key: str, default: Any = ...) -> Any: ...


def read_repeating_message_form(form: FormLike) -> dict:
    """Extract a submitted repeating-message form into a raw field dict.

    Pure function — no I/O. Mirrors the fields the legacy create/edit views
    read: the target ``channel_id``, the ``message_content``, the optional
    ``role_id`` mention, the ``start_time`` datetime-local value, and the
    ``interval_minutes`` cadence. Scalar text fields are stripped. The result is
    suitable both for validation and for re-rendering the form after a
    validation failure.
    """
    return {
        "channel_id": (form.get("channel_id") or "").strip(),
        "message_content": (form.get("message_content") or "").strip(),
        "role_id": (form.get("role_id") or "").strip(),
        "start_time": (form.get("start_time") or "").strip(),
        "interval_minutes": (form.get("interval_minutes") or "").strip(),
    }


def validate_repeating_message_form(data: dict) -> tuple[bool, list[str], dict]:
    """Validate a raw repeating-message form dict, returning cleaned values.

    Pure function — does **not** mutate ``data``. Returns
    ``(is_valid, errors, cleaned)``; ``cleaned`` holds the typed values ready to
    pass to :class:`RepeatingMessageOperations`. Mirrors the legacy validation
    rules: required channel, message content, start time, and an
    ``interval_minutes`` of at least one minute. The optional ``role_id``
    collapses to ``None`` when blank.
    """
    errors: list[str] = []

    channel_id = str(data.get("channel_id", "")).strip()
    if not channel_id:
        errors.append("Channel is required")

    message_content = str(data.get("message_content", "")).strip()
    if not message_content:
        errors.append("Message content is required")

    start_time_str = str(data.get("start_time", "")).strip()
    start_time: datetime | None = None
    if not start_time_str:
        errors.append("Start time is required")
    else:
        try:
            start_time = parse_campaign_datetime(start_time_str)
        except (ValueError, TypeError):
            errors.append("Invalid start time format")

    interval_raw = str(data.get("interval_minutes", "")).strip()
    interval_minutes: int | None = None
    if not interval_raw:
        errors.append("Interval is required")
    else:
        try:
            interval_minutes = int(interval_raw)
        except (ValueError, TypeError):
            errors.append("Interval must be a whole number")
        else:
            if interval_minutes < 1:
                errors.append("Interval must be at least 1 minute")

    role_id = str(data.get("role_id", "")).strip() or None

    cleaned = {
        "channel_id": channel_id,
        "message_content": message_content,
        "role_id": role_id,
        "start_time": start_time,
        "interval_minutes": interval_minutes,
    }
    return len(errors) == 0, errors, cleaned


class RepeatingMessagesAdminController(Controller):
    """Repeating-message CRUD and toggle for a guild under ``/admin/bot``."""

    path = "/admin/bot"
    guards = [auth_guard]

    # -- List -----------------------------------------------------------------

    @get(
        "/guilds/{guild_id:str}/repeating-messages",
        guards=[auth_guard, Permission("administrator")],
    )
    async def repeating_messages_list(
        self, request: Request, db_session: AsyncSession, guild_id: str
    ) -> Response:
        """List a guild's repeating messages, newest first."""
        guild, error = await fetch_guild_or_error(request, db_session, guild_id)
        if error is not None:
            return error

        messages = await RepeatingMessageOperations(
            db_session
        ).get_guild_repeating_messages(guild_id)
        _, roles = await load_channels_and_roles(guild_id)

        ctx = await get_admin_context(request, db_session)
        return TemplateResponse(
            "admin/bot/repeating_messages/list.html",
            context={
                "guild": guild,
                "messages": messages,
                "roles": roles,
                "active_page": _ACTIVE_PAGE,
                "guild_id": guild_id,
                "flash_messages": get_flash_messages(request),
                **ctx,
            },
        )

    # -- Create ---------------------------------------------------------------

    @get(
        "/guilds/{guild_id:str}/repeating-messages/create",
        guards=[auth_guard, Permission("administrator")],
    )
    async def repeating_message_create_form(
        self, request: Request, db_session: AsyncSession, guild_id: str
    ) -> Response:
        """Render the blank create-repeating-message form."""
        guild, error = await fetch_guild_or_error(request, db_session, guild_id)
        if error is not None:
            return error

        return await render_create_form(
            request, db_session, guild, guild_id, None, []
        )

    @post(
        "/guilds/{guild_id:str}/repeating-messages/create",
        guards=[auth_guard, Permission("administrator")],
    )
    async def repeating_message_create(
        self, request: Request, db_session: AsyncSession, guild_id: str
    ) -> Response:
        """Validate and create a repeating message, then redirect to the list."""
        guild, error = await fetch_guild_or_error(request, db_session, guild_id)
        if error is not None:
            return error

        form = await request.form()
        data = read_repeating_message_form(form)
        is_valid, errors, cleaned = validate_repeating_message_form(data)

        if not is_valid:
            return await render_create_form(
                request, db_session, guild, guild_id, data, errors,
                status_code=400,
            )

        await RepeatingMessageOperations(db_session).create_repeating_message(
            guild_id=guild_id,
            channel_id=cleaned["channel_id"],
            message_content=cleaned["message_content"],
            start_time=cleaned["start_time"],
            interval_minutes=cleaned["interval_minutes"],
            created_by=_CREATED_BY,
            role_id=cleaned["role_id"],
        )

        flash_success(request, "Repeating message created successfully!")
        return Redirect(
            path=f"/admin/bot/guilds/{guild_id}/repeating-messages"
        )

    # -- Edit -----------------------------------------------------------------

    @get(
        "/guilds/{guild_id:str}/repeating-messages/{message_id:uuid}/edit",
        guards=[auth_guard, Permission("administrator")],
    )
    async def repeating_message_edit_form(
        self,
        request: Request,
        db_session: AsyncSession,
        guild_id: str,
        message_id: UUID,
    ) -> Response:
        """Render the edit form pre-populated from an existing message."""
        guild, error = await fetch_guild_or_error(request, db_session, guild_id)
        if error is not None:
            return error

        message = await load_guild_message(db_session, guild_id, message_id)
        if message is None:
            return await render_message_not_found(request, db_session, guild_id)

        return await render_edit_form(
            request, db_session, guild, guild_id, message, None, []
        )

    @post(
        "/guilds/{guild_id:str}/repeating-messages/{message_id:uuid}/edit",
        guards=[auth_guard, Permission("administrator")],
    )
    async def repeating_message_edit(
        self,
        request: Request,
        db_session: AsyncSession,
        guild_id: str,
        message_id: UUID,
    ) -> Response:
        """Validate and apply edits to a message, then redirect to the list."""
        guild, error = await fetch_guild_or_error(request, db_session, guild_id)
        if error is not None:
            return error

        ops = RepeatingMessageOperations(db_session)
        message = await load_guild_message(db_session, guild_id, message_id)
        if message is None:
            return await render_message_not_found(request, db_session, guild_id)

        form = await request.form()
        data = read_repeating_message_form(form)
        is_valid, errors, cleaned = validate_repeating_message_form(data)

        if not is_valid:
            return await render_edit_form(
                request, db_session, guild, guild_id, message, data, errors,
                status_code=400,
            )

        await ops.update_repeating_message(
            message_id,
            channel_id=cleaned["channel_id"],
            message_content=cleaned["message_content"],
            role_id=cleaned["role_id"],
            start_time=cleaned["start_time"],
            interval_minutes=cleaned["interval_minutes"],
        )

        flash_success(request, "Repeating message updated successfully!")
        return Redirect(
            path=f"/admin/bot/guilds/{guild_id}/repeating-messages"
        )

    # -- Toggle ---------------------------------------------------------------

    @post(
        "/guilds/{guild_id:str}/repeating-messages/{message_id:uuid}/toggle",
        guards=[auth_guard, Permission("administrator")],
    )
    async def repeating_message_toggle(
        self,
        request: Request,
        db_session: AsyncSession,
        guild_id: str,
        message_id: UUID,
    ) -> Response:
        """Flip a repeating message's active flag, then redirect to the list."""
        ops = RepeatingMessageOperations(db_session)
        message = await load_guild_message(db_session, guild_id, message_id)
        if message is None:
            flash_error(request, "Repeating message not found.")
            return Redirect(
                path=f"/admin/bot/guilds/{guild_id}/repeating-messages"
            )

        await ops.toggle_repeating_message(message_id, not message.is_active)
        flash_success(
            request,
            "Repeating message "
            + ("disabled." if message.is_active else "enabled."),
        )
        return Redirect(
            path=f"/admin/bot/guilds/{guild_id}/repeating-messages"
        )

    # -- Delete ---------------------------------------------------------------

    @post(
        "/guilds/{guild_id:str}/repeating-messages/{message_id:uuid}/delete",
        guards=[auth_guard, Permission("administrator")],
    )
    async def repeating_message_delete(
        self,
        request: Request,
        db_session: AsyncSession,
        guild_id: str,
        message_id: UUID,
    ) -> Redirect:
        """Delete a repeating message, then redirect back to the list."""
        ops = RepeatingMessageOperations(db_session)
        message = await load_guild_message(db_session, guild_id, message_id)
        if message is None:
            flash_error(request, "Repeating message not found.")
            return Redirect(
                path=f"/admin/bot/guilds/{guild_id}/repeating-messages"
            )

        await ops.delete_repeating_message(message_id)
        flash_success(request, "Repeating message deleted successfully!")
        return Redirect(
            path=f"/admin/bot/guilds/{guild_id}/repeating-messages"
        )


async def load_channels_and_roles(guild_id: str) -> tuple[list, list]:
    """Fetch the guild's announcement channels and roles, empty on error.

    Parity with the legacy view, which degraded to empty lists rather than
    failing the whole page when a Discord fetch raised.
    """
    client = get_admin_discord_client()
    try:
        channels = await client.get_announcement_channels(guild_id)
    except DiscordAdminError:
        logger.warning(
            "Failed to fetch channels for guild %s; using empty list", guild_id
        )
        channels = []
    try:
        roles = await client.get_guild_roles(guild_id)
    except DiscordAdminError:
        logger.warning(
            "Failed to fetch roles for guild %s; using empty list", guild_id
        )
        roles = []
    return channels, roles


async def load_guild_message(
    db_session: AsyncSession, guild_id: str, message_id: UUID
) -> RepeatingMessage | None:
    """Load a repeating message, enforcing that it belongs to ``guild_id``.

    Mirrors the legacy ``message.guild_id != guild_id`` ownership check: a
    message belonging to another guild is treated as not found.
    """
    message = await RepeatingMessageOperations(db_session).get_repeating_message(
        message_id
    )
    if message is None or message.guild_id != guild_id:
        return None
    return message


async def render_message_not_found(
    request: Request, db_session: AsyncSession, guild_id: str
) -> TemplateResponse:
    """Render the shared guild error page for a missing repeating message."""
    ctx = await get_admin_context(request, db_session)
    return TemplateResponse(
        "admin/bot/guilds/error.html",
        context={
            "error": "Repeating message not found.",
            "error_code": 404,
            "active_page": _ACTIVE_PAGE,
            "guild_id": guild_id,
            **ctx,
        },
        status_code=404,
    )


async def render_create_form(
    request: Request,
    db_session: AsyncSession,
    guild: object,
    guild_id: str,
    form_data: dict | None,
    errors: list[str],
    status_code: int = 200,
) -> TemplateResponse:
    """Render the create form with (optionally) submitted values and errors."""
    channels, roles = await load_channels_and_roles(guild_id)
    ctx = await get_admin_context(request, db_session)
    return TemplateResponse(
        "admin/bot/repeating_messages/create.html",
        context={
            "guild": guild,
            "channels": channels,
            "roles": roles,
            "errors": errors,
            "form_data": form_data,
            "active_page": _ACTIVE_PAGE,
            "guild_id": guild_id,
            **ctx,
        },
        status_code=status_code,
    )


async def render_edit_form(
    request: Request,
    db_session: AsyncSession,
    guild: object,
    guild_id: str,
    message: object,
    form_data: dict | None,
    errors: list[str],
    status_code: int = 200,
) -> TemplateResponse:
    """Render the edit form with (optionally) submitted values and errors."""
    channels, roles = await load_channels_and_roles(guild_id)
    ctx = await get_admin_context(request, db_session)
    return TemplateResponse(
        "admin/bot/repeating_messages/edit.html",
        context={
            "guild": guild,
            "message": message,
            "channels": channels,
            "roles": roles,
            "errors": errors,
            "form_data": form_data,
            "active_page": _ACTIVE_PAGE,
            "guild_id": guild_id,
            **ctx,
        },
        status_code=status_code,
    )
