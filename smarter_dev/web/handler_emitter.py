"""Discord REST emitter for the worker tier.

The handler runtime runs in the agent-worker process, which has no gateway
connection — so it emits to Discord over the REST API with the bot token. This
is the *only* way a handler reaches a channel; the sandboxed script can call it
solely through the metered external functions in
:mod:`smarter_dev.web.handler_runtime`.

Kept deliberately small: send a message, add a reaction. Mirrors the request
shape of :class:`smarter_dev.web.admin.discord.DiscordClient` but for the
POST/PUT endpoints that client does not expose.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

_API_BASE = "https://discord.com/api/v10"
_MESSAGE_MAX = 2000
# Discord message flag: suppress auto-generated link-preview embeds. Handler
# output is often a list of links (e.g. a news digest); without this each URL
# explodes into a large preview card, flooding the channel.
_SUPPRESS_EMBEDS = 1 << 2


class DiscordEmitError(Exception):
    """A Discord REST emit failed."""


@dataclass
class DiscordEmitter:
    """Minimal bot-token REST emitter used by handler executions."""

    bot_token: str
    timeout: float = 15.0

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bot {self.bot_token}",
            "User-Agent": "SmarterDev-Handlers/1.0",
        }

    async def _request(self, method: str, endpoint: str, **kwargs) -> httpx.Response:
        url = f"{_API_BASE}{endpoint}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.request(
                method, url, headers=self._headers, **kwargs
            )
        if response.status_code >= 400:
            body = response.text[:500]
            raise DiscordEmitError(
                f"{method} {endpoint} -> {response.status_code}: {body}"
            )
        return response

    async def create_message(self, channel_id: str, content: str) -> str:
        """Post a message to a channel; return the new message id.

        Link-preview embeds are suppressed so a handler that posts URLs doesn't
        flood the channel with large preview cards.
        """
        payload = {"content": content[:_MESSAGE_MAX], "flags": _SUPPRESS_EMBEDS}
        response = await self._request(
            "POST", f"/channels/{channel_id}/messages", json=payload
        )
        return str(response.json().get("id", ""))

    async def add_reaction(self, channel_id: str, message_id: str, emoji: str) -> None:
        """React to a message. ``emoji`` is ``name:id`` for custom, else unicode."""
        cleaned = emoji.strip().lstrip("<").rstrip(">")
        encoded = quote(cleaned, safe="")
        await self._request(
            "PUT",
            f"/channels/{channel_id}/messages/{message_id}/reactions/{encoded}/@me",
        )
