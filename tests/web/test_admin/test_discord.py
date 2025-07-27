"""Tests for Discord REST API client."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, Mock, patch
import httpx

from smarter_dev.web.admin.discord import (
    DiscordClient,
    DiscordGuild,
    DiscordRole,
    DiscordAPIError,
    GuildNotFoundError,
    UnauthorizedError,
    get_discord_client,
    get_bot_guilds,
    get_guild_info,
    get_guild_roles
)


class TestDiscordClient:
    """Test suite for Discord API client."""
    
    def test_discord_client_initialization(self):
        """Test Discord client initialization."""
        client = DiscordClient("test_token")
        
        assert client.bot_token == "test_token"
        assert client.base_url == "https://discord.com/api/v10"
        assert client.headers["Authorization"] == "Bot test_token"
        assert "SmarterDev-AdminInterface" in client.headers["User-Agent"]
    
    @pytest.mark.asyncio
    async def test_make_request_success(self):
        """Test successful API request."""
        client = DiscordClient("test_token")
        
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"id": "123", "name": "Test"}
            
            # Create a proper async context manager mock
            mock_client_instance = Mock()
            mock_client_instance.request = AsyncMock(return_value=mock_response)
            
            # Configure the class and context manager properly
            async def mock_aenter(self):
                return mock_client_instance
            async def mock_aexit(self, exc_type, exc_val, exc_tb):
                return None
            mock_client_class.return_value.__aenter__ = mock_aenter
            mock_client_class.return_value.__aexit__ = mock_aexit
            
            result = await client._make_request("GET", "/test")
            
            assert result == {"id": "123", "name": "Test"}
    
    @pytest.mark.asyncio
    async def test_make_request_404_error(self):
        """Test API request with 404 error."""
        client = DiscordClient("test_token")
        
        with patch("httpx.AsyncClient") as mock_client_class:
            # Create proper response mock - avoid AsyncMock in return_value chain
            mock_response = Mock()
            mock_response.status_code = 404
            mock_response.content = b'{"message": "Not Found"}'
            mock_response.json.return_value = {"message": "Not Found"}
            
            # Create async context manager mock with proper awaitable
            mock_client_instance = AsyncMock()
            mock_client_instance.request.return_value = mock_response
            
            # Configure the class and context manager properly
            mock_client_class.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_class.return_value.__aexit__ = AsyncMock(return_value=None)
            
            with pytest.raises(GuildNotFoundError, match="Resource not found"):
                await client._make_request("GET", "/test")
    
    @pytest.mark.asyncio
    async def test_make_request_unauthorized_error(self):
        """Test API request with unauthorized error."""
        client = DiscordClient("test_token")
        
        with patch("httpx.AsyncClient") as mock_client_class:
            # Create proper response mock - avoid AsyncMock in return_value chain
            mock_response = Mock()
            mock_response.status_code = 401
            mock_response.content = b'{"message": "Unauthorized"}'
            mock_response.json.return_value = {"message": "Unauthorized"}
            
            # Create async context manager mock with proper awaitable
            mock_client_instance = AsyncMock()
            mock_client_instance.request.return_value = mock_response
            
            # Configure the class and context manager properly
            mock_client_class.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_class.return_value.__aexit__ = AsyncMock(return_value=None)
            
            with pytest.raises(UnauthorizedError, match="Unauthorized access"):
                await client._make_request("GET", "/test")
    
    @pytest.mark.asyncio
    async def test_make_request_timeout_error(self):
        """Test API request with timeout error."""
        client = DiscordClient("test_token")
        
        with patch("httpx.AsyncClient") as mock_client_class:
            # Create a proper async context manager mock
            mock_client_instance = Mock()
            mock_client_instance.request = AsyncMock(side_effect=httpx.TimeoutException("Timeout"))
            
            # Configure the class and context manager properly
            async def mock_aenter(self):
                return mock_client_instance
            async def mock_aexit(self, exc_type, exc_val, exc_tb):
                return None
            mock_client_class.return_value.__aenter__ = mock_aenter
            mock_client_class.return_value.__aexit__ = mock_aexit
            
            with pytest.raises(DiscordAPIError, match="Request to Discord API timed out"):
                await client._make_request("GET", "/test")
    
    @pytest.mark.asyncio
    async def test_get_bot_guilds_success(self):
        """Test successful bot guilds retrieval."""
        client = DiscordClient("test_token")
        
        mock_data = [
            {
                "id": "123",
                "name": "Test Guild",
                "icon": "icon_hash",
                "owner_id": "456",
                "description": "Test description"
            }
        ]
        
        with patch.object(client, "_make_request", return_value=mock_data):
            guilds = await client.get_bot_guilds()
            
            assert len(guilds) == 1
            assert isinstance(guilds[0], DiscordGuild)
            assert guilds[0].id == "123"
            assert guilds[0].name == "Test Guild"
            assert guilds[0].icon == "icon_hash"
            assert guilds[0].owner_id == "unknown"  # Not available in /users/@me/guilds endpoint
            assert guilds[0].description is None  # Not available in /users/@me/guilds endpoint
    
    @pytest.mark.asyncio
    async def test_get_guild_success(self):
        """Test successful guild retrieval."""
        client = DiscordClient("test_token")
        
        mock_data = {
            "id": "123",
            "name": "Test Guild",
            "icon": "icon_hash",
            "owner_id": "456",
            "approximate_member_count": 100,
            "description": "Test description"
        }
        
        with patch.object(client, "_make_request", return_value=mock_data):
            guild = await client.get_guild("123")
            
            assert isinstance(guild, DiscordGuild)
            assert guild.id == "123"
            assert guild.name == "Test Guild"
            assert guild.member_count == 100
    
    @pytest.mark.asyncio
    async def test_get_guild_roles_success(self):
        """Test successful guild roles retrieval."""
        client = DiscordClient("test_token")
        
        mock_data = [
            {
                "id": "role1",
                "name": "Admin",
                "color": 16711680,
                "position": 10,
                "permissions": "8",
                "managed": False,
                "mentionable": True
            },
            {
                "id": "role2",
                "name": "Member",
                "color": 65280,
                "position": 5,
                "permissions": "1024",
                "managed": False,
                "mentionable": True
            }
        ]
        
        with patch.object(client, "_make_request", return_value=mock_data):
            roles = await client.get_guild_roles("123")
            
            assert len(roles) == 2
            assert isinstance(roles[0], DiscordRole)
            # Should be sorted by position (highest first)
            assert roles[0].position == 10
            assert roles[1].position == 5
    
    @pytest.mark.asyncio
    async def test_get_guild_member_count_success(self):
        """Test successful guild member count retrieval."""
        client = DiscordClient("test_token")
        
        mock_guild = DiscordGuild(
            id="123",
            name="Test Guild",
            icon=None,
            owner_id="456",
            member_count=100
        )
        
        with patch.object(client, "get_guild", return_value=mock_guild):
            count = await client.get_guild_member_count("123")
            
            assert count == 100


class TestDiscordDataClasses:
    """Test suite for Discord data classes."""
    
    def test_discord_guild_icon_url(self):
        """Test DiscordGuild icon URL property."""
        guild = DiscordGuild(
            id="123",
            name="Test Guild",
            icon="icon_hash",
            owner_id="456"
        )
        
        expected_url = "https://cdn.discordapp.com/icons/123/icon_hash.png"
        assert guild.icon_url == expected_url
    
    def test_discord_guild_icon_url_none(self):
        """Test DiscordGuild icon URL with no icon."""
        guild = DiscordGuild(
            id="123",
            name="Test Guild",
            icon=None,
            owner_id="456"
        )
        
        assert guild.icon_url is None
    
    def test_discord_role_color_hex(self):
        """Test DiscordRole color hex property."""
        role = DiscordRole(
            id="role1",
            name="Admin",
            color=16711680,  # Red
            position=10,
            permissions="8",
            managed=False,
            mentionable=True
        )
        
        assert role.color_hex == "#ff0000"
    
    def test_discord_role_color_hex_default(self):
        """Test DiscordRole color hex with default color."""
        role = DiscordRole(
            id="role1",
            name="Admin",
            color=0,  # No color
            position=10,
            permissions="8",
            managed=False,
            mentionable=True
        )
        
        assert role.color_hex == "#99aab5"


class TestDiscordGlobalFunctions:
    """Test suite for global Discord functions."""
    
    @patch("smarter_dev.web.admin.discord.get_settings")
    def test_get_discord_client_success(self, mock_settings):
        """Test successful Discord client creation."""
        mock_settings.return_value.discord_bot_token = "test_token"
        
        client = get_discord_client()
        
        assert isinstance(client, DiscordClient)
        assert client.bot_token == "test_token"
    
    @patch("smarter_dev.web.admin.discord.get_settings")
    def test_get_discord_client_no_token(self, mock_settings):
        """Test Discord client creation without token."""
        mock_settings.return_value.discord_bot_token = ""
        
        with pytest.raises(DiscordAPIError, match="Discord bot token not configured"):
            get_discord_client()
    
    @pytest.mark.asyncio
    @patch("smarter_dev.web.admin.discord.get_discord_client")
    async def test_get_bot_guilds_convenience_function(self, mock_get_client):
        """Test get_bot_guilds convenience function."""
        mock_client = AsyncMock()
        mock_client.get_bot_guilds.return_value = []
        mock_get_client.return_value = mock_client
        
        result = await get_bot_guilds()
        
        assert result == []
        mock_client.get_bot_guilds.assert_called_once()
    
    @pytest.mark.asyncio
    @patch("smarter_dev.web.admin.discord.get_discord_client")
    async def test_get_guild_info_convenience_function(self, mock_get_client):
        """Test get_guild_info convenience function."""
        mock_client = AsyncMock()
        mock_guild = DiscordGuild(id="123", name="Test", icon=None, owner_id="456")
        mock_client.get_guild.return_value = mock_guild
        mock_get_client.return_value = mock_client
        
        result = await get_guild_info("123")
        
        assert result == mock_guild
        mock_client.get_guild.assert_called_once_with("123")
    
    @pytest.mark.asyncio
    @patch("smarter_dev.web.admin.discord.get_discord_client")
    async def test_get_guild_roles_convenience_function(self, mock_get_client):
        """Test get_guild_roles convenience function."""
        mock_client = AsyncMock()
        mock_client.get_guild_roles.return_value = []
        mock_get_client.return_value = mock_client
        
        result = await get_guild_roles("123")
        
        assert result == []
        mock_client.get_guild_roles.assert_called_once_with("123")