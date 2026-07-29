"""Discord chat-tool integration for pending web-search preview links."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch
from uuid import uuid4

import pytest

from smarter_dev.bot.agents.chat_tools import ChatDeps
from smarter_dev.bot.agents.chat_tools import _search_status
from smarter_dev.bot.agents.chat_tools import web_search


@pytest.mark.asyncio
async def test_search_reserves_and_posts_link_before_provider_call():
    events: list[str] = []
    preview_id = uuid4()
    preview_url = "https://smarter.dev/ai/search/capability"
    bot = SimpleNamespace(rest=SimpleNamespace())

    async def post_message(*args, **kwargs):
        events.append("status")

    async def reserve(query):
        events.append("reserve")
        return SimpleNamespace(id=preview_id, url=preview_url)

    async def search(client, query, num_results):
        events.append("search")
        return [
            {
                "title": "Example",
                "url": "https://example.com",
                "description": "Result",
            }
        ]

    async def populate(identifier, results):
        assert identifier == preview_id
        events.append("populate")

    bot.rest.create_message = AsyncMock(side_effect=post_message)
    ctx = SimpleNamespace(deps=ChatDeps(bot=bot, channel_id=123, guild_id=456))

    with (
        patch(
            "smarter_dev.bot.agents.chat_tools.reserve_search_preview",
            new=reserve,
        ),
        patch("smarter_dev.bot.agents.chat_tools.brave_search", new=search),
        patch(
            "smarter_dev.bot.agents.chat_tools.populate_search_preview",
            new=populate,
        ),
    ):
        results = await web_search(ctx, "python task groups")

    assert events == ["reserve", "status", "search", "populate"]
    assert results[0]["title"] == "Example"
    bot.rest.create_message.assert_awaited_once_with(
        123,
        '> -# Searching the web: ["python task groups"]'
        "(https://smarter.dev/ai/search/capability)",
    )


@pytest.mark.asyncio
async def test_preview_failure_does_not_block_search():
    bot = SimpleNamespace(
        rest=SimpleNamespace(create_message=AsyncMock())
    )
    ctx = SimpleNamespace(deps=ChatDeps(bot=bot, channel_id=123, guild_id=456))
    expected = [{"title": "Result", "url": "https://example.com", "description": ""}]

    with (
        patch(
            "smarter_dev.bot.agents.chat_tools.reserve_search_preview",
            new=AsyncMock(side_effect=RuntimeError("database unavailable")),
        ),
        patch(
            "smarter_dev.bot.agents.chat_tools.brave_search",
            new=AsyncMock(return_value=expected),
        ),
    ):
        results = await web_search(ctx, "still search")

    assert results == expected
    bot.rest.create_message.assert_awaited_once_with(
        123, '> -# Searching the web: "still search"'
    )


def test_search_status_escapes_markdown_and_preserves_long_url():
    url = "https://smarter.dev/ai/search/" + "x" * 90
    status = _search_status("look at [this]\\thing", url)

    assert '["look at \\[this\\]\\\\thing"]' in status
    assert status.endswith(f"({url})")
    assert len(f"> -# {status}") <= 2_000
