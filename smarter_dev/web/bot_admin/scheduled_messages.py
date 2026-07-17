"""Scheduled-message management for the Skrift admin panel.

Ports the ``scheduled_messages_*`` views from the legacy
``smarter_dev.web.admin.views`` onto a Skrift-native Litestar controller nested
under a campaign at ``/admin/bot/guilds/{guild_id}/campaigns/{campaign_id}/
scheduled-messages``. Scheduled messages are informational campaign
announcements sent at a fixed time; they live in the ``scheduled_messages``
table (see :class:`smarter_dev.web.models.ScheduledMessage`).

Form reading and validation are factored into pure module-level helpers so the
accepted-field contract can be unit-tested without a request or a database.
The legacy views carried redirect-back status in ``?created=1`` style query
params; this controller uses ``skrift.flash`` like the sibling campaigns
controller. Guild resolution and the campaign-not-found rendering are shared
with :mod:`smarter_dev.web.bot_admin.campaigns`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
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
    render_campaign_not_found,
)
from smarter_dev.web.crud import CampaignOperations, ScheduledMessageOperations

logger = logging.getLogger(__name__)

_CREATED_BY = "admin"


class FormLike(Protocol):
    """The subset of the submitted-form interface the readers rely on.

    Litestar's ``FormMultiDict`` satisfies this; tests can build one directly.
    """

    def get(self, key: str, default: Any = ...) -> Any: ...


def read_scheduled_message_form(form: FormLike) -> dict:
    """Extract a submitted scheduled-message form into a raw field dict.

    Pure function — no I/O. Mirrors the fields the legacy create/edit views
    read: ``title``, ``description``, the optional
    ``announcement_channel_message``, and the ``scheduled_time`` datetime-local
    value. Scalar text fields are stripped. The result is suitable both for
    validation and for re-rendering the form after a validation failure.
    """
    return {
        "title": (form.get("title") or "").strip(),
        "description": (form.get("description") or "").strip(),
        "announcement_channel_message": (
            form.get("announcement_channel_message") or ""
        ).strip(),
        "scheduled_time": (form.get("scheduled_time") or "").strip(),
    }


def validate_scheduled_message_form(
    data: dict, *, require_future: bool
) -> tuple[bool, list[str], dict]:
    """Validate a raw scheduled-message form dict, returning cleaned values.

    Pure function — does **not** mutate ``data``. Returns
    ``(is_valid, errors, cleaned)``; ``cleaned`` holds the typed values ready to
    pass to :class:`ScheduledMessageOperations`. Mirrors the legacy validation
    rules: required title/description/scheduled-time, and — when
    ``require_future`` (create, or editing an unsent message) — a scheduled time
    in the future. The optional ``announcement_channel_message`` collapses to
    ``None`` when blank.
    """
    errors: list[str] = []

    title = str(data.get("title", "")).strip()
    if not title:
        errors.append("Title is required")

    description = str(data.get("description", "")).strip()
    if not description:
        errors.append("Description is required")

    scheduled_time_str = str(data.get("scheduled_time", "")).strip()
    if not scheduled_time_str:
        errors.append("Scheduled time is required")

    scheduled_time = None
    if scheduled_time_str:
        try:
            scheduled_time = parse_campaign_datetime(scheduled_time_str)
            if require_future and scheduled_time <= datetime.now(timezone.utc):
                errors.append("Scheduled time must be in the future")
        except (ValueError, TypeError):
            errors.append("Invalid scheduled time format")

    announcement_channel_message = (
        str(data.get("announcement_channel_message", "")).strip() or None
    )

    cleaned = {
        "title": title,
        "description": description,
        "announcement_channel_message": announcement_channel_message,
        "scheduled_time": scheduled_time,
    }
    return len(errors) == 0, errors, cleaned


class ScheduledMessagesAdminController(Controller):
    """Scheduled-message CRUD nested under a campaign at ``/admin/bot``."""

    path = "/admin/bot"
    guards = [auth_guard]

    # -- List -----------------------------------------------------------------

    @get(
        "/guilds/{guild_id:str}/campaigns/{campaign_id:uuid}/scheduled-messages",
        guards=[auth_guard, Permission("administrator")],
    )
    async def scheduled_messages_list(
        self,
        request: Request,
        db_session: AsyncSession,
        guild_id: str,
        campaign_id: UUID,
    ) -> Response:
        """List a campaign's scheduled messages, ordered by scheduled time."""
        guild, error = await fetch_guild_or_error(request, db_session, guild_id)
        if error is not None:
            return error

        campaign = await CampaignOperations(db_session).get_campaign_by_id(
            campaign_id, guild_id
        )
        if campaign is None:
            return await render_campaign_not_found(request, db_session, guild_id)

        scheduled_messages = await ScheduledMessageOperations(
            db_session
        ).get_scheduled_messages_by_campaign(campaign_id)

        ctx = await get_admin_context(request, db_session)
        return TemplateResponse(
            "admin/bot/scheduled_messages/list.html",
            context={
                "guild": guild,
                "campaign": campaign,
                "scheduled_messages": scheduled_messages,
                "active_page": "campaigns",
                "guild_id": guild_id,
                "flash_messages": get_flash_messages(request),
                **ctx,
            },
        )

    # -- Create ---------------------------------------------------------------

    @get(
        "/guilds/{guild_id:str}/campaigns/{campaign_id:uuid}"
        "/scheduled-messages/create",
        guards=[auth_guard, Permission("administrator")],
    )
    async def scheduled_message_create_form(
        self,
        request: Request,
        db_session: AsyncSession,
        guild_id: str,
        campaign_id: UUID,
    ) -> Response:
        """Render the blank create-scheduled-message form."""
        guild, error = await fetch_guild_or_error(request, db_session, guild_id)
        if error is not None:
            return error

        campaign = await CampaignOperations(db_session).get_campaign_by_id(
            campaign_id, guild_id
        )
        if campaign is None:
            return await render_campaign_not_found(request, db_session, guild_id)

        return await render_create_form(
            request, db_session, guild, guild_id, campaign, None, []
        )

    @post(
        "/guilds/{guild_id:str}/campaigns/{campaign_id:uuid}"
        "/scheduled-messages/create",
        guards=[auth_guard, Permission("administrator")],
    )
    async def scheduled_message_create(
        self,
        request: Request,
        db_session: AsyncSession,
        guild_id: str,
        campaign_id: UUID,
    ) -> Response:
        """Validate and create a scheduled message, then redirect to the list."""
        guild, error = await fetch_guild_or_error(request, db_session, guild_id)
        if error is not None:
            return error

        campaign = await CampaignOperations(db_session).get_campaign_by_id(
            campaign_id, guild_id
        )
        if campaign is None:
            return await render_campaign_not_found(request, db_session, guild_id)

        form = await request.form()
        data = read_scheduled_message_form(form)
        is_valid, errors, cleaned = validate_scheduled_message_form(
            data, require_future=True
        )

        if not is_valid:
            return await render_create_form(
                request, db_session, guild, guild_id, campaign, data, errors,
                status_code=400,
            )

        await ScheduledMessageOperations(db_session).create_scheduled_message(
            campaign_id=campaign_id,
            title=cleaned["title"],
            description=cleaned["description"],
            announcement_channel_message=cleaned["announcement_channel_message"],
            scheduled_time=cleaned["scheduled_time"],
            created_by=_CREATED_BY,
        )

        flash_success(request, "Scheduled message created successfully!")
        return Redirect(
            path=(
                f"/admin/bot/guilds/{guild_id}/campaigns/{campaign_id}"
                "/scheduled-messages"
            )
        )

    # -- Edit -----------------------------------------------------------------

    @get(
        "/guilds/{guild_id:str}/campaigns/{campaign_id:uuid}"
        "/scheduled-messages/{message_id:uuid}/edit",
        guards=[auth_guard, Permission("administrator")],
    )
    async def scheduled_message_edit_form(
        self,
        request: Request,
        db_session: AsyncSession,
        guild_id: str,
        campaign_id: UUID,
        message_id: UUID,
    ) -> Response:
        """Render the edit form pre-populated from an existing message."""
        guild, error = await fetch_guild_or_error(request, db_session, guild_id)
        if error is not None:
            return error

        campaign = await CampaignOperations(db_session).get_campaign_by_id(
            campaign_id, guild_id
        )
        if campaign is None:
            return await render_campaign_not_found(request, db_session, guild_id)

        message = await ScheduledMessageOperations(
            db_session
        ).get_scheduled_message_by_id(message_id, campaign_id)
        if message is None:
            return await render_message_not_found(request, db_session, guild_id)

        return await render_edit_form(
            request, db_session, guild, guild_id, campaign, message, None, []
        )

    @post(
        "/guilds/{guild_id:str}/campaigns/{campaign_id:uuid}"
        "/scheduled-messages/{message_id:uuid}/edit",
        guards=[auth_guard, Permission("administrator")],
    )
    async def scheduled_message_edit(
        self,
        request: Request,
        db_session: AsyncSession,
        guild_id: str,
        campaign_id: UUID,
        message_id: UUID,
    ) -> Response:
        """Validate and apply edits to a message, then redirect to the list."""
        guild, error = await fetch_guild_or_error(request, db_session, guild_id)
        if error is not None:
            return error

        campaign = await CampaignOperations(db_session).get_campaign_by_id(
            campaign_id, guild_id
        )
        if campaign is None:
            return await render_campaign_not_found(request, db_session, guild_id)

        ops = ScheduledMessageOperations(db_session)
        message = await ops.get_scheduled_message_by_id(message_id, campaign_id)
        if message is None:
            return await render_message_not_found(request, db_session, guild_id)

        form = await request.form()
        data = read_scheduled_message_form(form)
        # An already-sent message may be edited without a future-time constraint,
        # matching the legacy view.
        is_valid, errors, cleaned = validate_scheduled_message_form(
            data, require_future=not message.is_sent
        )

        if not is_valid:
            return await render_edit_form(
                request, db_session, guild, guild_id, campaign, message, data,
                errors, status_code=400,
            )

        await ops.update_scheduled_message(
            message_id=message_id,
            campaign_id=campaign_id,
            title=cleaned["title"],
            description=cleaned["description"],
            announcement_channel_message=cleaned["announcement_channel_message"],
            scheduled_time=cleaned["scheduled_time"],
        )

        flash_success(request, "Scheduled message updated successfully!")
        return Redirect(
            path=(
                f"/admin/bot/guilds/{guild_id}/campaigns/{campaign_id}"
                "/scheduled-messages"
            )
        )

    # -- Delete ---------------------------------------------------------------

    @post(
        "/guilds/{guild_id:str}/campaigns/{campaign_id:uuid}"
        "/scheduled-messages/{message_id:uuid}/delete",
        guards=[auth_guard, Permission("administrator")],
    )
    async def scheduled_message_delete(
        self,
        request: Request,
        db_session: AsyncSession,
        guild_id: str,
        campaign_id: UUID,
        message_id: UUID,
    ) -> Redirect:
        """Delete a scheduled message, then redirect back to the list."""
        deleted = await ScheduledMessageOperations(
            db_session
        ).delete_scheduled_message(message_id, campaign_id)
        if deleted:
            flash_success(request, "Scheduled message deleted successfully!")
        else:
            flash_error(request, "Scheduled message not found.")
        return Redirect(
            path=(
                f"/admin/bot/guilds/{guild_id}/campaigns/{campaign_id}"
                "/scheduled-messages"
            )
        )


