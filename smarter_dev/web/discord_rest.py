"""Shared bot-token Discord REST plumbing for the worker tier.

The handler runtime runs in the agent-worker process, which has no gateway
connection — everything a handler does to Discord goes out as a plain
bot-token REST call. This module is the single place that builds those calls
(auth headers, base URL, error mapping); :class:`DiscordEmitter` and
:class:`AdminActor` are thin subclasses that add their endpoints.

Rate limits are absorbed here, but only just: a 429 is retried exactly once
and only when Discord asks us to wait at most
:data:`MAX_RETRY_AFTER_SECONDS`. A handler fire is time-bounded, so a long
``Retry-After`` means the bucket is genuinely exhausted and stalling the
worker on it buys nothing — failing fast frees the worker for other fires.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from typing import ClassVar

import httpx

API_BASE = "https://discord.com/api/v10"
_ERROR_BODY_MAX = 500

# Longest ``Retry-After`` we are willing to wait out inside a single fire.
MAX_RETRY_AFTER_SECONDS = 5.0
# Used when Discord sends a 429 with no wait hint at all.
_DEFAULT_RETRY_AFTER_SECONDS = 1.0

# Module-level indirection so tests can swap in a recorder and never sleep.
_sleep = asyncio.sleep


class DiscordRestError(Exception):
    """A Discord REST call failed with an error status.

    ``status_code`` carries the HTTP status of the failed response so callers
    can branch on it (e.g. map 404 to a not-found error) without parsing the
    message string. It is ``None`` for transport-level failures.

    ``error_code`` carries Discord's own JSON error code (e.g. 40003, "opening
    direct messages too fast") when the body supplied one. Discord overloads a
    single HTTP status across wildly different causes, so this is what callers
    branch on to tell a retryable condition from a permanent one. It is
    ``None`` whenever the body was not a JSON object with an integer ``code``.
    """

    status_code: int | None = None
    error_code: int | None = None


def _json_object(response: httpx.Response) -> dict[str, Any]:
    """Body as a JSON object, or ``{}`` for anything else.

    Error paths must never be derailed by an unparseable body, so every
    failure mode (non-JSON, JSON array, truncated stream) collapses to ``{}``.
    """
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 - any parse failure means "no fields"
        return {}
    return payload if isinstance(payload, dict) else {}


def _retry_after_seconds(response: httpx.Response) -> float:
    """How long Discord wants us to wait before retrying this 429.

    The header is authoritative (and may be fractional); the JSON body's
    ``retry_after`` is the fallback for responses that omit it. Anything
    unparseable falls back to a short fixed wait rather than failing to honour
    the rate limit at all.
    """
    raw: Any = response.headers.get("Retry-After")
    if raw is None:
        raw = _json_object(response).get("retry_after")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_RETRY_AFTER_SECONDS


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

    def _error(
        self, method: str, endpoint: str, response: httpx.Response
    ) -> DiscordRestError:
        """Build the exception for a failed response, including Discord's code."""
        error = self.error_type(
            f"{method} {endpoint} -> {response.status_code}: "
            f"{response.text[:_ERROR_BODY_MAX]}"
        )
        error.status_code = response.status_code
        code = _json_object(response).get("code")
        # bool is an int subclass; a JSON ``true`` is not an error code.
        if isinstance(code, int) and not isinstance(code, bool):
            error.error_code = code
        return error

    async def _send(
        self, method: str, endpoint: str, merged_headers: dict[str, str], kwargs: dict
    ) -> httpx.Response:
        async with httpx.AsyncClient(
            timeout=self.timeout, transport=self.transport
        ) as client:
            return await client.request(
                method, f"{self.api_base}{endpoint}", headers=merged_headers, **kwargs
            )

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        headers: dict[str, str] | None = None,
        **kwargs,
    ) -> httpx.Response:
        """Send one request; extra ``headers`` merge over the auth headers.

        A 429 is retried at most once, after waiting the ``Retry-After``
        header (or the body's ``retry_after``, or
        :data:`_DEFAULT_RETRY_AFTER_SECONDS` when neither is given). The retry
        is capped at :data:`MAX_RETRY_AFTER_SECONDS`: a longer wait means the
        bucket is genuinely exhausted, and a handler fire is time-bounded, so
        we raise immediately rather than park the worker on it. Only 429 is
        retried — every other >=400 raises on the first response, and a second
        failure after the retry raises too.

        Raises ``error_type`` with ``status_code`` set and ``error_code`` set
        to Discord's JSON ``code`` when the body carried one.
        """
        merged_headers = {**self._headers, **(headers or {})}
        response = await self._send(method, endpoint, merged_headers, kwargs)
        if response.status_code == 429:
            wait = _retry_after_seconds(response)
            if wait <= MAX_RETRY_AFTER_SECONDS:
                await _sleep(wait)
                response = await self._send(method, endpoint, merged_headers, kwargs)
        if response.status_code >= 400:
            raise self._error(method, endpoint, response)
        return response
