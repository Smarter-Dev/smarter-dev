"""Tests for the Discord client calls that code jam setup uses."""

from __future__ import annotations

import json

import httpx
import pytest

from smarter_dev.web.discord_admin_client import DiscordAdminClient
from smarter_dev.web.discord_admin_client import DiscordAdminError

_REASON = "Code jam setup from the admin panel"
_ROLE = {"color": 0, "position": 1, "managed": False, "mentionable": False}


def _client(handler) -> tuple[DiscordAdminClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = DiscordAdminClient(bot_token="tok", transport=httpx.MockTransport(handle))
    return client, seen


async def test_channels_carry_their_category_and_roles_their_permissions():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/channels"):
            return httpx.Response(
                200,
                json=[
                    {"id": "1", "name": "Code Jams", "type": 4, "position": 0},
                    {
                        "id": "2",
                        "name": "jam-news",
                        "type": 0,
                        "position": 1,
                        "parent_id": "1",
                    },
                ],
            )
        return httpx.Response(
            200,
            json=[
                {**_ROLE, "id": "5", "name": "Jammer", "permissions": "0"},
                {**_ROLE, "id": "6", "name": "Mods", "permissions": "8"},
            ],
        )

    client, _ = _client(handler)

    channels = {c.id: c for c in await client.get_guild_channels("9")}
    roles = {r.name: r for r in await client.get_guild_roles("9")}

    assert channels["1"].parent_id is None
    assert channels["2"].parent_id == "1"
    assert (roles["Jammer"].permissions, roles["Mods"].permissions) == (0, 8)


async def test_get_bot_user_id():
    client, seen = _client(lambda request: httpx.Response(200, json={"id": "77"}))

    assert await client.get_bot_user_id() == "77"
    assert seen[0].method == "GET"
    assert seen[0].url.path.endswith("/users/@me")


async def test_create_guild_channel_posts_the_payload_with_a_reason():
    payload = {"name": "jam-news", "type": 0, "parent_id": "1"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"id": "2", "position": 3, **payload})

    client, seen = _client(handler)

    channel = await client.create_guild_channel("9", payload, reason=_REASON)

    (request,) = seen
    assert request.method == "POST"
    assert request.url.path.endswith("/guilds/9/channels")
    assert json.loads(request.content) == payload
    assert request.headers["X-Audit-Log-Reason"] == _REASON
    assert request.headers["Authorization"] == "Bot tok"
    assert (channel.id, channel.name, channel.type, channel.parent_id) == (
        "2",
        "jam-news",
        0,
        "1",
    )


async def test_create_guild_role_grants_nothing_and_is_not_mentionable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "5", "name": "Jammer", "position": 1})

    client, seen = _client(handler)

    role = await client.create_guild_role("9", "Jammer", reason=_REASON)

    (request,) = seen
    assert request.method == "POST"
    assert request.url.path.endswith("/guilds/9/roles")
    assert json.loads(request.content) == {
        "name": "Jammer",
        "permissions": "0",
        "mentionable": False,
    }
    assert request.headers["X-Audit-Log-Reason"] == _REASON
    assert (role.id, role.name, role.permissions) == ("5", "Jammer", 0)


@pytest.mark.parametrize("body", [{"id": "3"}, {"id": "3", "available_tags": None}])
async def test_a_forum_without_tags_has_an_empty_list(body):
    client, _ = _client(lambda request: httpx.Response(200, json=body))

    assert await client.get_forum_tags("3") == []


async def test_get_forum_tags_returns_discords_tag_objects():
    tags = [{"id": "30", "name": "2026", "moderated": True}]
    client, seen = _client(
        lambda request: httpx.Response(200, json={"id": "3", "available_tags": tags})
    )

    assert await client.get_forum_tags("3") == tags
    assert seen[0].method == "GET"
    assert seen[0].url.path.endswith("/channels/3")


async def test_set_forum_tags_sends_the_whole_list_and_returns_what_was_stored():
    sent = [{"id": "30", "name": "2026"}, {"name": "Holiday Jam", "moderated": True}]
    stored = [sent[0], {"id": "31", **sent[1]}]
    client, seen = _client(
        lambda request: httpx.Response(200, json={"id": "3", "available_tags": stored})
    )

    assert await client.set_forum_tags("3", sent, reason=_REASON) == stored

    (request,) = seen
    assert request.method == "PATCH"
    assert request.url.path.endswith("/channels/3")
    assert json.loads(request.content) == {"available_tags": sent}
    assert request.headers["X-Audit-Log-Reason"] == _REASON


@pytest.mark.parametrize("status", [403, 429, 500])
async def test_a_refused_write_raises_with_the_status(status):
    client, _ = _client(lambda request: httpx.Response(status, json={"message": "no"}))

    with pytest.raises(DiscordAdminError) as raised:
        await client.create_guild_role("9", "Jammer", reason=_REASON)

    assert raised.value.status_code == status
