"""Tests for the channel model override API router (real API client + bot auth)."""

from __future__ import annotations

import pytest


def _url(guild_id: str, channel_id: str) -> str:
    return f"/guilds/{guild_id}/channels/{channel_id}/model-override"


@pytest.fixture
def channel_id() -> str:
    return "555000111222333444"


async def test_put_then_get_round_trips(real_api_client, bot_headers, test_guild_id, channel_id):
    url = _url(test_guild_id, channel_id)
    put_resp = await real_api_client.put(
        url,
        headers=bot_headers,
        json={
            "model_key": "gpt-5-4",
            "daily_token_budget": 5000,
            "hourly_token_budget": 500,
        },
    )
    assert put_resp.status_code == 200, put_resp.text
    body = put_resp.json()
    assert body["guild_id"] == test_guild_id
    assert body["channel_id"] == channel_id
    assert body["model_key"] == "gpt-5-4"
    assert body["daily_token_budget"] == 5000
    assert body["hourly_token_budget"] == 500

    get_resp = await real_api_client.get(url, headers=bot_headers)
    assert get_resp.status_code == 200
    assert get_resp.json()["model_key"] == "gpt-5-4"


async def test_put_is_upsert(real_api_client, bot_headers, test_guild_id, channel_id):
    url = _url(test_guild_id, channel_id)
    await real_api_client.put(
        url, headers=bot_headers, json={"model_key": "kimi-k2-6"}
    )
    second = await real_api_client.put(
        url,
        headers=bot_headers,
        json={"model_key": "glm-5-2", "daily_token_budget": 42},
    )
    assert second.status_code == 200
    assert second.json()["model_key"] == "glm-5-2"
    assert second.json()["daily_token_budget"] == 42

    get_resp = await real_api_client.get(url, headers=bot_headers)
    assert get_resp.json()["model_key"] == "glm-5-2"  # single row, updated in place


async def test_put_round_trips_reasoning_level(
    real_api_client, bot_headers, test_guild_id, channel_id
):
    url = _url(test_guild_id, channel_id)
    put_resp = await real_api_client.put(
        url,
        headers=bot_headers,
        json={"model_key": "glm-5-2", "reasoning_level": "high"},
    )
    assert put_resp.status_code == 200, put_resp.text
    assert put_resp.json()["reasoning_level"] == "high"

    get_resp = await real_api_client.get(url, headers=bot_headers)
    assert get_resp.json()["reasoning_level"] == "high"


async def test_put_reasoning_level_defaults_to_null(
    real_api_client, bot_headers, test_guild_id, channel_id
):
    url = _url(test_guild_id, channel_id)
    put_resp = await real_api_client.put(
        url, headers=bot_headers, json={"model_key": "glm-5-2"}
    )
    assert put_resp.status_code == 200, put_resp.text
    assert put_resp.json()["reasoning_level"] is None


async def test_put_invalid_reasoning_level_is_422(
    real_api_client, bot_headers, test_guild_id, channel_id
):
    resp = await real_api_client.put(
        _url(test_guild_id, channel_id),
        headers=bot_headers,
        json={"model_key": "glm-5-2", "reasoning_level": "ludicrous"},
    )
    assert resp.status_code == 422


async def test_get_missing_returns_404(real_api_client, bot_headers, test_guild_id, channel_id):
    resp = await real_api_client.get(_url(test_guild_id, channel_id), headers=bot_headers)
    assert resp.status_code == 404


async def test_put_invalid_model_key_is_422(real_api_client, bot_headers, test_guild_id, channel_id):
    resp = await real_api_client.put(
        _url(test_guild_id, channel_id),
        headers=bot_headers,
        json={"model_key": "not-a-real-model"},
    )
    assert resp.status_code == 422


async def test_put_negative_budget_is_422(real_api_client, bot_headers, test_guild_id, channel_id):
    resp = await real_api_client.put(
        _url(test_guild_id, channel_id),
        headers=bot_headers,
        json={"model_key": "kimi-k2-6", "daily_token_budget": -1},
    )
    assert resp.status_code == 422


async def test_put_over_limit_budget_is_422(
    real_api_client, bot_headers, test_guild_id, channel_id
):
    # A value larger than a 32-bit signed integer must be rejected before it
    # reaches the Integer column (otherwise Postgres raises an opaque 500).
    resp = await real_api_client.put(
        _url(test_guild_id, channel_id),
        headers=bot_headers,
        json={"model_key": "kimi-k2-6", "daily_token_budget": 3_000_000_000},
    )
    assert resp.status_code == 422


async def test_put_budget_at_int32_max_is_accepted(
    real_api_client, bot_headers, test_guild_id, channel_id
):
    url = _url(test_guild_id, channel_id)
    resp = await real_api_client.put(
        url,
        headers=bot_headers,
        json={
            "model_key": "kimi-k2-6",
            "daily_token_budget": 2_147_483_647,
            "hourly_token_budget": 2_147_483_647,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["daily_token_budget"] == 2_147_483_647
    assert resp.json()["hourly_token_budget"] == 2_147_483_647


async def test_delete_removes_override(real_api_client, bot_headers, test_guild_id, channel_id):
    url = _url(test_guild_id, channel_id)
    await real_api_client.put(url, headers=bot_headers, json={"model_key": "kimi-k2-6"})

    delete_resp = await real_api_client.delete(url, headers=bot_headers)
    assert delete_resp.status_code == 204

    get_resp = await real_api_client.get(url, headers=bot_headers)
    assert get_resp.status_code == 404


async def test_delete_is_idempotent(real_api_client, bot_headers, test_guild_id, channel_id):
    resp = await real_api_client.delete(_url(test_guild_id, channel_id), headers=bot_headers)
    assert resp.status_code == 204  # deleting a non-existent override is fine


async def test_requires_auth(real_api_client, test_guild_id, channel_id):
    url = _url(test_guild_id, channel_id)
    assert (await real_api_client.get(url)).status_code in (401, 403)
    assert (
        await real_api_client.put(url, json={"model_key": "kimi-k2-6"})
    ).status_code in (401, 403)
    assert (await real_api_client.delete(url)).status_code in (401, 403)
