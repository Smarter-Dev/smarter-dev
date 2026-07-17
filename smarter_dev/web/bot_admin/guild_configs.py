"""Per-guild feature configuration pages for the Skrift admin panel.

Ports ``audit_log_config``, ``advent_of_code_config`` and
``attachment_filter_config`` from the legacy ``smarter_dev.web.admin.views`` onto
a single Skrift-native Litestar controller under ``/admin/bot``. Guild identity
comes from Discord (via :mod:`smarter_dev.web.discord_admin_client`); the three
settings groups live in the ``audit_log_configs``, ``advent_of_code_configs`` and
``attachment_filter_configs`` tables.

Form parsing is factored into pure module-level helpers so the accepted-field
contract can be unit-tested without a request or a database.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from litestar import Controller, Request, get, post
from litestar.response import Redirect, Template as TemplateResponse
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.admin.helpers import get_admin_context
from skrift.auth.guards import Permission, auth_guard
from skrift.flash import flash_error, flash_success, get_flash_messages

from smarter_dev.web.bot_admin.squads import fetch_guild_or_error
from smarter_dev.web.crud import (
    AdventOfCodeConfigOperations,
    AttachmentFilterConfigOperations,
    AuditLogConfigOperations,
)
from smarter_dev.web.discord_admin_client import (
    DiscordAdminError,
    get_admin_discord_client,
)
from smarter_dev.web.models import (
    AdventOfCodeConfig,
    AttachmentFilterConfig,
    AuditLogConfig,
)

logger = logging.getLogger(__name__)

audit_ops = AuditLogConfigOperations()
advent_ops = AdventOfCodeConfigOperations()
attachment_ops = AttachmentFilterConfigOperations()

# Discord channel type ids used by the two channel pickers.
_TEXT_CHANNEL_TYPES: frozenset[int] = frozenset({0, 5})  # GUILD_TEXT, GUILD_NEWS
_FORUM_CHANNEL_TYPE: int = 15  # GUILD_FORUM

# The nine boolean event toggles the audit-log form submits, in display order.
_AUDIT_EVENT_FIELDS: tuple[str, ...] = (
    "log_member_join",
    "log_member_leave",
    "log_member_ban",
    "log_member_unban",
    "log_message_edit",
    "log_message_delete",
    "log_username_change",
    "log_nickname_change",
    "log_role_change",
)


class InvalidChannelError(ValueError):
    """Raised when a submitted channel id is not a Discord snowflake."""


def validate_channel_id(raw: str | None) -> str | None:
    """Normalise a submitted channel id, or ``None`` when the field is blank.

    Pure function — no I/O — so the parsing rules can be unit-tested directly.
    A blank value clears the channel (a valid, expected choice from the "Disabled"
    option). A non-blank value must be a Discord snowflake (all digits); anything
    else is a malformed submission and fails fast.

    Raises:
        InvalidChannelError: If a non-blank value is not all digits.
    """
    channel_id = (raw or "").strip()
    if not channel_id:
        return None
    if not channel_id.isdigit():
        raise InvalidChannelError(f"Invalid channel id: {channel_id!r}")
    return channel_id


def parse_extensions(raw: str | None) -> list[str]:
    """Parse a file-extension textarea into a normalised, de-duplicated list.

    Pure function mirroring the legacy view: accepts one-per-line or
    comma-separated input, lower-cases each entry, ensures a leading dot, and
    removes duplicates while preserving first-seen order.
    """
    extensions: list[str] = []
    for line in (raw or "").replace(",", "\n").split("\n"):
        ext = line.strip().lower()
        if ext:
            if not ext.startswith("."):
                ext = "." + ext
            extensions.append(ext)
    return list(dict.fromkeys(extensions))


def parse_audit_log_form(form: Mapping[str, str]) -> dict:
    """Turn a submitted audit-log form into keyword arguments for the model.

    Pure function. The channel id is validated as a snowflake (or cleared); each
    of the nine event toggles is a checkbox that submits ``"on"`` when checked.

    Raises:
        InvalidChannelError: If ``audit_channel_id`` is a non-blank non-snowflake.
    """
    updates: dict = {
        "audit_channel_id": validate_channel_id(form.get("audit_channel_id")),
    }
    for field in _AUDIT_EVENT_FIELDS:
        updates[field] = form.get(field) == "on"
    return updates


def parse_advent_of_code_form(form: Mapping[str, str]) -> dict:
    """Turn a submitted Advent-of-Code form into keyword arguments for the model.

    Pure function.

    Raises:
        InvalidChannelError: If ``forum_channel_id`` is a non-blank non-snowflake.
    """
    return {
        "forum_channel_id": validate_channel_id(form.get("forum_channel_id")),
        "is_active": form.get("is_active") == "on",
    }


def parse_attachment_filter_form(form: Mapping[str, str]) -> dict:
    """Turn a submitted attachment-filter form into keyword arguments.

    Pure function mirroring the legacy view.
    """
    return {
        "is_active": form.get("is_active") == "on",
        "warn_message": (form.get("warn_message") or "").strip() or None,
        "delete_message": (form.get("delete_message") or "").strip() or None,
        "ignored_extensions": parse_extensions(form.get("ignored_extensions")),
        "warn_extensions": parse_extensions(form.get("warn_extensions")),
    }


async def load_or_create_audit_config(
    db_session: AsyncSession, guild_id: str
) -> AuditLogConfig:
    """Return the guild's audit-log config, persisting a default row if absent."""
    config = await audit_ops.get_or_create_config(db_session, guild_id)
    await db_session.commit()
    return config


