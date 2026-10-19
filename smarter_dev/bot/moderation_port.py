"""The moderation gateway's door to Discord from the gateway bot (hikari).

:func:`bot_gateway` is what the bot's moderators (the spam engine, the spam
ring, the attachment and content filters, the staff-ping triage and the slash
commands) call instead of ``bot.rest``. Reads go to REST, not the cache: the
gateway re-checks a member's real state right before it acts.

hikari maps Discord's answers onto exceptions; this module sorts them into
the gateway's contract: 403 and 400 mean Discord refused (nothing happened),
404 means the member or message is not there, and anything else (5xx after
hikari's own retries, a timeout, a dropped connection) is an unknown outcome.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC
from datetime import datetime
from typing import Any

import hikari

from smarter_dev.bot.mod_action_dispatch import dispatch_mod_action
from smarter_dev.bot.purge_core import BULK_DELETE_MAX_AGE
from smarter_dev.bot.purge_core import snowflake_to_datetime
from smarter_dev.bot.staff_protection import staff_refusal
from smarter_dev.web.moderation_gateway import ModerationGateway
from smarter_dev.web.moderation_gateway import Trigger
from smarter_dev.web.moderation_gateway import types as gw
from smarter_dev.web.moderation_gateway.types import DeleteResult
from smarter_dev.web.moderation_gateway.types import EffectNotFound
from smarter_dev.web.moderation_gateway.types import EffectRejected
from smarter_dev.web.moderation_gateway.types import TimeoutState

logger = logging.getLogger(__name__)

#: How far back a report whose post may have landed is looked for.
FIND_REPORT_WINDOW = 50


def _timeout_expiry(member: Any) -> datetime | None:
    """The expiry Discord stored, however the member object carries it."""
    raw = getattr(member, "raw_communication_disabled_until", None)
    if isinstance(raw, datetime):
        return raw
    value = getattr(member, "communication_disabled_until", None)
    if callable(value):
        value = value()
    return value if isinstance(value, datetime) else None


def _refusal(error: hikari.ClientHTTPResponseError) -> EffectRejected:
    if isinstance(error, hikari.NotFoundError):
        return EffectNotFound("not found")
    if isinstance(error, hikari.ForbiddenError):
        return EffectRejected(403, "missing permission or role hierarchy")
    return EffectRejected(getattr(error, "status", 400), "Discord rejected it")


_DEFINITE = (hikari.ForbiddenError, hikari.NotFoundError, hikari.BadRequestError)


class HikariModerationPort:
    """:class:`~smarter_dev.web.moderation_gateway.ModerationPort` over ``bot.rest``."""

    def __init__(self, bot: Any, *, extra_role_ids: Iterable[str] = ()) -> None:
        self.bot = bot
        self.extra_role_ids = tuple(str(role_id) for role_id in extra_role_ids)

    async def bot_identity(self) -> tuple[str | None, str | None]:
        me = self.bot.get_me()
        return (str(me.id), me.username) if me is not None else (None, None)

    async def staff_refusal(
        self, guild_id: str, user_id: str, *, is_bot: bool | None = None
    ) -> str | None:
        return await staff_refusal(
            self.bot,
            guild_id,
            user_id,
            user_is_bot=is_bot,
            extra_role_ids=self.extra_role_ids,
        )

    async def timeout_state(self, guild_id: str, user_id: str) -> TimeoutState:
        try:
            member = await self.bot.rest.fetch_member(int(guild_id), int(user_id))
        except hikari.NotFoundError:
            return TimeoutState(is_member=False, until=None)
        until = _timeout_expiry(member)
        return TimeoutState(
            is_member=True, until=until.timestamp() if until is not None else None
        )

    async def is_banned(self, guild_id: str, user_id: str) -> bool:
        try:
            await self.bot.rest.fetch_ban(int(guild_id), int(user_id))
        except hikari.NotFoundError:
            return False
        return True

    async def message_exists(self, channel_id: str, message_id: str) -> bool:
        try:
            await self.bot.rest.fetch_message(int(channel_id), int(message_id))
        except hikari.NotFoundError:
            return False
        return True

    async def message_author(
        self, channel_id: str, message_id: str
    ) -> tuple[str, str | None, bool] | None:
        try:
            message = await self.bot.rest.fetch_message(
                int(channel_id), int(message_id)
            )
        except hikari.NotFoundError:
            return None
        author = message.author
        return (
            str(author.id),
            getattr(author, "username", None),
            bool(author.is_bot) or getattr(message, "webhook_id", None) is not None,
        )

    async def ban(
        self, guild_id: str, user_id: str, *, reason: str, delete_message_seconds: int
    ) -> None:
        try:
            await self.bot.rest.ban_user(
                int(guild_id),
                int(user_id),
                delete_message_seconds=int(delete_message_seconds),
                reason=reason or hikari.UNDEFINED,
            )
        except _DEFINITE as error:
            raise _refusal(error) from error

    async def kick(self, guild_id: str, user_id: str, *, reason: str) -> None:
        try:
            await self.bot.rest.kick_user(
                int(guild_id), int(user_id), reason=reason or hikari.UNDEFINED
            )
        except _DEFINITE as error:
            raise _refusal(error) from error

    async def set_timeout(
        self, guild_id: str, user_id: str, until: datetime | None, *, reason: str
    ) -> float | None:
        try:
            member = await self.bot.rest.edit_member(
                int(guild_id),
                int(user_id),
                communication_disabled_until=until,
                reason=reason or hikari.UNDEFINED,
            )
        except _DEFINITE as error:
            raise _refusal(error) from error
        if until is None:
            return None
        stored = _timeout_expiry(member)
        return stored.timestamp() if stored is not None else until.timestamp()

    async def delete_messages(
        self, channel_id: str, message_ids: list[str]
    ) -> DeleteResult:
        """Bulk delete what Discord allows, then one at a time; never raises."""
        result = DeleteResult()
        ids = [int(message_id) for message_id in message_ids]
        cutoff = datetime.now(UTC) - BULK_DELETE_MAX_AGE
        bulk = [
            message_id
            for message_id in ids
            if snowflake_to_datetime(message_id) >= cutoff
        ]
        single = [message_id for message_id in ids if message_id not in bulk]
        if len(bulk) >= 2:
            try:
                await self.bot.rest.delete_messages(int(channel_id), bulk)
                result.deleted.extend(str(message_id) for message_id in bulk)
                bulk = []
            except hikari.BulkDeleteError as error:
                done = {
                    int(message.id) if hasattr(message, "id") else int(message)
                    for message in error.deleted_messages
                }
                result.deleted.extend(
                    str(message_id) for message_id in bulk if message_id in done
                )
                bulk = [message_id for message_id in bulk if message_id not in done]
            except hikari.ForbiddenError:
                result.refused.extend(str(message_id) for message_id in ids)
                return result
            except Exception:  # noqa: BLE001 — one at a time below
                logger.warning(
                    "Bulk delete in channel %s failed; deleting one at a time",
                    channel_id,
                    exc_info=True,
                )
        for message_id in [*bulk, *single]:
            try:
                await self.bot.rest.delete_message(int(channel_id), message_id)
            except hikari.NotFoundError:
                result.missing.append(str(message_id))
            except hikari.ForbiddenError:
                result.refused.append(str(message_id))
            except Exception:  # noqa: BLE001 — may have landed
                logger.warning(
                    "Deleting message %s may not have landed", message_id, exc_info=True
                )
                result.unknown.append(str(message_id))
            else:
                result.deleted.append(str(message_id))
        return result

    async def send_message(
        self,
        channel_id: str,
        content: str,
        *,
        role_mention_ids: list[str] | None = None,
        attachment: tuple[str, bytes] | None = None,
    ) -> str:
        try:
            message = await self.bot.rest.create_message(
                hikari.Snowflake(int(channel_id)),
                content,
                attachment=hikari.Bytes(attachment[1], attachment[0])
                if attachment
                else hikari.UNDEFINED,
                mentions_everyone=False,
                user_mentions=False,
                role_mentions=[hikari.Snowflake(int(role)) for role in role_mention_ids]
                if role_mention_ids
                else False,
            )
        except _DEFINITE as error:
            raise _refusal(error) from error
        return str(message.id)

    async def edit_message(
        self,
        channel_id: str,
        message_id: str,
        content: str,
        *,
        attachment: tuple[str, bytes] | None = None,
    ) -> None:
        try:
            await self.bot.rest.edit_message(
                hikari.Snowflake(int(channel_id)),
                hikari.Snowflake(int(message_id)),
                content,
                attachment=hikari.Bytes(attachment[1], attachment[0])
                if attachment
                else hikari.UNDEFINED,
                mentions_everyone=False,
                user_mentions=False,
                role_mentions=False,
            )
        except _DEFINITE as error:
            raise _refusal(error) from error

    async def find_message(self, channel_id: str, marker: str) -> str | None:
        me = self.bot.get_me()
        async for message in self.bot.rest.fetch_messages(int(channel_id)).limit(
            FIND_REPORT_WINDOW
        ):
            author = getattr(message, "author", None)
            if me is not None and author is not None and author.id != me.id:
                continue
            if marker in (message.content or ""):
                return str(message.id)
        return None

    async def announce(self, action) -> None:
        await dispatch_mod_action(action, bot=self.bot)


def bot_gateway(bot: Any, *, extra_role_ids: Iterable[str] = ()) -> ModerationGateway:
    """The gateway for one bot-side action, with the guild's staff-exempt roles."""
    return ModerationGateway(HikariModerationPort(bot, extra_role_ids=extra_role_ids))


def manual_trigger(
    *,
    guild_id: Any,
    channel_id: Any,
    moderator: Any,
    reason: str | None,
) -> Trigger:
    """A moderator's slash command, recorded through the gateway.

    Manual actions skip the automated checks (a moderator chose them) but are
    serialised with the automation on the same member, and a moderator's
    timeout is one the automation never shortens or replaces.
    """
    return Trigger(
        guild_id=str(guild_id),
        actor="manual",
        reason=reason or "",
        source=gw.SOURCE_MANUAL,
        channel_id=str(channel_id) if channel_id else None,
        moderator_user_id=str(moderator.id),
        moderator_username=moderator.username,
    )
