"""Bot modal reply when the submit route answers a squad cooldown 429 (#432)."""

from __future__ import annotations

import re
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock

import hikari
import httpx
import pytest

from smarter_dev.bot.services.api_client import APIClient
from smarter_dev.bot.services.exceptions import APIError
from smarter_dev.bot.services.exceptions import RateLimitError
from smarter_dev.bot.views.challenge_views import SolutionSubmissionModalHandler


def _interaction(solution: str = "K7Q2") -> Mock:
    component = SimpleNamespace(custom_id="solution", value=solution)
    interaction = Mock()
    interaction.components = [SimpleNamespace(components=[component])]
    interaction.create_initial_response = AsyncMock()
    return interaction


def _handler(error: Exception) -> SolutionSubmissionModalHandler:
    api_client = Mock()
    api_client.post = AsyncMock(side_effect=error)
    return SolutionSubmissionModalHandler(
        challenge_id="c1",
        challenge_title="Frame one",
        guild_id="123",
        user=SimpleNamespace(id=1001),
        api_client=api_client,
    )


async def _client_error(
    status: int, body: dict, headers: dict | None = None
) -> APIError:
    """The exception APIClient raises for this response."""
    response = httpx.Response(status, json=body, headers=headers)
    client = APIClient.__new__(APIClient)
    with pytest.raises(APIError) as info:
        await client._handle_error_response(response)
    return info.value


async def test_cooldown_429_says_when_the_squad_can_try_again():
    error = await _client_error(
        429,
        {
            "detail": "Squad is on submission cooldown",
            "retry_after": 118,
            "retry_at": "2026-11-01T12:05:00+00:00",
            "tier": 2,
        },
        headers={"Retry-After": "118"},
    )
    assert isinstance(error, RateLimitError)
    interaction = _interaction()

    before = int(time.time())
    await _handler(error).handle_submit(interaction)
    after = int(time.time())

    kwargs = interaction.create_initial_response.await_args.kwargs
    content = kwargs["content"]
    assert "cooldown" in content
    stamp = int(re.search(r"<t:(\d+):R>", content).group(1))
    assert before + 118 <= stamp <= after + 118
    assert kwargs["flags"] == hikari.MessageFlag.EPHEMERAL


async def test_other_429_keeps_the_generic_failure_reply():
    error = await _client_error(429, {"detail": "Rate limit exceeded"})
    interaction = _interaction()

    await _handler(error).handle_submit(interaction)

    content = interaction.create_initial_response.await_args.kwargs["content"]
    assert content == "❌ Submission failed."
