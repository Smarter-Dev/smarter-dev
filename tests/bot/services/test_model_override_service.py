"""Tests for ModelOverrideService — API paths and cache invalidation on write."""

from __future__ import annotations

import pytest

from smarter_dev.bot.services.exceptions import APIError
from smarter_dev.bot.services.model_override_service import ModelOverrideService
from smarter_dev.bot.services.models import ChannelModelOverride
from tests.bot.services.conftest import create_mock_response

GUILD = "111"
CHANNEL = "222"
PATH = f"/guilds/{GUILD}/channels/{CHANNEL}/model-override"


def _payload(model_key: str = "kimi-k2", daily: int = 0, hourly: int = 0) -> dict:
    return {
        "guild_id": GUILD,
        "channel_id": CHANNEL,
        "model_key": model_key,
        "daily_token_budget": daily,
        "hourly_token_budget": hourly,
        "created_at": "2026-07-14T00:00:00+00:00",
        "updated_at": "2026-07-14T00:00:00+00:00",
    }


@pytest.fixture
async def service(mock_api_client, mock_cache_manager) -> ModelOverrideService:
    svc = ModelOverrideService(mock_api_client, mock_cache_manager)
    await svc.initialize()
    return svc


async def test_get_override_hits_correct_path(service, mock_api_client):
    mock_api_client.get.return_value = create_mock_response(200, _payload("glm-4-6"))

    result = await service.get_override(GUILD, CHANNEL)

    mock_api_client.get.assert_awaited_once_with(PATH)
    assert isinstance(result, ChannelModelOverride)
    assert result.model_key == "glm-4-6"
    assert result.created_at is not None


async def test_get_override_returns_none_on_404(service, mock_api_client):
    # The real APIClient raises APIError for any status >= 400; a 404 means the
    # channel simply has no override configured.
    mock_api_client.get.side_effect = APIError("not found", status_code=404)
    assert await service.get_override(GUILD, CHANNEL) is None


async def test_get_override_propagates_non_404_api_error(service, mock_api_client):
    # A server-side failure must not be silently swallowed as "no override".
    mock_api_client.get.side_effect = APIError("boom", status_code=500)
    with pytest.raises(APIError) as excinfo:
        await service.get_override(GUILD, CHANNEL)
    assert excinfo.value.status_code == 500


async def test_get_override_caches_no_override_result(service, mock_api_client):
    # The common no-override case must be cached too, keeping the hot chat path
    # off the wire after the first lookup.
    mock_api_client.get.side_effect = APIError("not found", status_code=404)
    assert await service.get_override(GUILD, CHANNEL) is None
    assert await service.get_override(GUILD, CHANNEL) is None
    assert mock_api_client.get.await_count == 1


async def test_get_override_uses_cache(service, mock_api_client):
    mock_api_client.get.return_value = create_mock_response(200, _payload())
    await service.get_override(GUILD, CHANNEL)
    await service.get_override(GUILD, CHANNEL)
    # Second call served from cache — API hit only once.
    assert mock_api_client.get.await_count == 1


async def test_set_override_puts_and_returns_dto(service, mock_api_client):
    mock_api_client.put.return_value = create_mock_response(
        200, _payload("gpt-5-4", daily=100, hourly=10)
    )

    result = await service.set_override(
        GUILD, CHANNEL, "gpt-5-4", 100, 10, reasoning_level="high"
    )

    mock_api_client.put.assert_awaited_once_with(
        PATH,
        json_data={
            "model_key": "gpt-5-4",
            "reasoning_level": "high",
            "daily_token_budget": 100,
            "hourly_token_budget": 10,
        },
    )
    assert result.model_key == "gpt-5-4"
    assert result.daily_token_budget == 100


async def test_set_override_invalidates_cache(service, mock_api_client):
    # Prime the cache with an initial GET.
    mock_api_client.get.return_value = create_mock_response(200, _payload("kimi-k2"))
    await service.get_override(GUILD, CHANNEL)

    # Write a new value — this must invalidate the cache.
    mock_api_client.put.return_value = create_mock_response(200, _payload("glm-4-6"))
    await service.set_override(GUILD, CHANNEL, "glm-4-6", 0, 0)

    # Next GET must refetch (fresh value), not serve the stale cached one.
    mock_api_client.get.return_value = create_mock_response(200, _payload("glm-4-6"))
    refetched = await service.get_override(GUILD, CHANNEL)
    assert refetched.model_key == "glm-4-6"
    assert mock_api_client.get.await_count == 2


async def test_clear_override_deletes_and_invalidates(service, mock_api_client):
    mock_api_client.get.return_value = create_mock_response(200, _payload("kimi-k2"))
    await service.get_override(GUILD, CHANNEL)

    mock_api_client.delete.return_value = create_mock_response(204)
    await service.clear_override(GUILD, CHANNEL)
    mock_api_client.delete.assert_awaited_once_with(PATH)

    # Cache cleared → next GET refetches (now 404 → no override).
    mock_api_client.get.side_effect = APIError("not found", status_code=404)
    assert await service.get_override(GUILD, CHANNEL) is None
    assert mock_api_client.get.await_count == 2
