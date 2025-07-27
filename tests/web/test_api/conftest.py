"""Test configuration and fixtures specifically for API testing."""

from __future__ import annotations

from typing import AsyncGenerator, Dict, Any
from unittest.mock import Mock, AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from smarter_dev.shared.config import override_settings
from smarter_dev.shared.database import Base, async_sessionmaker
from smarter_dev.web.api.app import api


@pytest.fixture(scope="function")
def api_settings():
    """Create API-specific test settings."""
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
async def real_db_engine(api_settings):
    """Create a fresh isolated database engine for each test."""
    import tempfile
    import os
    import uuid
    
    # Create a unique temporary database file for complete isolation
    temp_dir = tempfile.mkdtemp()
    db_name = f"test_db_{uuid.uuid4().hex}.db"
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
    """Create a clean database session for each test."""
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


@pytest.fixture
async def api_client(api_settings) -> AsyncGenerator[AsyncClient, None]:
    """Create an async HTTP client for API testing (with mocked database)."""
    # Mock database initialization to avoid connection issues in tests
    with patch('smarter_dev.web.api.app.init_database'), \
         patch('smarter_dev.web.api.app.close_database'), \
         patch('smarter_dev.shared.config.get_settings', return_value=api_settings), \
         patch('smarter_dev.web.api.dependencies.get_settings', return_value=api_settings), \
         patch('smarter_dev.web.api.routers.auth.get_settings', return_value=api_settings):
        
        # Override settings in the API app
        api.state.settings = api_settings
        
        try:
            async with AsyncClient(
                transport=ASGITransport(app=api),
                base_url="http://test"
            ) as client:
                yield client
        finally:
            # Ensure API state cleanup
            try:
                if hasattr(api, 'state'):
                    api.state.settings = None
            except Exception:
                pass  # Ignore cleanup errors


@pytest.fixture(scope="function")
async def real_api_client(api_settings, real_db_engine) -> AsyncGenerator[AsyncClient, None]:
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
    with patch('smarter_dev.shared.config.get_settings', return_value=api_settings), \
         patch('smarter_dev.shared.config.settings', api_settings), \
         patch('smarter_dev.web.api.dependencies.get_settings', return_value=api_settings), \
         patch('smarter_dev.shared.database.get_engine', side_effect=mock_get_engine), \
         patch('smarter_dev.shared.database.get_session_maker', side_effect=mock_get_session_maker), \
         patch('smarter_dev.shared.database.get_db_session', side_effect=mock_get_db_session), \
         patch('smarter_dev.web.api.app.init_database'), \
         patch('smarter_dev.web.api.app.close_database'):
        
        # Override settings in the API app
        api.state.settings = api_settings
        
        try:
            async with AsyncClient(
                transport=ASGITransport(app=api),
                base_url="http://test"
            ) as client:
                yield client
        finally:
            # Ensure API state cleanup
            try:
                if hasattr(api, 'state'):
                    api.state.settings = None
            except Exception:
                pass  # Ignore cleanup errors


@pytest.fixture(scope="function")
def bot_headers():
    """Valid bot authentication headers."""
    return {"Authorization": "Bearer test_bot_token_12345"}


@pytest.fixture
def invalid_headers():
    """Invalid authentication headers."""
    return {"Authorization": "Bearer invalid_token"}


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
        mock_instance.get_or_create_balance = AsyncMock()
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