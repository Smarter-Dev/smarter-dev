"""Site-admin overview of the proactive chat bot's channels, per guild.

Lists every channel that has a proactive settings row, which means every
channel someone has switched on with ``/proactive on`` or the ``/configure``
form, including those switched off since. Each row has an on/off button.
Channels that were never configured are not listed: turning one on for the
first time stays in Discord, where the moderator is in the channel. Forums
are the exception (#248): a whole forum can be switched on here, either one
the guild's channel list shows or one entered by ID, and forums are labelled
as such. Switching a forum on wakes nobody: each post reaches the agent when
someone writes in it.

Each button goes through :func:`set_proactive_channel_enabled`, so switching
a channel on wakes the guild agent exactly as ``/proactive on`` does. That
helper does no authorization, so the toggle checks for itself:

- the normal admin guard (signed in, ``administrator`` permission) and the
  form's CSRF token;
- the channel has a settings row filed under that guild. The helper keys
  its write on the channel alone, so without this a guild/channel mismatch
  would switch another guild's channel and wake the wrong agent;
- before switching it on, Discord confirms that the bot can still see the
  channel and that it belongs to that guild (and, to add one, that it is a
  forum). Switching off needs no Discord check, so an outage never stops an
  admin turning the bot off.

Channel and guild names come from Discord and are display only. The page
must stay reachable in a Discord outage, since that is when someone needs the
off buttons, so name lookups share one short deadline
(:data:`NAME_DEADLINE_SECONDS`) and whatever has not answered by then shows
as IDs. Names are cached in-process for :data:`NAME_CACHE_SECONDS`; the
Discord check before switching a channel on is never cached. There is no link to the full per-channel
form, because that form authorizes only on a link the bot signs for a Discord
user; ``/configure bot`` in the channel still mints one.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable
from collections.abc import Callable
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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.redis_client import get_redis_client
from smarter_dev.web.crud import get_proactive_channel_settings
from smarter_dev.web.crud import list_all_proactive_channel_settings
from smarter_dev.web.discord_admin_client import DiscordAdminError
from smarter_dev.web.discord_admin_client import DiscordChannel
from smarter_dev.web.discord_admin_client import get_admin_discord_client
from smarter_dev.web.models import ProactiveChannelSettings
from smarter_dev.web.proactive_channels import set_proactive_channel_enabled

logger = logging.getLogger(__name__)

BASE_PATH = "/admin/bot/proactive-channels"
_ACTIVE_PAGE = "proactive_channels"
_GUARDS = [auth_guard, Permission("administrator")]

# The Discord client's timeout is per phase, not per request, so a stalled or
# trickling read could hold the page; these bound the whole wait.
NAME_DEADLINE_SECONDS = 2.0
CHANNEL_CHECK_DEADLINE_SECONDS = 5.0
NAME_LOOKUP_CONCURRENCY = 5
NAME_CACHE_SECONDS = 300.0
NAME_CACHE_MAX_ENTRIES = 256
# GUILD_FORUM and GUILD_MEDIA: every post is a thread of its own.
FORUM_CHANNEL_TYPES = frozenset({15, 16})
_SNOWFLAKE = re.compile(r"[0-9]{15,22}")

# Only successful lookups are cached: key -> (expires at, names or channels
# by id), oldest write first.
_name_cache: OrderedDict[str, tuple[float, dict]] = OrderedDict()
_clock = time.monotonic
# One limit on Discord name lookups across every render in this process.
_name_lookup_gate = asyncio.Semaphore(NAME_LOOKUP_CONCURRENCY)


@dataclass(frozen=True)
class ProactiveChannelRow:
    """One channel's switch, ready for templating."""

    channel_id: str
    name: str | None
    enabled: bool
    is_forum: bool = False
    # False for a forum the guild has that was never switched on: its button
    # adds it rather than toggling a row.
    configured: bool = True


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


async def _guild_names(client) -> dict[str, str]:
    return {guild.id: guild.name for guild in await client.list_bot_guilds()}


async def _guild_channels(client, guild_id: str) -> dict[str, DiscordChannel]:
    return {
        channel.id: channel
        for channel in await client.get_guild_channels(guild_id)
    }


