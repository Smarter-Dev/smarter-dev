"""Wire-contract tests for the proactive-settings bot API."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

from litestar.testing import TestClient

_GUILD = "123456789012345678"
_CHANNEL = "555000111222333444"


def _url(guild_id: str = _GUILD, channel_id: str = _CHANNEL) -> str:
    return f"/api/guilds/{guild_id}/channels/{channel_id}/proactive-settings"


def _record(**overrides) -> SimpleNamespace:
    fields = {
        "guild_id": _GUILD,
        "channel_id": _CHANNEL,
        "enabled": True,
        "watch_addendum": "wake me when alice posts benchmarks",
        "created_at": datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 8, 17, 13, 0, tzinfo=timezone.utc),
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


class TestGetProactiveSettings:
    def test_returns_settings(
        self, proactive_settings_client: TestClient, proactive_settings_crud_mock
    ):
        proactive_settings_crud_mock.get.return_value = _record()

        response = proactive_settings_client.get(_url())

        assert response.status_code == 200
        body = response.json()
        assert body["enabled"] is True
        assert body["watch_addendum"] == "wake me when alice posts benchmarks"
        assert body["guild_id"] == _GUILD

    def test_unconfigured_channel_is_404(
        self, proactive_settings_client: TestClient, proactive_settings_crud_mock
    ):
        proactive_settings_crud_mock.get.return_value = None

        response = proactive_settings_client.get(_url())

        assert response.status_code == 404
        assert response.json()["detail"]["detail"].startswith(
            "Proactive settings"
        )


class TestPutProactiveSettings:
    def test_upserts_and_returns_row(
        self, proactive_settings_client: TestClient, proactive_settings_crud_mock
    ):
        proactive_settings_crud_mock.upsert.return_value = _record(enabled=False)

        response = proactive_settings_client.put(
            _url(), json={"enabled": False, "watch_addendum": ""}
        )

        assert response.status_code == 200
        assert response.json()["enabled"] is False
        kwargs = proactive_settings_crud_mock.upsert.await_args.kwargs
        assert kwargs["enabled"] is False
        assert kwargs["watch_addendum"] == ""
        assert kwargs["guild_id"] == _GUILD
        assert kwargs["channel_id"] == _CHANNEL

    def test_missing_enabled_is_422(
        self, proactive_settings_client: TestClient, proactive_settings_crud_mock
    ):
        response = proactive_settings_client.put(
            _url(), json={"watch_addendum": "x"}
        )
        assert response.status_code == 400 or response.status_code == 422

    def test_oversized_addendum_is_rejected(
        self, proactive_settings_client: TestClient, proactive_settings_crud_mock
    ):
        response = proactive_settings_client.put(
            _url(), json={"enabled": True, "watch_addendum": "x" * 4001}
        )
        assert response.status_code in (400, 422)


def _usage_payload(**overrides) -> dict:
    payload = {
        "wake_id": "abc123",
        "metered_at": "2026-08-26T01:00:00Z",
        "passive": False,
        "responses": 1,
        "entries": [
            {
                "model_id": "deepseek/deepseek-v4-flash",
                "operation": "watcher",
                "input_tokens": 1_000_000,
                "output_tokens": 0,
                "cache_read_tokens": 0,
            },
            {
                "model_id": "gemini-3.7-flash",
                "operation": "agent",
                "input_tokens": 0,
                "output_tokens": 1_000_000,
                "cache_read_tokens": 0,
            },
        ],
    }
    payload.update(overrides)
    return payload


class TestPostProactiveWakeUsage:
    def test_records_priced_rows_per_model(
        self, proactive_settings_client: TestClient, session_mock: AsyncMock
    ):
        session_mock.scalar.return_value = None

        response = proactive_settings_client.post(
            _url() + "/usage", json=_usage_payload()
        )

        assert response.status_code == 200
        assert response.json()["recorded"] == 2
        rows = [call.args[0] for call in session_mock.add.call_args_list]
        by_operation = {row.operation_type: row for row in rows}

        watcher = by_operation["proactive-watcher"]
        assert watcher.operation_key == (
            "proactive:abc123:watcher:deepseek/deepseek-v4-flash"
        )
        assert watcher.product_mode == "discord"
        assert watcher.provider_key == "openrouter"
        assert watcher.catalog_model_key == "deepseek-v4"
        assert watcher.model_id == "deepseek/deepseek-v4-flash"
        assert watcher.input_tokens == 1_000_000
        # 1M input tokens at OpenRouter's DeepSeek V4 Flash rate.
        assert watcher.cost_usd == Decimal("0.0867")
        assert watcher.details == {
            "guild_id": _GUILD,
            "channel_id": _CHANNEL,
            "passive": False,
            "responses": 1,
        }
        assert watcher.metered_at == datetime(
            2026, 8, 26, 1, 0, tzinfo=timezone.utc
        )

        agent = by_operation["proactive-agent"]
        assert agent.provider_key == "google"
        assert agent.catalog_model_key == "gemini-3-7-flash"
        assert agent.output_tokens == 1_000_000
        # 1M output tokens at Gemini 3.7 Flash's rate.
        assert agent.cost_usd == Decimal("3.75")

        session_mock.commit.assert_awaited_once()

    def test_replayed_wake_records_nothing(
        self, proactive_settings_client: TestClient, session_mock: AsyncMock
    ):
        session_mock.scalar.return_value = SimpleNamespace()

        response = proactive_settings_client.post(
            _url() + "/usage", json=_usage_payload()
        )

        assert response.status_code == 200
        assert response.json()["recorded"] == 0
        session_mock.add.assert_not_called()

    def test_unknown_operation_is_rejected(
        self, proactive_settings_client: TestClient, session_mock: AsyncMock
    ):
        payload = _usage_payload()
        payload["entries"][0]["operation"] = "skim"

        response = proactive_settings_client.post(_url() + "/usage", json=payload)

        assert response.status_code in (400, 422)
        session_mock.add.assert_not_called()

    def test_negative_tokens_are_rejected(
        self, proactive_settings_client: TestClient, session_mock: AsyncMock
    ):
        payload = _usage_payload()
        payload["entries"][0]["input_tokens"] = -5

        response = proactive_settings_client.post(_url() + "/usage", json=payload)

        assert response.status_code in (400, 422)
        session_mock.add.assert_not_called()
