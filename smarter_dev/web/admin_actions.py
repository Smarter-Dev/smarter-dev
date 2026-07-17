"""Moderation actions for admin handlers, via Discord REST (worker tier).

These are the privileged operations an admin-handler script can call as external
functions: ban, kick, timeout, delete-message. Each is a single bot-token REST
call. Standard (member) handlers never get an ``AdminActor`` and so can't reach
any of this.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import ClassVar
from urllib.parse import quote

from smarter_dev.web.discord_rest import DiscordBotClient, DiscordRestError


class AdminActionError(DiscordRestError):
    """A moderation action's REST call failed."""


@dataclass(kw_only=True)
class AdminActor(DiscordBotClient):
    """Performs moderation actions for one guild via Discord REST."""

    guild_id: str

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
        try:
            await self._request("DELETE", f"/channels/{thread_id}")
        except AdminActionError as error:
            if error.status_code == 404:
                return False
            raise
        return True

    async def _patch_thread(self, thread_id: str, payload: dict) -> bool:
        """PATCH a thread channel; 404 (gone) -> False, other failures raise."""
        try:
            await self._request("PATCH", f"/channels/{thread_id}", json=payload)
        except AdminActionError as error:
            if error.status_code == 404:
                return False
            raise
        return True
