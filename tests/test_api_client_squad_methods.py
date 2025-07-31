"""Test missing squad API client methods using TDD approach."""

import pytest
from unittest.mock import AsyncMock, Mock

from bot.services.api_client import APIClient
from bot.config import BotConfig
from shared.exceptions import APIError


class TestAPIClientSquadMethods:
    """Test that API client has all required squad methods with proper interfaces."""

    @pytest.fixture
    def config(self):
        """Mock bot configuration."""
        config = Mock(spec=BotConfig)
        config.api_base_url = "http://localhost:8000/api/v1"
        config.api_key = "test-api-key"
        return config

    @pytest.fixture
    def api_client(self, config):
        """API client instance."""
        return APIClient(config)

    @pytest.fixture
    async def started_api_client(self, api_client):
        """Started API client with mocked httpx client."""
        await api_client.start()
        
        # Mock the underlying httpx client
        api_client._client = AsyncMock()
        
        yield api_client
        
        await api_client.stop()

    @pytest.mark.asyncio
    async def test_get_squad_info_method_exists(self, started_api_client):
        """Test that get_squad_info method exists and has correct interface."""
        # This test will fail until we implement the method
        assert hasattr(started_api_client, 'get_squad_info'), \
            "APIClient should have get_squad_info method"
        
        # Verify method signature by inspecting callable
        import inspect
        sig = inspect.signature(started_api_client.get_squad_info)
        params = list(sig.parameters.keys())
        
        # Should have guild_id and squad_id parameters
        assert 'guild_id' in params, "get_squad_info should accept guild_id parameter"
        assert 'squad_id' in params, "get_squad_info should accept squad_id parameter"

    @pytest.mark.asyncio
    async def test_get_squad_info_calls_correct_endpoint(self, started_api_client):
        """Test that get_squad_info calls the correct API endpoint."""
        # Mock successful response
        mock_response = {
            "id": "squad_123",
            "name": "Test Squad",
            "description": "A test squad",
            "role_id": "role_456",
            "switch_cost": 50,
            "is_active": True,
            "member_count": 5
        }
        
        started_api_client._client.request = AsyncMock()
        started_api_client._client.request.return_value.json.return_value = mock_response
        started_api_client._client.request.return_value.raise_for_status = Mock()
        
        # Call method (will fail until implemented)
        result = await started_api_client.get_squad_info("guild_123", "squad_456")
        
        # Verify correct endpoint was called
        started_api_client._client.request.assert_called_once_with(
            "GET", "/guilds/guild_123/squads/squad_456"
        )
        
        # Verify return value
        assert result == mock_response

    @pytest.mark.asyncio
    async def test_get_squad_members_paginated_method_exists(self, started_api_client):
        """Test that get_squad_members_paginated method exists."""
        assert hasattr(started_api_client, 'get_squad_members_paginated'), \
            "APIClient should have get_squad_members_paginated method"
        
        import inspect
        sig = inspect.signature(started_api_client.get_squad_members_paginated)
        params = list(sig.parameters.keys())
        
        # Should have required parameters
        assert 'guild_id' in params
        assert 'squad_id' in params
        assert 'limit' in params
        assert 'offset' in params

    @pytest.mark.asyncio
    async def test_get_squad_members_paginated_calls_correct_endpoint(self, started_api_client):
        """Test that get_squad_members_paginated calls correct endpoint with pagination."""
        mock_response = {
            "squad": {
                "id": "squad_123",
                "name": "Test Squad"
            },
            "members": [
                {"user_id": "user_1", "joined_at": "2024-01-01T00:00:00Z"},
                {"user_id": "user_2", "joined_at": "2024-01-02T00:00:00Z"}
            ],
            "total_count": 2,
            "page_info": {
                "limit": 10,
                "offset": 0,
                "has_more": False
            }
        }
        
        started_api_client._client.request = AsyncMock()
        started_api_client._client.request.return_value.json.return_value = mock_response
        started_api_client._client.request.return_value.raise_for_status = Mock()
        
        result = await started_api_client.get_squad_members_paginated(
            "guild_123", "squad_456", limit=10, offset=0
        )
        
        # Verify correct endpoint with query parameters
        started_api_client._client.request.assert_called_once_with(
            "GET", "/guilds/guild_123/squads/squad_456/members/paginated",
            params={"limit": 10, "offset": 0}
        )
        
        assert result == mock_response

    @pytest.mark.asyncio
    async def test_assign_user_role_method_exists(self, started_api_client):
        """Test that assign_user_role method exists."""
        assert hasattr(started_api_client, 'assign_user_role'), \
            "APIClient should have assign_user_role method"
        
        import inspect
        sig = inspect.signature(started_api_client.assign_user_role)
        params = list(sig.parameters.keys())
        
        assert 'guild_id' in params
        assert 'user_id' in params
        assert 'role_id' in params

    @pytest.mark.asyncio
    async def test_assign_user_role_calls_correct_endpoint(self, started_api_client):
        """Test that assign_user_role calls correct endpoint."""
        mock_response = {"success": True}
        
        started_api_client._client.request = AsyncMock()
        started_api_client._client.request.return_value.json.return_value = mock_response
        started_api_client._client.request.return_value.raise_for_status = Mock()
        
        result = await started_api_client.assign_user_role(
            "guild_123", "user_456", "role_789"
        )
        
        # Should POST to role assignment endpoint
        started_api_client._client.request.assert_called_once_with(
            "POST", "/guilds/guild_123/users/user_456/roles/role_789/assign"
        )
        
        assert result == mock_response

    @pytest.mark.asyncio
    async def test_remove_user_role_method_exists(self, started_api_client):
        """Test that remove_user_role method exists."""
        assert hasattr(started_api_client, 'remove_user_role'), \
            "APIClient should have remove_user_role method"
        
        import inspect
        sig = inspect.signature(started_api_client.remove_user_role)
        params = list(sig.parameters.keys())
        
        assert 'guild_id' in params
        assert 'user_id' in params
        assert 'role_id' in params

    @pytest.mark.asyncio
    async def test_remove_user_role_calls_correct_endpoint(self, started_api_client):
        """Test that remove_user_role calls correct endpoint."""
        mock_response = {"success": True}
        
        started_api_client._client.request = AsyncMock()
        started_api_client._client.request.return_value.json.return_value = mock_response
        started_api_client._client.request.return_value.raise_for_status = Mock()
        
        result = await started_api_client.remove_user_role(
            "guild_123", "user_456", "role_789"
        )
        
        # Should POST to role removal endpoint
        started_api_client._client.request.assert_called_once_with(
            "POST", "/guilds/guild_123/users/user_456/roles/role_789/remove"
        )
        
        assert result == mock_response

    @pytest.mark.asyncio
    async def test_squad_methods_error_handling(self, started_api_client):
        """Test that squad methods properly handle API errors."""
        # Mock HTTP error
        started_api_client._client.request = AsyncMock()
        started_api_client._client.request.side_effect = Exception("Network error")
        
        # All methods should raise APIError on failure
        with pytest.raises(APIError):
            await started_api_client.get_squad_info("guild_123", "squad_456")
        
        with pytest.raises(APIError):
            await started_api_client.get_squad_members_paginated(
                "guild_123", "squad_456", 10, 0
            )
        
        with pytest.raises(APIError):
            await started_api_client.assign_user_role("guild_123", "user_456", "role_789")
        
        with pytest.raises(APIError):
            await started_api_client.remove_user_role("guild_123", "user_456", "role_789")

    @pytest.mark.asyncio
    async def test_squad_methods_return_types(self, started_api_client):
        """Test that squad methods return correct data types."""
        # Mock responses
        started_api_client._client.request = AsyncMock()
        started_api_client._client.request.return_value.json.return_value = {"test": "data"}
        started_api_client._client.request.return_value.raise_for_status = Mock()
        
        # All methods should return dict from JSON response
        result1 = await started_api_client.get_squad_info("guild_123", "squad_456")
        assert isinstance(result1, dict)
        
        result2 = await started_api_client.get_squad_members_paginated(
            "guild_123", "squad_456", 10, 0
        )
        assert isinstance(result2, dict)
        
        result3 = await started_api_client.assign_user_role("guild_123", "user_456", "role_789")
        assert isinstance(result3, dict)
        
        result4 = await started_api_client.remove_user_role("guild_123", "user_456", "role_789")
        assert isinstance(result4, dict)