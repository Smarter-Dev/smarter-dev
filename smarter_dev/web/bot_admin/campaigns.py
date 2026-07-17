"""Campaign and challenge management for the Skrift admin panel.

Ports the ``campaigns_*`` and challenge views from the legacy
``smarter_dev.web.admin.views`` onto a Skrift-native Litestar controller under
``/admin/bot``. Guild identity comes from Discord (via
:mod:`smarter_dev.web.discord_admin_client`); campaigns and their challenges
live in the ``campaigns`` / ``challenges`` tables.

Form reading and validation are factored into pure module-level helpers so the
accepted-field contract can be unit-tested without a request or a database.
Redirect-back status that the legacy views carried in ``?created=1`` style
query params is now carried by ``skrift.flash``, matching the sibling
bytes-config and squads controllers.
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

from smarter_dev.web.crud import CampaignOperations, ConflictError
from smarter_dev.web.discord_admin_client import (
    DiscordAdminError,
    GuildNotFoundError,
    get_admin_discord_client,
)

logger = logging.getLogger(__name__)

_CAMPAIGN_PAGE_SIZE = 20
_MIN_RELEASE_CADENCE_HOURS = 1
_MAX_RELEASE_CADENCE_HOURS = 168
_CREATED_BY = "admin"


class FormLike(Protocol):
    """The subset of the submitted-form interface the readers rely on.

    Litestar's ``FormMultiDict`` satisfies this; tests can build one directly to
    exercise the multi-value (``getall``) announcement-channels field the same
    way the runtime does.
    """

    def get(self, key: str, default: Any = ...) -> Any: ...

    def getall(self, key: str, default: Any = ...) -> list: ...


def parse_campaign_datetime(value: str) -> datetime:
    """Parse an HTML ``datetime-local`` value into a timezone-aware UTC datetime.

    Mirrors the legacy view: the ``datetime-local`` format (``YYYY-MM-DDTHH:MM``)
    has its ``T`` separator swapped for a space, and a naive result is assumed to
    be UTC.

    Raises:
        ValueError: If ``value`` is empty or not an ISO datetime.
    """
    parsed = datetime.fromisoformat(value.replace("T", " "))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def read_campaign_form(form: FormLike) -> dict:
    """Extract a submitted campaign form into a raw field dict.

    Pure function — no I/O. Mirrors the fields the legacy create/edit views
    read: scalar text fields are stripped, ``announcement_channels`` comes from
    the multi-value form interface, ``is_active`` is the edit toggle, and the
    three ``scheduled_message_*`` fields are the optional create-time kickoff
    message. The result is suitable both for validation and for re-rendering the
    form after a validation failure.
    """
    return {
        "title": (form.get("title") or "").strip(),
        "description": (form.get("description") or "").strip(),
        "start_time": (form.get("start_time") or "").strip(),
        "release_cadence_hours": form.get("release_cadence_hours") or "24",
        "announcement_channels": list(form.getall("announcement_channels", [])),
        "is_active": form.get("is_active") == "on",
        "scheduled_message_title": (
            form.get("scheduled_message_title") or ""
        ).strip(),
        "scheduled_message_description": (
            form.get("scheduled_message_description") or ""
        ).strip(),
        "scheduled_message_time": (form.get("scheduled_message_time") or "").strip(),
    }


def validate_campaign_form(
    data: dict, *, require_future_start: bool
) -> tuple[bool, list[str], dict]:
    """Validate a raw campaign form dict, returning cleaned values.

    Pure function — does **not** mutate ``data``. Returns
    ``(is_valid, errors, cleaned)``; ``cleaned`` holds the typed values ready to
    pass to :class:`CampaignOperations`. Mirrors the legacy validation rules:
    required title/description/start-time, at least one announcement channel, a
    1-168 hour release cadence, and — on create (``require_future_start``) — a
    start time in the future. The optional kickoff ``scheduled_message_*`` fields
    require a title whenever a time is given.
    """
    errors: list[str] = []

    title = str(data.get("title", "")).strip()
    if not title:
        errors.append("Title is required")

    description = str(data.get("description", "")).strip()
    if not description:
        errors.append("Description is required")

    start_time_str = str(data.get("start_time", "")).strip()
    if not start_time_str:
        errors.append("Start time is required")

    raw_channels = data.get("announcement_channels", [])
    announcement_channels = [
        channel.strip() for channel in raw_channels if channel and channel.strip()
    ]
    if not announcement_channels:
        errors.append("At least one announcement channel is required")

    release_cadence_hours = 24
    try:
        release_cadence_hours = int(data.get("release_cadence_hours", 24))
        if not (
            _MIN_RELEASE_CADENCE_HOURS
            <= release_cadence_hours
            <= _MAX_RELEASE_CADENCE_HOURS
        ):
            errors.append("Release cadence must be between 1 and 168 hours")
    except (ValueError, TypeError):
        errors.append("Invalid release cadence")

    start_time = None
    if start_time_str:
        try:
            start_time = parse_campaign_datetime(start_time_str)
            if require_future_start and start_time <= datetime.now(timezone.utc):
                errors.append("Start time must be in the future")
        except (ValueError, TypeError):
            errors.append("Invalid start time format")

    scheduled_message_title = str(data.get("scheduled_message_title", "")).strip()
    scheduled_message_description = str(
        data.get("scheduled_message_description", "")
    ).strip()
    scheduled_message_time_str = str(data.get("scheduled_message_time", "")).strip()
    scheduled_message_time = None
    if scheduled_message_time_str:
        try:
            scheduled_message_time = parse_campaign_datetime(
                scheduled_message_time_str
            )
        except (ValueError, TypeError):
            errors.append("Invalid scheduled message time format")
    if scheduled_message_time and not scheduled_message_title:
        errors.append(
            "Scheduled message title is required when scheduled message time is set"
        )

    cleaned = {
        "title": title,
        "description": description,
        "start_time": start_time,
        "release_cadence_hours": release_cadence_hours,
        "announcement_channels": announcement_channels,
        "is_active": bool(data.get("is_active", False)),
        "scheduled_message_title": scheduled_message_title or None,
        "scheduled_message_description": scheduled_message_description or None,
        "scheduled_message_time": scheduled_message_time,
    }
    return len(errors) == 0, errors, cleaned


def validate_challenge_fields(title: str, description: str) -> list[str]:
    """Validate a submitted challenge's title/description. Pure function.

    Mirrors the legacy ``challenge_create`` view's required-field checks.
    """
    errors: list[str] = []
    if not title.strip():
        errors.append("Challenge title is required")
    if not description.strip():
        errors.append("Challenge description is required")
    return errors


def next_challenge_position(challenges: list) -> int:
    """Return the next 1-based order position after the existing challenges.

    Pure function. Mirrors the legacy view: the new challenge is appended after
    the highest existing ``order_position`` (or position 1 for an empty
    campaign).
    """
    if not challenges:
        return 1
    return max(challenge.order_position for challenge in challenges) + 1


async def load_announcement_channels(guild_id: str) -> list:
    """Fetch the guild's announcement-eligible channels, empty on Discord error.

    Parity with the legacy view, which degraded to an empty channel list rather
    than failing the whole page when the channel fetch raised.
    """
    client = get_admin_discord_client()
    try:
        return await client.get_announcement_channels(guild_id)
    except DiscordAdminError:
        logger.warning(
            "Failed to fetch channels for guild %s; using empty list", guild_id
        )
        return []


class CampaignsAdminController(Controller):
    """Campaign CRUD and challenge creation under ``/admin/bot``."""

    path = "/admin/bot"
    guards = [auth_guard]

    # -- List -----------------------------------------------------------------

    @get(
        "/guilds/{guild_id:str}/campaigns",
        guards=[auth_guard, Permission("administrator")],
    )
    async def campaigns_list(
        self,
        request: Request,
        db_session: AsyncSession,
        guild_id: str,
        page: int = 1,
    ) -> TemplateResponse:
        """List a guild's campaigns, paginated."""
        guild, error = await fetch_guild_or_error(
            request, db_session, guild_id
        )
        if error is not None:
            return error

        page = max(1, page)
        offset = (page - 1) * _CAMPAIGN_PAGE_SIZE
        campaigns, total_count = await CampaignOperations(
            db_session
        ).get_campaigns_by_guild(
            guild_id=guild_id, limit=_CAMPAIGN_PAGE_SIZE, offset=offset
        )
        total_pages = (total_count + _CAMPAIGN_PAGE_SIZE - 1) // _CAMPAIGN_PAGE_SIZE

        ctx = await get_admin_context(request, db_session)
        return TemplateResponse(
            "admin/bot/campaigns/list.html",
            context={
                "guild": guild,
                "campaigns": campaigns,
                "page": page,
                "total_pages": total_pages,
                "total_count": total_count,
                "active_page": "campaigns",
                "guild_id": guild_id,
                "flash_messages": get_flash_messages(request),
                **ctx,
            },
        )

    # -- Create ---------------------------------------------------------------

    @get(
        "/guilds/{guild_id:str}/campaigns/create",
        guards=[auth_guard, Permission("administrator")],
    )
    async def campaign_create_form(
        self, request: Request, db_session: AsyncSession, guild_id: str
    ) -> TemplateResponse:
        """Render the blank create-campaign form."""
        guild, error = await fetch_guild_or_error(
            request, db_session, guild_id
        )
        if error is not None:
            return error

        channels = await load_announcement_channels(guild_id)
        ctx = await get_admin_context(request, db_session)
        return TemplateResponse(
            "admin/bot/campaigns/create.html",
            context={
                "guild": guild,
                "channels": channels,
                "errors": [],
                "form_data": None,
                "active_page": "campaigns",
                "guild_id": guild_id,
                **ctx,
            },
        )

    @post(
        "/guilds/{guild_id:str}/campaigns/create",
        guards=[auth_guard, Permission("administrator")],
    )
    async def campaign_create(
        self, request: Request, db_session: AsyncSession, guild_id: str
    ) -> Response:
        """Validate and create a campaign, then redirect to the list."""
        guild, error = await fetch_guild_or_error(
            request, db_session, guild_id
        )
        if error is not None:
            return error

        form = await request.form()
        data = read_campaign_form(form)
        is_valid, errors, cleaned = validate_campaign_form(
            data, require_future_start=True
        )

        if not is_valid:
            return await render_create_form(
                request, db_session, guild, guild_id, data, errors, status_code=400
            )

        try:
            await CampaignOperations(db_session).create_campaign(
                guild_id=guild_id, created_by=_CREATED_BY, **_campaign_kwargs(cleaned)
            )
        except ConflictError as exc:
            return await render_create_form(
                request, db_session, guild, guild_id, data, [str(exc)],
                status_code=400,
            )

        flash_success(request, "Campaign created successfully!")
        return Redirect(path=f"/admin/bot/guilds/{guild_id}/campaigns")

    # -- Edit -----------------------------------------------------------------

    @get(
        "/guilds/{guild_id:str}/campaigns/{campaign_id:uuid}/edit",
        guards=[auth_guard, Permission("administrator")],
    )
    async def campaign_edit_form(
        self,
        request: Request,
        db_session: AsyncSession,
        guild_id: str,
        campaign_id: UUID,
    ) -> Response:
        """Render the edit form pre-populated from an existing campaign."""
        guild, error = await fetch_guild_or_error(
            request, db_session, guild_id
        )
        if error is not None:
            return error

        campaign = await CampaignOperations(db_session).get_campaign_by_id(
            campaign_id, guild_id
        )
        if campaign is None:
            return await render_campaign_not_found(
                request, db_session, guild_id
            )

        channels = await load_announcement_channels(guild_id)
        ctx = await get_admin_context(request, db_session)
        return TemplateResponse(
            "admin/bot/campaigns/edit.html",
            context={
                "guild": guild,
                "campaign": campaign,
                "channels": channels,
                "errors": [],
                "form_data": None,
                "active_page": "campaigns",
                "guild_id": guild_id,
                **ctx,
            },
        )

    @post(
        "/guilds/{guild_id:str}/campaigns/{campaign_id:uuid}/edit",
        guards=[auth_guard, Permission("administrator")],
    )
    async def campaign_edit(
        self,
        request: Request,
        db_session: AsyncSession,
        guild_id: str,
        campaign_id: UUID,
    ) -> Response:
        """Validate and apply edits to a campaign, then redirect to the list."""
        guild, error = await fetch_guild_or_error(
            request, db_session, guild_id
        )
        if error is not None:
            return error

        ops = CampaignOperations(db_session)
        campaign = await ops.get_campaign_by_id(campaign_id, guild_id)
        if campaign is None:
            return await render_campaign_not_found(
                request, db_session, guild_id
            )

        form = await request.form()
        data = read_campaign_form(form)
        is_valid, errors, cleaned = validate_campaign_form(
            data, require_future_start=False
        )

        if not is_valid:
            return await render_edit_form(
                request, db_session, guild, guild_id, campaign, data, errors,
                status_code=400,
            )

        try:
            await ops.update_campaign(
                campaign_id=campaign_id,
                guild_id=guild_id,
                title=cleaned["title"],
                description=cleaned["description"],
                start_time=cleaned["start_time"],
                release_cadence_hours=cleaned["release_cadence_hours"],
                announcement_channels=cleaned["announcement_channels"],
                is_active=cleaned["is_active"],
            )
        except ConflictError as exc:
            return await render_edit_form(
                request, db_session, guild, guild_id, campaign, data, [str(exc)],
                status_code=400,
            )

        flash_success(request, "Campaign updated successfully!")
        return Redirect(path=f"/admin/bot/guilds/{guild_id}/campaigns")

    # -- Delete ---------------------------------------------------------------

    @post(
        "/guilds/{guild_id:str}/campaigns/{campaign_id:uuid}/delete",
        guards=[auth_guard, Permission("administrator")],
    )
    async def campaign_delete(
        self,
        request: Request,
        db_session: AsyncSession,
        guild_id: str,
        campaign_id: UUID,
    ) -> Redirect:
        """Soft-delete (deactivate) a campaign, then redirect to the list."""
        deleted = await CampaignOperations(db_session).delete_campaign(
            campaign_id, guild_id
        )
        if deleted:
            flash_success(request, "Campaign deleted successfully!")
        else:
            flash_error(request, "Campaign not found.")
        return Redirect(path=f"/admin/bot/guilds/{guild_id}/campaigns")

    # -- Challenges -----------------------------------------------------------

    @get(
        "/guilds/{guild_id:str}/campaigns/{campaign_id:uuid}/challenges",
        guards=[auth_guard, Permission("administrator")],
    )
    async def campaign_challenges(
        self,
        request: Request,
        db_session: AsyncSession,
        guild_id: str,
        campaign_id: UUID,
    ) -> Response:
        """List the challenges within a campaign."""
        guild, error = await fetch_guild_or_error(
            request, db_session, guild_id
        )
        if error is not None:
            return error

        campaign = await CampaignOperations(db_session).get_campaign_with_challenges(
            campaign_id, guild_id
        )
        if campaign is None:
            return await render_campaign_not_found(
                request, db_session, guild_id
            )

        ctx = await get_admin_context(request, db_session)
        return TemplateResponse(
            "admin/bot/campaigns/challenges.html",
            context={
                "guild": guild,
                "campaign": campaign,
                "challenges": campaign.challenges,
                "active_page": "campaigns",
                "guild_id": guild_id,
                "flash_messages": get_flash_messages(request),
                **ctx,
            },
        )

    @get(
        "/guilds/{guild_id:str}/campaigns/{campaign_id:uuid}/challenges/create",
        guards=[auth_guard, Permission("administrator")],
    )
    async def challenge_create_form(
        self,
        request: Request,
        db_session: AsyncSession,
        guild_id: str,
        campaign_id: UUID,
    ) -> Response:
        """Render the blank create-challenge form for one campaign."""
        guild, error = await fetch_guild_or_error(
            request, db_session, guild_id
        )
        if error is not None:
            return error

        campaign = await CampaignOperations(db_session).get_campaign_by_id(
            campaign_id, guild_id
        )
        if campaign is None:
            return await render_campaign_not_found(
                request, db_session, guild_id
            )

        ctx = await get_admin_context(request, db_session)
        return TemplateResponse(
            "admin/bot/campaigns/challenge_create.html",
            context={
                "guild": guild,
                "campaign": campaign,
                "errors": [],
                "form_data": None,
                "active_page": "campaigns",
                "guild_id": guild_id,
                **ctx,
            },
        )

    @post(
        "/guilds/{guild_id:str}/campaigns/{campaign_id:uuid}/challenges/create",
        guards=[auth_guard, Permission("administrator")],
    )
    async def challenge_create(
        self,
        request: Request,
        db_session: AsyncSession,
        guild_id: str,
        campaign_id: UUID,
    ) -> Response:
        """Validate and create a challenge in a campaign, then redirect back."""
        guild, error = await fetch_guild_or_error(
            request, db_session, guild_id
        )
        if error is not None:
            return error

        ops = CampaignOperations(db_session)
        campaign = await ops.get_campaign_by_id(campaign_id, guild_id)
        if campaign is None:
            return await render_campaign_not_found(
                request, db_session, guild_id
            )

        form = await request.form()
        title = (form.get("title") or "").strip()
        description = (form.get("description") or "").strip()
        errors = validate_challenge_fields(title, description)

        python_script, script_error = await _read_python_script(
            form.get("python_script")
        )
        if script_error:
            errors.append(script_error)

        if errors:
            return await render_challenge_create_form(
                request, db_session, guild, guild_id, campaign,
                {"title": title, "description": description}, errors,
                status_code=400,
            )

        await ops.create_challenge(
            campaign_id=campaign_id,
            title=title,
            description=description,
            order_position=next_challenge_position(campaign.challenges),
            python_script=python_script,
            input_generator_script=python_script,
        )

        flash_success(request, "Challenge created successfully!")
        return Redirect(
            path=f"/admin/bot/guilds/{guild_id}/campaigns/{campaign_id}/challenges"
        )

