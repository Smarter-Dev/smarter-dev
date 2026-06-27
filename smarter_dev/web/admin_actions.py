"""Moderation actions for admin handlers, via Discord REST (worker tier).

These are the privileged operations an admin-handler script can call as external
functions: ban, kick, timeout, delete-message. Each is a single bot-token REST
call. Standard (member) handlers never get an ``AdminActor`` and so can't reach
any of this.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

_API_BASE = "https://discord.com/api/v10"


class AdminActionError(Exception):
    """A moderation action's REST call failed."""


@dataclass
class AdminActor:
    """Performs moderation actions for one guild via Discord REST."""

    bot_token: str
    guild_id: str
    timeout: float = 15.0

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bot {self.bot_token}",
            "User-Agent": "SmarterDev-AdminHandlers/1.0",
        }

    async def _request(self, method: str, endpoint: str, **kwargs) -> None:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.request(
                method, f"{_API_BASE}{endpoint}", headers=self._headers, **kwargs
            )
        if response.status_code >= 400:
            raise AdminActionError(
                f"{method} {endpoint} -> {response.status_code}: {response.text[:300]}"
            )

    async def ban_user(self, user_id: str, reason: str | None = None) -> str:
        kwargs: dict = {}
        if reason:
            kwargs["headers"] = {**self._headers, "X-Audit-Log-Reason": reason[:400]}
        await self._request(
            "PUT", f"/guilds/{self.guild_id}/bans/{user_id}", **kwargs
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
