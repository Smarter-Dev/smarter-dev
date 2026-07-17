"""Moderation actions for admin handlers, via Discord REST (worker tier).

These are the privileged operations an admin-handler script can call as external
functions: ban, kick, timeout, delete-message. Each is a single bot-token REST
call. Standard (member) handlers never get an ``AdminActor`` and so can't reach
any of this.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import ClassVar
from urllib.parse import quote

from smarter_dev.web.discord_rest import DiscordBotClient, DiscordRestError

# Discord channel types 10/11/12: announcement, public, and private threads.
_THREAD_CHANNEL_TYPES = frozenset({10, 11, 12})


class AdminActionError(DiscordRestError):
    """A moderation action's REST call failed."""


@dataclass(kw_only=True)
class AdminActor(DiscordBotClient):
    """Performs moderation actions for one guild via Discord REST."""

    guild_id: str
    _verified_thread_ids: set[str] = field(default_factory=set, init=False)

    user_agent: ClassVar[str] = "SmarterDev-AdminHandlers/1.0"
    error_type: ClassVar[type[DiscordRestError]] = AdminActionError

    async def ban_user(self, user_id: str, reason: str | None = None) -> str:
        # Discord expects the audit-log reason URL-encoded; encoding also keeps
        # non-latin-1 reasons (em dashes, emoji) out of raw header bytes.
        headers = (
            {"X-Audit-Log-Reason": quote(reason[:400])} if reason else None
        )
        await self._request(
            "PUT", f"/guilds/{self.guild_id}/bans/{user_id}", headers=headers
        )
        return f"banned {user_id}"

    async def kick_user(self, user_id: str) -> str:
        await self._request("DELETE", f"/guilds/{self.guild_id}/members/{user_id}")
        return f"kicked {user_id}"

    async def timeout_user(self, user_id: str, duration_seconds: int = 600) -> str:
        until = (
            datetime.now(timezone.utc) + timedelta(seconds=int(duration_seconds))
        ).isoformat()
        await self._request(
            "PATCH",
            f"/guilds/{self.guild_id}/members/{user_id}",
            json={"communication_disabled_until": until},
        )
        return f"timed out {user_id} for {int(duration_seconds)}s"

    async def delete_message(self, channel_id: str, message_id: str) -> str:
        await self._request(
            "DELETE", f"/channels/{channel_id}/messages/{message_id}"
        )
        return f"deleted message {message_id}"

    async def close_thread(self, thread_id: str) -> bool:
        """Archive a thread. A gone thread (404) is a silent no-op -> False."""
        return await self._patch_thread(thread_id, {"archived": True})

    async def lock_thread(self, thread_id: str) -> bool:
        """Lock and archive a thread. A gone thread (404) -> False."""
        return await self._patch_thread(
            thread_id, {"locked": True, "archived": True}
        )

    async def reopen_thread(self, thread_id: str) -> bool:
        """Unarchive a thread. A gone thread (404) -> False."""
        return await self._patch_thread(thread_id, {"archived": False})

    async def delete_thread(self, thread_id: str) -> bool:
        """Delete a thread. A gone thread (404) is a silent no-op -> False."""
        if not await self._verify_thread_target(thread_id):
            return False
        try:
            await self._request("DELETE", f"/channels/{thread_id}")
        except AdminActionError as error:
            if error.status_code == 404:
                return False
            raise
        return True

    async def _patch_thread(self, thread_id: str, payload: dict) -> bool:
        """PATCH a thread channel; 404 (gone) -> False, other failures raise."""
        if not await self._verify_thread_target(thread_id):
            return False
        try:
            await self._request("PATCH", f"/channels/{thread_id}", json=payload)
        except AdminActionError as error:
            if error.status_code == 404:
                return False
            raise
        return True

    async def _verify_thread_target(self, thread_id: str) -> bool:
        """Confirm the target is a thread in this actor's guild before mutating.

        Scripts supply thread ids; without this rail a laundered constant could
        aim PATCH/DELETE /channels/{id} at a regular channel (deleting it
        wholesale) or at another guild the bot inhabits. A gone target (404)
        -> False, matching the mutations' silent no-op contract; a non-thread
        or foreign-guild target raises. Verified ids are cached for the fire so
        sweeps pay one fetch per thread.
        """
        if thread_id in self._verified_thread_ids:
            return True
        try:
            response = await self._request("GET", f"/channels/{thread_id}")
        except AdminActionError as error:
            if error.status_code == 404:
                return False
            raise
        channel = response.json()
        if channel.get("type") not in _THREAD_CHANNEL_TYPES:
            raise AdminActionError(
                f"thread op target {thread_id} is not a thread "
                f"(channel type {channel.get('type')})"
            )
        if str(channel.get("guild_id")) != self.guild_id:
            raise AdminActionError(
                f"thread op target {thread_id} is outside guild {self.guild_id}"
            )
        self._verified_thread_ids.add(thread_id)
        return True
