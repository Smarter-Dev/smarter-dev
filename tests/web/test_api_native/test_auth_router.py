"""Parity tests for the native (Litestar) auth controller + health probes.

Port of the FastAPI suite ``tests/web/test_api/test_auth.py`` response-shape
assertions against ``smarter_dev.web.api_native.auth`` (unit U1). The key
lookup seam (``resolve_request_api_key``) is patched with a Skrift-key-shaped
mock; guard rejection paths use the real guards.

Intentional status changes vs. the FastAPI mount (see the module docstring and
docs/v2/legacy-sunset/04-api-rewrite.md "401-parity"): missing/non-Bearer
Authorization headers answer 401 from the Skrift ``auth_guard`` where
``HTTPBearer(auto_error=True)`` answered 403.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest
from litestar.di import Provide
from litestar.plugins.pydantic import PydanticPlugin
from litestar.testing import TestClient, create_test_client

from smarter_dev.web.api_native import auth as auth_module
from smarter_dev.web.api_native.auth import ApiHealthController, AuthController

KEY_NAME = "Bot Service Key"
KEY_PREFIX = "sk_abcd1234"


@pytest.fixture
def resolved_key_mock(monkeypatch) -> Mock:
    """Stub the key lookup with a Skrift-key-shaped mock (no expiry)."""
    key = Mock()
    key.display_name = KEY_NAME
    key.key_prefix = KEY_PREFIX
    key.scoped_permission_list = ["bot-api", "bot-api-admin"]
    key.expires_at = None
    monkeypatch.setattr(
        auth_module, "resolve_request_api_key", AsyncMock(return_value=key)
    )
    return key


@pytest.fixture
def settings_mock(monkeypatch) -> Mock:
    """Pin the settings the auth endpoints echo."""
    settings = Mock()
    settings.discord_bot_token = "configured-token"
    settings.environment = "testing"
    monkeypatch.setattr(auth_module, "get_settings", lambda: settings)
    return settings


@pytest.fixture
def client(resolved_key_mock, settings_mock) -> Iterator[TestClient]:
    """Client serving both auth-unit controllers with guards bypassed.

    The guarded routes share the ``auth.BOT_API_GUARDS`` list by reference, so
    emptying it before the app is built removes the guards for these tests
    only. Guard behavior is covered by the guard tests below.
    """
    original_guards = list(auth_module.BOT_API_GUARDS)
    auth_module.BOT_API_GUARDS.clear()
    try:
        with create_test_client(
            route_handlers=[ApiHealthController, AuthController],
            plugins=[PydanticPlugin()],
        ) as test_client:
            yield test_client
    finally:
        auth_module.BOT_API_GUARDS[:] = original_guards


def test_api_health_is_open_and_static(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "healthy", "version": "1.0.0"}


def test_auth_health_healthy_when_token_configured(client):
    resp = client.get("/api/auth/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert data["version"] == "1.0.0"
    assert isinstance(data["timestamp"], str)
    assert data["database"] is True
    assert data["redis"] is True


def test_auth_health_degraded_without_token(client, settings_mock):
    settings_mock.discord_bot_token = ""
    resp = client.get("/api/auth/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "degraded"


def test_validate_token_success(client):
    resp = client.post("/api/auth/validate")
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is True
    assert data["expires_at"] is None


def test_validate_token_echoes_expiry(client, resolved_key_mock):
    expiry = datetime(2027, 1, 1, tzinfo=timezone.utc)
    resolved_key_mock.expires_at = expiry
    resp = client.post("/api/auth/validate")
    assert resp.status_code == 200
    assert resp.json()["expires_at"] == expiry.isoformat()


def test_auth_status_success(client):
    resp = client.get("/api/auth/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["authenticated"] is True
    assert data["key_name"] == KEY_NAME
    assert data["key_prefix"] == KEY_PREFIX
    assert data["scopes"] == ["bot-api", "bot-api-admin"]
    assert data["usage_count"] == 0
    assert data["rate_limit"] == 10000
    assert data["expires_at"] is None
    assert data["environment"] == "testing"
    assert data["api_version"] == "1.0.0"
    assert "timestamp" in data


def test_revocation_race_answers_plain_401(client, monkeypatch):
    monkeypatch.setattr(
        auth_module, "resolve_request_api_key", AsyncMock(
            side_effect=auth_module.BotApiException(
                401, {"detail": "Authentication failed"}
            )
        ),
    )
    resp = client.get("/api/auth/status")
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Authentication failed"}


# --------------------------------------------------------------------------- #
# Guard behavior (real guards, no bypass)
# --------------------------------------------------------------------------- #


@pytest.fixture
def guarded_client(settings_mock) -> Iterator[TestClient]:
    """Client serving the auth controllers with their real auth guards."""
    with create_test_client(
        route_handlers=[ApiHealthController, AuthController],
        plugins=[PydanticPlugin()],
    ) as test_client:
        yield test_client


def test_validate_missing_authorization_header_rejected(guarded_client):
    assert guarded_client.post("/api/auth/validate").status_code == 401


def test_status_missing_authorization_header_rejected(guarded_client):
    assert guarded_client.get("/api/auth/status").status_code == 401


def test_status_non_sk_bearer_rejected(guarded_client):
    response = guarded_client.get(
        "/api/auth/status",
        headers={"Authorization": "Bearer sk-legacy-format-key-000000000000000000000"},
    )
    assert response.status_code == 401


def test_health_endpoints_stay_open_with_real_guards(guarded_client):
    assert guarded_client.get("/api/health").status_code == 200
    assert guarded_client.get("/api/auth/health").status_code == 200
