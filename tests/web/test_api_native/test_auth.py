"""Auth-guard parity tests for the native bytes controller.

Unlike the endpoint tests these use the REAL controller guards
(``[auth_guard, APIKeyOnly(), Permission("bot-api")]``). They cover the
credential-shape failures that short-circuit before any database lookup:
missing bearer and a non-``sk_`` bearer both reject with 401.

NOTE — intentional status change vs. the FastAPI mount: the legacy
``HTTPBearer(auto_error=True)`` returned **403** for a missing ``Authorization``
header, whereas the Skrift ``auth_guard`` raises ``NotAuthorizedException`` →
**401**. The harness ``auth-missing-key-401`` check accepts ``(401, 403)`` and
``auth-malformed-key-401`` accepts ``(401,)``; see
docs/v2/legacy-sunset/04-api-rewrite.md ("401-parity"). Unknown *well-formed*
``sk_`` keys are rejected via the DB path and are covered by the harness
(``auth-unknown-skrift-key-401``), not here.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import Mock

import pytest
from litestar.di import Provide
from litestar.plugins.pydantic import PydanticPlugin
from litestar.testing import TestClient, create_test_client

from smarter_dev.web.api_native.bytes import BytesController

_GUILD = "123456789012345678"


@pytest.fixture
def guarded_client() -> Iterator[TestClient]:
    """Client serving the bytes controller with its real auth guards."""
    with create_test_client(
        route_handlers=[BytesController],
        plugins=[PydanticPlugin()],
        dependencies={"db_session": Provide(lambda: Mock(), sync_to_thread=False)},
    ) as client:
        yield client


def test_missing_authorization_header_rejected(guarded_client: TestClient):
    response = guarded_client.get(f"/api/guilds/{_GUILD}/bytes/config")
    assert response.status_code == 401


def test_non_sk_bearer_rejected(guarded_client: TestClient):
    response = guarded_client.get(
        f"/api/guilds/{_GUILD}/bytes/config",
        headers={"Authorization": "Bearer not-a-skrift-key"},
    )
    assert response.status_code == 401


def test_session_cookie_does_not_authenticate_api(guarded_client: TestClient):
    # APIKeyOnly means a session identity must never satisfy the guard.
    response = guarded_client.get(
        f"/api/guilds/{_GUILD}/bytes/config",
        headers={"Authorization": "Basic dXNlcjpwYXNz"},
    )
    assert response.status_code == 401
