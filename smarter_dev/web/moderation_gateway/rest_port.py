"""The moderation gateway's door to Discord from the handler workers (REST).

The worker has no gateway connection, so every read and effect here is one
bot-token REST call through the fire's
:class:`~smarter_dev.web.admin_actions.AdminActor` (its rate-limit handling
and its per-fire staff-protection cache included).

Discord's status codes sort into the gateway's contract: 403 and 400 mean it
refused (nothing happened), 404 that the member or message is not there, and
anything else (5xx, a 429 past the one retry, a transport failure) is an
unknown outcome.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import TYPE_CHECKING
from typing import Any
from urllib.parse import quote

from smarter_dev.shared.exception_logging import log_exception
from smarter_dev.web.moderation_gateway.types import DeleteResult
from smarter_dev.web.moderation_gateway.types import EffectNotFound
from smarter_dev.web.moderation_gateway.types import EffectRejected
from smarter_dev.web.moderation_gateway.types import TimeoutState

if TYPE_CHECKING:
    from smarter_dev.web.admin_actions import AdminActor

logger = logging.getLogger(__name__)

_DISCORD_EPOCH_MS = 1420070400000
_BULK_DELETE_MAX_AGE = timedelta(days=14)
_FIND_REPORT_WINDOW = 50


def _created_at(snowflake: str) -> datetime | None:
    try:
        return datetime.fromtimestamp(
            ((int(snowflake) >> 22) + _DISCORD_EPOCH_MS) / 1000, UTC
        )
    except (TypeError, ValueError):
        return None


def _expiry(payload: dict) -> float | None:
    raw = payload.get("communication_disabled_until")
    if not raw:
        return None
    try:
        until = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (until if until.tzinfo else until.replace(tzinfo=UTC)).timestamp()


def _audit_reason(reason: str | None) -> dict[str, str] | None:
    return {"X-Audit-Log-Reason": quote(reason[:400])} if reason else None


class RestModerationPort:
    """:class:`~smarter_dev.web.moderation_gateway.ModerationPort` over one AdminActor."""

    def __init__(self, actor: AdminActor, *, chain_depth: int = 0) -> None:
        self.actor = actor
        self.chain_depth = chain_depth
        self._identity: tuple[str | None, str | None] | None = None

    def _error_type(self):
        return self.actor.error_type

    async def _call(self, method: str, endpoint: str, **kwargs: Any):
        """One request; definitive refusals become the gateway's exceptions."""
        try:
            return await self.actor._request(method, endpoint, **kwargs)
        except self._error_type() as error:
            status = getattr(error, "status_code", None)
            if status == 404:
                raise EffectNotFound("not found") from error
            if status in (400, 403):
                raise EffectRejected(
                    status,
                    "missing permission or role hierarchy"
                    if status == 403
                    else "Discord rejected it",
                ) from error
            raise

    async def bot_identity(self) -> tuple[str | None, str | None]:
        if self._identity is None:
            me = (await self.actor._request("GET", "/users/@me")).json()
            self._identity = (str(me.get("id")), me.get("username"))
        return self._identity

    async def staff_refusal(
        self, guild_id: str, user_id: str, *, is_bot: bool | None = None
    ) -> str | None:
        return await self.actor.staff_refusal(str(user_id), is_bot=is_bot)

    async def timeout_state(self, guild_id: str, user_id: str) -> TimeoutState:
        try:
            response = await self._call("GET", f"/guilds/{guild_id}/members/{user_id}")
        except EffectNotFound:
            return TimeoutState(is_member=False, until=None)
        return TimeoutState(is_member=True, until=_expiry(response.json()))

    async def is_banned(self, guild_id: str, user_id: str) -> bool:
        try:
            await self._call("GET", f"/guilds/{guild_id}/bans/{user_id}")
        except EffectNotFound:
            return False
        return True

    async def message_exists(self, channel_id: str, message_id: str) -> bool:
        try:
            await self._call("GET", f"/channels/{channel_id}/messages/{message_id}")
        except EffectNotFound:
            return False
        return True

    async def message_author(
        self, channel_id: str, message_id: str
    ) -> tuple[str, str | None, bool] | None:
        try:
            response = await self._call(
                "GET", f"/channels/{channel_id}/messages/{message_id}"
            )
        except EffectNotFound:
            return None
        message = response.json()
        author = message.get("author") or {}
        return (
            str(author.get("id", "")),
            author.get("username"),
            bool(author.get("bot") or message.get("webhook_id")),
        )

    async def ban(
        self, guild_id: str, user_id: str, *, reason: str, delete_message_seconds: int
    ) -> None:
        await self._call(
            "PUT",
            f"/guilds/{guild_id}/bans/{user_id}",
            headers=_audit_reason(reason),
            json={"delete_message_seconds": int(delete_message_seconds)},
        )

    async def kick(self, guild_id: str, user_id: str, *, reason: str) -> None:
        await self._call(
            "DELETE",
            f"/guilds/{guild_id}/members/{user_id}",
            headers=_audit_reason(reason),
        )

    async def set_timeout(
        self, guild_id: str, user_id: str, until: datetime | None, *, reason: str
    ) -> float | None:
        response = await self._call(
            "PATCH",
            f"/guilds/{guild_id}/members/{user_id}",
            headers=_audit_reason(reason),
            json={"communication_disabled_until": until.isoformat() if until else None},
        )
        try:
            stored = _expiry(response.json())
        except Exception:  # noqa: BLE001 — a 204 or odd body: trust what was asked
            stored = None
        if stored is not None:
            return stored
        return until.timestamp() if until is not None else None

    async def delete_messages(
        self, channel_id: str, message_ids: list[str]
    ) -> DeleteResult:
        result = DeleteResult()
        cutoff = datetime.now(UTC) - _BULK_DELETE_MAX_AGE
        bulk = [
            m
            for m in message_ids
            if (created := _created_at(m)) is not None and created >= cutoff
        ]
        single = [m for m in message_ids if m not in bulk]
        if len(bulk) >= 2:
            try:
                for start in range(0, len(bulk), 100):
                    chunk = bulk[start : start + 100]
                    await self._call(
                        "POST",
                        f"/channels/{channel_id}/messages/bulk-delete",
                        json={"messages": chunk},
                    )
                    result.deleted.extend(chunk)
                bulk = []
            except EffectRejected as error:
                if error.status == 403:
                    result.refused.extend(
                        m for m in message_ids if m not in result.deleted
                    )
                    return result
            except Exception:  # noqa: BLE001 — one at a time below
                log_exception(
                    logger,
                    "Bulk delete in channel %s failed; deleting one at a time",
                    channel_id,
                    level=logging.WARNING,
                )
            bulk = [m for m in bulk if m not in result.deleted]
        for message_id in [*bulk, *single]:
            try:
                await self._call(
                    "DELETE", f"/channels/{channel_id}/messages/{message_id}"
                )
            except EffectNotFound:
                result.missing.append(message_id)
            except EffectRejected:
                result.refused.append(message_id)
            except Exception:  # noqa: BLE001 — may have landed
                result.unknown.append(message_id)
            else:
                result.deleted.append(message_id)
        return result

    def _message_body(
        self,
        content: str,
        role_mention_ids: list[str] | None,
        attachment: tuple[str, bytes] | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "content": content,
            "allowed_mentions": {"parse": [], "roles": list(role_mention_ids or [])},
        }
        if attachment is None:
            return {"json": payload}
        payload["attachments"] = [{"id": 0, "filename": attachment[0]}]
        return {
            "data": {"payload_json": json.dumps(payload)},
            "files": {"files[0]": (attachment[0], attachment[1], "text/plain")},
        }

    async def send_message(
        self,
        channel_id: str,
        content: str,
        *,
        role_mention_ids: list[str] | None = None,
        attachment: tuple[str, bytes] | None = None,
    ) -> str:
        response = await self._call(
            "POST",
            f"/channels/{channel_id}/messages",
            **self._message_body(content, role_mention_ids, attachment),
        )
        return str(response.json()["id"])

    async def edit_message(
        self,
        channel_id: str,
        message_id: str,
        content: str,
        *,
        attachment: tuple[str, bytes] | None = None,
    ) -> None:
        await self._call(
            "PATCH",
            f"/channels/{channel_id}/messages/{message_id}",
            **self._message_body(content, None, attachment),
        )

    async def find_message(self, channel_id: str, marker: str) -> str | None:
        bot_id, _ = await self.bot_identity()
        response = await self._call(
            "GET",
            f"/channels/{channel_id}/messages",
            params={"limit": _FIND_REPORT_WINDOW},
        )
        for message in response.json():
            if bot_id and str((message.get("author") or {}).get("id")) != bot_id:
                continue
            if marker in (message.get("content") or ""):
                return str(message["id"])
        return None

    async def announce(self, action) -> None:
        """Short-term memory and the ``mod_action`` handlers; never raises."""
        from smarter_dev.shared.guild_event_log import mod_action_event
        from smarter_dev.web.guild_event_recorder import record_guild_event
        from smarter_dev.web.handler_dispatch import build_mod_action_context
        from smarter_dev.web.handler_dispatch import dispatch_handler_event
        from smarter_dev.web.moderation_gateway.gateway import default_session_factory

        try:
            context = build_mod_action_context(action)
            await record_guild_event(
                mod_action_event(context, guild_id=str(action.guild_id))
            )
            # A handler's action -> mod-log handler -> whatever THAT does is a
            # real chain: it descends one generation, so the dispatch choke
            # point cuts it past MAX_CHAIN_DEPTH.
            async with default_session_factory()() as session:
                await dispatch_handler_event(
                    session,
                    guild_id=str(action.guild_id),
                    channel_id="",
                    trigger_type="mod_action",
                    trigger_context=context,
                    chain_depth=self.chain_depth + 1,
                )
        except Exception:  # noqa: BLE001 — observers never break an action
            log_exception(logger, "mod_action announcement failed", level=logging.DEBUG)