def _cache_names(key: str, names: dict) -> None:
    now = _clock()
    for stale in [k for k, (expires, _) in _name_cache.items() if expires <= now]:
        del _name_cache[stale]
    _name_cache[key] = (now + NAME_CACHE_SECONDS, names)
    _name_cache.move_to_end(key)
    while len(_name_cache) > NAME_CACHE_MAX_ENTRIES:
        _name_cache.popitem(last=False)


async def _lookup_names(
    lookups: dict[str, Callable[[], Awaitable[dict]]],
) -> dict[str, dict]:
    """Run the uncached lookups under one deadline; return what answered.

    A failed or unfinished lookup is simply missing from the result (its
    rows show IDs). The task group cancels and awaits every lookup on any
    exit, the deadline or this render being cancelled, so none outlives it.
    """
    now = _clock()
    found: dict[str, dict] = {}
    for key in list(lookups):
        cached = _name_cache.get(key)
        if cached is not None and cached[0] > now:
            found[key] = cached[1]
            del lookups[key]
    if not lookups:
        return found

    async def run(key: str, lookup) -> None:
        try:
            async with _name_lookup_gate:
                names = await lookup()
        except Exception as error:  # noqa: BLE001 — names are display only
            logger.warning(
                "proactive overview: Discord names unavailable for %s (%s)", key, error
            )
            return
        found[key] = names
        _cache_names(key, names)

    try:
        async with asyncio.timeout(NAME_DEADLINE_SECONDS), asyncio.TaskGroup() as group:
            for key, lookup in lookups.items():
                group.create_task(run(key, lookup), name=f"proactive-name-lookup:{key}")
    except TimeoutError:
        logger.warning(
            "proactive overview: Discord names timed out for %s",
            sorted(set(lookups) - set(found)),
        )
    return found