async def fetch_guild_or_error(
    request: Request, db_session: AsyncSession, guild_id: str
) -> tuple[object | None, TemplateResponse | None]:
    """Fetch guild details, returning an error template on failure.

    Returns ``(guild, None)`` on success or ``(None, response)`` when the guild
    is missing (404) or Discord is unavailable (503). Mirrors the error handling
    used by the sibling bytes-config and squads controllers.
    """
    client = get_admin_discord_client()
    try:
        guild = await client.get_guild(guild_id)
    except GuildNotFoundError:
        return None, await render_error(
            request, db_session, guild_id,
            f"Guild {guild_id} not found or bot is not a member.", 404,
        )
    except DiscordAdminError as exc:
        return None, await render_error(
            request, db_session, guild_id, f"Discord API error: {exc}", 503
        )
    return guild, None


async def render_error(
    request: Request,
    db_session: AsyncSession,
    guild_id: str,
    message: str,
    status_code: int,
) -> TemplateResponse:
    """Render the shared guild error page."""
    ctx = await get_admin_context(request, db_session)
    return TemplateResponse(
        "admin/bot/guilds/error.html",
        context={
            "error": message,
            "error_code": status_code,
            "active_page": "campaigns",
            "guild_id": guild_id,
            **ctx,
        },
        status_code=status_code,
    )


