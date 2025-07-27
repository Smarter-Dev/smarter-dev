"""Test fixtures for admin interface tests."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, Mock, patch
from typing import Dict, Any, List

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.routing import Mount, Router
from starlette.testclient import TestClient
from httpx import AsyncClient

from smarter_dev.web.admin.routes import admin_routes
from smarter_dev.web.admin.discord import DiscordGuild, DiscordRole


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
    # Perform a login to establish session
    with patch("smarter_dev.web.admin.auth.get_settings", return_value=mock_settings):
        response = admin_client.post("/admin/login", data={
            "username": "admin",
            "password": "password"
        }, follow_redirects=False)
        
        # Verify login was successful
        assert response.status_code == 303
    
    return admin_client


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