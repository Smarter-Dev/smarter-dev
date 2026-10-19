"""Site-admin overview of the proactive chat bot's channels, per guild.

Lists every channel that has a proactive settings row, which means every
channel someone has switched on with ``/proactive on`` or the ``/configure``
form, including those switched off since. Each row has an on/off button.
Channels that were never configured are not listed: turning one on for the
first time stays in Discord, where the moderator is in the channel.

Each button goes through :func:`set_proactive_channel_enabled`, so switching
a channel on wakes the guild agent exactly as ``/proactive on`` does. That
helper does no authorization, so the toggle checks for itself:

- the normal admin guard (signed in, ``administrator`` permission) and the
  form's CSRF token;
- the channel has a settings row filed under that guild. The helper keys
  its write on the channel alone, so without this a guild/channel mismatch
  would switch another guild's channel and wake the wrong agent;
- before switching it on, Discord confirms that the bot can still see the
  channel and that it belongs to that guild. Switching off needs no Discord
  check, so an outage never stops an admin turning the bot off.

Channel and guild names come from Discord and are display only: when Discord
is unavailable the page shows IDs. There is no link to the full per-channel
form, because that form authorizes only on a link the bot signs for a Discord
user; ``/configure bot`` in the channel still mints one.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from litestar import Controller
from litestar import Request
from litestar import get
from litestar import post
from litestar.response import Redirect
from litestar.response import Template as TemplateResponse
from skrift.admin.helpers import get_admin_context
from skrift.auth.guards import Permission
from skrift.auth.guards import auth_guard
from skrift.flash import flash_error
from skrift.flash import flash_success
from skrift.flash import get_flash_messages
from skrift.forms.core import verify_csrf
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.redis_client import get_redis_client
from smarter_dev.web.crud import get_proactive_channel_settings
from smarter_dev.web.crud import list_all_proactive_channel_settings
from smarter_dev.web.discord_admin_client import DiscordAdminError
from smarter_dev.web.discord_admin_client import get_admin_discord_client
from smarter_dev.web.models import ProactiveChannelSettings
from smarter_dev.web.proactive_channels import set_proactive_channel_enabled

logger = logging.getLogger(__name__)

BASE_PATH = "/admin/bot/proactive-channels"
_ACTIVE_PAGE = "proactive_channels"
_GUARDS = [auth_guard, Permission("administrator")]


@dataclass(frozen=True)
class ProactiveChannelRow:
    """One channel's switch, ready for templating."""

    channel_id: str
    name: str | None
    enabled: bool


@dataclass(frozen=True)
class ProactiveGuildSection:
    """One guild's configured channels: on before off, named before unnamed."""

    guild_id: str
    name: str | None
    channels: list[ProactiveChannelRow]
    enabled_count: int
    # Discord did not answer for this guild's channel list, so every row
    # shows its ID. Threads are never in that list and always show an ID.
    names_unavailable: bool


async def _guild_names(client) -> dict[str, str] | None:
    try:
        return {guild.id: guild.name for guild in await client.list_bot_guilds()}
    except Exception as error:  # noqa: BLE001 — names are display only
        logger.warning("proactive overview: guild names unavailable (%s)", error)
        return None


async def _channel_names(client, guild_id: str) -> dict[str, str] | None:
    try:
        return {
            channel.id: channel.name
            for channel in await client.get_guild_channels(guild_id)
        }
    except Exception as error:  # noqa: BLE001 — names are display only
        logger.warning(
            "proactive overview: channel names unavailable guild=%s (%s)",
            guild_id,
            error,
        )
        return None