async def render_campaign_not_found(
    request: Request, db_session: AsyncSession, guild_id: str
) -> TemplateResponse:
    """Render the shared error page for a missing campaign."""
    return await render_error(
        request, db_session, guild_id, "Campaign not found.", 404
    )


async def render_create_form(
    request: Request,
    db_session: AsyncSession,
    guild: object,
    guild_id: str,
    form_data: dict | None,
    errors: list[str],
    status_code: int,
) -> TemplateResponse:
    """Render the create form with (optionally) submitted values and errors."""
    channels = await load_announcement_channels(guild_id)
    ctx = await get_admin_context(request, db_session)
    return TemplateResponse(
        "admin/bot/campaigns/create.html",
        context={
            "guild": guild,
            "channels": channels,
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
    form_data: dict | None,
    errors: list[str],
    status_code: int,
) -> TemplateResponse:
    """Render the edit form with (optionally) submitted values and errors."""
    channels = await load_announcement_channels(guild_id)
    ctx = await get_admin_context(request, db_session)
    return TemplateResponse(
        "admin/bot/campaigns/edit.html",
        context={
            "guild": guild,
            "campaign": campaign,
            "channels": channels,
            "errors": errors,
            "form_data": form_data,
            "active_page": "campaigns",
            "guild_id": guild_id,
            **ctx,
        },
        status_code=status_code,
    )


async def render_challenge_create_form(
    request: Request,
    db_session: AsyncSession,
    guild: object,
    guild_id: str,
    campaign: object,
    form_data: dict | None,
    errors: list[str],
    status_code: int,
) -> TemplateResponse:
    """Render the challenge-create form with submitted values and errors."""
    ctx = await get_admin_context(request, db_session)
    return TemplateResponse(
        "admin/bot/campaigns/challenge_create.html",
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


def _campaign_kwargs(cleaned: dict) -> dict:
    """Map a validated campaign form dict to ``create_campaign`` keyword args.

    Pure function. Includes the optional kickoff scheduled-message fields the
    create path accepts.
    """
    return {
        "title": cleaned["title"],
        "description": cleaned["description"],
        "start_time": cleaned["start_time"],
        "release_cadence_hours": cleaned["release_cadence_hours"],
        "announcement_channels": cleaned["announcement_channels"],
        "scheduled_message_title": cleaned["scheduled_message_title"],
        "scheduled_message_description": cleaned["scheduled_message_description"],
        "scheduled_message_time": cleaned["scheduled_message_time"],
    }


async def _read_python_script(upload: object) -> tuple[str | None, str | None]:
    """Read an optional uploaded Python script, returning ``(content, error)``.

    Mirrors the legacy view: an empty upload yields ``(None, None)``; a
    non-``.py`` filename yields an error; a decode failure yields an error.
    """
    if not upload or not getattr(upload, "filename", ""):
        return None, None

    filename = upload.filename
    if not filename.endswith(".py"):
        return None, "Script file must be a .py file"

    try:
        content = await upload.read()
        return content.decode("utf-8"), None
    except (UnicodeDecodeError, ValueError) as exc:
        return None, f"Error reading script file: {exc}"
