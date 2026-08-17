"""Wire-contract tests for the proactive-settings bot API."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

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