async def collect_proactive_overview(
    records: list[ProactiveChannelSettings], client
) -> list[ProactiveGuildSection]:
    """Group the settings rows by guild and name them from Discord.

    ``client`` is the admin Discord client, or ``None`` when there is none;
    every Discord failure degrades to IDs rather than failing the page.
    """
    by_guild: dict[str, list[ProactiveChannelSettings]] = {}
    for record in records:
        by_guild.setdefault(record.guild_id, []).append(record)
    if not by_guild:
        return []

    guild_ids = sorted(by_guild)
    if client is None:
        guild_names = None
        channel_names: list[dict[str, str] | None] = [None] * len(guild_ids)
    else:
        guild_names, *channel_names = await asyncio.gather(
            _guild_names(client),
            *(_channel_names(client, guild_id) for guild_id in guild_ids),
        )

    sections = []
    for guild_id, names in zip(guild_ids, channel_names):
        rows = [
            ProactiveChannelRow(
                channel_id=record.channel_id,
                name=(names or {}).get(record.channel_id),
                enabled=record.enabled,
            )
            for record in by_guild[guild_id]
        ]
        rows.sort(
            key=lambda row: (
                not row.enabled,
                row.name is None,
                (row.name or "").lower(),
                row.channel_id,
            )
        )
        sections.append(
            ProactiveGuildSection(
                guild_id=guild_id,
                name=(guild_names or {}).get(guild_id),
                channels=rows,
                enabled_count=sum(row.enabled for row in rows),
                names_unavailable=names is None,
            )
        )
    sections.sort(key=lambda section: ((section.name or "").lower(), section.guild_id))
    return sections


async def channel_on_refusal(guild_id: str, channel_id: str) -> str | None:
    """Why Discord will not vouch for switching this channel on, or ``None``.

    The bot must still see the channel, and it must be that guild's.
    """
    try:
        owner = await get_admin_discord_client().get_channel_guild_id(channel_id)
    except DiscordAdminError as error:
        if error.status_code in (403, 404):
            return (
                "Discord says that channel no longer exists or the bot can't "
                "see it, so it was not switched on."
            )
        logger.warning(
            "proactive overview: channel check failed guild=%s channel=%s (%s)",
            guild_id,
            channel_id,
            error,
        )
        return "Couldn't reach Discord to check the channel. Please try again."
    except Exception as error:  # noqa: BLE001 — httpx transport errors
        logger.warning(
            "proactive overview: channel check failed guild=%s channel=%s (%s)",
            guild_id,
            channel_id,
            type(error).__name__,
        )
        return "Couldn't reach Discord to check the channel. Please try again."
    if owner != guild_id:
        return "That channel isn't in this server, so it was not switched on."
    return None


class ProactiveChannelsAdminController(Controller):
    """Every guild's proactive channels, with an on/off switch per channel."""

    path = BASE_PATH
    guards = [auth_guard]

    @get("/", guards=_GUARDS)
    async def overview(
        self, request: Request, db_session: AsyncSession
    ) -> TemplateResponse:
        ctx = await get_admin_context(request, db_session)
        records = await list_all_proactive_channel_settings(db_session)
        try:
            client = get_admin_discord_client()
        except DiscordAdminError:
            client = None
        sections = await collect_proactive_overview(records, client)
        return TemplateResponse(
            "admin/bot/proactive_channels.html",
            context={
                "sections": sections,
                "flash_messages": get_flash_messages(request),
                "active_page": _ACTIVE_PAGE,
                "guild_id": None,
                **ctx,
            },
        )

    @post("/{guild_id:str}/{channel_id:str}", guards=_GUARDS)
    async def toggle(
        self,
        request: Request,
        db_session: AsyncSession,
        guild_id: str,
        channel_id: str,
    ) -> Redirect:
        back = Redirect(path=BASE_PATH)
        if not await verify_csrf(request):
            flash_error(request, "Your session expired. Please try again.")
            return back
        form = await request.form()
        enabled = str(form.get("enabled") or "") == "on"

        record = await get_proactive_channel_settings(db_session, guild_id, channel_id)
        if record is None:
            flash_error(request, "That channel isn't on this page. Reload and try again.")
            return back
        if enabled and (refusal := await channel_on_refusal(guild_id, channel_id)):
            flash_error(request, refusal)
            return back

        ctx = await get_admin_context(request, db_session)
        # Commits; switching the channel on wakes the agent.
        woke = await set_proactive_channel_enabled(
            db_session,
            get_redis_client(),
            guild_id=guild_id,
            channel_id=channel_id,
            enabled=enabled,
        )
        logger.info(
            "proactive channel switched from admin overview guild=%s channel=%s "
            "enabled=%s woke=%s by site user=%s",
            guild_id,
            channel_id,
            enabled,
            woke,
            ctx["user"].id if ctx.get("user") else None,
        )
        flash_success(
            request,
            f"Proactive bot is now {'on' if enabled else 'off'} in channel {channel_id}.",
        )
        return back
