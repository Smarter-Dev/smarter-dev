"""Per-guild feature configuration pages for the Skrift admin panel.

Ports ``audit_log_config``, ``advent_of_code_config`` and
``attachment_filter_config`` from the legacy ``smarter_dev.web.admin.views`` onto
a single Skrift-native Litestar controller under ``/admin/bot``, and adds the
moderation-filter page for the core content filters and spam engine. Guild
identity comes from Discord (via :mod:`smarter_dev.web.discord_admin_client`);
the four settings groups live in the ``audit_log_configs``,
``advent_of_code_configs``, ``attachment_filter_configs`` and
``moderation_filter_configs`` tables.

Form parsing is factored into pure module-level helpers so the accepted-field
contract can be unit-tested without a request or a database.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass

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
    ModerationFilterConfigOperations,
)
from smarter_dev.web.discord_admin_client import (
    DiscordAdminError,
    get_admin_discord_client,
)
from smarter_dev.web.models import (
    AdventOfCodeConfig,
    AttachmentFilterConfig,
    AuditLogConfig,
    ModerationFilterConfig,
)

logger = logging.getLogger(__name__)

audit_ops = AuditLogConfigOperations()
advent_ops = AdventOfCodeConfigOperations()
attachment_ops = AttachmentFilterConfigOperations()
moderation_filter_ops = ModerationFilterConfigOperations()

# Discord channel type ids used by the channel pickers.
_TEXT_CHANNEL_TYPES: frozenset[int] = frozenset({0, 5})  # GUILD_TEXT, GUILD_NEWS
_FORUM_CHANNEL_TYPE: int = 15  # GUILD_FORUM
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


# The boolean toggles the moderation-filter form submits, in display order.
_MODERATION_TOGGLE_FIELDS: tuple[str, ...] = (
    "content_filter_enabled",
    "invite_filter_enabled",
    "webhook_killer_enabled",
    "spam_engine_enabled",
)

# The nullable channel pickers on the moderation-filter form.
_MODERATION_CHANNEL_FIELDS: tuple[str, ...] = (
    "mod_log_channel_id",
    "mod_alert_channel_id",
    "scam_log_channel_id",
)

# The positive-integer thresholds on the moderation-filter form.
_MODERATION_THRESHOLD_FIELDS: tuple[str, ...] = (
    "message_rate_threshold",
    "message_rate_window_seconds",
    "channel_spread_threshold",
    "channel_spread_window_seconds",
    "duplicate_message_min_length",
    "duplicate_message_window_seconds",
    "mass_mention_window_seconds",
    "warning_reoffense_window_seconds",
    "mute_duration_seconds",
)

# Guild id used only to build the throwaway instance the threshold defaults are
# read from; the model, not this module, owns the legacy values.
_DEFAULTS_PROBE_GUILD_ID = "0"

_MODERATION_THRESHOLD_DEFAULTS: dict[str, int] = {
    field: getattr(
        ModerationFilterConfig.get_defaults(_DEFAULTS_PROBE_GUILD_ID), field
    )
    for field in _MODERATION_THRESHOLD_FIELDS
}

# Discord rejects a member timeout more than 28 days out with a 400, which aborts
# the whole enforcement action — including the scam-link deletion that precedes
# it. The form refuses the value rather than letting an admin arm that failure.
DISCORD_MAX_TIMEOUT_SECONDS: int = 28 * 24 * 60 * 60


@dataclass(frozen=True)
class ThresholdCeiling:
    """An externally imposed upper bound on a form threshold, with its reason.

    The reason is shown to the admin verbatim, so it explains *why* the number
    is capped rather than restating the number.
    """

    limit: int
    reason: str


# Thresholds with an API-imposed ceiling. Everything absent here is a local
# sliding window with no upper bound worth enforcing.
_MODERATION_THRESHOLD_CEILINGS: dict[str, ThresholdCeiling] = {
    "mute_duration_seconds": ThresholdCeiling(
        limit=DISCORD_MAX_TIMEOUT_SECONDS,
        reason="Discord's hard limit on a member timeout is 28 days",
    ),
}

# At least two dot-separated labels of hostname-legal characters. Deliberately
# stricter than DNS (no underscores, no trailing hyphens) because these entries
# are matched against registrable domains, not looked up.
_HOSTNAME_PATTERN = re.compile(
    r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+"
)


class ConfigFormError(ValueError):
    """Base class for guild-config form submissions that fail validation."""


class InvalidChannelError(ConfigFormError):
    """Raised when a submitted channel id is not a Discord snowflake."""


class InvalidRoleError(ConfigFormError):
    """Raised when a submitted role id is not a Discord snowflake."""


class InvalidThresholdError(ConfigFormError):
    """Raised when a submitted numeric threshold is outside its allowed range."""


class InvalidDomainError(ConfigFormError):
    """Raised when a submitted domain-list entry is not a usable hostname."""


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


def validate_role_id(raw: str | None) -> str | None:
    """Normalise a submitted role id, or ``None`` when the field is blank.

    Pure function. A blank value clears the role (the "None" option in the
    picker); a non-blank value must be a Discord snowflake.

    Raises:
        InvalidRoleError: If a non-blank value is not all digits.
    """
    role_id = (raw or "").strip()
    if not role_id:
        return None
    if not role_id.isdigit():
        raise InvalidRoleError(f"Invalid role id: {role_id!r}")
    return role_id


def split_id_list(raw: str | None) -> list[str]:
    """Split a comma- or newline-separated id field into unique entries.

    Pure function; performs no snowflake validation, so callers pair it with
    the validator matching the kind of id they expect.
    """
    entries = (
        entry.strip() for entry in (raw or "").replace(",", "\n").split("\n")
    )
    return list(dict.fromkeys(entry for entry in entries if entry))


def parse_role_ids(raw: str | None) -> list[str]:
    """Parse a role-id field into a de-duplicated list of snowflakes.

    Pure function.

    Raises:
        InvalidRoleError: If any entry is not all digits.
    """
    role_ids = split_id_list(raw)
    for role_id in role_ids:
        if not role_id.isdigit():
            raise InvalidRoleError(f"Invalid role id: {role_id!r}")
    return role_ids


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


def parse_positive_int(
    raw: str | None,
    field_name: str,
    default: int,
    maximum: ThresholdCeiling | None = None,
) -> int:
    """Parse a threshold field, falling back to ``default`` when it is blank.

    Pure function. Blank means "keep the documented default", which is how the
    form offers a reset; anything non-blank must be a positive whole number, and
    must not exceed ``maximum`` when the field has an externally imposed ceiling.

    Raises:
        InvalidThresholdError: If the value is non-numeric, not positive, or
            above ``maximum``.
    """
    value = (raw or "").strip()
    if not value:
        return default

    label = field_name.replace("_", " ")
    try:
        threshold = int(value)
    except ValueError as exc:
        raise InvalidThresholdError(
            f"{label} must be a positive whole number (got {value!r})"
        ) from exc
    if threshold <= 0:
        raise InvalidThresholdError(
            f"{label} must be a positive whole number (got {value!r})"
        )
    if maximum is not None and threshold > maximum.limit:
        raise InvalidThresholdError(
            f"{label} must be at most {maximum.limit} "
            f"({maximum.reason}) — got {value!r}"
        )
    return threshold


def parse_hostnames(raw: str | None) -> list[str]:
    """Parse a domain textarea into bare, lower-cased, de-duplicated hostnames.

    Pure function. Accepts one-per-line or comma-separated input and tolerates a
    pasted URL: the scheme, any userinfo, the port and everything from the first
    ``/``, ``?`` or ``#`` are dropped, leaving the hostname alone. A trailing
    root dot is removed and first-seen order is preserved.

    Raises:
        InvalidDomainError: If an entry has no dot, or contains a character that
            cannot appear in a hostname.
    """
    hostnames: list[str] = []
    for line in (raw or "").replace(",", "\n").split("\n"):
        entry = line.strip().lower()
        if not entry:
            continue
        hostnames.append(_hostname_of(entry))
    return list(dict.fromkeys(hostnames))


def _hostname_of(entry: str) -> str:
    """Reduce one submitted domain entry to its bare hostname.

    Pure helper for :func:`parse_hostnames`; ``entry`` is already stripped and
    lower-cased.

    Raises:
        InvalidDomainError: If nothing hostname-shaped remains.
    """
    without_scheme = entry.split("://", 1)[-1]
    authority = re.split(r"[/?#]", without_scheme, maxsplit=1)[0]
    host = authority.rpartition("@")[2].partition(":")[0].rstrip(".")
    if not _HOSTNAME_PATTERN.fullmatch(host):
        raise InvalidDomainError(
            f"Invalid domain: {entry!r} — use a bare hostname such as 't.me'"
        )
    return host


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


def parse_moderation_filter_form(form: Mapping[str, str]) -> dict:
    """Turn a submitted moderation-filter form into keyword arguments.

    Pure function covering both subsystems on the page: the content filters
    (blocked TLDs, scam-link domains, invite filter, webhook killer) and the
    spam engine. Blank thresholds fall back to the model's legacy defaults; an
    emptied ``scam_link_domains`` textarea means the guild wants no domain-list
    matching, so it is saved as an empty list rather than re-seeded. Unknown
    submitted keys are dropped.

    Raises:
        InvalidChannelError: If a channel or category id is a non-blank
            non-snowflake.
        InvalidRoleError: If a role id is a non-blank non-snowflake.
        InvalidThresholdError: If a threshold is non-numeric, not positive, or
            above its ceiling (see ``_MODERATION_THRESHOLD_CEILINGS``).
        InvalidDomainError: If a scam-link entry is not a usable hostname.
    """
    updates: dict = {
        "blocked_tlds": parse_extensions(form.get("blocked_tlds")),
        "scam_link_domains": parse_hostnames(form.get("scam_link_domains")),
        "invite_filter_exempt_category_ids": parse_channel_ids(
            form.get("invite_filter_exempt_category_ids")
        ),
        "staff_exempt_role_ids": parse_role_ids(form.get("staff_exempt_role_ids")),
        "mod_ping_role_id": validate_role_id(form.get("mod_ping_role_id")),
    }
    for field in _MODERATION_TOGGLE_FIELDS:
        updates[field] = form.get(field) == "on"
    for field in _MODERATION_CHANNEL_FIELDS:
        updates[field] = validate_channel_id(form.get(field))
    for field, default in _MODERATION_THRESHOLD_DEFAULTS.items():
        updates[field] = parse_positive_int(
            form.get(field),
            field,
            default,
            maximum=_MODERATION_THRESHOLD_CEILINGS.get(field),
        )
    return updates


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


async def load_or_create_moderation_filter_config(
    db_session: AsyncSession, guild_id: str
) -> ModerationFilterConfig:
    """Return the guild's moderation-filter config, persisting a default row."""
    config = await moderation_filter_ops.get_or_create_config(db_session, guild_id)
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
    """Audit-log, AoC, attachment- and moderation-filter config under ``/admin/bot``."""

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

    # -- Moderation filter ----------------------------------------------------

    @get(
        "/guilds/{guild_id:str}/moderation-filter",
        guards=[auth_guard, Permission("administrator")],
    )
    async def moderation_filter_config(
        self, request: Request, db_session: AsyncSession, guild_id: str
    ) -> TemplateResponse:
        """Render the moderation-filter configuration form for one guild."""
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
        guild_roles = await fetch_guild_roles(guild_id)
        config = await load_or_create_moderation_filter_config(db_session, guild_id)

        return TemplateResponse(
            "admin/bot/guild_configs/moderation_filter.html",
            context={
                "guild": guild,
                "config": config,
                "text_channels": text_channels,
                "category_channels": category_channels,
                "guild_roles": guild_roles,
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
        """Persist submitted moderation-filter settings, then redirect back."""
        redirect = Redirect(
            path=f"/admin/bot/guilds/{guild_id}/moderation-filter"
        )

        form = await request.form()
        try:
            updates = parse_moderation_filter_form(form)
        except ConfigFormError as exc:
            flash_error(request, f"Moderation filter not saved: {exc}")
            return redirect

        await moderation_filter_ops.update_config(db_session, guild_id, **updates)
        await db_session.commit()

        flash_success(
            request, "Moderation filter configuration updated successfully!"
        )
        return redirect
