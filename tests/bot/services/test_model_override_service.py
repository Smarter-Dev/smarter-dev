"""Tests for ModelOverrideService — API paths and cache invalidation on write."""

from __future__ import annotations

import pytest

from smarter_dev.bot.services import model_override_service
from smarter_dev.bot.services.exceptions import APIError
from smarter_dev.bot.services.model_override_service import ModelOverrideService
from smarter_dev.bot.services.models import ChannelModelOverride
from tests.bot.services.conftest import create_mock_response

GUILD = "111"
CHANNEL = "222"
PATH = f"/guilds/{GUILD}/channels/{CHANNEL}/model-override"


def _payload(model_key: str = "kimi-k2", daily: int = 0, hourly: int = 0, **extra) -> dict:
    return {
        "guild_id": GUILD,
        "channel_id": CHANNEL,
        "model_key": model_key,
        "daily_token_budget": daily,
        "hourly_token_budget": hourly,
        "auto_respond": False,
        "fallback_model_key": None,
        "response_filter": None,
        "drafter_model": None,
        "created_at": "2026-07-14T00:00:00+00:00",
        "updated_at": "2026-07-14T00:00:00+00:00",
        **extra,
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
            "auto_respond": False,
            "fallback_model_key": None,
            "response_filter": None,
            "drafter_model": None,
        },
    )
    assert result.model_key == "gpt-5-4"
    assert result.daily_token_budget == 100


async def test_get_override_parses_new_settings(service, mock_api_client):
    mock_api_client.get.return_value = create_mock_response(
        200,
        _payload(
            auto_respond=True,
            fallback_model_key="glm-4-6",
            response_filter="Only coding questions.",
        ),
    )

    result = await service.get_override(GUILD, CHANNEL)

    assert result.auto_respond is True
    assert result.fallback_model_key == "glm-4-6"
    assert result.response_filter == "Only coding questions."


async def test_get_override_defaults_new_settings_when_absent(service, mock_api_client):
    # An older API response that omits the new keys must degrade to the defaults.
    legacy_payload = _payload()
    del legacy_payload["auto_respond"]
    del legacy_payload["fallback_model_key"]
    del legacy_payload["response_filter"]
    mock_api_client.get.return_value = create_mock_response(200, legacy_payload)

    result = await service.get_override(GUILD, CHANNEL)

    assert result.auto_respond is False
    assert result.fallback_model_key is None
    assert result.response_filter is None


async def test_set_override_sends_new_settings(service, mock_api_client):
    mock_api_client.put.return_value = create_mock_response(
        200,
        _payload(
            auto_respond=True,
            fallback_model_key="glm-4-6",
            response_filter="Only coding questions.",
        ),
    )

    result = await service.set_override(
        GUILD,
        CHANNEL,
        "gpt-5-4",
        0,
        0,
        auto_respond=True,
        fallback_model_key="glm-4-6",
        response_filter="Only coding questions.",
    )

    mock_api_client.put.assert_awaited_once_with(
        PATH,
        json_data={
            "model_key": "gpt-5-4",
            "reasoning_level": None,
            "daily_token_budget": 0,
            "hourly_token_budget": 0,
            "auto_respond": True,
            "fallback_model_key": "glm-4-6",
            "response_filter": "Only coding questions.",
            "drafter_model": None,
        },
    )
    assert result.auto_respond is True
    assert result.fallback_model_key == "glm-4-6"
    assert result.response_filter == "Only coding questions."


async def test_get_override_parses_drafter_model(service, mock_api_client):
    mock_api_client.get.return_value = create_mock_response(
        200, _payload(drafter_model="glm-4-6")
    )

    result = await service.get_override(GUILD, CHANNEL)

    assert result.drafter_model == "glm-4-6"


async def test_get_override_defaults_drafter_model_when_absent(service, mock_api_client):
    # An older API response that omits drafter_model must degrade to None.
    legacy_payload = _payload()
    del legacy_payload["drafter_model"]
    mock_api_client.get.return_value = create_mock_response(200, legacy_payload)

    result = await service.get_override(GUILD, CHANNEL)

    assert result.drafter_model is None


async def test_set_override_sends_drafter_model(service, mock_api_client):
    mock_api_client.put.return_value = create_mock_response(
        200, _payload(drafter_model="glm-4-6")
    )

    result = await service.set_override(
        GUILD, CHANNEL, "gpt-5-4", 0, 0, drafter_model="glm-4-6"
    )

    _, put_kwargs = mock_api_client.put.call_args
    assert put_kwargs["json_data"]["drafter_model"] == "glm-4-6"
    assert result.drafter_model == "glm-4-6"


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


