"""Shared test fixtures for web tests."""

import pytest
import pytest_asyncio
from httpx import AsyncClient
from unittest.mock import AsyncMock, Mock, patch

from web.app import create_app


@pytest.fixture
def mock_config():
    """Create a mock config for testing."""
    config = Mock()
    config.dev_mode = True
    config.base_url = "http://test"

    # Mock session_secret properly
    mock_secret = Mock()
    mock_secret.get_secret_value.return_value = (
        "test-secret-key-that-is-long-enough-32chars"
    )
    config.session_secret = mock_secret

    config.redis_url = "redis://localhost:6379"
    config.serve_static = True
    config.static_url = "/static"
    config.database_url = "postgresql://test:test@localhost/test"
    config.api_key_for_bot = "test_api_key_for_bot"
    return config


@pytest.fixture
def mock_database():
    """Create a mock database for testing."""
    db = AsyncMock()
    db.connect = AsyncMock()
    db.disconnect = AsyncMock()
    return db


@pytest.fixture
def mock_redis():
    """Create a mock Redis client for testing."""
    redis_client = AsyncMock()
    redis_client.get = AsyncMock(return_value=None)
    redis_client.set = AsyncMock()
    redis_client.setex = AsyncMock()
    redis_client.publish = AsyncMock()
    return redis_client


@pytest_asyncio.fixture
async def auth_api_client(mock_config, mock_database, mock_redis):
    """Create an authenticated API client for testing."""
    with patch("web.database.get_db", return_value=mock_database):
        with patch("redis.asyncio.from_url", return_value=mock_redis):
            app = create_app(mock_config)

            # Create client with authentication headers
            headers = {
                "Authorization": f"Bearer {mock_config.api_key_for_bot}",
                "Content-Type": "application/json",
            }

            from httpx import ASGITransport

            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test", headers=headers
            ) as client:
                yield client


@pytest.fixture
def mock_db():
    """Mock database session."""
    return AsyncMock()


@pytest.fixture
def mock_api_client():
    """Mock API client for testing."""
    client = AsyncMock()
    with patch("httpx.AsyncClient", return_value=client):
        yield client
