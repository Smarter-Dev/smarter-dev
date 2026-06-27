"""Structured moderation actions for the privileged admin tier (worker tier).

Privileged routines run a structured action spec — NOT a sandboxed script — so
there is no author, no judge, no Monty. This executor maps a validated action to
the matching Discord REST call with the bot token. It is a completely separate
code path from the member-handler runtime.

Action shape: ``{"kind": "timeout"|"kick"|"ban"|"delete", ...}`` with
``target_user_id`` (timeout/kick/ban), optional ``duration_seconds`` and
``reason`` (timeout/ban), and ``channel_id`` + ``message_id`` (delete).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

_API_BASE = "https://discord.com/api/v10"

ACTION_KINDS = ("timeout", "kick", "ban", "delete")


class PrivilegedActionError(Exception):
    """A privileged action was malformed or its REST call failed."""


@dataclass
class PrivilegedActor:
    """Performs a single structured moderation action via Discord REST."""

    bot_token: str
    timeout: float = 15.0

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bot {self.bot_token}",
            "User-Agent": "SmarterDev-PrivilegedRoutines/1.0",
        }

    async def _request(self, method: str, endpoint: str, **kwargs) -> None:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.request(
                method, f"{_API_BASE}{endpoint}", headers=self._headers, **kwargs
            )
        if response.status_code >= 400:
            raise PrivilegedActionError(
                f"{method} {endpoint} -> {response.status_code}: {response.text[:300]}"
            )

    async def execute(self, action: dict, guild_id: str) -> str:
        """Execute ``action`` for ``guild_id``; return a short outcome label."""
        kind = action.get("kind")
        if kind not in ACTION_KINDS:
            raise PrivilegedActionError(f"unknown action kind {kind!r}")

        if kind == "timeout":
            user_id = _require(action, "target_user_id")
            seconds = int(action.get("duration_seconds", 600))
            until = (
                datetime.now(timezone.utc) + timedelta(seconds=seconds)
            ).isoformat()
            await self._request(
                "PATCH",
                f"/guilds/{guild_id}/members/{user_id}",
                json={"communication_disabled_until": until},
            )
            return f"timed out {user_id} for {seconds}s"

        if kind == "kick":
            user_id = _require(action, "target_user_id")
            await self._request(
                "DELETE", f"/guilds/{guild_id}/members/{user_id}"
            )
            return f"kicked {user_id}"

        if kind == "ban":
            user_id = _require(action, "target_user_id")
            await self._request("PUT", f"/guilds/{guild_id}/bans/{user_id}")
            return f"banned {user_id}"

        # delete
        channel_id = _require(action, "channel_id")
        message_id = _require(action, "message_id")
        await self._request(
            "DELETE", f"/channels/{channel_id}/messages/{message_id}"
        )
        return f"deleted message {message_id}"


def _require(action: dict, key: str) -> str:
    value = action.get(key)
    if not value:
        raise PrivilegedActionError(f"action requires {key}")
    return str(value)


def validate_action(action: dict) -> None:
    """Raise PrivilegedActionError if the action spec is malformed."""
    kind = action.get("kind")
    if kind not in ACTION_KINDS:
        raise PrivilegedActionError(f"unknown action kind {kind!r}")
    if kind in ("timeout", "kick", "ban"):
        _require(action, "target_user_id")
    else:  # delete
        _require(action, "channel_id")
        _require(action, "message_id")
