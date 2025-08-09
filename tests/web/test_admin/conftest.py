"""Test fixtures for admin interface tests."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, Mock, patch
from typing import Dict, Any, List, AsyncGenerator

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.routing import Mount, Router
from starlette.testclient import TestClient
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from smarter_dev.web.admin.routes import admin_routes
from smarter_dev.web.admin.discord import DiscordGuild, DiscordRole
from smarter_dev.shared.config import override_settings
from smarter_dev.shared.database import Base, async_sessionmaker
from smarter_dev.web.api.app import api


@pytest.fixture
def admin_app():
    """Create a test Starlette app with admin routes."""
    middleware = [
        Middleware(
            SessionMiddleware,
            secret_key="test-secret-key",
            max_age=86400,
        )
    ]
    
    app = Starlette(
        routes=[Mount("/admin", Mount("", routes=admin_routes))],
        middleware=middleware,
    )
    
    return app


@pytest.fixture
def admin_client(admin_app):
    """Create a test client for admin interface."""
    return TestClient(admin_app)


@pytest.fixture
async def admin_async_client(admin_app):
    """Create an async test client for admin interface."""
    async with AsyncClient(app=admin_app, base_url="http://testserver") as client:
        yield client


@pytest.fixture
def authenticated_client(admin_client, mock_settings):
    """Create an authenticated admin client."""
    # Mock the admin_required decorator to always pass authentication
    def mock_admin_required(func):
        """Mock admin_required decorator that always allows access."""
        return func
    
    with patch("smarter_dev.web.admin.auth.admin_required", side_effect=mock_admin_required):
        # Also mock the session check for any direct session access
        with patch.object(admin_client, "session", {"is_admin": True, "discord_user": {"id": "123456789", "username": "testadmin"}}):
            yield admin_client


@pytest.fixture
async def authenticated_async_client(admin_async_client):
    """Create an authenticated async admin client."""
    # Note: Session handling for async client is more complex
    # This would need proper session middleware setup
    return admin_async_client


@pytest.fixture
def mock_discord_guilds() -> List[DiscordGuild]:
    """Mock Discord guild data."""
    return [
        DiscordGuild(
            id="123456789012345678",
            name="Test Guild 1",
            icon="test_icon_1",
            owner_id="owner123",
            member_count=100,
            description="Test guild for unit tests"
        ),
        DiscordGuild(
            id="234567890123456789",
            name="Test Guild 2",
            icon=None,
            owner_id="owner456",
            member_count=50,
            description=None
        )
    ]


@pytest.fixture
def mock_discord_roles() -> List[DiscordRole]:
    """Mock Discord role data."""
    return [
        DiscordRole(
            id="role123",
            name="Admin",
            color=0xFF0000,
            position=10,
            permissions="8",
            managed=False,
            mentionable=True
        ),
        DiscordRole(
            id="role456",
            name="Member",
            color=0x00FF00,
            position=5,
            permissions="1024",
            managed=False,
            mentionable=True
        ),
        DiscordRole(
            id="role789",
            name="Bot",
            color=0x0000FF,
            position=15,
            permissions="8",
            managed=True,
            mentionable=False
        )
    ]


@pytest.fixture
def mock_discord_client(mock_discord_guilds, mock_discord_roles):
    """Mock Discord client with test data."""
    client = AsyncMock()
    client.get_bot_guilds.return_value = mock_discord_guilds
    client.get_guild.return_value = mock_discord_guilds[0]
    client.get_guild_roles.return_value = mock_discord_roles
    client.get_guild_member_count.return_value = 100
    
    return client


@pytest.fixture
def mock_discord_api(mock_discord_client):
    """Mock Discord API functions."""
    # Patch get_discord_client to return mock without affecting global state
    with patch("smarter_dev.web.admin.discord.get_discord_client") as mock_get_client:
        mock_get_client.return_value = mock_discord_client
        yield mock_discord_client


@pytest.fixture(autouse=True)
def reset_discord_global_state():
    """Reset Discord global state before each test."""
    import smarter_dev.web.admin.discord
    smarter_dev.web.admin.discord._discord_client = None
    smarter_dev.web.admin.discord._guild_cache = {}
    smarter_dev.web.admin.discord._cache_expiry = 0
    yield
    # Cleanup after test
    smarter_dev.web.admin.discord._discord_client = None
    smarter_dev.web.admin.discord._guild_cache = {}
    smarter_dev.web.admin.discord._cache_expiry = 0


@pytest.fixture
def mock_database():
    """Mock database operations."""
    with patch("smarter_dev.web.admin.views.get_db_session_context") as mock_session:
        mock_session_instance = AsyncMock()
        mock_session.__aenter__.return_value = mock_session_instance
        mock_session.__aexit__.return_value = None
        
        # Mock database query results
        mock_session_instance.execute.return_value = Mock(
            scalar=Mock(return_value=10),
            scalars=Mock(return_value=Mock(all=Mock(return_value=[]))),
            first=Mock(return_value=Mock(
                total_users=10,
                total_balance=1000,
                total_transactions=50
            ))
        )
        
        yield mock_session_instance


@pytest.fixture
def mock_bytes_operations():
    """Mock bytes operations."""
    with patch("smarter_dev.web.admin.views.BytesOperations") as mock_ops:
        mock_instance = AsyncMock()
        mock_ops.return_value = mock_instance
        
        # Mock return values
        mock_instance.get_leaderboard.return_value = []
        mock_instance.get_config.return_value = None
        mock_instance.update_config.return_value = Mock()
        
        yield mock_instance


@pytest.fixture
def mock_squad_operations():
    """Mock squad operations."""
    with patch("smarter_dev.web.admin.views.SquadOperations") as mock_ops:
        mock_instance = AsyncMock()
        mock_ops.return_value = mock_instance
        
        # Mock return values
        mock_instance.list_squads.return_value = []
        mock_instance.create_squad.return_value = Mock()
        mock_instance.update_squad.return_value = Mock()
        mock_instance.delete_squad.return_value = Mock()
        
        yield mock_instance


@pytest.fixture
def mock_settings():
    """Mock application settings."""
    with patch("smarter_dev.web.admin.auth.get_settings") as mock_settings:
        mock_config = Mock()
        mock_config.is_development = True
        mock_config.admin_username = "admin"
        mock_config.admin_password = "password"
        mock_config.discord_bot_token = "test_token"
        
        mock_settings.return_value = mock_config
        yield mock_config


@pytest.fixture
def sample_form_data() -> Dict[str, Any]:
    """Sample form data for testing."""
    return {
        "starting_balance": "100",
        "daily_amount": "10",
        "max_transfer": "1000",
        "transfer_cooldown_hours": "0",
        "streak_7_bonus": "2",
        "streak_14_bonus": "4",
        "streak_30_bonus": "10",
        "streak_60_bonus": "20"
    }


@pytest.fixture
def sample_squad_data() -> Dict[str, Any]:
    """Sample squad data for testing."""
    return {
        "action": "create",
        "name": "Test Squad",
        "description": "A test squad",
        "role_id": "role123",
        "switch_cost": "50",
        "max_members": "10"
    }


@pytest.fixture
def sample_forum_agent_data() -> Dict[str, Any]:
    """Sample forum agent data for testing."""
    return {
        "name": "Python Helper",
        "system_prompt": "You are a helpful Python programming assistant. Help users with Python-related questions, provide code examples, and explain programming concepts clearly.",
        "monitored_forums": ["123456789012345678", "234567890123456789"],
        "response_threshold": "0.7",
        "max_responses_per_hour": "5"
    }


@pytest.fixture
def mock_forum_agents():
    """Mock forum agent data for testing."""
    from uuid import uuid4
    from unittest.mock import Mock
    
    agents = []
    
    # Create mock forum agents
    agent1 = Mock()
    agent1.id = uuid4()
    agent1.guild_id = "123456789012345678"
    agent1.name = "Python Helper"
    agent1.system_prompt = "You are a helpful Python programming assistant."
    agent1.monitored_forums = ["123456789012345678", "234567890123456789"]
    agent1.response_threshold = 0.7
    agent1.max_responses_per_hour = 5
    agent1.is_active = True
    agent1.created_at = None
    agent1.updated_at = None
    agents.append(agent1)
    
    agent2 = Mock()
    agent2.id = uuid4()
    agent2.guild_id = "123456789012345678"
    agent2.name = "Code Reviewer"
    agent2.system_prompt = "You are a code review assistant. Review code for best practices."
    agent2.monitored_forums = ["123456789012345678"]
    agent2.response_threshold = 0.8
    agent2.max_responses_per_hour = 3
    agent2.is_active = False
    agent2.created_at = None
    agent2.updated_at = None
    agents.append(agent2)
    
    return agents


@pytest.fixture 
def mock_forum_responses(mock_forum_agents):
    """Mock forum agent response data for testing."""
    from uuid import uuid4
    from unittest.mock import Mock
    from datetime import datetime, timezone
    
    responses = []
    
    for i, agent in enumerate(mock_forum_agents):
        # Create multiple responses per agent
        for j in range(3):
            response = Mock()
            response.id = uuid4()
            response.forum_agent_id = agent.id
            response.channel_id = f"channel_{i}_{j}"
            response.thread_id = f"thread_{i}_{j}"
            response.post_title = f"Test Question {i}_{j}"
            response.post_content = f"This is test post content {i}_{j}"
            response.author_display_name = f"TestUser{i}_{j}"
            response.post_tags = ["python", "help"]
            response.attachments = []
            response.decision_reason = f"This question matches my expertise area {i}_{j}"
            response.confidence_score = 0.85 + (j * 0.02)
            response.response_content = f"Test response content {i}_{j}"
            response.tokens_used = 250 + (j * 50)
            response.response_time_ms = 800 + (j * 100)
            response.responded = j % 2 == 0  # Alternate responded status
            response.created_at = datetime.now(timezone.utc)
            responses.append(response)
    
    return responses


@pytest.fixture
def mock_forum_operations():
    """Mock forum agent operations."""
    with patch("smarter_dev.web.admin.views.ForumAgentOperations") as mock_ops:
        mock_instance = AsyncMock()
        mock_ops.return_value = mock_instance
        
        # Mock return values
        mock_instance.list_agents.return_value = []
        mock_instance.create_agent.return_value = Mock()
        mock_instance.get_agent.return_value = None
        mock_instance.update_agent.return_value = Mock()
        mock_instance.delete_agent.return_value = Mock()
        mock_instance.get_agent_analytics.return_value = {}
        
        yield mock_instance


@pytest.fixture
async def admin_auth_headers(real_db_session) -> dict[str, str]:
    """Create admin authentication headers for API testing."""
    # For now, create a special admin API key
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
         patch('smarter_dev.web.api.app.init_database'), \
         patch('smarter_dev.web.api.app.close_database'):
        
        # Override settings in the API app
        api.state.settings = admin_api_settings
        
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