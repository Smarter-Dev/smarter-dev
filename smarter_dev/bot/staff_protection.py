"""The staff-protection rule, fed from the gateway bot's cache and REST.

:func:`staff_refusal` is what every bot-side automated actor calls before it
acts on a member or on a member's message. It reads the cache first and falls
back to REST; anything it cannot establish refuses (see
:mod:`smarter_dev.shared.staff_protection` for the rule itself).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

import hikari

from smarter_dev.shared.staff_protection import A_BOT
from smarter_dev.shared.staff_protection import BOT_ITSELF
from smarter_dev.shared.staff_protection import GuildFacts
from smarter_dev.shared.staff_protection import MemberFacts
from smarter_dev.shared.staff_protection import Overwrite
from smarter_dev.shared.staff_protection import member_refusal
from smarter_dev.shared.staff_protection import non_member_refusal
from smarter_dev.shared.staff_protection import unknown

logger = logging.getLogger(__name__)


def _bot_user_id(bot: Any) -> str | None:
    me = bot.get_me()
    return str(me.id) if me is not None else None


def _cache(bot: Any):
    return getattr(bot, "cache", None)


def _overwrites(channel: Any) -> dict[str, Overwrite]:
    # Threads carry no overwrites of their own; their parent is in the list.
    raw = getattr(channel, "permission_overwrites", None) or {}
    return {
        str(target_id): Overwrite(allow=int(overwrite.allow), deny=int(overwrite.deny))
        for target_id, overwrite in raw.items()
    }


async def guild_facts(
    bot: Any, guild_id: int | str, *, role_ids: Iterable[Any] = ()
) -> GuildFacts:
    """Owner, role permissions and every channel's overwrites; cache first.

    ``role_ids`` are the roles the caller is about to read: if the cache lacks
    any of them the roles are fetched over REST. Raises when REST fails.
    """
    gid = int(guild_id)
    cache = _cache(bot)
    guild = cache.get_guild(gid) if cache is not None else None
    if guild is not None:
        owner_id = guild.owner_id
        roles = {
            str(rid): int(role.permissions) for rid, role in guild.get_roles().items()
        }
        channels = list(cache.get_guild_channels_view_for_guild(gid).values())
    else:
        fetched = await bot.rest.fetch_guild(gid)
        owner_id = fetched.owner_id
        roles = {str(rid): int(role.permissions) for rid, role in fetched.roles.items()}
        channels = []
    needed = {str(gid), *(str(role_id) for role_id in role_ids)}
    if not needed <= roles.keys():
        roles = {
            str(role.id): int(role.permissions)
            for role in await bot.rest.fetch_roles(gid)
        }
    if not channels:
        channels = list(await bot.rest.fetch_guild_channels(gid))
    return GuildFacts(
        guild_id=str(gid),
        owner_id=str(owner_id) if owner_id is not None else None,
        role_permissions=roles,
        channel_overwrites=tuple(_overwrites(channel) for channel in channels),
    )


async def _user_is_bot(bot: Any, user_id: str) -> bool | None:
    cache = _cache(bot)
    user = cache.get_user(int(user_id)) if cache is not None else None
    if user is None:
        try:
            user = await bot.rest.fetch_user(int(user_id))
        except Exception:  # noqa: BLE001 — unknown, which protects
            return None
    return bool(user.is_bot)


async def staff_refusal(
    bot: Any,
    guild_id: int | str,
    user_id: int | str,
    *,
    member: Any = None,
    user_is_bot: bool | None = None,
    extra_role_ids: Iterable[str] = (),
) -> str | None:
    """Why automation may not act on this user, or None when it may.

    ``member`` is the member object when the caller already has one (a gateway
    event carries it); otherwise the cache and then REST are asked.
    ``user_is_bot`` is what the caller knows from a message author. Never
    raises: a failure is an "could not be checked" refusal.
    """
    uid = str(user_id)
    try:
        bot_user_id = _bot_user_id(bot)
        if bot_user_id is not None and uid == bot_user_id:
            return BOT_ITSELF
        if user_is_bot:
            return A_BOT
        if member is None:
            cache = _cache(bot)
            if cache is not None:
                member = cache.get_member(int(guild_id), int(uid))
        if member is None:
            try:
                member = await bot.rest.fetch_member(int(guild_id), int(uid))
            except hikari.NotFoundError:
                if user_is_bot is None:
                    user_is_bot = await _user_is_bot(bot, uid)
                return non_member_refusal(
                    uid, is_bot=user_is_bot, bot_user_id=bot_user_id
                )
        role_ids = tuple(str(role_id) for role_id in member.role_ids)
        guild = await guild_facts(bot, guild_id, role_ids=role_ids)
        return member_refusal(
            MemberFacts(user_id=uid, is_bot=bool(member.is_bot), role_ids=role_ids),
            guild,
            bot_user_id=bot_user_id,
            extra_role_ids=extra_role_ids,
        )
    except Exception as error:  # noqa: BLE001 — unknown protects
        logger.warning(
            "Staff protection check for user %s in guild %s failed: %s",
            uid,
            guild_id,
            type(error).__name__,
        )
        return unknown(type(error).__name__)
