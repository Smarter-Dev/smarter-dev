"""Test configuration and fixtures specifically for API testing."""

from __future__ import annotations

from typing import AsyncGenerator, Dict, Any
from unittest.mock import Mock, AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.config import override_settings
from smarter_dev.web.api.app import api


@pytest.fixture
def api_settings():
    """Create API-specific test settings."""
    return override_settings(
        environment="testing",
        database_url="sqlite+aiosqlite:///:memory:",
        discord_bot_token="test_bot_token_12345",
        discord_application_id="123456789",
        api_secret_key="test_api_secret_key",
        debug=True,
        log_level="DEBUG",
    )


@pytest.fixture
async def api_client(api_settings) -> AsyncGenerator[AsyncClient, None]:
    """Create an async HTTP client for API testing."""
    # Mock database initialization to avoid connection issues in tests
    with patch('smarter_dev.web.api.app.init_database'), \
         patch('smarter_dev.web.api.app.close_database'), \
         patch('smarter_dev.shared.config.get_settings', return_value=api_settings), \
         patch('smarter_dev.web.api.dependencies.get_settings', return_value=api_settings), \
         patch('smarter_dev.web.api.routers.auth.get_settings', return_value=api_settings):
        
        # Override settings in the API app
        api.state.settings = api_settings
        
        async with AsyncClient(
            transport=ASGITransport(app=api),
            base_url="http://test"
        ) as client:
            yield client


@pytest.fixture
def bot_headers():
    """Valid bot authentication headers."""
    return {"Authorization": "Bearer test_bot_token_12345"}


@pytest.fixture
def invalid_headers():
    """Invalid authentication headers."""
    return {"Authorization": "Bearer invalid_token"}


@pytest.fixture
def test_guild_id() -> str:
    """Test guild ID."""
    return "123456789012345678"


@pytest.fixture
def test_user_id() -> str:
    """Test user ID."""
    return "987654321098765432"


@pytest.fixture
def test_user_id_2() -> str:
    """Second test user ID."""
    return "111111111111111111"


@pytest.fixture
def test_role_id() -> str:
    """Test role ID."""
    return "555555555555555555"


@pytest.fixture
def sample_bytes_balance_data(test_guild_id, test_user_id) -> Dict[str, Any]:
    """Sample bytes balance data."""
    return {
        "guild_id": test_guild_id,
        "user_id": test_user_id,
        "balance": 100,
        "total_received": 150,
        "total_sent": 50,
        "streak_count": 3,
        "last_daily": None
    }


@pytest.fixture
def sample_bytes_config_data(test_guild_id) -> Dict[str, Any]:
    """Sample bytes configuration data."""
    return {
        "guild_id": test_guild_id,
        "daily_amount": 10,
        "starting_balance": 100,
        "max_transfer": 1000,
        "daily_cooldown_hours": 24,
        "streak_bonuses": {"4": 2, "7": 2, "14": 3, "30": 5},
        "transfer_tax_rate": 0.0,
        "is_enabled": True
    }


@pytest.fixture
def sample_squad_data(test_guild_id, test_role_id) -> Dict[str, Any]:
    """Sample squad data."""
    return {
        "guild_id": test_guild_id,
        "role_id": test_role_id,
        "name": "Test Squad",
        "description": "A test squad for testing",
        "max_members": 10,
        "switch_cost": 50,
        "is_active": True
    }


@pytest.fixture
def sample_transaction_data(test_guild_id, test_user_id, test_user_id_2) -> Dict[str, Any]:
    """Sample transaction data."""
    return {
        "giver_id": test_user_id,
        "giver_username": "TestUser1",
        "receiver_id": test_user_id_2,
        "receiver_username": "TestUser2",
        "amount": 25,
        "reason": "Test payment"
    }


@pytest.fixture
def mock_bytes_operations():
    """Mock BytesOperations for testing."""
    with patch('smarter_dev.web.api.routers.bytes.BytesOperations') as mock:
        mock_instance = Mock()
        mock.return_value = mock_instance
        
        # Configure common mock returns
        mock_instance.get_balance = AsyncMock()
        mock_instance.create_transaction = AsyncMock()
        mock_instance.get_leaderboard = AsyncMock()
        mock_instance.get_transaction_history = AsyncMock()
        mock_instance.update_daily_reward = AsyncMock()
        mock_instance.reset_streak = AsyncMock()
        
        yield mock_instance


@pytest.fixture
def mock_bytes_config_operations():
    """Mock BytesConfigOperations for testing."""
    with patch('smarter_dev.web.api.routers.bytes.BytesConfigOperations') as mock:
        mock_instance = Mock()
        mock.return_value = mock_instance
        
        # Configure common mock returns
        mock_instance.get_config = AsyncMock()
        mock_instance.create_config = AsyncMock()
        mock_instance.update_config = AsyncMock()
        mock_instance.delete_config = AsyncMock()
        
        yield mock_instance


@pytest.fixture
def mock_squad_operations():
    """Mock SquadOperations for testing."""
    with patch('smarter_dev.web.api.routers.squads.SquadOperations') as mock:
        mock_instance = Mock()
        mock.return_value = mock_instance
        
        # Configure common mock returns
        mock_instance.get_squad = AsyncMock()
        mock_instance.get_guild_squads = AsyncMock()
        mock_instance.create_squad = AsyncMock()
        mock_instance.join_squad = AsyncMock()
        mock_instance.leave_squad = AsyncMock()
        mock_instance.get_user_squad = AsyncMock()
        mock_instance.get_squad_members = AsyncMock()
        mock_instance._get_squad_member_count = AsyncMock()
        
        yield mock_instance


@pytest.fixture
def mock_db_session():
    """Mock database session for API testing."""
    with patch('smarter_dev.web.api.dependencies.get_db_session') as mock:
        session_mock = AsyncMock(spec=AsyncSession)
        session_mock.commit = AsyncMock()
        session_mock.rollback = AsyncMock()
        session_mock.close = AsyncMock()
        
        async def mock_get_session():
            yield session_mock
            
        mock.side_effect = mock_get_session
        yield session_mock