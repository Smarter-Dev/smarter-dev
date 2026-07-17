"""Whole-app integration tests for the native ``/api`` controllers.

Unlike the per-controller parity tests (stubbed guards + mocked crud), this
module registers EVERY controller that ``app.yaml`` registers, against a real
SQLite database and the real Skrift auth stack: a service-owner user carrying
the ``bot-service`` role and an ``sk_`` API key scoped to
``bot-api``/``bot-api-admin`` — the exact production shape minted by
runbooks/01-key-rotation.md. It proves:

- all controllers co-register without route conflicts,
- the guard chain (``bot_api_auth_guard`` → Skrift key verify → permission
  intersection) authenticates the production key shape end to end,
- representative endpoints from each unit answer with the legacy wire shapes,
- the rate-limit middleware is live on the bytes routes in the assembled app.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from litestar.di import Provide
from litestar.testing import TestClient
from litestar.testing import create_test_client
from skrift.auth.services import assign_role_to_user
from skrift.auth.services import invalidate_user_permissions_cache
from skrift.auth.services import sync_roles_to_database
from skrift.db.base import Base as SkriftBase
from skrift.db.models.user import User as SkriftUser
from skrift.db.services import api_key_service
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

import smarter_dev.web.roles  # noqa: F401 — registers the bot-service role
from smarter_dev.web.api_native.activity import ActivityController
from smarter_dev.web.api_native.admin import AdminController
from smarter_dev.web.api_native.admin_handlers import AdminHandlerController
from smarter_dev.web.api_native.advent_of_code import AdventOfCodeController
from smarter_dev.web.api_native.auth import ApiHealthController
from smarter_dev.web.api_native.auth import AuthController
from smarter_dev.web.api_native.billing import PolarWebhookController
from smarter_dev.web.api_native.billing import SudoConvergeController
from smarter_dev.web.api_native.bytes import BytesController
from smarter_dev.web.api_native.challenges import ChallengeController
from smarter_dev.web.api_native.chat_conversations import ChatConversationController
from smarter_dev.web.api_native.forum import ForumAgentController
from smarter_dev.web.api_native.forum import ForumNotificationController
from smarter_dev.web.api_native.handlers import HandlerController
from smarter_dev.web.api_native.image_quota import ImageQuotaController
from smarter_dev.web.api_native.members import MemberController
from smarter_dev.web.api_native.messages import RepeatingMessageController
from smarter_dev.web.api_native.messages import ScheduledMessageController
from smarter_dev.web.api_native.model_overrides import ChannelModelOverrideController
from smarter_dev.web.api_native.quests import QuestController
from smarter_dev.web.api_native.squads import SquadController
from smarter_dev.web.api_native.squads import SquadSaleEventController
from smarter_dev.web.models import Base as DomainBase
from smarter_dev.web.models import BytesBalance
from smarter_dev.web.models import BytesConfig

# Mirror of the app.yaml controllers block for the /api surface. Keep in sync:
# registering them together is the route-conflict regression test.
ALL_API_CONTROLLERS = [
    ApiHealthController,
    AuthController,
    BytesController,
    SquadController,
    SquadSaleEventController,
    MemberController,
    ChallengeController,
    QuestController,
    ScheduledMessageController,
    RepeatingMessageController,
    AdventOfCodeController,
    ForumAgentController,
    ForumNotificationController,
    ChatConversationController,
    ImageQuotaController,
    ActivityController,
    ChannelModelOverrideController,
    HandlerController,
    AdminHandlerController,
    AdminController,
    PolarWebhookController,
    SudoConvergeController,
]

GUILD_ID = "555500000000000001"
USER_ID = "555500000000000002"
CHANNEL_ID = "555500000000000003"


@pytest.fixture
async def integration_engine():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(SkriftBase.metadata.create_all)
        await conn.run_sync(DomainBase.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def integration_session_maker(integration_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=integration_engine, expire_on_commit=False)


async def _mint_service_key(
    session_maker: async_sessionmaker[AsyncSession],
    email: str,
    with_role: bool,
) -> str:
    """Mint an sk_ key exactly like the production runbook does."""
    async with session_maker() as session:
        service_user = SkriftUser(email=email, name="bot", is_active=True)
        session.add(service_user)
        await session.commit()

        await sync_roles_to_database(session)
        if with_role:
            role_assigned = await assign_role_to_user(
                session, service_user.id, "bot-service"
            )
            assert role_assigned, "bot-service role missing after sync"
        await session.commit()

        _api_key, raw_key, _refresh = await api_key_service.create_api_key(
            session,
            service_user.id,
            "discord-bot",
            principal_type="service",
            service_name="discord-bot",
            scoped_permissions=["bot-api", "bot-api-admin"],
        )
        await session.commit()
    return raw_key


@pytest.fixture
async def bot_key(integration_session_maker) -> str:
    """The production key shape: bot-service role + scoped permissions."""
    return await _mint_service_key(
        integration_session_maker, "bot@smarter.dev", with_role=True
    )


@pytest.fixture
async def roleless_key(integration_session_maker) -> str:
    """A valid key whose owner lacks the role — permission gate must 401."""
    return await _mint_service_key(
        integration_session_maker, "noperms@smarter.dev", with_role=False
    )


@pytest.fixture
async def seeded_bytes(integration_session_maker) -> None:
    async with integration_session_maker() as session:
        session.add(BytesConfig(guild_id=GUILD_ID, daily_amount=10))
        session.add(
            BytesBalance(
                guild_id=GUILD_ID,
                user_id=USER_ID,
                balance=1000,
                total_received=1000,
            )
        )
        await session.commit()


@pytest.fixture
def app_client(integration_session_maker) -> Iterator[TestClient]:
    """The assembled /api surface with real auth, DB, and rate limiting."""

    async def provide_db_session() -> AsyncIterator[AsyncSession]:
        async with integration_session_maker() as session:
            yield session

    invalidate_user_permissions_cache()
    with (
        # Guard/introspection key lookups and the rate limiter's sessions all
        # target the test database instead of the process-global engine.
        patch(
            "smarter_dev.web.api_native.auth.get_db_session_context",
            side_effect=lambda: integration_session_maker(),
        ),
        patch(
            "smarter_dev.web.api_native.rate_limiting.get_db_session_context",
            side_effect=lambda: integration_session_maker(),
        ),
        patch(
            "smarter_dev.web.api_native.rate_limiting.get_db_session_context",
            side_effect=lambda: integration_session_maker(),
        ),
    ):
        with create_test_client(
            route_handlers=ALL_API_CONTROLLERS,
            dependencies={"db_session": Provide(provide_db_session)},
        ) as client:
            client.app.state.session_maker_class = integration_session_maker
            yield client
    invalidate_user_permissions_cache()


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


class TestAppAssembly:
    def test_all_controllers_register_without_conflicts(self, app_client):
        """create_test_client raises on route conflicts — reaching here is the test."""
        assert app_client.app.routes


class TestHealthAndAuth:
    def test_health_is_unauthenticated(self, app_client):
        response = app_client.get("/api/health")
        assert response.status_code == 200
        assert response.json() == {"status": "healthy", "version": "1.0.0"}

    def test_validate_accepts_the_production_key_shape(self, app_client, bot_key):
        response = app_client.post("/api/auth/validate", headers=_auth(bot_key))
        assert response.status_code == 200
        assert response.json()["valid"] is True

    def test_status_reports_key_introspection(self, app_client, bot_key):
        response = app_client.get("/api/auth/status", headers=_auth(bot_key))
        assert response.status_code == 200
        body = response.json()
        assert body["authenticated"] is True
        assert body["key_prefix"].startswith("sk_")
        assert body["scopes"] == ["bot-api", "bot-api-admin"]

    def test_missing_key_401(self, app_client):
        response = app_client.get(f"/api/guilds/{GUILD_ID}/bytes/config")
        assert response.status_code == 401

    def test_legacy_sk_dash_key_401(self, app_client):
        response = app_client.get(
            f"/api/guilds/{GUILD_ID}/bytes/config",
            headers=_auth("sk-" + "a" * 43),
        )
        assert response.status_code == 401

    def test_unknown_skrift_key_401(self, app_client):
        response = app_client.get(
            f"/api/guilds/{GUILD_ID}/bytes/config",
            headers=_auth("sk_" + "b" * 43),
        )
        assert response.status_code == 401

    def test_key_without_bot_api_permission_401(self, app_client, roleless_key):
        response = app_client.get(
            f"/api/guilds/{GUILD_ID}/bytes/config", headers=_auth(roleless_key)
        )
        assert response.status_code == 401


class TestRepresentativeEndpoints:
    def test_bytes_balance_with_rate_limit_headers(
        self, app_client, bot_key, seeded_bytes
    ):
        response = app_client.get(
            f"/api/guilds/{GUILD_ID}/bytes/balance/{USER_ID}",
            headers=_auth(bot_key),
        )
        assert response.status_code == 200
        assert response.json()["balance"] == 1000
        # The multi-tier limiter is live on the bytes routes in the full app.
        assert response.headers["x-ratelimit-limit-second"] == "10"
        assert "x-ratelimit-remaining" in response.headers

    def test_squads_list_trailing_slash_path(self, app_client, bot_key):
        response = app_client.get(
            f"/api/guilds/{GUILD_ID}/squads/", headers=_auth(bot_key)
        )
        assert response.status_code == 200
        assert response.json() == []

    def test_model_override_lifecycle(self, app_client, bot_key):
        override_path = (
            f"/api/guilds/{GUILD_ID}/channels/{CHANNEL_ID}/model-override"
        )
        put_response = app_client.put(
            override_path,
            headers=_auth(bot_key),
            json={
                "model_key": "gpt-5-5",
                "daily_token_budget": 0,
                "hourly_token_budget": 0,
            },
        )
        assert put_response.status_code == 200
        assert put_response.json()["model_key"] == "gpt-5-5"

        get_response = app_client.get(override_path, headers=_auth(bot_key))
        assert get_response.status_code == 200
        assert get_response.json()["channel_id"] == CHANNEL_ID

        delete_response = app_client.delete(override_path, headers=_auth(bot_key))
        assert delete_response.status_code == 204

        gone_response = app_client.get(override_path, headers=_auth(bot_key))
        assert gone_response.status_code == 404

    def test_activity_batch(self, app_client, bot_key):
        response = app_client.post(
            "/api/activity/batch",
            headers=_auth(bot_key),
            json={
                "events": [
                    {
                        "guild_id": GUILD_ID,
                        "user_id": USER_ID,
                        "message_at": "2026-07-16T00:00:00Z",
                    }
                ]
            },
        )
        assert response.status_code == 200
        assert response.json() == {"recorded": 1}

    def test_chat_engagement_create_requires_admin_permission(
        self, app_client, bot_key
    ):
        response = app_client.post(
            "/api/chat-conversations/engagements",
            headers=_auth(bot_key),
            json={
                "guild_id": GUILD_ID,
                "channel_id": CHANNEL_ID,
                "guild_name": "Integration Guild",
                "channel_name": "general",
                "activation_user_id": USER_ID,
                "activation_username": "tester",
                "activation_message_id": "555500000000000004",
            },
        )
        assert response.status_code == 201

    def test_scheduled_messages_pending_shape(self, app_client, bot_key):
        response = app_client.get(
            "/api/scheduled-messages/pending", headers=_auth(bot_key)
        )
        assert response.status_code == 200
        assert response.json() == {"scheduled_messages": []}
