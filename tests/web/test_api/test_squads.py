"""Tests for squad management API endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import Mock, patch
from uuid import uuid4, UUID

import pytest
from httpx import AsyncClient

from smarter_dev.web.crud import NotFoundError, ConflictError, DatabaseOperationError


class TestSquadListing:
    """Test squad listing endpoints."""
    
    async def test_list_squads_success(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str,
        mock_squad_operations,
        sample_squad_data: dict
    ):
        """Test successful squad listing."""
        # Mock squad data
        squads = []
        for i in range(3):
            squad_mock = Mock()
            squad_data = sample_squad_data.copy()
            squad_data["name"] = f"Squad {i}"
            squad_data["role_id"] = f"role_{i}"
            for key, value in squad_data.items():
                setattr(squad_mock, key, value)
            squad_mock.id = uuid4()
            squad_mock.created_at = datetime.now(timezone.utc)
            squad_mock.updated_at = datetime.now(timezone.utc)
            squads.append(squad_mock)
        
        mock_squad_operations.get_guild_squads.return_value = squads
        mock_squad_operations._get_squad_member_count.return_value = 2
        
        response = await api_client.get(
            f"/guilds/{test_guild_id}/squads/",
            headers=bot_headers
        )
        
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 3
        assert all(squad["member_count"] == 2 for squad in data)
        assert data[0]["name"] == "Squad 0"
    
    async def test_list_squads_with_inactive(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str,
        mock_squad_operations
    ):
        """Test squad listing including inactive squads."""
        mock_squad_operations.get_guild_squads.return_value = []
        mock_squad_operations._get_squad_member_count.return_value = 0
        
        response = await api_client.get(
            f"/guilds/{test_guild_id}/squads/?include_inactive=true",
            headers=bot_headers
        )
        
        assert response.status_code == 200
        # Verify active_only=False was passed
        mock_squad_operations.get_guild_squads.assert_called_with(
            mock_squad_operations.get_guild_squads.call_args[0][0],  # session
            test_guild_id,
            active_only=False
        )
    
    async def test_list_squads_empty(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str,
        mock_squad_operations
    ):
        """Test squad listing when no squads exist."""
        mock_squad_operations.get_guild_squads.return_value = []
        
        response = await api_client.get(
            f"/guilds/{test_guild_id}/squads/",
            headers=bot_headers
        )
        
        assert response.status_code == 200
        assert response.json() == []


class TestSquadCreation:
    """Test squad creation endpoints."""
    
    async def test_create_squad_success(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str,
        mock_squad_operations,
        sample_squad_data: dict
    ):
        """Test successful squad creation."""
        # Mock created squad
        squad_mock = Mock()
        for key, value in sample_squad_data.items():
            setattr(squad_mock, key, value)
        squad_mock.id = uuid4()
        squad_mock.created_at = datetime.now(timezone.utc)
        squad_mock.updated_at = datetime.now(timezone.utc)
        mock_squad_operations.create_squad.return_value = squad_mock
        
        create_data = {
            "role_id": sample_squad_data["role_id"],
            "name": sample_squad_data["name"],
            "description": sample_squad_data["description"],
            "max_members": sample_squad_data["max_members"],
            "switch_cost": sample_squad_data["switch_cost"]
        }
        
        response = await api_client.post(
            f"/guilds/{test_guild_id}/squads/",
            headers=bot_headers,
            json=create_data
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == sample_squad_data["name"]
        assert data["role_id"] == sample_squad_data["role_id"]
        assert data["member_count"] == 0  # New squad has no members
    
    async def test_create_squad_role_already_used(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str,
        mock_squad_operations,
        sample_squad_data: dict
    ):
        """Test squad creation with role already in use."""
        mock_squad_operations.create_squad.side_effect = ConflictError(
            "Role already associated with a squad"
        )
        
        create_data = {
            "role_id": sample_squad_data["role_id"],
            "name": sample_squad_data["name"]
        }
        
        response = await api_client.post(
            f"/guilds/{test_guild_id}/squads/",
            headers=bot_headers,
            json=create_data
        )
        
        assert response.status_code == 400
        assert "Role already associated" in response.json()["detail"]
    
    async def test_create_squad_invalid_data(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str
    ):
        """Test squad creation with invalid data."""
        invalid_data = {
            "role_id": "invalid_role",
            "name": "",  # Empty name
            "max_members": -1,  # Negative members
            "switch_cost": -10  # Negative cost
        }
        
        response = await api_client.post(
            f"/guilds/{test_guild_id}/squads/",
            headers=bot_headers,
            json=invalid_data
        )
        
        assert response.status_code == 422  # Validation error
    
    async def test_create_squad_minimal_data(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str,
        mock_squad_operations,
        test_role_id: str
    ):
        """Test squad creation with minimal required data."""
        squad_mock = Mock()
        squad_mock.id = uuid4()
        squad_mock.guild_id = test_guild_id
        squad_mock.role_id = test_role_id
        squad_mock.name = "Minimal Squad"
        squad_mock.description = None
        squad_mock.max_members = None
        squad_mock.switch_cost = 50  # Default
        squad_mock.is_active = True
        squad_mock.created_at = datetime.now(timezone.utc)
        squad_mock.updated_at = datetime.now(timezone.utc)
        mock_squad_operations.create_squad.return_value = squad_mock
        
        create_data = {
            "role_id": test_role_id,
            "name": "Minimal Squad"
        }
        
        response = await api_client.post(
            f"/guilds/{test_guild_id}/squads/",
            headers=bot_headers,
            json=create_data
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Minimal Squad"
        assert data["switch_cost"] == 50


class TestSquadRetrieval:
    """Test individual squad retrieval."""
    
    async def test_get_squad_success(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str,
        mock_squad_operations,
        sample_squad_data: dict
    ):
        """Test successful squad retrieval."""
        squad_id = uuid4()
        squad_mock = Mock()
        for key, value in sample_squad_data.items():
            setattr(squad_mock, key, value)
        squad_mock.id = squad_id
        squad_mock.created_at = datetime.now(timezone.utc)
        squad_mock.updated_at = datetime.now(timezone.utc)
        mock_squad_operations.get_squad.return_value = squad_mock
        mock_squad_operations._get_squad_member_count.return_value = 5
        
        response = await api_client.get(
            f"/guilds/{test_guild_id}/squads/{squad_id}",
            headers=bot_headers
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(squad_id)
        assert data["name"] == sample_squad_data["name"]
        assert data["member_count"] == 5
    
    async def test_get_squad_not_found(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str,
        mock_squad_operations
    ):
        """Test squad retrieval when squad doesn't exist."""
        squad_id = uuid4()
        mock_squad_operations.get_squad.side_effect = NotFoundError("Squad not found")
        
        response = await api_client.get(
            f"/guilds/{test_guild_id}/squads/{squad_id}",
            headers=bot_headers
        )
        
        assert response.status_code == 404
        assert "Squad not found" in response.json()["detail"]
    
    async def test_get_squad_wrong_guild(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str,
        mock_squad_operations,
        sample_squad_data: dict
    ):
        """Test squad retrieval for squad in different guild."""
        squad_id = uuid4()
        squad_mock = Mock()
        for key, value in sample_squad_data.items():
            setattr(squad_mock, key, value)
        squad_mock.guild_id = "different_guild_id"  # Different guild
        mock_squad_operations.get_squad.return_value = squad_mock
        
        response = await api_client.get(
            f"/guilds/{test_guild_id}/squads/{squad_id}",
            headers=bot_headers
        )
        
        assert response.status_code == 404
        assert "Squad not found in this guild" in response.json()["detail"]


