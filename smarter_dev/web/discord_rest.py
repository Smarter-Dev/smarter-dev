"""Shared bot-token Discord REST plumbing for the worker tier.

The handler runtime runs in the agent-worker process, which has no gateway
connection — everything a handler does to Discord goes out as a plain
bot-token REST call. This module is the single place that builds those calls
(auth headers, base URL, error mapping); :class:`DiscordEmitter` and
:class:`AdminActor` are thin subclasses that add their endpoints.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

import httpx

API_BASE = "https://discord.com/api/v10"
_ERROR_BODY_MAX = 500


class DiscordRestError(Exception):
    """A Discord REST call failed with an error status.

    ``status_code`` carries the HTTP status of the failed response so callers
    can branch on it (e.g. map 404 to a not-found error) without parsing the
    message string. It is ``None`` for transport-level failures.
    """

    status_code: int | None = None


@dataclass(kw_only=True)
class DiscordBotClient:
    """Minimal bot-token REST caller.

    Subclasses override ``user_agent`` to identify themselves and
    ``error_type`` so callers can keep catching their existing exception.
    """

    bot_token: str
    timeout: float = 15.0
    # Handed to httpx.AsyncClient so tests can drive the real request path
    # with a MockTransport; None means httpx's default network transport.
    transport: httpx.AsyncBaseTransport | None = None
    # Base URL for every call; overridable so the smoke harness can point a
    # client at its local mock Discord API. Defaults to the real API.
    api_base: str = API_BASE

    user_agent: ClassVar[str] = "SmarterDev/1.0"
    error_type: ClassVar[type[DiscordRestError]] = DiscordRestError

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bot {self.bot_token}",
            "User-Agent": self.user_agent,
        }

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        headers: dict[str, str] | None = None,
        **kwargs,
    ) -> httpx.Response:
        """Send one request; extra ``headers`` merge over the auth headers."""
        merged_headers = {**self._headers, **(headers or {})}
        async with httpx.AsyncClient(
            timeout=self.timeout, transport=self.transport
        ) as client:
            response = await client.request(
                method, f"{self.api_base}{endpoint}", headers=merged_headers, **kwargs
            )
        if response.status_code >= 400:
            error = self.error_type(
                f"{method} {endpoint} -> {response.status_code}: "
                f"{response.text[:_ERROR_BODY_MAX]}"
            )
            error.status_code = response.status_code
            raise error
        return response
