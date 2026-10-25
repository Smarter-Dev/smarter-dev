"""Per-guild feature configuration pages for the Skrift admin panel.

Ports ``audit_log_config`` from the legacy ``smarter_dev.web.admin.views`` onto
a single Skrift-native Litestar controller under ``/admin/bot``, and adds the
Content Filters page for the bot-core content filters plus the guild-rules
editor the ``/rule`` command cites. Guild identity comes from Discord (via
:mod:`smarter_dev.web.discord_admin_client`); the three settings groups live in
the ``audit_log_configs``, ``moderation_filter_configs`` and
``guild_rules_configs`` tables.

The attachment filter and the spam engine, spam ring, staff roles and report
channels the old forms edited are the guild's moderation policy's (the
Moderation page, :mod:`smarter_dev.web.bot_admin.moderation_policy`); their
old tables and columns were dropped in #242. The old attachment-filter URL
redirects to the Moderation page.

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
from skrift.flash import (
    flash_error,
    flash_success,
    flash_warning,
    get_flash_messages,
)

from smarter_dev.web.bot_admin.squads import fetch_guild_or_error
from smarter_dev.web.crud import (
    AuditLogConfigOperations,
    GuildRulesConfigOperations,
    ModerationFilterConfigOperations,
)
from smarter_dev.web.discord_admin_client import (
    DiscordAdminError,
    get_admin_discord_client,
)
from smarter_dev.web.guild_rules import normalize_newlines, parse_guild_rules
from smarter_dev.web.models import (
    AuditLogConfig,
    GuildRulesConfig,
    ModerationFilterConfig,
)

logger = logging.getLogger(__name__)

audit_ops = AuditLogConfigOperations()
moderation_filter_ops = ModerationFilterConfigOperations()
guild_rules_ops = GuildRulesConfigOperations()

# Discord channel type ids used by the channel pickers.
_TEXT_CHANNEL_TYPES: frozenset[int] = frozenset({0, 5})  # GUILD_TEXT, GUILD_NEWS
_CATEGORY_CHANNEL_TYPE: int = 4  # GUILD_CATEGORY

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


# The boolean toggles the Content Filters form submits, in display order.
_CONTENT_FILTER_TOGGLE_FIELDS: tuple[str, ...] = (
    "content_filter_enabled",
    "invite_filter_enabled",
    "webhook_killer_enabled",
)

#: Everything the Content Filters form shows and saves: the content filter's
#: own settings. The rest of moderation is the guild's moderation policy's.
CONTENT_FILTER_FIELDS: frozenset[str] = frozenset(
    {
        *_CONTENT_FILTER_TOGGLE_FIELDS,
        "blocked_tlds",
        "invite_filter_exempt_category_ids",
        "mod_log_channel_id",
    }
)


class ConfigFormError(ValueError):
    """Base class for guild-config form submissions that fail validation."""


class InvalidChannelError(ConfigFormError):
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
    """Parse a dotted-token textarea into a normalised, de-duplicated list.

    Pure function mirroring the legacy view: accepts one-per-line or
    comma-separated input, lower-cases each entry, ensures a leading dot, and
    removes duplicates while preserving first-seen order. Used for both file
    extensions and blocked top-level domains, which share that exact shape.
    """
    extensions: list[str] = []
    for line in (raw or "").replace(",", "\n").split("\n"):
        ext = line.strip().lower()
        if ext:
            if not ext.startswith("."):
                ext = "." + ext
            extensions.append(ext)
    return list(dict.fromkeys(extensions))


def split_id_list(raw: str | None) -> list[str]:
    """Split a comma- or newline-separated id field into unique entries.

    Pure function; performs no snowflake validation, so callers pair it with
    the validator matching the kind of id they expect.
    """
    entries = (
        entry.strip() for entry in (raw or "").replace(",", "\n").split("\n")
    )
    return list(dict.fromkeys(entry for entry in entries if entry))


def parse_channel_ids(raw: str | None) -> list[str]:
    """Parse a channel/category-id field into a de-duplicated list of snowflakes.

    Pure function.

    Raises:
        InvalidChannelError: If any entry is not all digits.
    """
    channel_ids = split_id_list(raw)
    for channel_id in channel_ids:
        if not channel_id.isdigit():
            raise InvalidChannelError(f"Invalid channel id: {channel_id!r}")
    return channel_ids


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


def parse_moderation_filter_form(form: Mapping[str, str]) -> dict:
    """Turn a submitted Content Filters form into keyword arguments.

    Pure function. Only the content filter's own fields
    (:data:`CONTENT_FILTER_FIELDS`) are read; anything else submitted is
    dropped.

    Raises:
        InvalidChannelError: If a channel or category id is a non-blank
            non-snowflake.
    """
    updates: dict = {
        "blocked_tlds": parse_extensions(form.get("blocked_tlds")),
        "invite_filter_exempt_category_ids": parse_channel_ids(
            form.get("invite_filter_exempt_category_ids")
        ),
        "mod_log_channel_id": validate_channel_id(form.get("mod_log_channel_id")),
    }
    for field in _CONTENT_FILTER_TOGGLE_FIELDS:
        updates[field] = form.get(field) == "on"
    return updates


def parse_guild_rules_form(form: Mapping[str, str]) -> dict:
    """Turn a submitted guild-rules form into keyword arguments for the model.

    Pure function. The rules document is stored verbatim apart from two
    normalisations: browser CRLF endings become LF (so stored markdown matches
    what the parser sees), and surrounding blank space is trimmed (so an
    "emptied" textarea stores as the empty string rather than stray newlines).

    Never raises: any text is a valid submission. Whether it parses into rules
    is reported back to the admin by the page's preview, not by rejecting it.
    """
    return {"rules_markdown": normalize_newlines(form.get("rules_markdown")).strip()}


async def load_or_create_audit_config(
    db_session: AsyncSession, guild_id: str
) -> AuditLogConfig:
    """Return the guild's audit-log config, persisting a default row if absent."""
    config = await audit_ops.get_or_create_config(db_session, guild_id)
    await db_session.commit()
    return config