class TestSquadUpdate:
    """Test squad update endpoints."""
    
    async def test_update_squad_success(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str,
        mock_squad_operations,
        sample_squad_data: dict
    ):
        """Test successful squad update."""
        squad_id = uuid4()
        squad_mock = Mock()
        for key, value in sample_squad_data.items():
            setattr(squad_mock, key, value)
        squad_mock.id = squad_id
        squad_mock.created_at = datetime.now(timezone.utc)
        squad_mock.updated_at = datetime.now(timezone.utc)
        mock_squad_operations.get_squad.return_value = squad_mock
        mock_squad_operations._get_squad_member_count.return_value = 3
        
        update_data = {
            "name": "Updated Squad Name",
            "switch_cost": 75
        }
        
        response = await api_client.put(
            f"/guilds/{test_guild_id}/squads/{squad_id}",
            headers=bot_headers,
            json=update_data
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["member_count"] == 3
    
    async def test_update_squad_empty_data(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str,
        mock_squad_operations,
        sample_squad_data: dict
    ):
        """Test squad update with empty data."""
        squad_id = uuid4()
        
        # Mock squad
        squad_mock = Mock()
        for key, value in sample_squad_data.items():
            setattr(squad_mock, key, value)
        squad_mock.id = squad_id
        squad_mock.guild_id = test_guild_id
        mock_squad_operations.get_squad.return_value = squad_mock
        
        response = await api_client.put(
            f"/guilds/{test_guild_id}/squads/{squad_id}",
            headers=bot_headers,
            json={}
        )
        
        assert response.status_code == 400
        assert "No valid squad updates provided" in response.json()["detail"]


class TestSquadMembership:
    """Test squad membership endpoints."""
    
    async def test_join_squad_success(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str,
        test_user_id: str,
        mock_squad_operations,
        sample_squad_data: dict
    ):
        """Test successful squad joining."""
        squad_id = uuid4()
        
        # Mock membership creation
        membership_mock = Mock()
        membership_mock.squad_id = squad_id
        membership_mock.user_id = test_user_id
        membership_mock.guild_id = test_guild_id
        membership_mock.joined_at = datetime.now(timezone.utc)
        mock_squad_operations.join_squad.return_value = membership_mock
        
        # Mock squad for response
        squad_mock = Mock()
        for key, value in sample_squad_data.items():
            setattr(squad_mock, key, value)
        squad_mock.id = squad_id
        squad_mock.created_at = datetime.now(timezone.utc)
        squad_mock.updated_at = datetime.now(timezone.utc)
        mock_squad_operations.get_squad.return_value = squad_mock
        mock_squad_operations._get_squad_member_count.return_value = 1
        
        join_data = {"user_id": test_user_id}
        
        response = await api_client.post(
            f"/guilds/{test_guild_id}/squads/{squad_id}/join",
            headers=bot_headers,
            json=join_data
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["user_id"] == test_user_id
        assert data["squad_id"] == str(squad_id)
        assert data["squad"]["member_count"] == 1
    
    async def test_join_squad_already_member(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str,
        test_user_id: str,
        mock_squad_operations
    ):
        """Test joining squad when user is already a member."""
        squad_id = uuid4()
        mock_squad_operations.join_squad.side_effect = ConflictError(
            "User already in squad Test Squad"
        )
        
        join_data = {"user_id": test_user_id}
        
        response = await api_client.post(
            f"/guilds/{test_guild_id}/squads/{squad_id}/join",
            headers=bot_headers,
            json=join_data
        )
        
        assert response.status_code == 400
        assert "already in squad" in response.json()["detail"]
    
    async def test_join_squad_insufficient_balance(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str,
        test_user_id: str,
        mock_squad_operations
    ):
        """Test joining squad with insufficient balance."""
        squad_id = uuid4()
        mock_squad_operations.join_squad.side_effect = ConflictError(
            "Insufficient balance: 25 < 50"
        )
        
        join_data = {"user_id": test_user_id}
        
        response = await api_client.post(
            f"/guilds/{test_guild_id}/squads/{squad_id}/join",
            headers=bot_headers,
            json=join_data
        )
        
        assert response.status_code == 400
        assert "Insufficient balance" in response.json()["detail"]
    
    async def test_join_squad_invalid_user_id(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str
    ):
        """Test joining squad with invalid user ID."""
        squad_id = uuid4()
        join_data = {"user_id": "invalid_user_id"}
        
        response = await api_client.post(
            f"/guilds/{test_guild_id}/squads/{squad_id}/join",
            headers=bot_headers,
            json=join_data
        )
        
        assert response.status_code == 422  # Validation error
    
    async def test_leave_squad_success(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str,
        test_user_id: str,
        mock_squad_operations
    ):
        """Test successful squad leaving."""
        leave_data = {"user_id": test_user_id}
        
        import json
        response = await api_client.request(
            "DELETE",
            f"/guilds/{test_guild_id}/squads/leave",
            headers={**bot_headers, "Content-Type": "application/json"},
            content=json.dumps(leave_data)
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert test_user_id in data["message"]
        mock_squad_operations.leave_squad.assert_called_once()
    
    async def test_leave_squad_not_member(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str,
        test_user_id: str,
        mock_squad_operations
    ):
        """Test leaving squad when user is not a member."""
        mock_squad_operations.leave_squad.side_effect = NotFoundError(
            "User not in any squad"
        )
        
        leave_data = {"user_id": test_user_id}
        
        import json
        response = await api_client.request(
            "DELETE",
            f"/guilds/{test_guild_id}/squads/leave",
            headers={**bot_headers, "Content-Type": "application/json"},
            content=json.dumps(leave_data)
        )
        
        assert response.status_code == 404
        assert "not in any squad" in response.json()["detail"]


class TestUserSquadInfo:
    """Test user squad information endpoints."""
    
    async def test_get_user_squad_success(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str,
        test_user_id: str,
        mock_squad_operations,
        mock_db_session,
        sample_squad_data: dict
    ):
        """Test successful user squad retrieval."""
        squad_id = uuid4()
        
        # Mock squad
        squad_mock = Mock()
        for key, value in sample_squad_data.items():
            setattr(squad_mock, key, value)
        squad_mock.id = squad_id
        squad_mock.created_at = datetime.now(timezone.utc)
        squad_mock.updated_at = datetime.now(timezone.utc)
        mock_squad_operations.get_user_squad.return_value = squad_mock
        mock_squad_operations._get_squad_member_count.return_value = 3
        
        # Mock database session execute for membership query
        mock_membership = Mock()
        mock_membership.squad_id = squad_id
        mock_membership.user_id = test_user_id
        mock_membership.guild_id = test_guild_id
        mock_membership.joined_at = datetime.now(timezone.utc)
        
        # Mock the database session execute result
        mock_result = Mock()
        mock_result.scalar_one.return_value = mock_membership
        
        # Setup mock_db_session to return our mock result
        mock_db_session.execute.return_value = mock_result
        
        response = await api_client.get(
            f"/guilds/{test_guild_id}/squads/members/{test_user_id}",
            headers=bot_headers
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["user_id"] == test_user_id
        assert data["guild_id"] == test_guild_id
        assert data["squad"] is not None
        assert data["squad"]["id"] == str(squad_id)
    
    async def test_get_user_squad_no_squad(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str,
        test_user_id: str,
        mock_squad_operations
    ):
        """Test user squad retrieval when user has no squad."""
        mock_squad_operations.get_user_squad.return_value = None
        
        response = await api_client.get(
            f"/guilds/{test_guild_id}/squads/members/{test_user_id}",
            headers=bot_headers
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["user_id"] == test_user_id
        assert data["guild_id"] == test_guild_id
        assert data["squad"] is None
        assert data["membership"] is None
    
    async def test_get_user_squad_invalid_user_id(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str
    ):
        """Test user squad retrieval with invalid user ID."""
        response = await api_client.get(
            f"/guilds/{test_guild_id}/squads/members/invalid_user_id",
            headers=bot_headers
        )
        
        assert response.status_code == 400
        assert "Invalid user ID format" in response.json()["detail"]["detail"]


class TestSquadMembers:
    """Test squad member listing endpoints."""
    
    async def test_get_squad_members_success(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str,
        mock_squad_operations,
        sample_squad_data: dict
    ):
        """Test successful squad member listing."""
        squad_id = uuid4()
        
        # Mock squad
        squad_mock = Mock()
        for key, value in sample_squad_data.items():
            setattr(squad_mock, key, value)
        squad_mock.id = squad_id
        squad_mock.created_at = datetime.now(timezone.utc)
        squad_mock.updated_at = datetime.now(timezone.utc)
        mock_squad_operations.get_squad.return_value = squad_mock
        
        # Mock memberships
        memberships = []
        for i in range(2):
            membership_mock = Mock()
            membership_mock.squad_id = squad_id
            membership_mock.user_id = f"user_{i}"
            membership_mock.guild_id = test_guild_id
            membership_mock.joined_at = datetime.now(timezone.utc)
            memberships.append(membership_mock)
        
        mock_squad_operations.get_squad_members.return_value = memberships
        
        response = await api_client.get(
            f"/guilds/{test_guild_id}/squads/{squad_id}/members",
            headers=bot_headers
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["squad"]["id"] == str(squad_id)
        assert len(data["members"]) == 2
        assert data["total_members"] == 2
        assert data["members"][0]["user_id"] == "user_0"
        assert data["members"][1]["user_id"] == "user_1"
    
    async def test_get_squad_members_wrong_guild(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str,
        mock_squad_operations,
        sample_squad_data: dict
    ):
        """Test squad member listing for squad in different guild."""
        squad_id = uuid4()
        squad_mock = Mock()
        for key, value in sample_squad_data.items():
            setattr(squad_mock, key, value)
        squad_mock.guild_id = "different_guild_id"
        mock_squad_operations.get_squad.return_value = squad_mock
        
        response = await api_client.get(
            f"/guilds/{test_guild_id}/squads/{squad_id}/members",
            headers=bot_headers
        )
        
        assert response.status_code == 404
        assert "Squad not found in this guild" in response.json()["detail"]
    
    async def test_get_squad_members_empty(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str,
        mock_squad_operations,
        sample_squad_data: dict
    ):
        """Test squad member listing when squad has no members."""
        squad_id = uuid4()
        
        # Mock squad
        squad_mock = Mock()
        for key, value in sample_squad_data.items():
            setattr(squad_mock, key, value)
        squad_mock.id = squad_id
        squad_mock.created_at = datetime.now(timezone.utc)
        squad_mock.updated_at = datetime.now(timezone.utc)
        mock_squad_operations.get_squad.return_value = squad_mock
        mock_squad_operations.get_squad_members.return_value = []
        
        response = await api_client.get(
            f"/guilds/{test_guild_id}/squads/{squad_id}/members",
            headers=bot_headers
        )
        
        assert response.status_code == 200
        data = response.json()
        assert len(data["members"]) == 0
        assert data["total_members"] == 0


class TestSquadErrorHandling:
    """Test squad API error handling."""
    
    async def test_database_error_handling(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str,
        mock_squad_operations
    ):
        """Test handling of database errors."""
        mock_squad_operations.get_guild_squads.side_effect = DatabaseOperationError(
            "Database connection failed"
        )
        
        response = await api_client.get(
            f"/guilds/{test_guild_id}/squads/",
            headers=bot_headers
        )
        
        assert response.status_code == 500
        assert "Database error" in response.json()["detail"]
    
    async def test_invalid_uuid_format(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str
    ):
        """Test handling of invalid UUID format."""
        response = await api_client.get(
            f"/guilds/{test_guild_id}/squads/invalid-uuid",
            headers=bot_headers
        )
        
        assert response.status_code == 422  # Validation error
    
    async def test_unauthorized_access(
        self,
        api_client: AsyncClient,
        test_guild_id: str
    ):
        """Test unauthorized access to squad endpoints."""
        response = await api_client.get(f"/guilds/{test_guild_id}/squads/")
        
        assert response.status_code == 403