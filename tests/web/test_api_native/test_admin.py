"""Parity tests for the native (Litestar) admin controller.

Exercises ``smarter_dev.web.api_native.admin`` against a real in-memory SQLite
database (legacy key table + help conversations + security logs all live in the
shared ``Base.metadata``), asserting the status codes and JSON bodies the
FastAPI ``routers/admin.py`` produced. The caller-identity seam
(``resolve_request_api_key``) is patched — guard behavior is covered by
``test_auth.py``-style guard tests at the bottom.

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


def _key_body(**over):
    body = {
        "name": "Bot Production Key",
        "description": "Key for the production bot",
        "scopes": ["bot:read", "bot:write"],
        "rate_limit_per_hour": 5000,
    }
    body.update(over)
    return body


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
# API key CRUD (legacy key table)
# --------------------------------------------------------------------------- #


def test_create_api_key_returns_full_key_once(client):
    resp = client.post("/api/admin/api-keys", json=_key_body())
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Bot Production Key"
    assert data["scopes"] == ["bot:read", "bot:write"]
    assert data["api_key"].startswith("sk-")
    assert data["key_prefix"] == data["api_key"][:12]
    assert data["is_active"] is True
    assert data["usage_count"] == 0
    assert data["created_by"] == CALLER_KEY_NAME


def test_create_api_key_rejects_unknown_scope(client):
    resp = client.post(
        "/api/admin/api-keys", json=_key_body(scopes=["galaxy:conquer"])
    )
    assert resp.status_code == 422


def test_list_api_keys_paginates(client):
    for n in range(3):
        client.post("/api/admin/api-keys", json=_key_body(name=f"Key {n}"))
    resp = client.get("/api/admin/api-keys", params={"page": 1, "size": 2})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 3
    assert data["page"] == 1
    assert data["size"] == 2
    assert data["pages"] == 2
    assert len(data["items"]) == 2
    assert all("api_key" not in item for item in data["items"])


def test_get_api_key_detail_and_404(client):
    created = client.post("/api/admin/api-keys", json=_key_body())
    key_id = created.json()["id"]

    detail = client.get(f"/api/admin/api-keys/{key_id}")
    assert detail.status_code == 200
    assert detail.json()["id"] == key_id

    missing = client.get("/api/admin/api-keys/00000000-0000-0000-0000-000000000000")
    assert missing.status_code == 404
    assert missing.json() == {"detail": "API key not found"}


def test_get_api_key_malformed_id_is_422(client):
    resp = client.get("/api/admin/api-keys/not-a-uuid")
    assert resp.status_code == 422


def test_put_and_patch_update_api_key(client):
    created = client.post("/api/admin/api-keys", json=_key_body())
    key_id = created.json()["id"]

    updated = client.put(
        f"/api/admin/api-keys/{key_id}", json={"name": "Renamed Key"}
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "Renamed Key"

    patched = client.patch(
        f"/api/admin/api-keys/{key_id}", json={"rate_limit_per_hour": 900}
    )
    assert patched.status_code == 200
    assert patched.json()["rate_limit_per_hour"] == 900
    assert patched.json()["name"] == "Renamed Key"  # untouched by the patch


def test_revoke_api_key_then_conflict(client):
    created = client.post("/api/admin/api-keys", json=_key_body())
    key_id = created.json()["id"]

    revoked = client.delete(f"/api/admin/api-keys/{key_id}")
    assert revoked.status_code == 200
    body = revoked.json()
    assert body["message"] == "API key revoked successfully"
    assert body["key_id"] == key_id

    again = client.delete(f"/api/admin/api-keys/{key_id}")
    assert again.status_code == 409
    assert again.json() == {"detail": "API key is already revoked"}

    missing = client.delete("/api/admin/api-keys/00000000-0000-0000-0000-000000000000")
    assert missing.status_code == 404


# --------------------------------------------------------------------------- #
# Stats
# --------------------------------------------------------------------------- #


def test_admin_stats_counts_keys(client):
    client.post("/api/admin/api-keys", json=_key_body(name="Active Key"))
    created = client.post("/api/admin/api-keys", json=_key_body(name="Doomed Key"))
    client.delete(f"/api/admin/api-keys/{created.json()['id']}")

    resp = client.get("/api/admin/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_api_keys"] == 2
    assert data["active_api_keys"] == 1
    assert data["revoked_api_keys"] == 1
    assert isinstance(data["top_api_consumers"], list)


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


def test_admin_stats_missing_authorization_header_rejected(guarded_client):
    assert guarded_client.get("/api/admin/stats").status_code == 401


def test_admin_conversations_write_missing_authorization_header_rejected(
    guarded_client,
):
    response = guarded_client.post(
        "/api/admin/conversations", json=_conversation_body()
    )
    assert response.status_code == 401


def test_admin_api_keys_non_sk_bearer_rejected(guarded_client):
    response = guarded_client.get(
        "/api/admin/api-keys",
        headers={"Authorization": "Bearer not-a-skrift-key"},
    )
    assert response.status_code == 401
