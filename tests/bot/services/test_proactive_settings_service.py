"""Tests for ProactiveSettingsService — paths, defaults, cache, writes."""

from __future__ import annotations

import pytest

from smarter_dev.bot.services.exceptions import APIError
from smarter_dev.bot.services.proactive_settings_service import (
    ProactiveChannelSettings,
    ProactiveSettingsService,
)
from tests.bot.services.conftest import create_mock_response

GUILD = "111"
CHANNEL = "222"
PATH = f"/guilds/{GUILD}/channels/{CHANNEL}/proactive-settings"


def _payload(enabled: bool = True, watch_addendum: str = "") -> dict:
    return {
        "guild_id": GUILD,
        "channel_id": CHANNEL,
        "enabled": enabled,
        "watch_addendum": watch_addendum,
        "created_at": "2026-08-17T00:00:00+00:00",
        "updated_at": "2026-08-17T00:00:00+00:00",
    }


@pytest.fixture
async def service(mock_api_client, mock_cache_manager) -> ProactiveSettingsService:
    svc = ProactiveSettingsService(mock_api_client, mock_cache_manager)
    await svc.initialize()
    return svc


async def test_get_settings_hits_correct_path(service, mock_api_client):
    mock_api_client.get.return_value = create_mock_response(
        200, _payload(watch_addendum="watch for benchmarks")
    )
    settings = await service.get_settings(GUILD, CHANNEL)
    mock_api_client.get.assert_awaited_once_with(PATH)
    assert isinstance(settings, ProactiveChannelSettings)
    assert settings.enabled is True
    assert settings.watch_addendum == "watch for benchmarks"


async def test_get_settings_defaults_to_disabled_on_404(service, mock_api_client):
    mock_api_client.get.side_effect = APIError("not found", status_code=404)
    settings = await service.get_settings(GUILD, CHANNEL)
    assert settings.enabled is False
    assert settings.watch_addendum == ""


async def test_get_settings_propagates_non_404(service, mock_api_client):
    mock_api_client.get.side_effect = APIError("boom", status_code=500)
    with pytest.raises(APIError):
        await service.get_settings(GUILD, CHANNEL)


async def test_get_settings_caches_the_default(service, mock_api_client):
    mock_api_client.get.side_effect = APIError("not found", status_code=404)
    await service.get_settings(GUILD, CHANNEL)
    await service.get_settings(GUILD, CHANNEL)
    assert mock_api_client.get.await_count == 1


async def test_set_enabled_preserves_addendum_and_caches_result(
    service, mock_api_client
):
    mock_api_client.get.return_value = create_mock_response(
        200, _payload(enabled=False, watch_addendum="keep me")
    )
    mock_api_client.put.return_value = create_mock_response(
        200, _payload(enabled=True, watch_addendum="keep me")
    )

    settings = await service.set_enabled(GUILD, CHANNEL, True)

    mock_api_client.put.assert_awaited_once_with(
        PATH, json_data={"enabled": True, "watch_addendum": "keep me"}
    )
    assert settings.enabled is True
    # The write cached the fresh row: the next read stays off the wire.
    again = await service.get_settings(GUILD, CHANNEL)
    assert again.enabled is True
    assert mock_api_client.get.await_count == 1


async def test_set_watch_addendum_preserves_switch(service, mock_api_client):
    mock_api_client.get.return_value = create_mock_response(
        200, _payload(enabled=True, watch_addendum="old")
    )
    mock_api_client.put.return_value = create_mock_response(
        200, _payload(enabled=True, watch_addendum="new criteria")
    )

    settings = await service.set_watch_addendum(GUILD, CHANNEL, "new criteria")

    mock_api_client.put.assert_awaited_once_with(
        PATH, json_data={"enabled": True, "watch_addendum": "new criteria"}
    )
    assert settings.watch_addendum == "new criteria"
