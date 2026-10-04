"""Tests for the failed-auth security-log hookup in ``bot_api_auth_guard``.

Parity with the legacy ``verify_api_key``: every rejected bot-API request
recorded a failed-authentication security event (a ``login_failed`` log
since #81)
(docs/v2/legacy-sunset/04-api-rewrite.md, "Cross-cutting deletions").
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch

import pytest
from litestar.di import Provide
from litestar.plugins.pydantic import PydanticPlugin
from litestar.testing import TestClient
from litestar.testing import create_test_client
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.api_native.bytes import BytesController

CONFIG_PATH = "/api/guilds/123456789012345678/bytes/config"


@pytest.fixture
def security_logger_mock() -> Iterator[Mock]:
    """Replace the global security logger for the request lifecycle."""
    logger_mock = Mock()
    logger_mock.log_authentication_failed = AsyncMock(return_value=None)
    with patch(
        "smarter_dev.web.security_logger.get_security_logger",
        return_value=logger_mock,
    ):
        yield logger_mock


@pytest.fixture
def guarded_client(security_logger_mock: Mock) -> Iterator[TestClient]:
    """Bytes controller with its REAL guards (API-key-only auth)."""
    session_mock = AsyncMock(spec=AsyncSession)
    with create_test_client(
        route_handlers=[BytesController],
        plugins=[PydanticPlugin()],
        dependencies={
            "db_session": Provide(lambda: session_mock, sync_to_thread=False)
        },
    ) as client:
        yield client


class TestAuthenticationFailureLogging:
    async def test_missing_key_401_and_logged(
        self, guarded_client: TestClient, security_logger_mock: Mock
    ):
        response = guarded_client.get(CONFIG_PATH)

        assert response.status_code == 401
        security_logger_mock.log_authentication_failed.assert_awaited_once()
        call_kwargs = security_logger_mock.log_authentication_failed.await_args.kwargs
        assert call_kwargs["bearer_presented"] is False
        assert call_kwargs["reason"] == "no_valid_key"

    async def test_rejected_key_401_and_logged_without_any_of_it(
        self, guarded_client: TestClient, security_logger_mock: Mock
    ):
        legacy_token = "sk-" + "a" * 43

        response = guarded_client.get(
            CONFIG_PATH, headers={"Authorization": f"Bearer {legacy_token}"}
        )

        assert response.status_code == 401
        security_logger_mock.log_authentication_failed.assert_awaited_once()
        call_kwargs = security_logger_mock.log_authentication_failed.await_args.kwargs
        assert call_kwargs["bearer_presented"] is True
        assert "sk-" not in repr(call_kwargs)

    async def test_logging_failure_never_masks_the_401(
        self, guarded_client: TestClient, security_logger_mock: Mock
    ):
        security_logger_mock.log_authentication_failed.side_effect = RuntimeError(
            "log store down"
        )

        response = guarded_client.get(CONFIG_PATH)

        assert response.status_code == 401


class TestReasonCodes:
    """Skrift's wording contains "auth", which Logfire's scrubber redacts."""

    @pytest.mark.parametrize(
        ("detail", "code"),
        [
            ("Authentication required", "no_valid_key"),
            ("Insufficient permissions", "insufficient_permissions"),
        ],
    )
    def test_skrift_detail_maps_to_a_code(self, detail, code):
        from litestar.exceptions import NotAuthorizedException

        from smarter_dev.web.api_native.auth import _login_failure_reason

        assert _login_failure_reason(NotAuthorizedException(detail)) == code

    def test_codes_cover_every_rejection_the_installed_skrift_raises(self):
        """Pinned to Skrift's wording: a rewording must fail here, not turn
        every permission failure into ``no_valid_key`` unnoticed."""
        import inspect
        import re

        import skrift.auth.guards as skrift_guards
        from litestar.exceptions import NotAuthorizedException

        from smarter_dev.web.api_native.auth import _login_failure_reason

        raised = re.findall(
            r'NotAuthorizedException\(\s*"([^"]*)"', inspect.getsource(skrift_guards)
        )
        assert sorted(set(raised)) == ["Authentication required", "Insufficient permissions"]
        assert {
            detail: _login_failure_reason(NotAuthorizedException(detail))
            for detail in raised
        } == {
            "Authentication required": "no_valid_key",
            "Insufficient permissions": "insufficient_permissions",
        }
