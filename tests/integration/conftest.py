"""Fixtures for integration tests."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

# Import fixtures from admin test configuration to maintain compatibility
from tests.web.test_admin.conftest import (
    admin_auth_headers,
    real_api_client,
    real_db_session,
    admin_api_settings,
    real_db_engine
)

# Re-export all necessary fixtures so integration tests can use them
__all__ = [
    'admin_auth_headers',
    'real_api_client',
    'real_db_session',
    'admin_api_settings',
    'real_db_engine',
    'api_settings',
    'bot_headers',
    'test_guild_id',
    'test_user_id',
    'test_user_id_2',
    'test_role_id',
]


@pytest.fixture(scope="function")
def api_settings(admin_api_settings):
    """Alias for admin_api_settings so tests that reference api_settings work."""
    return admin_api_settings


@pytest.fixture(scope="function")
def test_guild_id() -> str:
    """Test guild ID with unique ID per test for isolation."""
    import uuid
    return str(int(uuid.uuid4().hex[:15], 16))


@pytest.fixture(scope="function")
def test_user_id() -> str:
    """Test user ID with unique ID per test for isolation."""
    import uuid
    return str(int(uuid.uuid4().hex[:15], 16))


@pytest.fixture(scope="function")
def test_user_id_2() -> str:
    """Second test user ID with unique ID per test for isolation."""
    import uuid
    return str(int(uuid.uuid4().hex[:15], 16))


@pytest.fixture(scope="function")
def test_role_id() -> str:
    """Test role ID with unique ID per test for isolation."""
    import uuid
    return str(int(uuid.uuid4().hex[:15], 16))


@pytest.fixture(scope="function")
async def bot_headers(real_db_session) -> dict:
    """Valid bot authentication headers with a real API key."""
    from smarter_dev.web.security import generate_secure_api_key
    from smarter_dev.web.models import APIKey

    # Generate secure API key
    full_key, key_hash, key_prefix = generate_secure_api_key()

    # Store in database
    api_key = APIKey(
        name="Integration Test Bot Key",
        key_hash=key_hash,
        key_prefix=key_prefix,
        scopes=["bot:read", "bot:write"],
        rate_limit_per_hour=10000,
        created_by="test_system",
        expires_at=None,
        is_active=True
    )

    real_db_session.add(api_key)
    await real_db_session.commit()

    return {"Authorization": f"Bearer {full_key}"}
