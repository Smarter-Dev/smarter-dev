"""Integration tests for the complete API functionality."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
from httpx import AsyncClient


@pytest.mark.integration
# @pytest.mark.skip(reason="Complex API integration test - skipping for core functionality focus")
class TestAPIIntegration:
    """Test complete API integration scenarios."""
    
    async def test_complete_bytes_workflow(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str,
        test_user_id: str,
        test_user_id_2: str,
        mock_bytes_operations,
        mock_bytes_config_operations
    ):
        """Test complete bytes economy workflow."""
        # 1. Get initial config (creates default if not exists)
        config_mock = Mock()
        config_mock.guild_id = test_guild_id
        config_mock.daily_amount = 10
        config_mock.starting_balance = 100
        config_mock.max_transfer = 1000
        config_mock.daily_cooldown_hours = 24
        config_mock.streak_bonuses = {"8": 2}
        config_mock.transfer_tax_rate = 0.0
        config_mock.is_enabled = True
        config_mock.created_at = datetime.now(timezone.utc)
        config_mock.updated_at = datetime.now(timezone.utc)
        mock_bytes_config_operations.get_config.return_value = config_mock
        
        config_response = await api_client.get(
            f"/guilds/{test_guild_id}/bytes/config",
            headers=bot_headers
        )
        assert config_response.status_code == 200
        
        # 2. Get user balance (creates if not exists)
        balance_mock = Mock()
        balance_mock.guild_id = test_guild_id
        balance_mock.user_id = test_user_id
        balance_mock.balance = 100
        balance_mock.total_received = 100
        balance_mock.total_sent = 0
        balance_mock.streak_count = 0
        balance_mock.last_daily = None
        balance_mock.created_at = datetime.now(timezone.utc)
        balance_mock.updated_at = datetime.now(timezone.utc)
        mock_bytes_operations.get_balance.return_value = balance_mock
        
        balance_response = await api_client.get(
            f"/guilds/{test_guild_id}/bytes/balance/{test_user_id}",
            headers=bot_headers
        )
        assert balance_response.status_code == 200
        assert balance_response.json()["balance"] == 100
        
        # 3. Claim daily reward
        updated_balance = Mock()
        updated_balance.guild_id = test_guild_id
        updated_balance.user_id = test_user_id
        updated_balance.balance = 110
        updated_balance.total_received = 110
        updated_balance.total_sent = 0
        updated_balance.streak_count = 1
        updated_balance.last_daily = datetime.now(timezone.utc).date()
        updated_balance.created_at = datetime.now(timezone.utc)
        updated_balance.updated_at = datetime.now(timezone.utc)
        mock_bytes_operations.update_daily_reward.return_value = updated_balance
        
        daily_response = await api_client.post(
            f"/guilds/{test_guild_id}/bytes/daily/{test_user_id}",
            headers=bot_headers
        )
        assert daily_response.status_code == 200
        assert daily_response.json()["balance"]["balance"] == 110
        
        # 4. Create transaction
        transaction_mock = Mock()
        transaction_mock.id = uuid4()
        transaction_mock.guild_id = test_guild_id
        transaction_mock.giver_id = test_user_id
        transaction_mock.giver_username = "TestUser1"
        transaction_mock.receiver_id = test_user_id_2
        transaction_mock.receiver_username = "TestUser2"
        transaction_mock.amount = 25
        transaction_mock.reason = "Test payment"
        transaction_mock.created_at = datetime.now(timezone.utc)
        mock_bytes_operations.create_transaction.return_value = transaction_mock
        
        transaction_data = {
            "giver_id": test_user_id,
            "giver_username": "TestUser1",
            "receiver_id": test_user_id_2,
            "receiver_username": "TestUser2",
            "amount": 25,
            "reason": "Test payment"
        }
        
        transaction_response = await api_client.post(
            f"/guilds/{test_guild_id}/bytes/transactions",
            headers=bot_headers,
            json=transaction_data
        )
        assert transaction_response.status_code == 200
        assert transaction_response.json()["amount"] == 25
        
        # 5. Get leaderboard
        leaderboard_data = [balance_mock, updated_balance]
        mock_bytes_operations.get_leaderboard.return_value = leaderboard_data
        
        leaderboard_response = await api_client.get(
            f"/guilds/{test_guild_id}/bytes/leaderboard",
            headers=bot_headers
        )
        assert leaderboard_response.status_code == 200
        assert len(leaderboard_response.json()["users"]) == 2
    
    async def test_complete_squad_workflow(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str,
        test_user_id: str,
        test_role_id: str,
        mock_squad_operations,
        mock_db_session
    ):
        """Test complete squad management workflow."""
        # 1. List squads (initially empty)
        mock_squad_operations.get_guild_squads.return_value = []
        
        list_response = await api_client.get(
            f"/guilds/{test_guild_id}/squads/",
            headers=bot_headers
        )
        assert list_response.status_code == 200
        assert list_response.json() == []
        
        # 2. Create a squad
        squad_id = uuid4()
        squad_mock = Mock()
        squad_mock.id = squad_id
        squad_mock.guild_id = test_guild_id
        squad_mock.role_id = test_role_id
        squad_mock.name = "Test Squad"
        squad_mock.description = "A test squad"
        squad_mock.max_members = 10
        squad_mock.switch_cost = 50
        squad_mock.is_active = True
        squad_mock.created_at = datetime.now(timezone.utc)
        squad_mock.updated_at = datetime.now(timezone.utc)
        # Add spec to prevent attribute access issues
        squad_mock._spec_class = None
        mock_squad_operations.create_squad.return_value = squad_mock
        
        create_data = {
            "role_id": test_role_id,
            "name": "Test Squad",
            "description": "A test squad",
            "max_members": 10,
            "switch_cost": 50
        }
        
        create_response = await api_client.post(
            f"/guilds/{test_guild_id}/squads/",
            headers=bot_headers,
            json=create_data
        )
        assert create_response.status_code == 200
        assert create_response.json()["name"] == "Test Squad"
        
        # 3. Join the squad
        membership_mock = Mock()
        membership_mock.squad_id = squad_id
        membership_mock.user_id = test_user_id
        membership_mock.guild_id = test_guild_id
        membership_mock.joined_at = datetime.now(timezone.utc)
        # Add spec to prevent attribute access issues
        membership_mock._spec_class = None
        mock_squad_operations.join_squad.return_value = membership_mock
        mock_squad_operations.get_squad.return_value = squad_mock
        mock_squad_operations._get_squad_member_count.return_value = 1
        
        join_data = {"user_id": test_user_id}
        
        join_response = await api_client.post(
            f"/guilds/{test_guild_id}/squads/{squad_id}/join",
            headers=bot_headers,
            json=join_data
        )
        assert join_response.status_code == 200
        assert join_response.json()["user_id"] == test_user_id
        
        # 4. Get user's squad
        mock_squad_operations.get_user_squad.return_value = squad_mock
        
        # Mock the database session to return membership data
        mock_result = Mock()
        mock_result.scalar_one.return_value = membership_mock
        mock_db_session.execute.return_value = mock_result
        
        user_squad_response = await api_client.get(
            f"/guilds/{test_guild_id}/squads/members/{test_user_id}",
            headers=bot_headers
        )
        
        assert user_squad_response.status_code == 200
        assert user_squad_response.json()["squad"] is not None
        
        # 5. Get squad members
        mock_squad_operations.get_squad_members.return_value = [membership_mock]
        
        members_response = await api_client.get(
            f"/guilds/{test_guild_id}/squads/{squad_id}/members",
            headers=bot_headers
        )
        assert members_response.status_code == 200
        assert len(members_response.json()["members"]) == 1
        
        # 6. Leave squad
        leave_data = {"user_id": test_user_id}
        
        leave_response = await api_client.request(
            "DELETE",
            f"/guilds/{test_guild_id}/squads/leave",
            headers=bot_headers,
            json=leave_data
        )
        assert leave_response.status_code == 200
        assert leave_response.json()["success"] is True
    
    async def test_cross_system_integration(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str,
        test_user_id: str,
        test_role_id: str,
        mock_bytes_operations,
        mock_squad_operations
    ):
        """Test integration between bytes and squad systems."""
        # 1. Create user with bytes balance
        balance_mock = Mock()
        balance_mock.guild_id = test_guild_id
        balance_mock.user_id = test_user_id
        balance_mock.balance = 100
        balance_mock.total_received = 100
        balance_mock.total_sent = 0
        balance_mock.streak_count = 0
        balance_mock.last_daily = None
        balance_mock.created_at = datetime.now(timezone.utc)
        balance_mock.updated_at = datetime.now(timezone.utc)
        mock_bytes_operations.get_balance.return_value = balance_mock
        
        # 2. Create squad with switch cost
        squad_id = uuid4()
        squad_mock = Mock()
        squad_mock.id = squad_id
        squad_mock.guild_id = test_guild_id
        squad_mock.role_id = test_role_id
        squad_mock.name = "Expensive Squad"
        squad_mock.description = None
        squad_mock.max_members = None
        squad_mock.switch_cost = 50  # Costs bytes to join
        squad_mock.is_active = True
        squad_mock.created_at = datetime.now(timezone.utc)
        squad_mock.updated_at = datetime.now(timezone.utc)
        # Add spec to prevent attribute access issues
        squad_mock._spec_class = None
        mock_squad_operations.create_squad.return_value = squad_mock
        
        create_data = {
            "role_id": test_role_id,
            "name": "Expensive Squad",
            "switch_cost": 50
        }
        
        create_response = await api_client.post(
            f"/guilds/{test_guild_id}/squads/",
            headers=bot_headers,
            json=create_data
        )
        assert create_response.status_code == 200
        
        # 3. Join squad (should deduct bytes cost)
        membership_mock = Mock()
        membership_mock.squad_id = squad_id
        membership_mock.user_id = test_user_id
        membership_mock.guild_id = test_guild_id
        membership_mock.joined_at = datetime.now(timezone.utc)
        # Add spec to prevent attribute access issues
        membership_mock._spec_class = None
        mock_squad_operations.join_squad.return_value = membership_mock
        mock_squad_operations.get_squad.return_value = squad_mock
        mock_squad_operations._get_squad_member_count.return_value = 1
        
        join_data = {"user_id": test_user_id}
        
        join_response = await api_client.post(
            f"/guilds/{test_guild_id}/squads/{squad_id}/join",
            headers=bot_headers,
            json=join_data
        )
        assert join_response.status_code == 200
        
        # Verify join_squad was called with correct parameters
        mock_squad_operations.join_squad.assert_called_with(
            mock_squad_operations.join_squad.call_args[0][0],  # session
            test_guild_id,
            test_user_id,
            squad_id
        )
    
    async def test_error_propagation(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str,
        test_user_id: str,
        mock_bytes_operations
    ):
        """Test that errors propagate correctly through the API."""
        from smarter_dev.web.crud import DatabaseOperationError
        
        # Test database error propagation
        mock_bytes_operations.get_balance.side_effect = DatabaseOperationError(
            "Connection timeout"
        )
        
        response = await api_client.get(
            f"/guilds/{test_guild_id}/bytes/balance/{test_user_id}",
            headers=bot_headers
        )
        
        assert response.status_code == 500
        assert "Database error" in response.json()["detail"]
        assert "Connection timeout" in response.json()["detail"]
    
    async def test_concurrent_requests(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str,
        mock_bytes_operations
    ):
        """Test handling of concurrent API requests."""
        import asyncio
        
        # Mock balance for concurrent requests
        balance_mock = Mock()
        balance_mock.guild_id = test_guild_id
        balance_mock.user_id = "123"
        balance_mock.balance = 100
        balance_mock.total_received = 100
        balance_mock.total_sent = 0
        balance_mock.streak_count = 0
        balance_mock.last_daily = None
        balance_mock.created_at = datetime.now(timezone.utc)
        balance_mock.updated_at = datetime.now(timezone.utc)
        mock_bytes_operations.get_balance.return_value = balance_mock
        
        # Make concurrent requests
        tasks = [
            api_client.get(
                f"/guilds/{test_guild_id}/bytes/balance/123",
                headers=bot_headers
            )
            for _ in range(10)
        ]
        
        responses = await asyncio.gather(*tasks)
        
        # All should succeed
        for response in responses:
            assert response.status_code == 200
            assert response.json()["balance"] == 100
    
    async def test_api_versioning_and_health(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str]
    ):
        """Test API versioning and health check endpoints."""
        # Test health check
        health_response = await api_client.get("/health")
        assert health_response.status_code == 200
        assert health_response.json()["status"] == "healthy"
        assert health_response.json()["version"] == "1.0.0"
        
        # Test auth health
        auth_health_response = await api_client.get("/auth/health")
        assert auth_health_response.status_code == 200
        
        # Test auth status
        auth_status_response = await api_client.get(
            "/auth/status",
            headers=bot_headers
        )
        assert auth_status_response.status_code == 200
        assert auth_status_response.json()["api_version"] == "1.0.0"


@pytest.mark.integration
class TestAPIValidation:
    """Test API input validation across all endpoints."""
    
    async def test_guild_id_validation(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str]
    ):
        """Test guild ID validation across endpoints."""
        # Empty strings result in 404 (route not found)
        response = await api_client.get(
            f"/guilds//bytes/balance/123456789",
            headers=bot_headers
        )
        assert response.status_code == 404
        
        # Invalid format strings result in 400 (validation error)
        invalid_guild_ids = ["invalid", "0", "-1"]
        
        for invalid_id in invalid_guild_ids:
            response = await api_client.get(
                f"/guilds/{invalid_id}/bytes/balance/123456789",
                headers=bot_headers
            )
            assert response.status_code == 400
    
    async def test_user_id_validation(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str
    ):
        """Test user ID validation across endpoints."""
        # Empty strings result in 404 (route not found)
        response = await api_client.get(
            f"/guilds/{test_guild_id}/bytes/balance/",
            headers=bot_headers
        )
        assert response.status_code == 404
        
        # Invalid format strings result in 400 (validation error)
        invalid_user_ids = ["invalid", "0", "-1"]
        
        for invalid_id in invalid_user_ids:
            response = await api_client.get(
                f"/guilds/{test_guild_id}/bytes/balance/{invalid_id}",
                headers=bot_headers
            )
            assert response.status_code == 400
    
    async def test_request_size_limits(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str
    ):
        """Test request size and field length limits."""
        # Test very long reason in transaction
        large_transaction = {
            "giver_id": "123456789",
            "giver_username": "TestUser1",
            "receiver_id": "987654321",
            "receiver_username": "TestUser2", 
            "amount": 10,
            "reason": "x" * 1000  # Very long reason
        }
        
        response = await api_client.post(
            f"/guilds/{test_guild_id}/bytes/transactions",
            headers=bot_headers,
            json=large_transaction
        )
        assert response.status_code == 422  # Validation error
    
    async def test_numeric_limits(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str
    ):
        """Test numeric field limits and validation."""
        # Test negative amounts
        negative_transaction = {
            "giver_id": "123456789",
            "giver_username": "TestUser1",
            "receiver_id": "987654321",
            "receiver_username": "TestUser2",
            "amount": -10
        }
        
        response = await api_client.post(
            f"/guilds/{test_guild_id}/bytes/transactions",
            headers=bot_headers,
            json=negative_transaction
        )
        assert response.status_code == 422
        
        # Test excessive amounts
        excessive_transaction = {
            "giver_id": "123456789", 
            "giver_username": "TestUser1",
            "receiver_id": "987654321",
            "receiver_username": "TestUser2",
            "amount": 100000  # Exceeds max
        }
        
        response = await api_client.post(
            f"/guilds/{test_guild_id}/bytes/transactions",
            headers=bot_headers,
            json=excessive_transaction
        )
        assert response.status_code == 422