async def load_or_create_advent_config(
    db_session: AsyncSession, guild_id: str
) -> AdventOfCodeConfig:
    """Return the guild's AoC config, persisting a default row if absent."""
    config = await advent_ops.get_or_create_config(db_session, guild_id)
    await db_session.commit()
    return config


async def load_or_create_attachment_config(
    db_session: AsyncSession, guild_id: str
) -> AttachmentFilterConfig:
    """Return the guild's attachment-filter config, persisting a default row."""
    config = await attachment_ops.get_or_create_config(db_session, guild_id)
    await db_session.commit()
    return config


async def fetch_channels_of_types(
    guild_id: str, channel_types: frozenset[int]
) -> list:
    """Return the guild's channels whose type is in ``channel_types``.

    Channel fetching is best-effort (parity with the legacy views): a Discord
    failure yields an empty list rather than failing the whole page.
    """
    client = get_admin_discord_client()
    try:
        channels = await client.get_guild_channels(guild_id)
    except DiscordAdminError:
        logger.warning(
            "Failed to fetch channels for guild %s; using empty list", guild_id
        )
        return []
    return [channel for channel in channels if channel.type in channel_types]


class GuildConfigsAdminController(Controller):
    """Audit-log, Advent-of-Code and attachment-filter config under ``/admin/bot``."""

    path = "/admin/bot"
    guards = [auth_guard]

    # -- Audit log ------------------------------------------------------------

    @get(
        "/guilds/{guild_id:str}/audit-logs",
        guards=[auth_guard, Permission("administrator")],
    )
    async def audit_log_config(
        self, request: Request, db_session: AsyncSession, guild_id: str
    ) -> TemplateResponse:
        """Render the audit-log configuration form for one guild."""
        guild, error = await fetch_guild_or_error(
            request, db_session, guild_id, "audit_logs"
        )
        if error is not None:
            return error

        ctx = await get_admin_context(request, db_session)
        channels = await fetch_channels_of_types(guild_id, _TEXT_CHANNEL_TYPES)
        config = await load_or_create_audit_config(db_session, guild_id)

        return TemplateResponse(
            "admin/bot/guild_configs/audit_logs.html",
            context={
                "guild": guild,
                "config": config,
                "channels": channels,
                "active_page": "audit_logs",
                "guild_id": guild_id,
                "flash_messages": get_flash_messages(request),
                **ctx,
            },
        )

    @post(
        "/guilds/{guild_id:str}/audit-logs",
        guards=[auth_guard, Permission("administrator")],
    )
    async def save_audit_log_config(
        self, request: Request, db_session: AsyncSession, guild_id: str
    ) -> Redirect:
        """Persist submitted audit-log settings, then redirect back."""
        redirect = Redirect(path=f"/admin/bot/guilds/{guild_id}/audit-logs")

        form = await request.form()
        try:
            updates = parse_audit_log_form(form)
        except InvalidChannelError:
            flash_error(request, "Invalid channel selection. Please try again.")
            return redirect

        await audit_ops.update_config(db_session, guild_id, **updates)
        await db_session.commit()

        flash_success(request, "Audit log configuration updated successfully!")
        return redirect

    # -- Advent of Code -------------------------------------------------------

    @get(
        "/guilds/{guild_id:str}/advent-of-code",
        guards=[auth_guard, Permission("administrator")],
    )
    async def advent_of_code_config(
        self, request: Request, db_session: AsyncSession, guild_id: str
    ) -> TemplateResponse:
        """Render the Advent-of-Code configuration form for one guild."""
        guild, error = await fetch_guild_or_error(
            request, db_session, guild_id, "advent_of_code"
        )
        if error is not None:
            return error

        ctx = await get_admin_context(request, db_session)
        forum_channels = await fetch_channels_of_types(
            guild_id, frozenset({_FORUM_CHANNEL_TYPE})
        )
        config = await load_or_create_advent_config(db_session, guild_id)
        threads = await advent_ops.get_guild_threads(db_session, guild_id)

        return TemplateResponse(
            "admin/bot/guild_configs/advent_of_code.html",
            context={
                "guild": guild,
                "config": config,
                "forum_channels": forum_channels,
                "threads": threads,
                "active_page": "advent_of_code",
                "guild_id": guild_id,
                "flash_messages": get_flash_messages(request),
                **ctx,
            },
        )

    @post(
        "/guilds/{guild_id:str}/advent-of-code",
        guards=[auth_guard, Permission("administrator")],
    )
    async def save_advent_of_code_config(
        self, request: Request, db_session: AsyncSession, guild_id: str
    ) -> Redirect:
        """Persist submitted Advent-of-Code settings, then redirect back."""
        redirect = Redirect(path=f"/admin/bot/guilds/{guild_id}/advent-of-code")

        form = await request.form()
        try:
            updates = parse_advent_of_code_form(form)
        except InvalidChannelError:
            flash_error(request, "Invalid channel selection. Please try again.")
            return redirect

        await advent_ops.update_config(db_session, guild_id, **updates)
        await db_session.commit()

        flash_success(
            request, "Advent of Code configuration updated successfully!"
        )
        return redirect

    # -- Attachment filter ----------------------------------------------------

    @get(
        "/guilds/{guild_id:str}/attachment-filter",
        guards=[auth_guard, Permission("administrator")],
    )
    async def attachment_filter_config(
        self, request: Request, db_session: AsyncSession, guild_id: str
    ) -> TemplateResponse:
        """Render the attachment-filter configuration form for one guild."""
        guild, error = await fetch_guild_or_error(
            request, db_session, guild_id, "attachment_filter"
        )
        if error is not None:
            return error

        ctx = await get_admin_context(request, db_session)
        config = await load_or_create_attachment_config(db_session, guild_id)

        return TemplateResponse(
            "admin/bot/guild_configs/attachment_filter.html",
            context={
                "guild": guild,
                "config": config,
                "active_page": "attachment_filter",
                "guild_id": guild_id,
                "flash_messages": get_flash_messages(request),
                **ctx,
            },
        )

    @post(
        "/guilds/{guild_id:str}/attachment-filter",
        guards=[auth_guard, Permission("administrator")],
    )
    async def save_attachment_filter_config(
        self, request: Request, db_session: AsyncSession, guild_id: str
    ) -> Redirect:
        """Persist submitted attachment-filter settings, then redirect back."""
        redirect = Redirect(
            path=f"/admin/bot/guilds/{guild_id}/attachment-filter"
        )

        form = await request.form()
        updates = parse_attachment_filter_form(form)

        await attachment_ops.update_config(db_session, guild_id, **updates)
        await db_session.commit()

        flash_success(
            request, "Attachment filter configuration updated successfully!"
        )
        return redirect
