"""Test configuration and fixtures for the Smarter Dev project."""

from __future__ import annotations

import asyncio
import os
from typing import AsyncGenerator
from typing import Dict
from typing import Any
from unittest.mock import AsyncMock
from unittest.mock import Mock

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from smarter_dev.shared.config import Settings
from smarter_dev.shared.config import override_settings
from smarter_dev.shared.database import Base
from smarter_dev.shared.database import async_sessionmaker
from smarter_dev.shared.redis_client import RedisManager


# Test configuration
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
TEST_REDIS_URL = "redis://localhost:6379/15"  # Use a different DB for tests


@pytest.fixture(scope="session")
def event_loop():
    """Create an instance of the default event loop for the test session."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
def test_settings() -> Settings:
    """Create test settings."""
    return override_settings(
        environment="testing",
        database_url=TEST_DATABASE_URL,
        redis_url=TEST_REDIS_URL,
        discord_bot_token="test_token",
        discord_application_id="123456789",
        api_secret_key="test_secret_key",
        debug=True,
        log_level="DEBUG",
    )


@pytest.fixture(scope="session")
async def test_engine():
    """Create test database engine."""
    engine = create_async_engine(
        TEST_DATABASE_URL,
        poolclass=StaticPool,
        echo=False,
        future=True,
        connect_args={"check_same_thread": False},
    )
    
    # Create all tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    yield engine
    
    # Drop all tables and close
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def db_session(test_engine) -> AsyncGenerator[AsyncSession, None]:
    """Create a test database session with transaction rollback."""
    # Recreate tables for each test to ensure isolation
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    
    session_maker = async_sessionmaker(test_engine, expire_on_commit=False)
    
    async with session_maker() as session:
        try:
            yield session
        finally:
            await session.close()


@pytest.fixture
async def redis_manager(test_settings) -> AsyncGenerator[RedisManager, None]:
    """Create a test Redis manager."""
    manager = RedisManager(test_settings)
    await manager.init()
    
    yield manager
    
    # Clean up test data
    if manager.client:
        await manager.client.flushdb()
        await manager.close()


@pytest.fixture
def mock_redis_manager() -> Mock:
    """Create a mock Redis manager."""
    manager = Mock(spec=RedisManager)
    manager.get = AsyncMock(return_value=None)
    manager.set = AsyncMock(return_value=True)
    manager.delete = AsyncMock(return_value=1)
    manager.exists = AsyncMock(return_value=0)
    manager.expire = AsyncMock(return_value=True)
    manager.ttl = AsyncMock(return_value=-1)
    manager.hget = AsyncMock(return_value=None)
    manager.hset = AsyncMock(return_value=1)
    manager.hgetall = AsyncMock(return_value={})
    manager.hdel = AsyncMock(return_value=1)
    manager.publish = AsyncMock(return_value=1)
    return manager


@pytest.fixture
async def api_client(test_settings) -> AsyncGenerator[AsyncClient, None]:
    """Create an async HTTP client for API testing."""
    # Import here to avoid circular imports
    from smarter_dev.web.api.app import create_app
    
    app = create_app(test_settings)
    
    async with AsyncClient(app=app, base_url="http://test") as client:
        yield client


@pytest.fixture
def mock_discord_bot() -> Mock:
    """Create a mock Discord bot."""
    bot = Mock()
    bot.cache = Mock()
    bot.rest = Mock()
    bot.d = Mock()
    return bot


@pytest.fixture
def mock_discord_guild() -> Mock:
    """Create a mock Discord guild."""
    guild = Mock()
    guild.id = 123456789
    guild.name = "Test Guild"
    guild.get_role = Mock(return_value=Mock(
        id=987654321,
        name="Test Role",
        color=0xFF0000,
        mention="<@&987654321>"
    ))
    guild.get_member = Mock(return_value=Mock(
        id=111111111,
        username="TestUser",
        discriminator="0001",
        display_name="Test User",
        mention="<@111111111>"
    ))
    return guild


@pytest.fixture
def mock_discord_user() -> Mock:
    """Create a mock Discord user."""
    user = Mock()
    user.id = 111111111
    user.username = "TestUser"
    user.discriminator = "0001"
    user.display_name = "Test User"
    user.mention = "<@111111111>"
    user.is_bot = False
    return user


@pytest.fixture
def mock_discord_member() -> Mock:
    """Create a mock Discord member."""
    member = Mock()
    member.id = 111111111
    member.username = "TestUser"
    member.discriminator = "0001"
    member.display_name = "Test User"
    member.mention = "<@111111111>"
    member.is_bot = False
    member.roles = []
    member.guild_id = 123456789
    return member


@pytest.fixture
def mock_lightbulb_context(mock_discord_bot, mock_discord_guild, mock_discord_user) -> Mock:
    """Create a mock Lightbulb context."""
    ctx = Mock()
    ctx.bot = mock_discord_bot
    ctx.guild_id = mock_discord_guild.id
    ctx.author = mock_discord_user
    ctx.respond = AsyncMock()
    ctx.edit_last_response = AsyncMock()
    ctx.delete_last_response = AsyncMock()
    ctx.get_guild = Mock(return_value=mock_discord_guild)
    ctx.get_channel = Mock()
    return ctx


@pytest.fixture
def sample_test_data() -> Dict[str, Any]:
    """Create sample test data for various tests."""
    return {
        "guild_id": "123456789",
        "user_id": "111111111",
        "username": "TestUser",
        "balance": 100,
        "streak_count": 5,
        "squad_id": "00000000-0000-0000-0000-000000000000",
        "squad_name": "Test Squad",
        "role_id": "987654321",
        "transaction_amount": 50,
        "transaction_reason": "Test transaction",
    }


@pytest.fixture
def mock_bytes_balance(sample_test_data) -> Mock:
    """Create a mock bytes balance object."""
    balance = Mock()
    balance.guild_id = sample_test_data["guild_id"]
    balance.user_id = sample_test_data["user_id"]
    balance.balance = sample_test_data["balance"]
    balance.total_received = 200
    balance.total_sent = 100
    balance.streak_count = sample_test_data["streak_count"]
    balance.last_daily = None
    balance.to_embed = Mock(return_value=Mock())
    return balance


@pytest.fixture
def mock_bytes_transaction(sample_test_data) -> Mock:
    """Create a mock bytes transaction object."""
    transaction = Mock()
    transaction.id = "trans-123"
    transaction.guild_id = sample_test_data["guild_id"]
    transaction.giver_id = sample_test_data["user_id"]
    transaction.giver_username = sample_test_data["username"]
    transaction.receiver_id = "222222222"
    transaction.receiver_username = "ReceiverUser"
    transaction.amount = sample_test_data["transaction_amount"]
    transaction.reason = sample_test_data["transaction_reason"]
    return transaction


@pytest.fixture
def mock_squad(sample_test_data) -> Mock:
    """Create a mock squad object."""
    squad = Mock()
    squad.id = sample_test_data["squad_id"]
    squad.guild_id = sample_test_data["guild_id"]
    squad.role_id = sample_test_data["role_id"]
    squad.name = sample_test_data["squad_name"]
    squad.description = "Test squad description"
    squad.switch_cost = 50
    squad.max_members = None
    squad.is_active = True
    squad.member_count = 0
    return squad


# Test utility functions
def create_mock_async_session() -> Mock:
    """Create a mock async session."""
    session = Mock(spec=AsyncSession)
    session.add = Mock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.close = AsyncMock()
    session.refresh = AsyncMock()
    session.get = AsyncMock()
    session.execute = AsyncMock()
    session.scalar = AsyncMock()
    session.scalars = AsyncMock()
    return session


def create_mock_discord_event(event_type: str = "message", **kwargs) -> Mock:
    """Create a mock Discord event."""
    event = Mock()
    event.app = Mock()
    event.shard = Mock()
    
    if event_type == "message":
        event.guild_id = kwargs.get("guild_id", 123456789)
        event.channel_id = kwargs.get("channel_id", 555555555)
        event.author = Mock(
            id=kwargs.get("author_id", 111111111),
            username=kwargs.get("username", "TestUser"),
            is_bot=kwargs.get("is_bot", False)
        )
        event.content = kwargs.get("content", "Test message")
        event.message_id = kwargs.get("message_id", 999999999)
    
    return event


# Pytest configuration
def pytest_configure(config):
    """Configure pytest settings."""
    config.addinivalue_line(
        "markers", "slow: marks tests as slow (deselect with '-m \"not slow\"')"
    )
    config.addinivalue_line(
        "markers", "integration: marks tests as integration tests"
    )
    config.addinivalue_line(
        "markers", "unit: marks tests as unit tests"
    )


def pytest_collection_modifyitems(config, items):
    """Modify test collection to add markers."""
    for item in items:
        # Add unit marker to all tests by default
        if not any(marker.name in ["slow", "integration"] for marker in item.iter_markers()):
            item.add_marker(pytest.mark.unit)
        
        # Add slow marker to tests that take longer
        if "slow" in item.nodeid or "integration" in item.nodeid:
            item.add_marker(pytest.mark.slow)


# Skip tests if dependencies are not available
def pytest_runtest_setup(item):
    """Set up test run with dependency checks."""
    # Skip Redis tests if Redis is not available
    if "redis" in item.fixturenames:
        try:
            import redis
        except ImportError:
            pytest.skip("Redis not available")
    
    # Skip database tests if async database drivers are not available
    if "db_session" in item.fixturenames:
        try:
            import aiosqlite
        except ImportError:
            pytest.skip("aiosqlite not available")
    
    # Skip Discord tests if Hikari is not available
    if any("discord" in fixture for fixture in item.fixturenames):
        try:
            import hikari
        except ImportError:
            pytest.skip("hikari not available")