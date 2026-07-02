"""Discord REST emitter for the worker tier.

The handler runtime runs in the agent-worker process, which has no gateway
connection — so it emits to Discord over the REST API with the bot token. This
is the *only* way a handler reaches a channel; the sandboxed script can call it
solely through the metered external functions in
:mod:`smarter_dev.web.handler_runtime`.

Kept deliberately small: send a message, add a reaction. Request plumbing
lives in :mod:`smarter_dev.web.discord_rest`, shared with ``AdminActor``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import ClassVar
from urllib.parse import quote

from smarter_dev.web.discord_rest import DiscordBotClient, DiscordRestError

logger = logging.getLogger(__name__)

_MESSAGE_MAX = 2000
# Discord message flag: suppress auto-generated link-preview embeds. Handler
# output is often a list of links (e.g. a news digest); without this each URL
# explodes into a large preview card, flooding the channel.
_SUPPRESS_EMBEDS = 1 << 2


class DiscordEmitError(DiscordRestError):
    """A Discord REST emit failed."""


@dataclass(kw_only=True)
class DiscordEmitter(DiscordBotClient):
    """Minimal bot-token REST emitter used by handler executions."""

    user_agent: ClassVar[str] = "SmarterDev-Handlers/1.0"
    error_type: ClassVar[type[DiscordRestError]] = DiscordEmitError

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
