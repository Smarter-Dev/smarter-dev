"""Tests for the admin-panel Discord read client."""

from __future__ import annotations

import json

import httpx
import pytest

from smarter_dev.web.discord_admin_client import (
    DiscordAdminClient,
    DiscordAdminError,
    GuildNotFoundError,
)


def _json_transport(handler) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        return handler(request)

    return httpx.MockTransport(handle)


async def test_list_bot_guilds_shapes_summaries():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/users/@me/guilds")
        body = json.dumps(
            [
                {"id": "1", "name": "First", "icon": "abc"},
                {"id": "2", "name": "Second", "icon": None},
            ]
        )
        return httpx.Response(200, text=body)

    client = DiscordAdminClient(bot_token="tok", transport=_json_transport(handler))
    guilds = await client.list_bot_guilds()

    assert [g.id for g in guilds] == ["1", "2"]
    assert guilds[0].icon_url == "https://cdn.discordapp.com/icons/1/abc.png"
    assert guilds[1].icon_url is None


async def test_get_guild_shapes_detail_with_member_count():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/guilds/42")
        assert request.url.params["with_counts"] == "true"
        body = json.dumps(
            {
                "id": "42",
                "name": "Answer",
                "icon": "hash",
                "owner_id": "99",
                "approximate_member_count": 1234,
                "description": "the guild",
            }
        )
        return httpx.Response(200, text=body)

    client = DiscordAdminClient(bot_token="tok", transport=_json_transport(handler))
    guild = await client.get_guild("42")

    assert guild.id == "42"
    assert guild.name == "Answer"
    assert guild.owner_id == "99"
    assert guild.member_count == 1234
    assert guild.description == "the guild"
    assert guild.icon_url == "https://cdn.discordapp.com/icons/42/hash.png"


async def test_get_guild_missing_member_count_is_none():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.dumps({"id": "42", "name": "Answer", "owner_id": "99"})
        return httpx.Response(200, text=body)

    client = DiscordAdminClient(bot_token="tok", transport=_json_transport(handler))
    guild = await client.get_guild("42")

    assert guild.member_count is None
    assert guild.icon_url is None


async def test_get_guild_404_raises_guild_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text='{"message": "Unknown Guild"}')

    client = DiscordAdminClient(bot_token="tok", transport=_json_transport(handler))
    with pytest.raises(GuildNotFoundError):
        await client.get_guild("nope")


async def test_get_guild_other_error_raises_admin_error_not_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = DiscordAdminClient(bot_token="tok", transport=_json_transport(handler))
    with pytest.raises(DiscordAdminError) as exc_info:
        await client.get_guild("42")
    assert not isinstance(exc_info.value, GuildNotFoundError)


async def test_api_base_override_is_used():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text="[]")

    client = DiscordAdminClient(
        bot_token="tok",
        api_base="http://localhost:9999",
        transport=_json_transport(handler),
    )
    await client.list_bot_guilds()
    assert seen == ["http://localhost:9999/users/@me/guilds"]
