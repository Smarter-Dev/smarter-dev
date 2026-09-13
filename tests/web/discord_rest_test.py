"""Tests for the shared bot-token Discord REST client."""

from __future__ import annotations

import httpx
import pytest

from smarter_dev.web import discord_rest
from smarter_dev.web.discord_rest import API_BASE
from smarter_dev.web.discord_rest import MAX_RETRY_AFTER_SECONDS
from smarter_dev.web.discord_rest import DiscordBotClient
from smarter_dev.web.discord_rest import DiscordRestError


def _recording_transport(
    requests: list[httpx.Request], status_code: int = 200, body: str = "{}"
) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code, text=body)

    return httpx.MockTransport(handle)


def _sequence_transport(
    requests: list[httpx.Request], responses: list[dict]
) -> httpx.MockTransport:
    """Replays ``responses`` (kwargs for httpx.Response) in order.

    A fresh Response is built per call so the same spec can be replayed, and
    running past the end repeats the last spec rather than exploding — the
    point of these tests is the request *count*, asserted separately.
    """

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        spec = responses[min(len(requests) - 1, len(responses) - 1)]
        return httpx.Response(**spec)

    return httpx.MockTransport(handle)


@pytest.fixture
def slept(monkeypatch) -> list[float]:
    """Records every retry wait instead of actually sleeping."""
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(discord_rest, "_sleep", fake_sleep)
    return delays


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


async def test_request_retries_once_after_rate_limit(slept):
    requests: list[httpx.Request] = []
    client = DiscordBotClient(
        bot_token="tok",
        transport=_sequence_transport(
            requests,
            [
                {"status_code": 429, "headers": {"Retry-After": "0.5"}, "text": "{}"},
                {"status_code": 200, "text": '{"id": "42"}'},
            ],
        ),
    )
    response = await client._request("POST", "/channels/C/messages")
    assert response.json() == {"id": "42"}
    assert len(requests) == 2
    assert slept == [0.5]


async def test_request_raises_after_second_rate_limit(slept):
    """One retry only: a still-limited bucket fails the fire rather than looping."""
    requests: list[httpx.Request] = []
    client = DiscordBotClient(
        bot_token="tok",
        transport=_sequence_transport(
            requests,
            [{"status_code": 429, "headers": {"Retry-After": "0.5"}, "text": "{}"}],
        ),
    )
    with pytest.raises(DiscordRestError) as exc_info:
        await client._request("POST", "/channels/C/messages")
    assert exc_info.value.status_code == 429
    assert len(requests) == 2
    assert slept == [0.5]


async def test_request_does_not_wait_out_a_long_retry_after(slept):
    """A long Retry-After means the bucket is exhausted; fail fast, don't stall."""
    requests: list[httpx.Request] = []
    over_cap = MAX_RETRY_AFTER_SECONDS + 0.1
    client = DiscordBotClient(
        bot_token="tok",
        transport=_sequence_transport(
            requests,
            [
                {
                    "status_code": 429,
                    "headers": {"Retry-After": str(over_cap)},
                    "text": "{}",
                },
                {"status_code": 200, "text": "{}"},
            ],
        ),
    )
    with pytest.raises(DiscordRestError) as exc_info:
        await client._request("POST", "/channels/C/messages")
    assert exc_info.value.status_code == 429
    assert len(requests) == 1
    assert slept == []


async def test_request_falls_back_to_body_retry_after(slept):
    requests: list[httpx.Request] = []
    client = DiscordBotClient(
        bot_token="tok",
        transport=_sequence_transport(
            requests,
            [
                {"status_code": 429, "text": '{"retry_after": 0.25}'},
                {"status_code": 200, "text": "{}"},
            ],
        ),
    )
    await client._request("POST", "/channels/C/messages")
    assert len(requests) == 2
    assert slept == [0.25]


async def test_error_exposes_discord_error_code():
    """Regression: a 400 40003 (DMs too fast) must be distinguishable by code."""
    requests: list[httpx.Request] = []
    client = DiscordBotClient(
        bot_token="tok",
        transport=_recording_transport(
            requests,
            status_code=400,
            body='{"message": "You are opening direct messages too fast.", '
            '"code": 40003}',
        ),
    )
    with pytest.raises(DiscordRestError) as exc_info:
        await client._request("POST", "/users/@me/channels")
    assert exc_info.value.status_code == 400
    assert exc_info.value.error_code == 40003


async def test_error_code_is_none_for_non_json_body():
    requests: list[httpx.Request] = []
    client = DiscordBotClient(
        bot_token="tok",
        transport=_recording_transport(
            requests, status_code=502, body="<html>bad gateway</html>"
        ),
    )
    with pytest.raises(DiscordRestError) as exc_info:
        await client._request("GET", "/gateway")
    assert exc_info.value.status_code == 502
    assert exc_info.value.error_code is None


async def test_non_429_error_is_not_retried(slept):
    requests: list[httpx.Request] = []
    client = DiscordBotClient(
        bot_token="tok",
        transport=_recording_transport(requests, status_code=400, body="{}"),
    )
    with pytest.raises(DiscordRestError):
        await client._request("POST", "/channels/C/messages")
    assert len(requests) == 1
    assert slept == []
