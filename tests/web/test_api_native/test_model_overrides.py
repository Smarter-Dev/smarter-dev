"""Parity tests for the native (Litestar) model-override API (unit U8).

Assert the wire contract of the ported ``routers/model_overrides.py`` directly:
exact status codes and JSON for the GET/PUT/DELETE happy paths, the nested
``{"detail": {ErrorResponse}}`` 404 the FastAPI ``create_not_found_error``
produced, the 422 on request-body validation (unknown model key, unknown
reasoning level, negative / over-int32 budgets), and the idempotent 204 delete.
Paths carry the final ``/api`` prefix and mirror exactly what the bot sends from
``smarter_dev/bot/services/model_override_service.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from litestar.testing import TestClient

_GUILD = "123456789012345678"
_CHANNEL = "555000111222333444"


def _url(guild_id: str = _GUILD, channel_id: str = _CHANNEL) -> str:
    return f"/api/guilds/{guild_id}/channels/{channel_id}/model-override"


def _record(**overrides) -> SimpleNamespace:
    """Build a ChannelModelOverride-like row for response serialization."""
    fields = {
        "guild_id": _GUILD,
        "channel_id": _CHANNEL,
        "model_key": "glm-5-2",
        "reasoning_level": None,
        "daily_token_budget": 5000,
        "hourly_token_budget": 500,
        "auto_respond": False,
        "fallback_model_key": None,
        "response_filter": None,
        "created_at": datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc),
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


class TestGetModelOverride:
    def test_returns_override(self, model_override_client: TestClient, model_override_crud_mock):
        model_override_crud_mock.get.return_value = _record()

        response = model_override_client.get(_url())

        assert response.status_code == 200
        body = response.json()
        assert body["guild_id"] == _GUILD
        assert body["channel_id"] == _CHANNEL
        assert body["model_key"] == "glm-5-2"
        assert body["reasoning_level"] is None
        assert body["daily_token_budget"] == 5000
        assert body["hourly_token_budget"] == 500
        assert body["auto_respond"] is False
        assert body["fallback_model_key"] is None
        assert body["response_filter"] is None
        assert body["created_at"] == "2026-01-01T12:00:00+00:00"
        assert body["updated_at"] == "2026-01-02T12:00:00+00:00"

    def test_missing_returns_nested_404(
        self, model_override_client: TestClient, model_override_crud_mock
    ):
        model_override_crud_mock.get.return_value = None

        response = model_override_client.get(_url())

        assert response.status_code == 404
        body = response.json()
        # Nested {"detail": {ErrorResponse}} shape with request_id=None, matching
        # the FastAPI ``create_not_found_error("Model override", channel_id)``.
        assert body["detail"]["detail"] == (
            f"Model override with identifier '{_CHANNEL}' not found"
        )
        assert body["detail"]["type"] == "not_found_error"
        assert body["detail"]["errors"] is None
        assert body["detail"]["request_id"] is None
        assert "timestamp" in body["detail"]


class TestPutModelOverride:
    def test_upsert_returns_row_and_commits(
        self, model_override_client: TestClient, model_override_crud_mock, session_mock
    ):
        model_override_crud_mock.upsert.return_value = _record(
            model_key="kimi-k2-6", daily_token_budget=42
        )

        response = model_override_client.put(
            _url(),
            json={"model_key": "kimi-k2-6", "daily_token_budget": 42},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["model_key"] == "kimi-k2-6"
        assert body["daily_token_budget"] == 42
        session_mock.commit.assert_awaited_once()
        # Budgets default to 0 (unlimited) when omitted.
        _, kwargs = model_override_crud_mock.upsert.call_args
        assert kwargs["hourly_token_budget"] == 0
        assert kwargs["reasoning_level"] is None
        # New per-channel settings default off when omitted (old-style write).
        assert kwargs["auto_respond"] is False
        assert kwargs["fallback_model_key"] is None
        assert kwargs["response_filter"] is None

    def test_upsert_accepts_null_model_key_for_server_default(
        self, model_override_client: TestClient, model_override_crud_mock, session_mock
    ):
        """model_key null = keep the server default model; budgets and
        behaviour flags still persist."""
        model_override_crud_mock.upsert.return_value = _record(
            model_key=None, daily_token_budget=1000, auto_respond=True
        )

        response = model_override_client.put(
            _url(),
            json={
                "model_key": None,
                "daily_token_budget": 1000,
                "auto_respond": True,
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["model_key"] is None
        assert body["daily_token_budget"] == 1000
        assert body["auto_respond"] is True
        _, kwargs = model_override_crud_mock.upsert.call_args
        assert kwargs["model_key"] is None
        session_mock.commit.assert_awaited_once()

    def test_new_settings_passed_through(
        self, model_override_client: TestClient, model_override_crud_mock
    ):
        model_override_crud_mock.upsert.return_value = _record(
            auto_respond=True,
            fallback_model_key="kimi-k2-6",
            response_filter="Only coding questions.",
        )

        response = model_override_client.put(
            _url(),
            json={
                "model_key": "glm-5-2",
                "auto_respond": True,
                "fallback_model_key": "kimi-k2-6",
                "response_filter": "Only coding questions.",
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["auto_respond"] is True
        assert body["fallback_model_key"] == "kimi-k2-6"
        assert body["response_filter"] == "Only coding questions."
        _, kwargs = model_override_crud_mock.upsert.call_args
        assert kwargs["auto_respond"] is True
        assert kwargs["fallback_model_key"] == "kimi-k2-6"
        assert kwargs["response_filter"] == "Only coding questions."

    def test_unknown_fallback_model_key_is_422(self, model_override_client: TestClient):
        response = model_override_client.put(
            _url(),
            json={"model_key": "glm-5-2", "fallback_model_key": "not-a-real-model"},
        )
        assert response.status_code == 422

    def test_over_length_response_filter_is_422(self, model_override_client: TestClient):
        response = model_override_client.put(
            _url(),
            json={"model_key": "glm-5-2", "response_filter": "x" * 4001},
        )
        assert response.status_code == 422

    def test_reasoning_level_passed_through(
        self, model_override_client: TestClient, model_override_crud_mock
    ):
        model_override_crud_mock.upsert.return_value = _record(reasoning_level="high")

        response = model_override_client.put(
            _url(), json={"model_key": "glm-5-2", "reasoning_level": "high"}
        )

        assert response.status_code == 200
        assert response.json()["reasoning_level"] == "high"

    def test_unknown_model_key_is_422(self, model_override_client: TestClient):
        response = model_override_client.put(
            _url(), json={"model_key": "not-a-real-model"}
        )
        assert response.status_code == 422

    def test_unknown_reasoning_level_is_422(self, model_override_client: TestClient):
        response = model_override_client.put(
            _url(), json={"model_key": "glm-5-2", "reasoning_level": "ludicrous"}
        )
        assert response.status_code == 422

    def test_negative_budget_is_422(self, model_override_client: TestClient):
        response = model_override_client.put(
            _url(), json={"model_key": "kimi-k2-6", "daily_token_budget": -1}
        )
        assert response.status_code == 422

    def test_over_int32_budget_is_422(self, model_override_client: TestClient):
        response = model_override_client.put(
            _url(),
            json={"model_key": "kimi-k2-6", "daily_token_budget": 3_000_000_000},
        )
        assert response.status_code == 422

    def test_int32_max_budget_accepted(
        self, model_override_client: TestClient, model_override_crud_mock
    ):
        model_override_crud_mock.upsert.return_value = _record(
            daily_token_budget=2_147_483_647, hourly_token_budget=2_147_483_647
        )

        response = model_override_client.put(
            _url(),
            json={
                "model_key": "kimi-k2-6",
                "daily_token_budget": 2_147_483_647,
                "hourly_token_budget": 2_147_483_647,
            },
        )

        assert response.status_code == 200
        assert response.json()["daily_token_budget"] == 2_147_483_647


class TestDeleteModelOverride:
    def test_delete_returns_204_and_commits(
        self, model_override_client: TestClient, model_override_crud_mock, session_mock
    ):
        model_override_crud_mock.delete.return_value = True

        response = model_override_client.delete(_url())

        assert response.status_code == 204
        assert response.content == b""
        session_mock.commit.assert_awaited_once()

    def test_delete_is_idempotent(
        self, model_override_client: TestClient, model_override_crud_mock
    ):
        # No row removed still returns 204 (idempotent), like the legacy router.
        model_override_crud_mock.delete.return_value = False

        response = model_override_client.delete(_url())

        assert response.status_code == 204