async def load_or_create_moderation_filter_config(
    db_session: AsyncSession, guild_id: str
) -> ModerationFilterConfig:
    """Return the guild's content filter config, persisting a default row."""
    config = await moderation_filter_ops.get_or_create_config(db_session, guild_id)
    await db_session.commit()
    return config


async def load_or_create_guild_rules_config(
    db_session: AsyncSession, guild_id: str
) -> GuildRulesConfig:
    """Return the guild's rules config, persisting an empty document if absent."""
    config = await guild_rules_ops.get_or_create_config(db_session, guild_id)
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


async def fetch_guild_roles(guild_id: str) -> list:
    """Return the guild's roles for a role picker.

    Best-effort like :func:`fetch_channels_of_types`: a Discord failure yields
    an empty picker rather than failing the whole page.
    """
    client = get_admin_discord_client()
    try:
        return await client.get_guild_roles(guild_id)
    except DiscordAdminError:
        logger.warning(
            "Failed to fetch roles for guild %s; using empty list", guild_id
        )
        return []


class GuildConfigsAdminController(Controller):
    """Audit-log, content filter and rules config under ``/admin/bot``."""

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

    # -- Attachment filter (moved) --------------------------------------------

    @get(
        "/guilds/{guild_id:str}/attachment-filter",
        guards=[auth_guard, Permission("administrator")],
    )
    async def attachment_filter_config(self, guild_id: str) -> Redirect:
        """The attachment rule is on the Moderation page (#242)."""
        return Redirect(path=f"/admin/bot/guilds/{guild_id}/moderation")

    # -- Moderation filter ----------------------------------------------------

    @get(
        "/guilds/{guild_id:str}/moderation-filter",
        guards=[auth_guard, Permission("administrator")],
    )
    async def moderation_filter_config(
        self, request: Request, db_session: AsyncSession, guild_id: str
    ) -> TemplateResponse:
        """Render the Content Filters form for one guild."""
        guild, error = await fetch_guild_or_error(
            request, db_session, guild_id, "moderation_filter"
        )
        if error is not None:
            return error

        ctx = await get_admin_context(request, db_session)
        text_channels = await fetch_channels_of_types(guild_id, _TEXT_CHANNEL_TYPES)
        category_channels = await fetch_channels_of_types(
            guild_id, frozenset({_CATEGORY_CHANNEL_TYPE})
        )
        config = await load_or_create_moderation_filter_config(db_session, guild_id)

        return TemplateResponse(
            "admin/bot/guild_configs/moderation_filter.html",
            context={
                "guild": guild,
                "config": config,
                "text_channels": text_channels,
                "category_channels": category_channels,
                "active_page": "moderation_filter",
                "guild_id": guild_id,
                "flash_messages": get_flash_messages(request),
                **ctx,
            },
        )

    @post(
        "/guilds/{guild_id:str}/moderation-filter",
        guards=[auth_guard, Permission("administrator")],
    )
    async def save_moderation_filter_config(
        self, request: Request, db_session: AsyncSession, guild_id: str
    ) -> Redirect:
        """Persist the submitted content filter settings, then redirect back.

        Only the content filter's own fields are saved
        (:func:`parse_moderation_filter_form`)."""
        redirect = Redirect(
            path=f"/admin/bot/guilds/{guild_id}/moderation-filter"
        )

        form = await request.form()
        try:
            updates = parse_moderation_filter_form(form)
        except ConfigFormError as exc:
            flash_error(request, f"Content filters not saved: {exc}")
            return redirect

        await moderation_filter_ops.update_config(db_session, guild_id, **updates)
        await db_session.commit()

        flash_success(request, "Content filters updated successfully!")
        return redirect

    # -- Guild rules ----------------------------------------------------------

    @get(
        "/guilds/{guild_id:str}/rules",
        guards=[auth_guard, Permission("administrator")],
    )
    async def guild_rules_config(
        self, request: Request, db_session: AsyncSession, guild_id: str
    ) -> TemplateResponse:
        """Render the rules editor and the parse preview for one guild."""
        guild, error = await fetch_guild_or_error(
            request, db_session, guild_id, "guild_rules"
        )
        if error is not None:
            return error

        ctx = await get_admin_context(request, db_session)
        config = await load_or_create_guild_rules_config(db_session, guild_id)

        return TemplateResponse(
            "admin/bot/guild_configs/guild_rules.html",
            context={
                "guild": guild,
                "config": config,
                "parsed_rules": parse_guild_rules(config.rules_markdown),
                "active_page": "guild_rules",
                "guild_id": guild_id,
                "flash_messages": get_flash_messages(request),
                **ctx,
            },
        )

    @post(
        "/guilds/{guild_id:str}/rules",
        guards=[auth_guard, Permission("administrator")],
    )
    async def save_guild_rules_config(
        self, request: Request, db_session: AsyncSession, guild_id: str
    ) -> Redirect:
        """Persist the submitted rules document, then redirect back.

        Any text is accepted — the ``/rule`` command simply has nothing to cite
        when nothing parses — so the outcome is reported by the flash message
        and the preview rather than by rejecting the submission. A document that
        yields no rules is the one case worth flagging, because it looks saved
        but leaves the command with nothing.
        """
        redirect = Redirect(path=f"/admin/bot/guilds/{guild_id}/rules")

        form = await request.form()
        updates = parse_guild_rules_form(form)

        await guild_rules_ops.update_config(db_session, guild_id, **updates)
        await db_session.commit()

        rules_markdown = updates["rules_markdown"]
        rules = parse_guild_rules(rules_markdown)
        if rules_markdown and not rules:
            flash_warning(
                request,
                "Rules saved, but no rules could be parsed from them. Start each "
                "rule with a markdown heading, e.g. '## No self-promotion'.",
            )
            return redirect

        flash_success(request, f"Rules saved — {len(rules)} rules parsed.")
        return redirect
