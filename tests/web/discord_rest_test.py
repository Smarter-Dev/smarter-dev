"""Tests for the shared bot-token Discord REST client."""

from __future__ import annotations

import httpx
import pytest

from smarter_dev.web.discord_rest import (
    API_BASE,
    DiscordBotClient,
    DiscordRestError,
)


def _recording_transport(
    requests: list[httpx.Request], status_code: int = 200, body: str = "{}"
) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code, text=body)

    return httpx.MockTransport(handle)


async def test_request_sends_bot_auth_headers():
    requests: list[httpx.Request] = []
    client = DiscordBotClient(
        bot_token="tok", transport=_recording_transport(requests)
    )
    await client._request("GET", "/users/@me")
    request = requests[0]
    assert str(request.url) == f"{API_BASE}/users/@me"
    assert request.headers["Authorization"] == "Bot tok"
    assert request.headers["User-Agent"] == DiscordBotClient.user_agent


async def test_request_merges_extra_headers_with_auth_headers():
    """Regression: extra headers must merge, not collide with the auth headers.

    The pre-extraction AdminActor passed ``headers`` both explicitly and via
    ``**kwargs``, so any ban with an audit-log reason crashed with a TypeError
    before the request was ever sent.
    """
    requests: list[httpx.Request] = []
    client = DiscordBotClient(
        bot_token="tok", transport=_recording_transport(requests)
    )
    await client._request(
        "PUT", "/guilds/G/bans/U", headers={"X-Audit-Log-Reason": "scam"}
    )
    request = requests[0]
    assert request.headers["Authorization"] == "Bot tok"
    assert request.headers["X-Audit-Log-Reason"] == "scam"


async def test_request_returns_response_on_success():
    requests: list[httpx.Request] = []
    client = DiscordBotClient(
        bot_token="tok",
        transport=_recording_transport(requests, body='{"id": "42"}'),
    )
    response = await client._request("POST", "/channels/C/messages")
    assert response.json() == {"id": "42"}


async def test_request_raises_error_type_on_error_status():
    requests: list[httpx.Request] = []
    client = DiscordBotClient(
        bot_token="tok",
        transport=_recording_transport(requests, status_code=403, body="Forbidden"),
    )
    with pytest.raises(DiscordRestError) as exc_info:
        await client._request("DELETE", "/guilds/G/members/U")
    message = str(exc_info.value)
    assert "DELETE /guilds/G/members/U" in message
    assert "403" in message
    assert "Forbidden" in message


async def test_subclass_error_type_and_user_agent_are_used():
    class _CustomError(DiscordRestError):
        pass

    class _CustomClient(DiscordBotClient):
        user_agent = "Custom-Agent/1.0"
        error_type = _CustomError

    requests: list[httpx.Request] = []
    client = _CustomClient(
        bot_token="tok",
        transport=_recording_transport(requests, status_code=500, body="boom"),
    )
    with pytest.raises(_CustomError):
        await client._request("GET", "/gateway")
    assert requests[0].headers["User-Agent"] == "Custom-Agent/1.0"