async def render_message_not_found(
    request: Request, db_session: AsyncSession, guild_id: str
) -> TemplateResponse:
    """Render the shared guild error page for a missing scheduled message."""
    ctx = await get_admin_context(request, db_session)
    return TemplateResponse(
        "admin/bot/guilds/error.html",
        context={
            "error": "Scheduled message not found.",
            "error_code": 404,
            "active_page": "campaigns",
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
    campaign: object,
    form_data: dict | None,
    errors: list[str],
    status_code: int = 200,
) -> TemplateResponse:
    """Render the create form with (optionally) submitted values and errors."""
    ctx = await get_admin_context(request, db_session)
    return TemplateResponse(
        "admin/bot/scheduled_messages/create.html",
        context={
            "guild": guild,
            "campaign": campaign,
            "errors": errors,
            "form_data": form_data,
            "active_page": "campaigns",
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
    campaign: object,
    message: object,
    form_data: dict | None,
    errors: list[str],
    status_code: int = 200,
) -> TemplateResponse:
    """Render the edit form with (optionally) submitted values and errors."""
    ctx = await get_admin_context(request, db_session)
    return TemplateResponse(
        "admin/bot/scheduled_messages/edit.html",
        context={
            "guild": guild,
            "campaign": campaign,
            "scheduled_message": message,
            "errors": errors,
            "form_data": form_data,
            "active_page": "campaigns",
            "guild_id": guild_id,
            **ctx,
        },
        status_code=status_code,
    )
