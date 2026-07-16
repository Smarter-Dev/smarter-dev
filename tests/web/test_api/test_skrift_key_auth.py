"""Tests for dual-source API key verification (Skrift-native + legacy).

Covers docs/v2/legacy-sunset/01-skrift-api-keys.md step 1/2: the bot API
accepts Skrift ``sk_`` keys (main DB) first and falls back to legacy ``sk-``
keys, with both key shapes passing format validation and everything else
rejected.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator
from unittest.mock import patch
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from skrift.db.models.api_key import APIKey as SkriftAPIKey
from skrift.db.models.role import Role, RolePermission, user_roles
from skrift.db.models.user import User as SkriftUser
from skrift.db.services import api_key_service

from smarter_dev.web.api.dependencies import (
    AuthenticatedKey,
    authenticated_key_from_legacy,
    authenticated_key_from_skrift,
)
from smarter_dev.web.models import APIKey as LegacyAPIKey
from smarter_dev.web.security import generate_secure_api_key, validate_api_key_format


class TestApiKeyFormatValidation:
    """Format gate accepts both key shapes without weakening entropy checks."""

    def test_legacy_key_format_accepted(self):
        full_key, _key_hash, _prefix = generate_secure_api_key()
        assert full_key.startswith("sk-")
        assert len(full_key) == 46
        assert validate_api_key_format(full_key) is True

    def test_skrift_key_format_accepted(self):
        raw_key, _prefix, _key_hash = api_key_service._generate_key()
        assert raw_key.startswith("sk_")
        assert len(raw_key) == 46
        assert validate_api_key_format(raw_key) is True

    def test_legacy_key_wrong_length_rejected(self):
        assert validate_api_key_format("sk-" + "a" * 42) is False
        assert validate_api_key_format("sk-" + "a" * 44) is False

    def test_skrift_key_too_short_rejected(self):
        assert validate_api_key_format("sk_" + "a" * 39) is False

    def test_skrift_key_too_long_rejected(self):
        assert validate_api_key_format("sk_" + "a" * 65) is False

    def test_skrift_key_invalid_characters_rejected(self):
        assert validate_api_key_format("sk_" + "!" * 43) is False
        assert validate_api_key_format("sk_" + "a" * 42 + "=") is False

    def test_unknown_prefix_rejected(self):
        assert validate_api_key_format("pk_" + "a" * 43) is False
        assert validate_api_key_format("a" * 46) is False

    def test_empty_and_non_string_rejected(self):
        assert validate_api_key_format("") is False
        assert validate_api_key_format(None) is False
        assert validate_api_key_format(12345) is False


class TestAuthenticatedKeyShim:
    """Both branches produce the same downstream contract."""

    def test_from_legacy_maps_all_consumer_fields(self):
        legacy_key = LegacyAPIKey(
            id=uuid4(),
            name="Discord Bot",
            key_hash="hash",
            key_prefix="sk-abc123de",
            scopes=["bot:read"],
            rate_limit_per_second=5,
            rate_limit_per_minute=60,
            rate_limit_per_15_minutes=500,
            rate_limit_per_hour=1000,
            usage_count=7,
            created_by="admin",
            expires_at=None,
            is_active=True,
        )
        authenticated = authenticated_key_from_legacy(legacy_key)

        assert authenticated.id == legacy_key.id
        assert authenticated.name == "Discord Bot"
        assert authenticated.key_prefix == "sk-abc123de"
        assert authenticated.created_by == "admin"
        assert authenticated.scopes == ["bot:read"]
        assert authenticated.usage_count == 7
        assert authenticated.rate_limit_per_second == 5
        assert authenticated.rate_limit_per_minute == 60
        assert authenticated.rate_limit_per_15_minutes == 500
        assert authenticated.rate_limit_per_hour == 1000
        assert authenticated.is_legacy is True
        assert authenticated.is_expired is False
        assert authenticated.is_valid is True

    def test_from_skrift_maps_all_consumer_fields(self):
        key_id = uuid4()
        user_id = uuid4()
        skrift_key = SkriftAPIKey(
            user_id=user_id,
            display_name="discord-bot",
            key_prefix="sk_abc123def",
            key_hash="hash",
            principal_type="service",
            service_name="discord-bot",
            scoped_permissions="bot:read\nbot:write",
        )
        skrift_key.id = key_id
        authenticated = authenticated_key_from_skrift(skrift_key)

        assert authenticated.id == key_id
        assert authenticated.name == "discord-bot"
        assert authenticated.key_prefix == "sk_abc123def"
        assert authenticated.created_by == "discord-bot"
        assert authenticated.scopes == ["bot:read", "bot:write"]
        assert authenticated.is_legacy is False
        # Skrift keys get the legacy-default rate limit windows
        assert authenticated.rate_limit_per_second == 10
        assert authenticated.rate_limit_per_minute == 180
        assert authenticated.rate_limit_per_15_minutes == 2500
        assert authenticated.rate_limit_per_hour == 10000

    def test_expired_shim_reports_expired(self):
        authenticated = AuthenticatedKey(
            id=uuid4(),
            name="k",
            key_prefix="sk_expired1",
            created_by="svc",
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        assert authenticated.is_expired is True
        assert authenticated.is_valid is False


@pytest.fixture(scope="function")
async def skrift_db_engine() -> AsyncGenerator[AsyncEngine, None]:
    """In-memory SQLite engine carrying the Skrift auth tables."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    skrift_auth_tables = [
        SkriftUser.__table__,
        SkriftAPIKey.__table__,
        Role.__table__,
        user_roles,
        RolePermission.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: SkriftUser.metadata.create_all(
                sync_conn, tables=skrift_auth_tables
            )
        )
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture(scope="function")
def skrift_session_maker(skrift_db_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=skrift_db_engine, expire_on_commit=False)


