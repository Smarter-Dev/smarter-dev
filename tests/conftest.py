"""Test configuration and fixtures for the Smarter Dev project."""

from __future__ import annotations

import asyncio
import os
from typing import AsyncGenerator
from typing import Dict
from typing import Any
from unittest.mock import AsyncMock
from unittest.mock import Mock
import uuid

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


@pytest.fixture(scope="function")
def event_loop():
    """Create an instance of the default event loop for each test function."""
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        yield loop
    finally:
        try:
            # Cancel all pending tasks before closing
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            # Give tasks time to cancel
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        except Exception:
            pass  # Ignore cleanup errors
        finally:
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


@pytest.fixture(scope="function")
async def test_engine():
    """Create test database engine."""
    import tempfile
    import os
    import uuid
    
    # Create a unique temporary database file for complete isolation
    temp_dir = tempfile.mkdtemp()
    db_name = f"test_db_{uuid.uuid4().hex}.db"
    db_path = os.path.join(temp_dir, db_name)
    database_url = f"sqlite+aiosqlite:///{db_path}"
    
    engine = create_async_engine(
        database_url,
        poolclass=StaticPool,
        echo=False,
        future=True,
        connect_args={"check_same_thread": False},
    )
    
    try:
        # Create all tables
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        
        yield engine
        
    finally:
        # Clean up with proper async handling
        try:
            await engine.dispose()
            # Give time for cleanup to complete
            import asyncio
            await asyncio.sleep(0.05)
            # Clean up the temporary database file
            if os.path.exists(db_path):
                os.remove(db_path)
            if os.path.exists(temp_dir):
                os.rmdir(temp_dir)
        except Exception:
            pass  # Ignore disposal errors


@pytest.fixture
async def db_session(test_engine) -> AsyncGenerator[AsyncSession, None]:
    """Create a test database session with balanced isolation and usability."""
    # Recreate tables for each test to ensure isolation
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    
    # Use expire_on_commit=False for better test usability
    # We'll handle isolation through table recreation and session cleanup
    session_maker = async_sessionmaker(test_engine, expire_on_commit=False)
    
    session = session_maker()
    try:
        yield session
    finally:
        try:
            # Close and clean up session properly
            await session.close()
            # Give time for session cleanup to complete
            import asyncio
            await asyncio.sleep(0.01)
        except Exception:
            pass  # Ignore close errors


@pytest.fixture
async def isolated_db_session(test_engine) -> AsyncGenerator[AsyncSession, None]:
    """Create a strictly isolated database session for tests requiring maximum isolation."""
    # Recreate tables for each test to ensure isolation
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    
    # Use expire_on_commit=True for maximum isolation
    session_maker = async_sessionmaker(test_engine, expire_on_commit=True)
    
    session = session_maker()
    try:
        yield session
    finally:
        try:
            # Expunge all objects to clear identity map
            session.expunge_all()
            await session.close()
            # Give time for session cleanup to complete
            import asyncio
            await asyncio.sleep(0.01)
        except Exception:
            pass  # Ignore close errors


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


@pytest.fixture(scope="function")
def unique_guild_id() -> str:
    """Generate unique guild ID per test function."""
    return f"test_guild_{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="function")
def unique_user_id() -> str:
    """Generate unique user ID per test function."""
    return f"test_user_{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="function")
def unique_squad_id() -> str:
    """Generate unique squad ID per test function."""
    return f"test_squad_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def mock_bytes_service() -> Mock:
    """Create a properly configured BytesService mock with all expected attributes."""
    from smarter_dev.bot.services.bytes_service import BytesService
    
    mock_service = AsyncMock(spec=BytesService)
    
    # Pre-configure all statistic attributes that the service uses
    mock_service._cache_hits = 0
    mock_service._cache_misses = 0
    mock_service._balance_requests = 0
    mock_service._daily_claims = 0
    mock_service._transfers = 0
    mock_service._service_name = "BytesService"
    
    # Configure common method returns
    mock_service.initialize = AsyncMock()
    mock_service.close = AsyncMock()
    
    return mock_service


@pytest.fixture  
def mock_squads_service() -> Mock:
    """Create a properly configured SquadsService mock with all expected attributes."""
    from smarter_dev.bot.services.squads_service import SquadsService
    
    mock_service = AsyncMock(spec=SquadsService)
    
    # Pre-configure all statistic attributes that the service uses
    mock_service._squad_list_requests = 0
    mock_service._member_lookups = 0
    mock_service._join_attempts = 0
    mock_service._leave_attempts = 0
    mock_service._service_name = "SquadsService"
    
    # Configure common method returns
    mock_service.initialize = AsyncMock()
    mock_service.close = AsyncMock()
    
    return mock_service


@pytest.fixture
def mock_api_client() -> Mock:
    """Create a properly configured APIClient mock with all expected attributes."""
    from smarter_dev.bot.services.api_client import APIClient
    
    mock_client = AsyncMock(spec=APIClient)
    
    # Pre-configure all statistic attributes that the client uses
    mock_client._request_count = 0
    mock_client._error_count = 0
    mock_client._total_response_time = 0.0
    
    # Configure common method returns
    mock_client.close = AsyncMock()
    
    return mock_client


@pytest.fixture
def mock_cache_manager() -> Mock:
    """Create a properly configured CacheManager mock with all expected attributes."""
    from smarter_dev.bot.services.cache_manager import CacheManager
    
    mock_manager = AsyncMock(spec=CacheManager)
    
    # Pre-configure all statistic attributes
    mock_manager._cache_hits = 0
    mock_manager._cache_misses = 0
    mock_manager._operations = 0
    
    # Configure cache operations
    mock_manager.get = AsyncMock(return_value=None)
    mock_manager.set = AsyncMock(return_value=True)
    mock_manager.delete = AsyncMock(return_value=True)
    mock_manager.clear = AsyncMock(return_value=True)
    mock_manager.close = AsyncMock()
    
    return mock_manager


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
    from smarter_dev.web.api.app import api
    from httpx import ASGITransport
    from unittest.mock import patch
    
    # Mock database initialization to avoid conflicts
    with patch('smarter_dev.web.api.app.init_database'), \
         patch('smarter_dev.web.api.app.close_database'), \
         patch('smarter_dev.shared.config.get_settings', return_value=test_settings):
        
        # Set test settings on the app
        api.state.settings = test_settings
        
        try:
            async with AsyncClient(
                transport=ASGITransport(app=api),
                base_url="http://test"
            ) as client:
                yield client
        finally:
            # Clean up API state
            try:
                if hasattr(api, 'state'):
                    api.state.settings = None
            except Exception:
                pass


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