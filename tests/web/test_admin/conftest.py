"""Test fixtures for the legacy FastAPI API-key management tests.

The legacy Starlette ``/bot-admin`` interface and its fixtures were removed when
the admin was ported to Skrift-native ``/admin`` controllers. What remains here
serves the API-key management tests that exercise the still-live FastAPI
``/admin/api-keys`` endpoints against an isolated in-memory SQLite database.
"""

from __future__ import annotations

import sqlite3
import uuid

# Register UUID adapter so SQLite can bind uuid.UUID objects as strings
sqlite3.register_adapter(uuid.UUID, lambda u: str(u))

import pytest
from unittest.mock import patch
from typing import AsyncGenerator

from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from smarter_dev.shared.config import override_settings
from smarter_dev.shared.database import Base, async_sessionmaker
from smarter_dev.web.api.app import api


@pytest.fixture
async def admin_auth_headers(real_db_session) -> dict[str, str]:
    """Create admin authentication headers for API testing."""
    from smarter_dev.web.security import generate_secure_api_key
    from smarter_dev.web.models import APIKey

    # Generate admin API key
    full_key, key_hash, key_prefix = generate_secure_api_key()

    # Create admin API key with elevated permissions
    admin_api_key = APIKey(
        name="Admin Interface Key",
        key_hash=key_hash,
        key_prefix=key_prefix,
        scopes=["admin:read", "admin:write", "admin:manage"],
        rate_limit_per_hour=10000,  # Higher limit for admin
        created_by="system",
        expires_at=None,
        is_active=True
    )

    real_db_session.add(admin_api_key)
    await real_db_session.commit()

    return {"Authorization": f"Bearer {full_key}"}


@pytest.fixture(scope="function")
def admin_api_settings():
    """Create API-specific test settings for admin tests."""
    import os

    # Clear any PostgreSQL environment variables that might interfere
    postgres_env_vars = [
        'DATABASE_URL', 'POSTGRES_HOST', 'POSTGRES_PORT', 'POSTGRES_DB',
        'POSTGRES_USER', 'POSTGRES_PASSWORD', 'PGHOST', 'PGPORT', 'PGDATABASE',
        'PGUSER', 'PGPASSWORD'
    ]

    # Store original values to restore later
    original_env = {}
    for var in postgres_env_vars:
        if var in os.environ:
            original_env[var] = os.environ[var]
            del os.environ[var]

    return override_settings(
        environment="testing",
        database_url="sqlite+aiosqlite:///:memory:",
        redis_url="redis://localhost:6379/15",  # Test Redis DB
        discord_bot_token="test_bot_token_12345",
        discord_application_id="123456789",
        api_secret_key="test_api_secret_key",
        bot_api_key="sk-test_api_key_for_testing_only_123456789",  # Test API key
        debug=True,
        log_level="DEBUG",
        # Explicitly disable any PostgreSQL settings
        postgres_host=None,
        postgres_port=None,
        postgres_db=None,
        postgres_user=None,
        postgres_password=None,
        # Force SQLite configuration
        db_dialect="sqlite",
        db_driver="aiosqlite",
    )


@pytest.fixture(scope="function")
async def real_db_engine(admin_api_settings):
    """Create a fresh isolated database engine for each admin test."""
    import tempfile
    import os
    import uuid

    # Create a unique temporary database file for complete isolation
    temp_dir = tempfile.mkdtemp()
    db_name = f"admin_test_db_{uuid.uuid4().hex}.db"
    db_path = os.path.join(temp_dir, db_name)
    database_url = f"sqlite+aiosqlite:///{db_path}"

    # Force SQLite configuration to override any global settings
    engine = create_async_engine(
        database_url,
        poolclass=StaticPool,
        echo=False,
        future=True,
        connect_args={"check_same_thread": False},
        # Ensure we're using SQLite
        module=None,  # Let SQLAlchemy auto-detect
    )

    try:
        # Create all tables
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        yield engine

    finally:
        # Enhanced async cleanup to prevent connection pool exhaustion
        try:
            # First, close all connections in the pool
            await engine.dispose()

            # Wait longer for all async operations to complete
            import asyncio
            await asyncio.sleep(0.2)

            # Force garbage collection to clean up any remaining references
            import gc
            gc.collect()

            # Additional wait for any pending async operations
            await asyncio.sleep(0.1)

            # Clean up the temporary database file
            if os.path.exists(db_path):
                try:
                    os.remove(db_path)
                except OSError:
                    pass  # File might be locked, ignore
            if os.path.exists(temp_dir):
                try:
                    os.rmdir(temp_dir)
                except OSError:
                    pass  # Directory might not be empty, ignore
        except Exception:
            pass  # Ignore disposal errors


