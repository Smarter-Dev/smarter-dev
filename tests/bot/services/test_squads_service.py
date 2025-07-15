"""Tests for SquadsService.

This module provides comprehensive tests for the SquadsService including
squad listing, membership operations, join/leave functionality, and error handling.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest

from smarter_dev.bot.services.exceptions import (
    APIError,
    NotInSquadError,
    ResourceNotFoundError,
    ServiceError,
    ValidationError
)
from smarter_dev.bot.services.models import (
    JoinSquadResult,
    Squad,
    SquadMember,
    UserSquadResponse
)
from smarter_dev.bot.services.squads_service import SquadsService


class TestSquadsServiceListing:
    """Test squad listing operations."""
    
    async def test_list_squads_success(
        self,
        squads_service,
        mock_api_client,
        test_guild_id,
        squads_list_api_response
    ):
        """Test successful squad listing."""
        # Mock API response
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = Mock(return_value=squads_list_api_response)
        mock_api_client.get.return_value = mock_response
        
        # Call service
        squads = await squads_service.list_squads(test_guild_id)
        
        # Verify result
        assert len(squads) == 2
        assert all(isinstance(squad, Squad) for squad in squads)
        
        # Check first squad
        first_squad = squads[0]
        assert first_squad.name == "Test Squad"
        assert first_squad.switch_cost == 100
        assert first_squad.max_members == 20
        assert first_squad.member_count == 5
        assert first_squad.is_active is True
        
        # Verify API call
        mock_api_client.get.assert_called_once_with(
            f"/guilds/{test_guild_id}/squads",
            params={},
            timeout=10.0
        )
    
    async def test_list_squads_include_inactive(
        self,
        squads_service,
        mock_api_client,
        test_guild_id,
        squads_list_api_response
    ):
        """Test squad listing including inactive squads."""
        # Mock API response
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = Mock(return_value=squads_list_api_response)
        mock_api_client.get.return_value = mock_response
        
        # Call service with include_inactive=True
        squads = await squads_service.list_squads(test_guild_id, include_inactive=True)
        
        # Verify result
        assert len(squads) == 2
        
        # Verify API call with correct parameter
        mock_api_client.get.assert_called_once_with(
            f"/guilds/{test_guild_id}/squads",
            params={"include_inactive": "true"},
            timeout=10.0
        )
    
    async def test_list_squads_with_caching(
        self,
        squads_service,
        mock_api_client,
        mock_cache_manager,
        test_guild_id,
        squads_list_api_response
    ):
        """Test squad listing uses caching correctly."""
        # Mock API response
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = Mock(return_value=squads_list_api_response)
        mock_api_client.get.return_value = mock_response
        
        # First call - should hit API and cache result
        squads1 = await squads_service.list_squads(test_guild_id)
        
        # Verify API was called and cache was set
        assert mock_api_client.get.call_count == 1
        assert mock_cache_manager.set.call_count == 1
        
        # Mock cache hit for second call
        mock_cache_manager.get.return_value = squads_list_api_response
        
        # Second call - should hit cache
        squads2 = await squads_service.list_squads(test_guild_id)
        
        # Verify API was not called again
        assert mock_api_client.get.call_count == 1
        assert mock_cache_manager.get.call_count == 2
        
        # Results should be equivalent
        assert len(squads1) == len(squads2)
        assert squads1[0].name == squads2[0].name
    
    async def test_list_squads_invalid_guild_id(self, squads_service):
        """Test squad listing with invalid guild ID."""
        # Empty guild ID
        with pytest.raises(ValidationError) as exc_info:
            await squads_service.list_squads("")
        assert exc_info.value.field == "guild_id"
        
        # Whitespace-only guild ID
        with pytest.raises(ValidationError) as exc_info:
            await squads_service.list_squads("   ")
        assert exc_info.value.field == "guild_id"
    
    async def test_list_squads_api_error(
        self,
        squads_service,
        mock_api_client,
        test_guild_id
    ):
        """Test squad listing with API error."""
        # Mock API error
        mock_response = AsyncMock()
        mock_response.status_code = 500
        mock_response.json = Mock(return_value={"detail": "Server error"})
        mock_api_client.get.return_value = mock_response
        
        # Should raise APIError
        with pytest.raises(APIError, match="Server error"):
            await squads_service.list_squads(test_guild_id)


class TestSquadsServiceGetSquad:
    """Test individual squad retrieval."""
    
    async def test_get_squad_success(
        self,
        squads_service,
        mock_api_client,
        test_guild_id,
        test_squad_id,
        squad_api_response
    ):
        """Test successful squad retrieval."""
        # Mock API response
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = Mock(return_value=squad_api_response)
        mock_api_client.get.return_value = mock_response
        
        # Call service
        squad = await squads_service.get_squad(test_guild_id, test_squad_id)
        
        # Verify result
        assert isinstance(squad, Squad)
        assert squad.id == test_squad_id
        assert squad.name == "Test Squad"
        assert squad.switch_cost == 100
        
        # Verify API call
        mock_api_client.get.assert_called_once_with(
            f"/guilds/{test_guild_id}/squads/{test_squad_id}",
            timeout=10.0
        )
    
    async def test_get_squad_not_found(
        self,
        squads_service,
        mock_api_client,
        test_guild_id,
        test_squad_id
    ):
        """Test squad retrieval for non-existent squad."""
        # Mock 404 response
        mock_response = AsyncMock()
        mock_response.status_code = 404
        mock_api_client.get.return_value = mock_response
        
        # Should raise ResourceNotFoundError
        with pytest.raises(ResourceNotFoundError) as exc_info:
            await squads_service.get_squad(test_guild_id, test_squad_id)
        
        assert exc_info.value.resource_type == "squad"
        assert str(test_squad_id) in exc_info.value.resource_id
    
    async def test_get_squad_invalid_inputs(self, squads_service):
        """Test squad retrieval with invalid inputs."""
        # Empty guild ID
        with pytest.raises(ValidationError) as exc_info:
            await squads_service.get_squad("", uuid4())
        assert exc_info.value.field == "guild_id"
        
        # None squad ID
        with pytest.raises(ValidationError) as exc_info:
            await squads_service.get_squad("guild_id", None)
        assert exc_info.value.field == "squad_id"


class TestSquadsServiceUserSquad:
    """Test user squad membership operations."""
    
    async def test_get_user_squad_success(
        self,
        squads_service,
        mock_api_client,
        test_guild_id,
        test_user_id,
        squad_api_response
    ):
        """Test successful user squad retrieval."""
        # Mock API response with squad data
        api_response = {
            "squad": squad_api_response,
            "member_since": "2024-01-10T12:00:00Z"
        }
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = Mock(return_value=api_response)
        mock_api_client.get.return_value = mock_response
        
        # Call service
        user_squad = await squads_service.get_user_squad(test_guild_id, test_user_id)
        
        # Verify result
        assert isinstance(user_squad, UserSquadResponse)
        assert user_squad.user_id == test_user_id
        assert user_squad.is_in_squad is True
        assert user_squad.squad is not None
        assert user_squad.squad.name == "Test Squad"
        assert user_squad.member_since is not None
        
        # Verify API call
        mock_api_client.get.assert_called_once_with(
            f"/guilds/{test_guild_id}/squads/members/{test_user_id}",
            timeout=10.0
        )
    
    async def test_get_user_squad_not_in_squad(
        self,
        squads_service,
        mock_api_client,
        test_guild_id,
        test_user_id
    ):
        """Test user squad retrieval when user is not in any squad."""
        # Mock 404 response (user not in any squad)
        mock_response = AsyncMock()
        mock_response.status_code = 404
        mock_api_client.get.return_value = mock_response
        
        # Call service
        user_squad = await squads_service.get_user_squad(test_guild_id, test_user_id)
        
        # Verify result
        assert isinstance(user_squad, UserSquadResponse)
        assert user_squad.user_id == test_user_id
        assert user_squad.is_in_squad is False
        assert user_squad.squad is None
        assert user_squad.member_since is None
    
    async def test_get_user_squad_with_caching(
        self,
        squads_service,
        mock_api_client,
        mock_cache_manager,
        test_guild_id,
        test_user_id
    ):
        """Test user squad retrieval uses caching correctly."""
        # Mock 404 response (not in squad)
        mock_response = AsyncMock()
        mock_response.status_code = 404
        mock_api_client.get.return_value = mock_response
        
        # First call
        await squads_service.get_user_squad(test_guild_id, test_user_id)
        
        # Verify cache was set
        assert mock_cache_manager.set.call_count == 1
        
        # Mock cache hit
        cached_data = {"user_id": test_user_id, "squad": None, "member_since": None}
        mock_cache_manager.get.return_value = cached_data
        
        # Second call - should hit cache
        user_squad = await squads_service.get_user_squad(test_guild_id, test_user_id)
        
        # Verify API was not called again
        assert mock_api_client.get.call_count == 1
        assert mock_cache_manager.get.call_count == 2
        assert user_squad.is_in_squad is False
    
    async def test_get_user_squad_invalid_inputs(self, squads_service):
        """Test user squad retrieval with invalid inputs."""
        # Empty guild ID
        with pytest.raises(ValidationError) as exc_info:
            await squads_service.get_user_squad("", "user_id")
        assert exc_info.value.field == "guild_id"
        
        # Empty user ID
        with pytest.raises(ValidationError) as exc_info:
            await squads_service.get_user_squad("guild_id", "")
        assert exc_info.value.field == "user_id"


class TestSquadsServiceJoinSquad:
    """Test squad joining operations."""
    
    async def test_join_squad_success(
        self,
        squads_service,
        mock_api_client,
        test_guild_id,
        test_user_id,
        test_squad_id,
        squad_api_response
    ):
        """Test successful squad joining."""
        # Mock user not in any squad
        no_squad_response = AsyncMock()
        no_squad_response.status_code = 404
        
        # Mock squad details
        squad_response = AsyncMock()
        squad_response.status_code = 200
        squad_response.json = Mock(return_value=squad_api_response)
        
        # Mock successful join
        join_response = AsyncMock()
        join_response.status_code = 200
        join_response.json = Mock(return_value={"success": True})
        
        # Set up API call sequence
        mock_api_client.get.side_effect = [no_squad_response, squad_response]
        mock_api_client.post.return_value = join_response
        
        # Call service
        result = await squads_service.join_squad(
            test_guild_id,
            test_user_id,
            test_squad_id,
            current_balance=200  # Sufficient for 100 cost squad
        )
        
        # Verify result
        assert isinstance(result, JoinSquadResult)
        assert result.success is True
        assert result.squad is not None
        assert result.squad.name == "Test Squad"
        assert result.previous_squad is None
        assert result.cost == 0  # No cost for first squad
        assert result.new_balance == 200
        
        # Verify API calls
        assert mock_api_client.get.call_count == 2  # User squad + squad details
        mock_api_client.post.assert_called_once_with(
            f"/guilds/{test_guild_id}/squads/{test_squad_id}/join",
            json_data={"user_id": test_user_id},
            timeout=15.0
        )
    
    async def test_join_squad_with_switch_cost(
        self,
        squads_service,
        mock_api_client,
        test_guild_id,
        test_user_id,
        test_squad_id,
        squad_api_response
    ):
        """Test squad joining with switch cost."""
        # Mock user currently in a different squad
        current_squad_data = squad_api_response.copy()
        current_squad_data["id"] = str(uuid4())
        current_squad_data["name"] = "Current Squad"
        
        current_squad_response = AsyncMock()
        current_squad_response.status_code = 200
        current_squad_response.json = Mock(return_value={
            "squad": current_squad_data,
            "member_since": "2024-01-10T12:00:00Z"
        })
        
        # Mock target squad details
        target_squad_response = AsyncMock()
        target_squad_response.status_code = 200
        target_squad_response.json = Mock(return_value=squad_api_response)
        
        # Mock successful join
        join_response = AsyncMock()
        join_response.status_code = 200
        join_response.json = Mock(return_value={"success": True})
        
        # Set up API call sequence
        mock_api_client.get.side_effect = [current_squad_response, target_squad_response]
        mock_api_client.post.return_value = join_response
        
        # Call service
        result = await squads_service.join_squad(
            test_guild_id,
            test_user_id,
            test_squad_id,
            current_balance=200
        )
        
        # Verify result
        assert result.success is True
        assert result.previous_squad is not None
        assert result.previous_squad.name == "Current Squad"
        assert result.cost == 100  # Switch cost from squad_api_response
        assert result.new_balance == 100  # 200 - 100
    
    async def test_join_squad_insufficient_balance(
        self,
        squads_service,
        mock_api_client,
        test_guild_id,
        test_user_id,
        test_squad_id,
        squad_api_response
    ):
        """Test squad joining with insufficient balance."""
        from uuid import uuid4
        
        # Create different squad for current squad (user is in different squad)
        current_squad_id = uuid4()
        current_squad_data = squad_api_response.copy()
        current_squad_data["id"] = str(current_squad_id)
        current_squad_data["name"] = "Current Squad"
        
        # Mock user in current squad
        current_squad_response = AsyncMock()
        current_squad_response.status_code = 200
        current_squad_response.json = Mock(return_value={
            "squad": current_squad_data,
            "member_since": "2024-01-10T12:00:00Z"
        })
        
        # Mock target squad details (different from current)
        target_squad_response = AsyncMock()
        target_squad_response.status_code = 200
        target_squad_response.json = Mock(return_value=squad_api_response)
        
        mock_api_client.get.side_effect = [current_squad_response, target_squad_response]
        
        # Call service with insufficient balance
        result = await squads_service.join_squad(
            test_guild_id,
            test_user_id,
            test_squad_id,
            current_balance=50  # Less than 100 switch cost
        )
        
        # Verify result
        assert result.success is False
        assert "Insufficient bytes" in result.reason
        assert result.cost == 100
    
    async def test_join_squad_already_in_squad(
        self,
        squads_service,
        mock_api_client,
        test_guild_id,
        test_user_id,
        test_squad_id,
        squad_api_response
    ):
        """Test joining squad user is already in."""
        # Mock user already in target squad
        current_squad_response = AsyncMock()
        current_squad_response.status_code = 200
        current_squad_response.json = Mock(return_value={
            "squad": squad_api_response,
            "member_since": "2024-01-10T12:00:00Z"
        })
        
        # Mock target squad details (same squad)
        target_squad_response = AsyncMock()
        target_squad_response.status_code = 200
        target_squad_response.json = Mock(return_value=squad_api_response)
        
        mock_api_client.get.side_effect = [current_squad_response, target_squad_response]
        
        # Call service
        result = await squads_service.join_squad(
            test_guild_id,
            test_user_id,
            test_squad_id,
            current_balance=200
        )
        
        # Verify result
        assert result.success is False
        assert "already in" in result.reason
    
    async def test_join_squad_not_found(
        self,
        squads_service,
        mock_api_client,
        test_guild_id,
        test_user_id,
        test_squad_id
    ):
        """Test joining non-existent squad."""
        # Mock user not in squad
        no_squad_response = AsyncMock()
        no_squad_response.status_code = 404
        
        # Mock squad not found
        squad_not_found_response = AsyncMock()
        squad_not_found_response.status_code = 404
        mock_api_client.get.side_effect = [no_squad_response, ResourceNotFoundError("squad", str(test_squad_id))]
        
        # Call service
        result = await squads_service.join_squad(
            test_guild_id,
            test_user_id,
            test_squad_id,
            current_balance=200
        )
        
        # Verify result
        assert result.success is False
        assert "not found" in result.reason
    
    async def test_join_squad_inactive_squad(
        self,
        squads_service,
        mock_api_client,
        test_guild_id,
        test_user_id,
        test_squad_id,
        squad_api_response
    ):
        """Test joining inactive squad."""
        # Mock user not in squad
        no_squad_response = AsyncMock()
        no_squad_response.status_code = 404
        
        # Mock inactive squad
        inactive_squad_data = squad_api_response.copy()
        inactive_squad_data["is_active"] = False
        
        squad_response = AsyncMock()
        squad_response.status_code = 200
        squad_response.json = Mock(return_value=inactive_squad_data)
        
        mock_api_client.get.side_effect = [no_squad_response, squad_response]
        
        # Call service
        result = await squads_service.join_squad(
            test_guild_id,
            test_user_id,
            test_squad_id,
            current_balance=200
        )
        
        # Verify result
        assert result.success is False
        assert "inactive" in result.reason
    
    async def test_join_squad_full_squad(
        self,
        squads_service,
        mock_api_client,
        test_guild_id,
        test_user_id,
        test_squad_id,
        squad_api_response
    ):
        """Test joining full squad."""
        # Mock user not in squad
        no_squad_response = AsyncMock()
        no_squad_response.status_code = 404
        
        # Mock full squad
        full_squad_data = squad_api_response.copy()
        full_squad_data["member_count"] = 20  # Same as max_members
        
        squad_response = AsyncMock()
        squad_response.status_code = 200
        squad_response.json = Mock(return_value=full_squad_data)
        
        mock_api_client.get.side_effect = [no_squad_response, squad_response]
        
        # Call service
        result = await squads_service.join_squad(
            test_guild_id,
            test_user_id,
            test_squad_id,
            current_balance=200
        )
        
        # Verify result
        assert result.success is False
        assert "full" in result.reason
        assert "20" in result.reason  # Max members mentioned
    
    async def test_join_squad_invalid_inputs(self, squads_service):
        """Test squad joining with invalid inputs."""
        # Empty guild ID
        with pytest.raises(ValidationError) as exc_info:
            await squads_service.join_squad("", "user_id", uuid4(), 100)
        assert exc_info.value.field == "guild_id"
        
        # Empty user ID
        with pytest.raises(ValidationError) as exc_info:
            await squads_service.join_squad("guild_id", "", uuid4(), 100)
        assert exc_info.value.field == "user_id"
        
        # None squad ID
        with pytest.raises(ValidationError) as exc_info:
            await squads_service.join_squad("guild_id", "user_id", None, 100)
        assert exc_info.value.field == "squad_id"
        
        # Negative balance
        with pytest.raises(ValidationError) as exc_info:
            await squads_service.join_squad("guild_id", "user_id", uuid4(), -10)
        assert exc_info.value.field == "current_balance"


class TestSquadsServiceLeaveSquad:
    """Test squad leaving operations."""
    
    async def test_leave_squad_success(
        self,
        squads_service,
        mock_api_client,
        test_guild_id,
        test_user_id,
        squad_api_response
    ):
        """Test successful squad leaving."""
        # Mock user in squad
        current_squad_response = AsyncMock()
        current_squad_response.status_code = 200
        current_squad_response.json = Mock(return_value={
            "squad": squad_api_response,
            "member_since": "2024-01-10T12:00:00Z"
        })
        
        # Mock successful leave
        leave_response = AsyncMock()
        leave_response.status_code = 200
        leave_response.json = Mock(return_value={"success": True})
        
        mock_api_client.get.return_value = current_squad_response
        mock_api_client.delete.return_value = leave_response
        
        # Call service
        result = await squads_service.leave_squad(test_guild_id, test_user_id)
        
        # Verify result
        assert isinstance(result, UserSquadResponse)
        assert result.user_id == test_user_id
        assert result.is_in_squad is False
        assert result.squad is None
        
        # Verify API calls
        mock_api_client.get.assert_called_once()
        mock_api_client.delete.assert_called_once_with(
            f"/guilds/{test_guild_id}/squads/leave",
            json_data={"user_id": test_user_id},
            timeout=10.0
        )
    
    async def test_leave_squad_not_in_squad(
        self,
        squads_service,
        mock_api_client,
        test_guild_id,
        test_user_id
    ):
        """Test leaving squad when not in any squad."""
        # Mock user not in squad
        no_squad_response = AsyncMock()
        no_squad_response.status_code = 404
        mock_api_client.get.return_value = no_squad_response
        
        # Should raise NotInSquadError
        with pytest.raises(NotInSquadError):
            await squads_service.leave_squad(test_guild_id, test_user_id)
    
    async def test_leave_squad_api_not_found(
        self,
        squads_service,
        mock_api_client,
        test_guild_id,
        test_user_id,
        squad_api_response
    ):
        """Test leaving squad when API returns 404."""
        # Mock user in squad (from cache/first check)
        current_squad_response = AsyncMock()
        current_squad_response.status_code = 200
        current_squad_response.json = Mock(return_value={
            "squad": squad_api_response,
            "member_since": "2024-01-10T12:00:00Z"
        })
        
        # Mock API 404 on leave (race condition)
        leave_response = AsyncMock()
        leave_response.status_code = 404
        
        mock_api_client.get.return_value = current_squad_response
        mock_api_client.delete.return_value = leave_response
        
        # Should raise NotInSquadError
        with pytest.raises(NotInSquadError):
            await squads_service.leave_squad(test_guild_id, test_user_id)
    
    async def test_leave_squad_invalid_inputs(self, squads_service):
        """Test squad leaving with invalid inputs."""
        # Empty guild ID
        with pytest.raises(ValidationError) as exc_info:
            await squads_service.leave_squad("", "user_id")
        assert exc_info.value.field == "guild_id"
        
        # Empty user ID
        with pytest.raises(ValidationError) as exc_info:
            await squads_service.leave_squad("guild_id", "")
        assert exc_info.value.field == "user_id"


class TestSquadsServiceMembers:
    """Test squad member operations."""
    
    async def test_get_squad_members_success(
        self,
        squads_service,
        mock_api_client,
        test_guild_id,
        test_squad_id
    ):
        """Test successful squad members retrieval."""
        # Mock API response
        members_data = {
            "members": [
                {
                    "user_id": "user1",
                    "username": "TestUser1",
                    "joined_at": "2024-01-10T12:00:00Z"
                },
                {
                    "user_id": "user2",
                    "username": "TestUser2",
                    "joined_at": "2024-01-11T12:00:00Z"
                }
            ]
        }
        
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = Mock(return_value=members_data)
        mock_api_client.get.return_value = mock_response
        
        # Call service
        members = await squads_service.get_squad_members(test_guild_id, test_squad_id)
        
        # Verify result
        assert len(members) == 2
        assert all(isinstance(member, SquadMember) for member in members)
        
        # Check first member
        first_member = members[0]
        assert first_member.user_id == "user1"
        assert first_member.username == "TestUser1"
        assert first_member.joined_at is not None
        
        # Verify API call
        mock_api_client.get.assert_called_once_with(
            f"/guilds/{test_guild_id}/squads/{test_squad_id}/members",
            timeout=10.0
        )
    
    async def test_get_squad_members_squad_not_found(
        self,
        squads_service,
        mock_api_client,
        test_guild_id,
        test_squad_id
    ):
        """Test squad members retrieval for non-existent squad."""
        # Mock 404 response
        mock_response = AsyncMock()
        mock_response.status_code = 404
        mock_api_client.get.return_value = mock_response
        
        # Should raise ResourceNotFoundError
        with pytest.raises(ResourceNotFoundError) as exc_info:
            await squads_service.get_squad_members(test_guild_id, test_squad_id)
        
        assert exc_info.value.resource_type == "squad"
        assert str(test_squad_id) in exc_info.value.resource_id
    
    async def test_get_squad_members_empty_squad(
        self,
        squads_service,
        mock_api_client,
        test_guild_id,
        test_squad_id
    ):
        """Test squad members retrieval for empty squad."""
        # Mock empty response
        members_data = {"members": []}
        
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = Mock(return_value=members_data)
        mock_api_client.get.return_value = mock_response
        
        # Call service
        members = await squads_service.get_squad_members(test_guild_id, test_squad_id)
        
        # Verify result
        assert len(members) == 0
    
    async def test_get_squad_members_with_caching(
        self,
        squads_service,
        mock_api_client,
        mock_cache_manager,
        test_guild_id,
        test_squad_id
    ):
        """Test squad members retrieval uses caching correctly."""
        # Mock API response
        members_data = {
            "members": [
                {"user_id": "user1", "username": "TestUser1", "joined_at": "2024-01-10T12:00:00Z"}
            ]
        }
        
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = Mock(return_value=members_data)
        mock_api_client.get.return_value = mock_response
        
        # First call
        await squads_service.get_squad_members(test_guild_id, test_squad_id)
        
        # Verify cache was set
        assert mock_cache_manager.set.call_count == 1
        
        # Mock cache hit
        cached_data = [{"user_id": "user1", "username": "TestUser1", "joined_at": "2024-01-10T12:00:00Z"}]
        mock_cache_manager.get.return_value = cached_data
        
        # Second call - should hit cache
        members = await squads_service.get_squad_members(test_guild_id, test_squad_id)
        
        # Verify API was not called again
        assert mock_api_client.get.call_count == 1
        assert mock_cache_manager.get.call_count == 2
        assert len(members) == 1
    
    async def test_get_squad_members_invalid_inputs(self, squads_service):
        """Test squad members retrieval with invalid inputs."""
        # Empty guild ID
        with pytest.raises(ValidationError) as exc_info:
            await squads_service.get_squad_members("", uuid4())
        assert exc_info.value.field == "guild_id"
        
        # None squad ID
        with pytest.raises(ValidationError) as exc_info:
            await squads_service.get_squad_members("guild_id", None)
        assert exc_info.value.field == "squad_id"


class TestSquadsServiceStats:
    """Test service statistics and monitoring."""
    
    async def test_get_service_stats(self, squads_service):
        """Test service statistics collection."""
        # Set some stats
        squads_service._squad_list_requests = 5
        squads_service._join_attempts = 3
        squads_service._leave_attempts = 2
        squads_service._member_lookups = 8
        squads_service._cache_hits = 6
        squads_service._cache_misses = 4
        
        stats = await squads_service.get_service_stats()
        
        assert stats["service_name"] == "SquadsService"
        assert stats["total_squad_list_requests"] == 5
        assert stats["total_join_attempts"] == 3
        assert stats["total_leave_attempts"] == 2
        assert stats["total_member_lookups"] == 8
        assert stats["cache_hits"] == 6
        assert stats["cache_misses"] == 4
        assert stats["cache_hit_rate"] == 0.6  # 6/(6+4)
        assert stats["cache_enabled"] is True


class TestSquadsServiceErrorHandling:
    """Test comprehensive error handling scenarios."""
    
    async def test_service_not_initialized(self, mock_api_client, mock_cache_manager):
        """Test operations on uninitialized service."""
        service = SquadsService(
            api_client=mock_api_client,
            cache_manager=mock_cache_manager
        )
        
        # Should raise ServiceError for uninitialized service
        with pytest.raises(ServiceError, match="not initialized"):
            await service.list_squads("guild_id")
    
    async def test_cache_invalidation_after_join(
        self,
        squads_service,
        mock_api_client,
        mock_cache_manager,
        test_guild_id,
        test_user_id,
        test_squad_id,
        squad_api_response
    ):
        """Test cache invalidation after squad join."""
        # Mock successful join flow
        no_squad_response = AsyncMock()
        no_squad_response.status_code = 404
        
        squad_response = AsyncMock()
        squad_response.status_code = 200
        squad_response.json = Mock(return_value=squad_api_response)
        
        join_response = AsyncMock()
        join_response.status_code = 200
        join_response.json = Mock(return_value={"success": True})
        
        mock_api_client.get.side_effect = [no_squad_response, squad_response]
        mock_api_client.post.return_value = join_response
        
        # Call service
        await squads_service.join_squad(test_guild_id, test_user_id, test_squad_id, 200)
        
        # Verify cache invalidations
        assert mock_cache_manager.delete.call_count >= 2  # User squad + squad members
        assert mock_cache_manager.clear_pattern.call_count >= 1  # Squad list pattern
    
    async def test_unexpected_errors_wrapped(
        self,
        squads_service,
        mock_api_client,
        test_guild_id
    ):
        """Test unexpected errors are wrapped in ServiceError."""
        # Mock unexpected error
        mock_api_client.get.side_effect = ValueError("Unexpected error")
        
        # Should wrap in ServiceError
        with pytest.raises(ServiceError, match="Failed to list squads"):
            await squads_service.list_squads(test_guild_id)