# --- Last-known fallback ---------------------------------------------------
# A DB/API outage must not change how a channel behaves. get_override_or_last_known
# reuses the previous answer rather than degrading to "no override", which would
# silently mute an auto-respond channel and drop its response filter.


@pytest.fixture
def expired_cache(monkeypatch):
    """Make the TTL cache expire immediately so every read hits the wire."""
    monkeypatch.setattr(model_override_service, "_OVERRIDE_CACHE_TTL", 0)


async def test_get_override_or_last_known_returns_the_live_value(
    service, mock_api_client
):
    mock_api_client.get.return_value = create_mock_response(200, _payload("glm-4-6"))

    result = await service.get_override_or_last_known(GUILD, CHANNEL)

    assert result.model_key == "glm-4-6"


async def test_get_override_or_last_known_reuses_last_value_on_api_error(
    service, mock_api_client, expired_cache
):
    mock_api_client.get.return_value = create_mock_response(
        200, _payload("glm-4-6", auto_respond=True)
    )
    await service.get_override_or_last_known(GUILD, CHANNEL)

    mock_api_client.get.side_effect = APIError("boom", status_code=500)
    result = await service.get_override_or_last_known(GUILD, CHANNEL)

    assert result.model_key == "glm-4-6"
    assert result.auto_respond is True


async def test_get_override_or_last_known_reuses_last_value_on_timeout(
    service, mock_api_client, expired_cache
):
    # A timeout carries no status code — the outage that prompted this fix.
    mock_api_client.get.return_value = create_mock_response(
        200, _payload(auto_respond=True)
    )
    await service.get_override_or_last_known(GUILD, CHANNEL)

    mock_api_client.get.side_effect = APIError("Request timeout after 10.0s")
    result = await service.get_override_or_last_known(GUILD, CHANNEL)

    assert result.auto_respond is True


async def test_get_override_or_last_known_remembers_the_no_override_answer(
    service, mock_api_client, expired_cache
):
    # "No override" is a real answer, not an absence — an outage must not turn it
    # into an error the caller has to guess about.
    mock_api_client.get.side_effect = APIError("not found", status_code=404)
    assert await service.get_override_or_last_known(GUILD, CHANNEL) is None

    mock_api_client.get.side_effect = APIError("boom", status_code=500)
    assert await service.get_override_or_last_known(GUILD, CHANNEL) is None


async def test_get_override_or_last_known_raises_without_a_prior_read(
    service, mock_api_client
):
    # Nothing to substitute (cold cache after a restart) — fail loudly and let the
    # caller keep its own fallback rather than invent an answer.
    mock_api_client.get.side_effect = APIError("boom", status_code=500)

    with pytest.raises(APIError):
        await service.get_override_or_last_known(GUILD, CHANNEL)


async def test_get_override_or_last_known_follows_a_write(
    service, mock_api_client, expired_cache
):
    mock_api_client.get.return_value = create_mock_response(200, _payload("kimi-k2"))
    await service.get_override_or_last_known(GUILD, CHANNEL)

    mock_api_client.put.return_value = create_mock_response(200, _payload("glm-4-6"))
    await service.set_override(GUILD, CHANNEL, "glm-4-6", 0, 0)

    mock_api_client.get.side_effect = APIError("boom", status_code=500)
    result = await service.get_override_or_last_known(GUILD, CHANNEL)

    assert result.model_key == "glm-4-6"


async def test_clear_override_forgets_the_last_known_value(
    service, mock_api_client, expired_cache
):
    # Otherwise an outage would resurrect an override an admin just deleted.
    mock_api_client.get.return_value = create_mock_response(200, _payload("kimi-k2"))
    await service.get_override_or_last_known(GUILD, CHANNEL)

    mock_api_client.delete.return_value = create_mock_response(204)
    await service.clear_override(GUILD, CHANNEL)

    mock_api_client.get.side_effect = APIError("boom", status_code=500)
    with pytest.raises(APIError):
        await service.get_override_or_last_known(GUILD, CHANNEL)


async def test_last_known_value_is_per_channel(service, mock_api_client, expired_cache):
    mock_api_client.get.return_value = create_mock_response(200, _payload("kimi-k2"))
    await service.get_override_or_last_known(GUILD, CHANNEL)

    mock_api_client.get.side_effect = APIError("boom", status_code=500)
    with pytest.raises(APIError):
        await service.get_override_or_last_known(GUILD, "other-channel")