@pytest.fixture(scope="function")
async def skrift_service_user(skrift_session_maker) -> SkriftUser:
    """Active Skrift user that owns the service API keys under test."""
    async with skrift_session_maker() as session:
        user = SkriftUser(email="bot@smarter.dev", name="discord-bot", is_active=True)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


@pytest.fixture(scope="function")
async def skrift_api_client(
    real_api_client: AsyncClient,
    skrift_session_maker: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncClient, None]:
    """real_api_client whose Skrift-branch lookups hit the test Skrift DB."""
    with patch(
        "smarter_dev.web.api.dependencies.get_skrift_db_session_context",
        side_effect=lambda: skrift_session_maker(),
    ):
        yield real_api_client


async def _create_skrift_key(
    skrift_session_maker: async_sessionmaker[AsyncSession],
    user_id,
    expires_at: datetime | None = None,
    is_active: bool = True,
) -> str:
    """Create a Skrift service key and return the raw sk_ token."""
    async with skrift_session_maker() as session:
        api_key, raw_key, _raw_refresh = await api_key_service.create_api_key(
            session,
            user_id,
            "discord-bot",
            principal_type="service",
            service_name="discord-bot",
            expires_at=expires_at,
        )
        if not is_active:
            api_key.is_active = False
            await session.commit()
    return raw_key


class TestSkriftKeyAuthentication:
    """End-to-end verification of the Skrift-native branch."""

    async def test_valid_skrift_key_authenticates(
        self, skrift_api_client: AsyncClient, skrift_session_maker, skrift_service_user
    ):
        raw_key = await _create_skrift_key(skrift_session_maker, skrift_service_user.id)

        response = await skrift_api_client.post(
            "/auth/validate", headers={"Authorization": f"Bearer {raw_key}"}
        )

        assert response.status_code == 200
        assert response.json()["valid"] is True

    async def test_skrift_key_auth_status_uses_shim_fields(
        self, skrift_api_client: AsyncClient, skrift_session_maker, skrift_service_user
    ):
        raw_key = await _create_skrift_key(skrift_session_maker, skrift_service_user.id)

        response = await skrift_api_client.get(
            "/auth/status", headers={"Authorization": f"Bearer {raw_key}"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["authenticated"] is True
        assert data["key_name"] == "discord-bot"
        assert data["key_prefix"].startswith("sk_")

    async def test_revoked_skrift_key_rejected(
        self, skrift_api_client: AsyncClient, skrift_session_maker, skrift_service_user
    ):
        raw_key = await _create_skrift_key(
            skrift_session_maker, skrift_service_user.id, is_active=False
        )

        response = await skrift_api_client.post(
            "/auth/validate", headers={"Authorization": f"Bearer {raw_key}"}
        )

        assert response.status_code == 401

    async def test_expired_skrift_key_rejected(
        self, skrift_api_client: AsyncClient, skrift_session_maker, skrift_service_user
    ):
        raw_key = await _create_skrift_key(
            skrift_session_maker,
            skrift_service_user.id,
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )

        response = await skrift_api_client.post(
            "/auth/validate", headers={"Authorization": f"Bearer {raw_key}"}
        )

        assert response.status_code == 401

    async def test_inactive_user_skrift_key_rejected(
        self, skrift_api_client: AsyncClient, skrift_session_maker, skrift_service_user
    ):
        raw_key = await _create_skrift_key(skrift_session_maker, skrift_service_user.id)
        async with skrift_session_maker() as session:
            user = await session.get(SkriftUser, skrift_service_user.id)
            user.is_active = False
            await session.commit()

        response = await skrift_api_client.post(
            "/auth/validate", headers={"Authorization": f"Bearer {raw_key}"}
        )

        assert response.status_code == 401

    async def test_unknown_skrift_key_rejected_without_legacy_lookup(
        self, skrift_api_client: AsyncClient, skrift_service_user
    ):
        """A well-formed sk_ token not in the Skrift table 401s and must not
        fall through to the legacy hash lookup — the formats are disjoint."""
        unknown_key, _prefix, _hash = api_key_service._generate_key()

        with patch(
            "smarter_dev.web.crud.APIKeyOperations.get_api_key_by_hash"
        ) as legacy_lookup:
            response = await skrift_api_client.post(
                "/auth/validate", headers={"Authorization": f"Bearer {unknown_key}"}
            )

        assert response.status_code == 401
        legacy_lookup.assert_not_called()


class TestLegacyFallbackAuthentication:
    """Legacy sk- keys keep working during the dual-verify window."""

    async def test_valid_legacy_key_still_authenticates(
        self, skrift_api_client: AsyncClient, bot_headers: dict[str, str]
    ):
        response = await skrift_api_client.post("/auth/validate", headers=bot_headers)

        assert response.status_code == 200
        assert response.json()["valid"] is True

    async def test_unknown_legacy_key_rejected(self, skrift_api_client: AsyncClient):
        unknown_legacy_key, _hash, _prefix = generate_secure_api_key()

        response = await skrift_api_client.post(
            "/auth/validate",
            headers={"Authorization": f"Bearer {unknown_legacy_key}"},
        )

        assert response.status_code == 401

    async def test_garbage_token_rejected(self, skrift_api_client: AsyncClient):
        response = await skrift_api_client.post(
            "/auth/validate", headers={"Authorization": "Bearer garbage-token"}
        )

        assert response.status_code == 401

    async def test_missing_bearer_rejected(self, skrift_api_client: AsyncClient):
        response = await skrift_api_client.post("/auth/validate")

        assert response.status_code == 403
