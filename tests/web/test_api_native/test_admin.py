"""Parity tests for the native (Litestar) admin controller.

Exercises ``smarter_dev.web.api_native.admin`` against a real in-memory SQLite
database (help conversations + security logs live in the shared
``Base.metadata``), asserting the status codes and JSON bodies the FastAPI
``routers/admin.py`` produced. The legacy-table API key endpoints were removed
in the phase-05 decommission, so only the conversation routes remain. The
caller-identity seam (``resolve_request_api_key``) is patched — guard behavior
is covered by ``test_auth.py``-style guard tests at the bottom.

Behavior note: ``GET /api/admin/conversations/stats`` is asserted REACHABLE
here. On the FastAPI mount the route was shadowed by
``/conversations/{conversation_id}`` and always answered 422 (verified
empirically during the port) — the Litestar port resolves the ambiguity as
docs/v2/legacy-sunset/04-api-rewrite.md instructs.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock, Mock

import pytest
from litestar.di import Provide
from litestar.plugins.pydantic import PydanticPlugin
from litestar.testing import TestClient, create_test_client
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from smarter_dev.shared.database import Base
from smarter_dev.web.api_native import admin as admin_module
from smarter_dev.web.api_native.admin import AdminController

CALLER_KEY_NAME = "Test Admin Key"


@pytest.fixture
async def db_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def caller_key_mock(monkeypatch) -> Mock:
    """Stub the caller-identity lookup with a Skrift-key-shaped mock."""
    key = Mock()
    key.display_name = CALLER_KEY_NAME
    key.key_prefix = "sk_test12345"
    key.scoped_permission_list = ["bot-api", "bot-api-admin"]
    key.expires_at = None
    monkeypatch.setattr(
        admin_module, "resolve_request_api_key", AsyncMock(return_value=key)
    )
    return key


@pytest.fixture
def client(db_session, caller_key_mock) -> Iterator[TestClient]:
    """Litestar client serving the admin controller with guards bypassed.

    The routes share the ``admin.BOT_API_ADMIN_GUARDS`` list by reference, so
    emptying it before the app is built removes the guards for these tests
    only. Guard behavior is covered by the guard tests below.
    """
    original_guards = list(admin_module.BOT_API_ADMIN_GUARDS)
    admin_module.BOT_API_ADMIN_GUARDS.clear()
    try:
        with create_test_client(
            route_handlers=[AdminController],
            plugins=[PydanticPlugin()],
            dependencies={
                "db_session": Provide(lambda: db_session, sync_to_thread=False)
            },
        ) as test_client:
            yield test_client
    finally:
        admin_module.BOT_API_ADMIN_GUARDS[:] = original_guards


def _conversation_body(**over):
    body = {
        "session_id": "sess-1",
        "guild_id": "G1",
        "channel_id": "C1",
        "user_id": "U1",
        "user_username": "tester",
        "interaction_type": "mention",
        "user_question": "how do bytes work?",
        "bot_response": "very well, thanks",
        "tokens_used": 42,
    }
    body.update(over)
    return body


# --------------------------------------------------------------------------- #
# Help conversations
# --------------------------------------------------------------------------- #


def test_create_and_get_conversation(client):
    created = client.post("/api/admin/conversations", json=_conversation_body())
    assert created.status_code == 201
    body = created.json()
    assert body["message"] == "Conversation recorded successfully"
    conversation_id = body["id"]

    detail = client.get(f"/api/admin/conversations/{conversation_id}")
    assert detail.status_code == 200
    data = detail.json()
    assert data["id"] == conversation_id
    assert data["user_question"] == "how do bytes work?"
    assert data["bot_response"] == "very well, thanks"
    assert data["tokens_used"] == 42
    assert data["is_resolved"] is False


def test_get_conversation_404_and_malformed_id(client):
    missing = client.get(
        "/api/admin/conversations/00000000-0000-0000-0000-000000000000"
    )
    assert missing.status_code == 404
    assert missing.json() == {"detail": "Conversation not found"}

    malformed = client.get("/api/admin/conversations/not-a-uuid")
    assert malformed.status_code == 422


def test_create_conversation_missing_fields_is_422(client):
    resp = client.post("/api/admin/conversations", json={"guild_id": "G1"})
    assert resp.status_code == 422


def test_list_conversations_filters_by_guild(client):
    client.post("/api/admin/conversations", json=_conversation_body(guild_id="G1"))
    client.post("/api/admin/conversations", json=_conversation_body(guild_id="G2"))

    everything = client.get("/api/admin/conversations")
    assert everything.status_code == 200
    assert everything.json()["total"] == 2

    filtered = client.get("/api/admin/conversations", params={"guild_id": "G2"})
    assert filtered.json()["total"] == 1
    assert filtered.json()["items"][0]["guild_id"] == "G2"


def test_list_conversations_search(client):
    client.post(
        "/api/admin/conversations",
        json=_conversation_body(user_question="how do SQUADS work?"),
    )
    client.post("/api/admin/conversations", json=_conversation_body())

    found = client.get("/api/admin/conversations", params={"search": "squads"})
    assert found.json()["total"] == 1


def test_conversation_stats_is_reachable_and_counts(client):
    client.post("/api/admin/conversations", json=_conversation_body())
    client.post(
        "/api/admin/conversations",
        json=_conversation_body(interaction_type="slash_command", tokens_used=8),
    )

    resp = client.get("/api/admin/conversations/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_conversations"] == 2
    assert data["total_tokens_used"] == 50
    assert data["conversation_types"] == {"mention": 1, "slash_command": 1}
    assert data["resolution_rate"] == 0.0
    assert data["top_users"][0]["user_id"] == "U1"


def test_conversation_stats_rejects_bad_days(client):
    resp = client.get("/api/admin/conversations/stats", params={"days": 0})
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# Guard behavior (real guards, no bypass)
# --------------------------------------------------------------------------- #


@pytest.fixture
def guarded_client() -> Iterator[TestClient]:
    """Client serving the admin controller with its real auth guards."""
    with create_test_client(
        route_handlers=[AdminController],
        plugins=[PydanticPlugin()],
        dependencies={"db_session": Provide(lambda: Mock(), sync_to_thread=False)},
    ) as test_client:
        yield test_client


def test_admin_conversations_read_missing_authorization_header_rejected(
    guarded_client,
):
    assert guarded_client.get("/api/admin/conversations").status_code == 401


def test_admin_conversations_write_missing_authorization_header_rejected(
    guarded_client,
):
    response = guarded_client.post(
        "/api/admin/conversations", json=_conversation_body()
    )
    assert response.status_code == 401


def test_admin_conversations_non_sk_bearer_rejected(guarded_client):
    response = guarded_client.get(
        "/api/admin/conversations",
        headers={"Authorization": "Bearer not-a-skrift-key"},
    )
    assert response.status_code == 401