async def collect_proactive_overview(
    records: list[ProactiveChannelSettings], client
) -> list[ProactiveGuildSection]:
    """Group the settings rows by guild and name them from Discord.

    ``client`` is the admin Discord client, or ``None`` when there is none;
    every Discord failure or timeout degrades to IDs rather than failing or
    holding the page.
    """
    by_guild: dict[str, list[ProactiveChannelSettings]] = {}
    for record in records:
        by_guild.setdefault(record.guild_id, []).append(record)
    if not by_guild:
        return []

    guild_ids = sorted(by_guild)
    found: dict[str, dict] = {}
    if client is not None:
        lookups = {"guilds": lambda: _guild_names(client)}
        for guild_id in guild_ids:
            lookups[f"channels:{guild_id}"] = (
                lambda guild_id=guild_id: _guild_channels(client, guild_id)
            )
        found = await _lookup_names(lookups)
    guild_names = found.get("guilds")
    guild_channels = [found.get(f"channels:{guild_id}") for guild_id in guild_ids]

    sections = []
    for guild_id, listing in zip(guild_ids, guild_channels):
        channels = listing or {}
        configured_ids = {record.channel_id for record in by_guild[guild_id]}
        rows = [
            ProactiveChannelRow(
                channel_id=record.channel_id,
                name=getattr(channels.get(record.channel_id), "name", None),
                enabled=record.enabled,
                is_forum=getattr(record, "is_forum", False)
                or getattr(channels.get(record.channel_id), "type", None)
                in FORUM_CHANNEL_TYPES,
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
        # The guild's forums that were never switched on, last, to add.
        rows.extend(
            ProactiveChannelRow(
                channel_id=channel.id,
                name=channel.name,
                enabled=False,
                is_forum=True,
                configured=False,
            )
            for channel in channels.values()
            if channel.type in FORUM_CHANNEL_TYPES
            and channel.id not in configured_ids
        )
        sections.append(
            ProactiveGuildSection(
                guild_id=guild_id,
                name=(guild_names or {}).get(guild_id),
                channels=rows,
                enabled_count=sum(row.enabled for row in rows),
                names_unavailable=listing is None,
            )
        )
    sections.sort(key=lambda section: ((section.name or "").lower(), section.guild_id))
    return sections


async def channel_on_refusal(guild_id: str, channel_id: str) -> str | None:
    """Why Discord will not vouch for switching this channel on, or ``None``.

    The bot must still see the channel, and it must be that guild's.
    """
    return (await channel_on_check(guild_id, channel_id))[0]


async def channel_on_check(
    guild_id: str, channel_id: str
) -> tuple[str | None, int | None]:
    """:func:`channel_on_refusal`, and the channel's Discord type when none."""
    try:
        owner, channel_type = await asyncio.wait_for(
            get_admin_discord_client().get_channel_placement(channel_id),
            timeout=CHANNEL_CHECK_DEADLINE_SECONDS,
        )
    except DiscordAdminError as error:
        if error.status_code in (403, 404):
            return (
                "Discord says that channel no longer exists or the bot can't "
                "see it, so it was not switched on."
            ), None
        logger.warning(
            "proactive overview: channel check failed guild=%s channel=%s (%s)",
            guild_id,
            channel_id,
            error,
        )
        return "Couldn't reach Discord to check the channel. Please try again.", None
    except Exception as error:  # noqa: BLE001 — timeout or httpx transport error
        logger.warning(
            "proactive overview: channel check failed guild=%s channel=%s (%s)",
            guild_id,
            channel_id,
            type(error).__name__,
        )
        return "Couldn't reach Discord to check the channel. Please try again.", None
    if owner != guild_id:
        return "That channel isn't in this server, so it was not switched on.", None
    return None, channel_type


async def _filed_elsewhere(session: AsyncSession, channel_id: str) -> bool:
    """Whether the channel has a settings row under some guild.

    Called only when it has none under the guild being asked about.
    """
    found = await session.scalar(
        select(ProactiveChannelSettings.id).where(
            ProactiveChannelSettings.channel_id == channel_id
        )
    )
    return found is not None


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
        # Switching off leaves the row's forum mark as it is.
        is_forum = None
        if enabled:
            refusal, channel_type = await channel_on_check(guild_id, channel_id)
            if refusal:
                flash_error(request, refusal)
                return back
            is_forum = channel_type in FORUM_CHANNEL_TYPES

        ctx = await get_admin_context(request, db_session)
        # Commits; switching a channel on wakes the agent, a forum nobody.
        woke = await set_proactive_channel_enabled(
            db_session,
            get_redis_client(),
            guild_id=guild_id,
            channel_id=channel_id,
            enabled=enabled,
            is_forum=is_forum,
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

    @post("/forums", guards=_GUARDS)
    async def add_forum(self, request: Request, db_session: AsyncSession) -> Redirect:
        """Switch on a whole forum, which may never have been configured.

        Discord must say the channel is a forum (or media channel) in that
        guild. Its posts inherit the forum's settings unless they have their
        own, and no wake is sent.
        """
        back = Redirect(path=BASE_PATH)
        if not await verify_csrf(request):
            flash_error(request, "Your session expired. Please try again.")
            return back
        form = await request.form()
        guild_id = str(form.get("guild_id") or "").strip()
        channel_id = str(form.get("channel_id") or "").strip()
        if not (_SNOWFLAKE.fullmatch(guild_id) and _SNOWFLAKE.fullmatch(channel_id)):
            flash_error(request, "Enter the server's ID and the forum's channel ID.")
            return back
        refusal, channel_type = await channel_on_check(guild_id, channel_id)
        if refusal:
            flash_error(request, refusal)
            return back
        if channel_type not in FORUM_CHANNEL_TYPES:
            flash_error(
                request,
                "That channel isn't a forum. Switch other channels on in Discord "
                "with /proactive on.",
            )
            return back

        ctx = await get_admin_context(request, db_session)
        # A row already filed under another guild must not be switched from
        # here, as the toggle refuses it.
        record = await get_proactive_channel_settings(db_session, guild_id, channel_id)
        if record is None and await _filed_elsewhere(db_session, channel_id):
            flash_error(request, "That channel isn't in this server, so it was not switched on.")
            return back
        await set_proactive_channel_enabled(
            db_session,
            get_redis_client(),
            guild_id=guild_id,
            channel_id=channel_id,
            enabled=True,
            is_forum=True,
        )
        logger.info(
            "proactive forum switched on from admin overview guild=%s forum=%s "
            "by site user=%s",
            guild_id,
            channel_id,
            ctx["user"].id if ctx.get("user") else None,
        )
        flash_success(
            request,
            f"Proactive bot is now on for forum {channel_id}: each post is its own "
            "channel, and posts switched off on their own stay off.",
        )
        return back