@pytest.fixture(scope="function")
async def real_db_session(real_db_engine) -> AsyncGenerator[AsyncSession, None]:
    """Create a clean database session for each admin test."""
    # Create a session maker bound to the fresh database engine
    session_maker = async_sessionmaker(bind=real_db_engine, expire_on_commit=False)

    session = session_maker()
    try:
        yield session
        # Let the test commit normally if it wants to
        if session.in_transaction():
            await session.commit()
    except Exception:
        # Rollback on any exception
        try:
            if session.in_transaction():
                await session.rollback()
        except Exception:
            pass  # Ignore rollback errors
        raise
    finally:
        # Enhanced session cleanup
        try:
            # Close the session properly
            await session.close()

            # Give more time for session cleanup to complete
            import asyncio
            await asyncio.sleep(0.05)

            # Force garbage collection for session cleanup
            import gc
            gc.collect()
        except Exception:
            pass  # Ignore close errors


@pytest.fixture(scope="function")
async def real_api_client(admin_api_settings, real_db_engine) -> AsyncGenerator[AsyncClient, None]:
    """Create an async HTTP client for real API integration testing with isolated SQLite database."""
    # Ensure we're using the exact same engine that has tables created
    from smarter_dev.shared.database import async_sessionmaker

    # Override the global database functions to use our test engine
    def mock_get_engine():
        return real_db_engine

    def mock_get_session_maker():
        return async_sessionmaker(bind=real_db_engine, expire_on_commit=False)

    async def mock_get_db_session():
        session_maker = mock_get_session_maker()
        async with session_maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

    # Patch ALL database-related functions to use our test engine
    with patch('smarter_dev.shared.config.get_settings', return_value=admin_api_settings), \
         patch('smarter_dev.shared.config.settings', admin_api_settings), \
         patch('smarter_dev.web.api.dependencies.get_settings', return_value=admin_api_settings), \
         patch('smarter_dev.shared.database.get_engine', side_effect=mock_get_engine), \
         patch('smarter_dev.shared.database.get_session_maker', side_effect=mock_get_session_maker), \
         patch('smarter_dev.shared.database.get_db_session', side_effect=mock_get_db_session), \
         patch('smarter_dev.shared.database.get_skrift_db_session', side_effect=mock_get_db_session), \
         patch('smarter_dev.web.api.app.init_database'), \
         patch('smarter_dev.web.api.app.close_database'):

        # Override settings in the API app
        api.state.settings = admin_api_settings

        # Override FastAPI dependencies so Depends(get_skrift_db_session) uses the test DB
        # Must use the exact function reference from the router module for identity match
        from smarter_dev.web.api.routers import quests as quests_router_mod
        api.dependency_overrides[quests_router_mod.get_skrift_db_session] = mock_get_db_session

        try:
            async with AsyncClient(
                transport=ASGITransport(app=api),
                base_url="http://test"
            ) as client:
                yield client
        finally:
            # Ensure API state cleanup
            try:
                api.dependency_overrides.pop(quests_router_mod.get_skrift_db_session, None)
                if hasattr(api, 'state'):
                    api.state.settings = None
            except Exception:
                pass  # Ignore cleanup errors
