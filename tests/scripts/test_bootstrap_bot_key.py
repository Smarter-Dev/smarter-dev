"""Tests for scripts/bootstrap.py Skrift-native bot key provisioning.

Phase 01 (docs/v2/legacy-sunset/01-skrift-api-keys.md step 4): bootstrap must
mint the local bot key via ``skrift.db.services.api_key_service`` against the
main DB instead of writing a legacy ``sk-`` row into ``public.api_keys``,
while keeping the 'reused' | 'rotated' | 'created' semantics.
"""

from __future__ import annotations

from typing import AsyncGenerator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from skrift.db.models.api_key import APIKey as SkriftAPIKey
from skrift.db.models.role import Role, RolePermission, user_roles
from skrift.db.models.user import User as SkriftUser
from skrift.db.services import api_key_service

from scripts.bootstrap import (
    BOT_SERVICE_NAME,
    BOT_SERVICE_USER_EMAIL,
    provision_skrift_bot_key,
)


@pytest.fixture
async def skrift_engine() -> AsyncGenerator[AsyncEngine, None]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    auth_tables = [
        SkriftUser.__table__,
        SkriftAPIKey.__table__,
        Role.__table__,
        user_roles,
        RolePermission.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: SkriftUser.metadata.create_all(sync_conn, tables=auth_tables)
        )
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def session_maker(skrift_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=skrift_engine, expire_on_commit=False)


async def _active_bot_keys(session: AsyncSession) -> list[SkriftAPIKey]:
    result = await session.execute(
        select(SkriftAPIKey).where(
            SkriftAPIKey.service_name == BOT_SERVICE_NAME,
            SkriftAPIKey.is_active.is_(True),
        )
    )
    return list(result.scalars().all())


class TestProvisionSkriftBotKey:
    async def test_creates_service_key_and_owner_user(self, session_maker):
        async with session_maker() as session:
            raw_key, status = await provision_skrift_bot_key(
                session, env_key=None, rotate=False
            )

        assert status == "created"
        assert raw_key.startswith("sk_")

        async with session_maker() as session:
            keys = await _active_bot_keys(session)
            assert len(keys) == 1
            assert keys[0].principal_type == "service"
            assert keys[0].service_name == BOT_SERVICE_NAME

            owner = await session.get(SkriftUser, keys[0].user_id)
            assert owner.email == BOT_SERVICE_USER_EMAIL
            assert owner.is_active is True

    async def test_minted_key_verifies_through_skrift_service(self, session_maker):
        async with session_maker() as session:
            raw_key, _status = await provision_skrift_bot_key(
                session, env_key=None, rotate=False
            )

        async with session_maker() as session:
            verified = await api_key_service.verify_api_key(session, raw_key)
            assert verified is not None
            assert verified.service_name == BOT_SERVICE_NAME

    async def test_reuses_key_when_env_matches_db(self, session_maker):
        async with session_maker() as session:
            first_key, _ = await provision_skrift_bot_key(
                session, env_key=None, rotate=False
            )

        async with session_maker() as session:
            second_key, status = await provision_skrift_bot_key(
                session, env_key=first_key, rotate=False
            )

        assert status == "reused"
        assert second_key == first_key

        async with session_maker() as session:
            assert len(await _active_bot_keys(session)) == 1

    async def test_rotates_when_env_key_missing(self, session_maker):
        async with session_maker() as session:
            first_key, _ = await provision_skrift_bot_key(
                session, env_key=None, rotate=False
            )

        async with session_maker() as session:
            second_key, status = await provision_skrift_bot_key(
                session, env_key=None, rotate=False
            )

        assert status == "rotated"
        assert second_key != first_key

        async with session_maker() as session:
            active = await _active_bot_keys(session)
            assert len(active) == 1
            # The stale key must no longer verify.
            assert await api_key_service.verify_api_key(session, first_key) is None
            assert await api_key_service.verify_api_key(session, second_key) is not None

    async def test_rotates_when_env_key_does_not_match(self, session_maker):
        async with session_maker() as session:
            first_key, _ = await provision_skrift_bot_key(
                session, env_key=None, rotate=False
            )

        async with session_maker() as session:
            second_key, status = await provision_skrift_bot_key(
                session, env_key="sk_" + "a" * 43, rotate=False
            )

        assert status == "rotated"
        assert second_key != first_key

    async def test_forced_rotation_overrides_matching_env_key(self, session_maker):
        async with session_maker() as session:
            first_key, _ = await provision_skrift_bot_key(
                session, env_key=None, rotate=False
            )

        async with session_maker() as session:
            second_key, status = await provision_skrift_bot_key(
                session, env_key=first_key, rotate=True
            )

        assert status == "rotated"
        assert second_key != first_key

    async def test_service_user_not_duplicated_across_runs(self, session_maker):
        async with session_maker() as session:
            await provision_skrift_bot_key(session, env_key=None, rotate=False)
        async with session_maker() as session:
            await provision_skrift_bot_key(session, env_key=None, rotate=False)

        async with session_maker() as session:
            owners = (
                await session.execute(
                    select(SkriftUser).where(SkriftUser.email == BOT_SERVICE_USER_EMAIL)
                )
            ).scalars().all()
            assert len(owners) == 1
