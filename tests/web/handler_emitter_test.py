"""Tests for DiscordEmitter (worker-tier message/reaction REST calls)."""

from __future__ import annotations

import json

import httpx
import pytest

from smarter_dev.web.handler_emitter import DiscordEmitError, DiscordEmitter


def _emitter(
    requests: list[httpx.Request], status_code: int = 200, body: str = "{}"
) -> DiscordEmitter:
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code, text=body)

    return DiscordEmitter(bot_token="t", transport=httpx.MockTransport(handle))


async def test_create_message_posts_content_and_returns_id():
    requests: list[httpx.Request] = []
    message_id = await _emitter(requests, body='{"id": "M9"}').create_message(
        "C1", "hello"
    )
    assert message_id == "M9"
    request = requests[0]
    assert request.method == "POST"
    assert request.url.path.endswith("/channels/C1/messages")
    assert request.headers["Authorization"] == "Bot t"
    payload = json.loads(request.content)
    assert payload["content"] == "hello"
    # Link-preview embeds are suppressed so URL-heavy output doesn't flood.
    assert payload["flags"] == 1 << 2


async def test_create_message_truncates_to_discord_limit():
    requests: list[httpx.Request] = []
    await _emitter(requests, body='{"id": "M9"}').create_message("C1", "x" * 3000)
    assert len(json.loads(requests[0].content)["content"]) == 2000


async def test_add_reaction_encodes_emoji():
    requests: list[httpx.Request] = []
    await _emitter(requests, status_code=204).add_reaction(
        "C1", "M1", "<party:12345>"
    )
    request = requests[0]
    assert request.method == "PUT"
    # str(url) preserves the wire encoding; url.path would show it decoded.
    assert str(request.url).endswith(
        "/channels/C1/messages/M1/reactions/party%3A12345/@me"
    )


async def test_error_status_raises_emit_error():
    requests: list[httpx.Request] = []
    with pytest.raises(DiscordEmitError):
        await _emitter(requests, status_code=403, body="nope").create_message(
            "C1", "hello"
        )
