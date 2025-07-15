"""Tests for BytesService.

This module provides comprehensive tests for the BytesService including
balance operations, daily claims, transfers, leaderboards, and error handling.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from smarter_dev.bot.services.bytes_service import BytesService
from smarter_dev.bot.services.exceptions import (
    AlreadyClaimedError,
    APIError,
    InsufficientBalanceError,
    ResourceNotFoundError,
    ServiceError,
    ValidationError
)
from smarter_dev.bot.services.models import (
    BytesBalance,
    BytesTransaction,
    DailyClaimResult,
    LeaderboardEntry,
    TransferResult
)


class MockUser:
    """Mock Discord user for testing transfer_bytes method."""
    
    def __init__(self, user_id: str, username: str):
        self._id = user_id
        self._username = username
    
    @property
    def id(self) -> str:
        return self._id
    
    def __str__(self) -> str:
        return self._username


class TestBytesServiceBalances:
    """Test balance-related operations."""
    
    async def test_get_balance_success(
        self,
        bytes_service,
        mock_api_client,
        test_guild_id,
        test_user_id,
        balance_api_response
    ):
        """Test successful balance retrieval."""
        # Mock API response
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = Mock(return_value=balance_api_response)
        mock_api_client.get.return_value = mock_response
        
        # Call service
        balance = await bytes_service.get_balance(test_guild_id, test_user_id)
        
        # Verify result
        assert isinstance(balance, BytesBalance)
        assert balance.guild_id == test_guild_id
        assert balance.user_id == test_user_id
        assert balance.balance == 100
        assert balance.total_received == 150
        assert balance.total_sent == 50
        assert balance.streak_count == 5
        assert balance.last_daily == date(2024, 1, 14)
        
        # Verify API call
        mock_api_client.get.assert_called_once_with(
            f"/guilds/{test_guild_id}/bytes/balance/{test_user_id}",
            timeout=10.0
        )
    
    async def test_get_balance_with_caching(
        self,
        bytes_service,
        mock_api_client,
        mock_cache_manager,
        test_guild_id,
        test_user_id,
        balance_api_response
    ):
        """Test balance retrieval uses caching correctly."""
        # Mock API response
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = Mock(return_value=balance_api_response)
        mock_api_client.get.return_value = mock_response
        
        # First call - should hit API and cache result
        balance1 = await bytes_service.get_balance(test_guild_id, test_user_id)
        
        # Verify API was called and cache was set
        assert mock_api_client.get.call_count == 1
        assert mock_cache_manager.set.call_count == 1
        
        # Second call - should hit cache
        mock_cache_manager.get.return_value = balance_api_response
        balance2 = await bytes_service.get_balance(test_guild_id, test_user_id)
        
        # Verify API was not called again
        assert mock_api_client.get.call_count == 1
        assert mock_cache_manager.get.call_count == 2
        
        # Both results should be equivalent
        assert balance1.guild_id == balance2.guild_id
        assert balance1.user_id == balance2.user_id
        assert balance1.balance == balance2.balance
    
    async def test_get_balance_cache_disabled(
        self,
        bytes_service,
        mock_api_client,
        test_guild_id,
        test_user_id,
        balance_api_response
    ):
        """Test balance retrieval with cache disabled."""
        # Mock API response
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = Mock(return_value=balance_api_response)
        mock_api_client.get.return_value = mock_response
        
        # Call with cache disabled
        balance = await bytes_service.get_balance(test_guild_id, test_user_id, use_cache=False)
        
        # Verify result
        assert balance.balance == 100
        
        # Verify API was called but cache was not used
        mock_api_client.get.assert_called_once()
    
    async def test_get_balance_user_not_found(
        self,
        bytes_service,
        mock_api_client,
        test_guild_id,
        test_user_id
    ):
        """Test balance retrieval for non-existent user."""
        # Mock 404 response
        mock_response = AsyncMock()
        mock_response.status_code = 404
        mock_api_client.get.return_value = mock_response
        
        # Should raise ResourceNotFoundError
        with pytest.raises(ResourceNotFoundError) as exc_info:
            await bytes_service.get_balance(test_guild_id, test_user_id)
        
        assert exc_info.value.resource_type == "user_balance"
        assert f"{test_guild_id}:{test_user_id}" in exc_info.value.resource_id
    
    async def test_get_balance_invalid_inputs(self, bytes_service):
        """Test balance retrieval with invalid inputs."""
        # Empty guild ID
        with pytest.raises(ValidationError) as exc_info:
            await bytes_service.get_balance("", "123456789012345678")
        assert exc_info.value.field == "guild_id"
        
        # Empty user ID
        with pytest.raises(ValidationError) as exc_info:
            await bytes_service.get_balance("123456789012345678", "")
        assert exc_info.value.field == "user_id"
        
        # Whitespace-only IDs
        with pytest.raises(ValidationError):
            await bytes_service.get_balance("   ", "123456789012345678")
        
        with pytest.raises(ValidationError):
            await bytes_service.get_balance("123456789012345678", "   ")
    
    async def test_get_balance_api_error(
        self,
        bytes_service,
        mock_api_client,
        test_guild_id,
        test_user_id
    ):
        """Test balance retrieval with API error."""
        # Mock API error
        mock_api_client.get.side_effect = APIError("API unavailable", status_code=500)
        
        # Should re-raise APIError
        with pytest.raises(APIError, match="API unavailable"):
            await bytes_service.get_balance(test_guild_id, test_user_id)
    
    async def test_get_balance_unexpected_error(
        self,
        bytes_service,
        mock_api_client,
        test_guild_id,
        test_user_id
    ):
        """Test balance retrieval with unexpected error."""
        # Mock unexpected error
        mock_api_client.get.side_effect = ValueError("Unexpected error")
        
        # Should wrap in ServiceError
        with pytest.raises(ServiceError, match="Failed to get balance"):
            await bytes_service.get_balance(test_guild_id, test_user_id)


class TestBytesServiceDailyClaims:
    """Test daily claim operations."""
    
    async def test_claim_daily_success(
        self,
        bytes_service,
        mock_api_client,
        test_guild_id,
        test_user_id,
        daily_claim_api_response
    ):
        """Test successful daily claim."""
        # Mock API response
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = Mock(return_value=daily_claim_api_response)
        mock_api_client.post.return_value = mock_response
        
        # Call service
        result = await bytes_service.claim_daily(test_guild_id, test_user_id, "TestUser")
        
        # Verify result
        assert isinstance(result, DailyClaimResult)
        assert result.success is True
        assert result.earned == 20
        assert result.streak == 6
        assert result.streak_bonus == 2
        assert result.balance.balance == 120
        assert result.next_claim_at is not None
        
        # Verify API call
        mock_api_client.post.assert_called_once_with(
            f"/guilds/{test_guild_id}/bytes/daily/{test_user_id}",
            timeout=15.0
        )
    
    async def test_claim_daily_already_claimed(
        self,
        bytes_service,
        mock_api_client,
        test_guild_id,
        test_user_id
    ):
        """Test daily claim when already claimed today."""
        # Mock 409 conflict response
        mock_response = AsyncMock()
        mock_response.status_code = 409
        mock_response.json = Mock(return_value={"detail": "Daily reward already claimed today"})
        mock_api_client.post.return_value = mock_response
        
        # Should raise AlreadyClaimedError
        with pytest.raises(AlreadyClaimedError):
            await bytes_service.claim_daily(test_guild_id, test_user_id, "TestUser")
    
    async def test_claim_daily_invalid_inputs(self, bytes_service):
        """Test daily claim with invalid inputs."""
        # Empty guild ID
        with pytest.raises(ValidationError) as exc_info:
            await bytes_service.claim_daily("", "user_id", "username")
        assert exc_info.value.field == "guild_id"
        
        # Empty user ID
        with pytest.raises(ValidationError) as exc_info:
            await bytes_service.claim_daily("guild_id", "", "username")
        assert exc_info.value.field == "user_id"
        
        # Empty username
        with pytest.raises(ValidationError) as exc_info:
            await bytes_service.claim_daily("guild_id", "user_id", "")
        assert exc_info.value.field == "username"
    
    async def test_claim_daily_api_error(
        self,
        bytes_service,
        mock_api_client,
        test_guild_id,
        test_user_id
    ):
        """Test daily claim with API error."""
        # Mock API error response
        mock_response = AsyncMock()
        mock_response.status_code = 500
        mock_response.json = Mock(return_value={"detail": "Internal server error"})
        mock_api_client.post.return_value = mock_response
        
        # Should raise APIError
        with pytest.raises(APIError, match="Internal server error"):
            await bytes_service.claim_daily(test_guild_id, test_user_id, "TestUser")
    
    async def test_claim_daily_cache_invalidation(
        self,
        bytes_service,
        mock_api_client,
        mock_cache_manager,
        test_guild_id,
        test_user_id,
        daily_claim_api_response
    ):
        """Test that daily claim invalidates relevant caches."""
        # Mock successful API response
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = Mock(return_value=daily_claim_api_response)
        mock_api_client.post.return_value = mock_response
        
        # Call service
        await bytes_service.claim_daily(test_guild_id, test_user_id, "TestUser")
        
        # Verify cache invalidations were called
        assert mock_cache_manager.delete.call_count >= 1  # Balance cache
        assert mock_cache_manager.clear_pattern.call_count >= 1  # Leaderboard cache


class TestBytesServiceTransfers:
    """Test transfer operations."""
    
    async def test_transfer_bytes_success(
        self,
        bytes_service,
        mock_api_client,
        test_guild_id,
        test_user_id,
        test_user_id_2,
        balance_api_response,
        transaction_api_response
    ):
        """Test successful bytes transfer."""
        # Mock balance check
        balance_response = AsyncMock()
        balance_response.status_code = 200
        balance_response.json = Mock(return_value=balance_api_response)
        
        # Mock transfer API response
        transfer_response = AsyncMock()
        transfer_response.status_code = 200
        transfer_response.json = Mock(return_value=transaction_api_response)
        
        # Set up API call responses
        mock_api_client.get.return_value = balance_response
        mock_api_client.post.return_value = transfer_response
        
        # Call service
        giver = MockUser(test_user_id, "TestUser1")
        receiver = MockUser(test_user_id_2, "TestUser2")
        result = await bytes_service.transfer_bytes(
            test_guild_id,
            giver,
            receiver,
            50,
            "Test transfer"
        )
        
        # Verify result
        assert isinstance(result, TransferResult)
        assert result.success is True
        assert result.transaction is not None
        assert result.transaction.amount == 50
        assert result.transaction.reason == "Test transfer"
        assert result.new_giver_balance == 50  # 100 - 50
        
        # Verify API calls
        assert mock_api_client.get.call_count == 2  # Balance checks for both users
        mock_api_client.post.assert_called_once()
    
    async def test_transfer_bytes_insufficient_balance(
        self,
        bytes_service,
        mock_api_client,
        test_guild_id,
        test_user_id,
        test_user_id_2,
        balance_api_response
    ):
        """Test transfer with insufficient balance."""
        # Mock balance with lower amount
        balance_data = balance_api_response.copy()
        balance_data["balance"] = 30  # Less than transfer amount of 50
        
        balance_response = AsyncMock()
        balance_response.status_code = 200
        balance_response.json = Mock(return_value=balance_data)
        mock_api_client.get.return_value = balance_response
        
        # Should raise InsufficientBalanceError
        giver = MockUser(test_user_id, "TestUser1")
        receiver = MockUser(test_user_id_2, "TestUser2")
        with pytest.raises(InsufficientBalanceError) as exc_info:
            await bytes_service.transfer_bytes(
                test_guild_id,
                giver,
                receiver,
                50,
                "Test transfer"
            )
        
        assert exc_info.value.required == 50
        assert exc_info.value.available == 30
        assert exc_info.value.operation == "transfer"
    
    async def test_transfer_bytes_self_transfer(
        self,
        bytes_service,
        test_guild_id,
        test_user_id
    ):
        """Test transfer to self."""
        user = MockUser(test_user_id, "TestUser")
        result = await bytes_service.transfer_bytes(
            test_guild_id,
            user,
            user,  # Same user
            50,
            "Self transfer"
        )
        
        assert result.success is False
        assert "yourself" in result.reason
    
    async def test_transfer_bytes_invalid_amount(
        self,
        bytes_service,
        test_guild_id,
        test_user_id,
        test_user_id_2
    ):
        """Test transfer with invalid amounts."""
        giver = MockUser(test_user_id, "TestUser1")
        receiver = MockUser(test_user_id_2, "TestUser2")
        
        # Zero amount
        result = await bytes_service.transfer_bytes(
            test_guild_id,
            giver,
            receiver,
            0,
            "Zero transfer"
        )
        assert result.success is False
        assert "positive" in result.reason
        
        # Negative amount
        result = await bytes_service.transfer_bytes(
            test_guild_id,
            giver,
            receiver,
            -50,
            "Negative transfer"
        )
        assert result.success is False
        assert "positive" in result.reason
        
        # Amount too large
        result = await bytes_service.transfer_bytes(
            test_guild_id,
            giver,
            receiver,
            20000,  # Over limit
            "Large transfer"
        )
        assert result.success is False
        assert "too large" in result.reason
    
    async def test_transfer_bytes_invalid_inputs(self, bytes_service):
        """Test transfer with invalid inputs."""
        # Empty guild ID
        with pytest.raises(ValidationError) as exc_info:
            await bytes_service.transfer_bytes_by_id("", "user1", "name1", "user2", "name2", 50)
        assert exc_info.value.field == "guild_id"
        
        # Empty giver ID
        with pytest.raises(ValidationError) as exc_info:
            await bytes_service.transfer_bytes_by_id("guild", "", "name1", "user2", "name2", 50)
        assert exc_info.value.field == "giver_id"
        
        # Empty receiver ID
        with pytest.raises(ValidationError) as exc_info:
            await bytes_service.transfer_bytes_by_id("guild", "user1", "name1", "", "name2", 50)
        assert exc_info.value.field == "receiver_id"
        
        # Empty giver username
        with pytest.raises(ValidationError) as exc_info:
            await bytes_service.transfer_bytes_by_id("guild", "user1", "", "user2", "name2", 50)
        assert exc_info.value.field == "giver_username"
        
        # Empty receiver username
        with pytest.raises(ValidationError) as exc_info:
            await bytes_service.transfer_bytes_by_id("guild", "user1", "name1", "user2", "", 50)
        assert exc_info.value.field == "receiver_username"
    
    async def test_transfer_bytes_api_error(
        self,
        bytes_service,
        mock_api_client,
        test_guild_id,
        test_user_id,
        test_user_id_2,
        balance_api_response
    ):
        """Test transfer with API error."""
        # Mock balance check success
        balance_response = AsyncMock()
        balance_response.status_code = 200
        balance_response.json = Mock(return_value=balance_api_response)
        
        # Mock transfer API error
        transfer_response = AsyncMock()
        transfer_response.status_code = 500
        transfer_response.json = Mock(return_value={"detail": "Server error"})
        
        mock_api_client.get.return_value = balance_response
        mock_api_client.post.return_value = transfer_response
        
        giver = MockUser(test_user_id, "TestUser1")
        receiver = MockUser(test_user_id_2, "TestUser2")
        result = await bytes_service.transfer_bytes(
            test_guild_id,
            giver,
            receiver,
            50,
            "Test transfer"
        )
        
        assert result.success is False
        assert "Server error" in result.reason
    
    async def test_transfer_bytes_reason_truncation(
        self,
        bytes_service,
        mock_api_client,
        test_guild_id,
        test_user_id,
        test_user_id_2,
        balance_api_response,
        transaction_api_response
    ):
        """Test transfer reason is truncated to 200 characters."""
        # Mock successful responses
        balance_response = AsyncMock()
        balance_response.status_code = 200
        balance_response.json = Mock(return_value=balance_api_response)
        
        transfer_response = AsyncMock()
        transfer_response.status_code = 200
        transfer_response.json = Mock(return_value=transaction_api_response)
        
        mock_api_client.get.return_value = balance_response
        mock_api_client.post.return_value = transfer_response
        
        # Very long reason
        long_reason = "x" * 500
        
        giver = MockUser(test_user_id, "TestUser1")
        receiver = MockUser(test_user_id_2, "TestUser2")
        await bytes_service.transfer_bytes(
            test_guild_id,
            giver,
            receiver,
            50,
            long_reason
        )
        
        # Verify the posted data has truncated reason
        call_args = mock_api_client.post.call_args
        posted_data = call_args.kwargs["json_data"]
        assert len(posted_data["reason"]) == 200


class TestBytesServiceLeaderboard:
    """Test leaderboard operations."""
    
    async def test_get_leaderboard_success(
        self,
        bytes_service,
        mock_api_client,
        test_guild_id,
        leaderboard_api_response
    ):
        """Test successful leaderboard retrieval."""
        # Mock API response
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = Mock(return_value=leaderboard_api_response)
        mock_api_client.get.return_value = mock_response
        
        # Call service
        leaderboard = await bytes_service.get_leaderboard(test_guild_id, limit=10)
        
        # Verify result
        assert len(leaderboard) == 3
        assert all(isinstance(entry, LeaderboardEntry) for entry in leaderboard)
        
        # Check first entry
        first_entry = leaderboard[0]
        assert first_entry.rank == 1
        assert first_entry.user_id == "user1"
        assert first_entry.balance == 1000
        assert first_entry.streak_count == 30
        
        # Verify API call
        mock_api_client.get.assert_called_once_with(
            f"/guilds/{test_guild_id}/bytes/leaderboard",
            params={"limit": 10},
            timeout=10.0
        )
    
    async def test_get_leaderboard_with_caching(
        self,
        bytes_service,
        mock_api_client,
        mock_cache_manager,
        test_guild_id,
        leaderboard_api_response
    ):
        """Test leaderboard uses caching correctly."""
        # Mock API response
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = Mock(return_value=leaderboard_api_response)
        mock_api_client.get.return_value = mock_response
        
        # First call - should hit API and cache result
        await bytes_service.get_leaderboard(test_guild_id, limit=5)
        
        # Verify API was called and cache was set
        assert mock_api_client.get.call_count == 1
        assert mock_cache_manager.set.call_count == 1
        
        # Mock cache hit for second call
        cached_data = [
            {"rank": 1, "user_id": "user1", "balance": 1000, "total_received": 1200, "streak_count": 30}
        ]
        mock_cache_manager.get.side_effect = None
        mock_cache_manager.get.return_value = cached_data
        
        # Second call - should hit cache
        leaderboard = await bytes_service.get_leaderboard(test_guild_id, limit=5)
        
        # Verify API was not called again
        assert mock_api_client.get.call_count == 1
        assert mock_cache_manager.get.call_count == 2
        assert len(leaderboard) == 1
    
    async def test_get_leaderboard_invalid_inputs(self, bytes_service):
        """Test leaderboard with invalid inputs."""
        # Empty guild ID
        with pytest.raises(ValidationError) as exc_info:
            await bytes_service.get_leaderboard("", limit=10)
        assert exc_info.value.field == "guild_id"
        
        # Invalid limit (too low)
        with pytest.raises(ValidationError) as exc_info:
            await bytes_service.get_leaderboard("guild_id", limit=0)
        assert exc_info.value.field == "limit"
        
        # Invalid limit (too high)
        with pytest.raises(ValidationError) as exc_info:
            await bytes_service.get_leaderboard("guild_id", limit=200)
        assert exc_info.value.field == "limit"
    
    async def test_get_leaderboard_api_error(
        self,
        bytes_service,
        mock_api_client,
        test_guild_id
    ):
        """Test leaderboard with API error."""
        # Mock API error
        mock_response = AsyncMock()
        mock_response.status_code = 500
        mock_response.json = Mock(return_value={"detail": "Server error"})
        mock_api_client.get.return_value = mock_response
        
        # Should raise APIError
        with pytest.raises(APIError, match="Server error"):
            await bytes_service.get_leaderboard(test_guild_id)


class TestBytesServiceStats:
    """Test service statistics and monitoring."""
    
    async def test_get_service_stats(self, bytes_service):
        """Test service statistics collection."""
        # Call some operations to generate stats
        bytes_service._balance_requests = 10
        bytes_service._daily_claims = 5
        bytes_service._transfers = 3
        bytes_service._cache_hits = 8
        bytes_service._cache_misses = 2
        
        stats = await bytes_service.get_service_stats()
        
        assert stats["service_name"] == "BytesService"
        assert stats["total_balance_requests"] == 10
        assert stats["total_daily_claims"] == 5
        assert stats["total_transfers"] == 3
        assert stats["cache_hits"] == 8
        assert stats["cache_misses"] == 2
        assert stats["cache_hit_rate"] == 0.8  # 8/(8+2)
        assert stats["cache_enabled"] is True
    
    async def test_service_stats_no_operations(self, bytes_service):
        """Test service statistics with no operations."""
        stats = await bytes_service.get_service_stats()
        
        assert stats["cache_hit_rate"] == 0.0
        assert all(count == 0 for key, count in stats.items() if "total_" in key)


class TestBytesServiceErrorHandling:
    """Test comprehensive error handling scenarios."""
    
    async def test_service_not_initialized(self, mock_api_client, mock_cache_manager):
        """Test operations on uninitialized service."""
        service = BytesService(
            api_client=mock_api_client,
            cache_manager=mock_cache_manager
        )
        
        # Should raise ServiceError for uninitialized service
        with pytest.raises(ServiceError, match="not initialized"):
            await service.get_balance("guild_id", "user_id")
    
    async def test_concurrent_operations(
        self,
        bytes_service,
        mock_api_client,
        test_guild_id,
        test_user_id,
        balance_api_response
    ):
        """Test concurrent service operations."""
        import asyncio
        
        # Mock API response
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = Mock(return_value=balance_api_response)
        mock_api_client.get.return_value = mock_response
        
        # Execute multiple concurrent balance requests
        tasks = [
            bytes_service.get_balance(test_guild_id, test_user_id)
            for _ in range(5)
        ]
        
        results = await asyncio.gather(*tasks)
        
        # All should succeed
        assert len(results) == 5
        assert all(result.balance == 100 for result in results)
    
    async def test_cache_failure_graceful_degradation(
        self,
        bytes_service,
        mock_api_client,
        mock_cache_manager,
        test_guild_id,
        test_user_id,
        balance_api_response
    ):
        """Test graceful degradation when cache fails."""
        # Mock API response
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = Mock(return_value=balance_api_response)
        mock_api_client.get.return_value = mock_response
        
        # Mock cache to fail
        mock_cache_manager.get.side_effect = Exception("Cache error")
        mock_cache_manager.set.side_effect = Exception("Cache error")
        
        # Should still work despite cache failures
        balance = await bytes_service.get_balance(test_guild_id, test_user_id)
        assert balance.balance == 100
        
        # API should have been called
        mock_api_client.get.assert_called_once()


class TestBytesServicePlanningCompliance:
    """Tests specifically required by the Session 4 planning document."""
    
    async def test_get_balance_cached(self, bytes_service, mock_api_client):
        """Test get_balance caching as specified in planning document."""
        # Setup - use valid Discord ID format
        guild_id = "123456789012345678"
        user_id = "987654321098765432"
        
        mock_api_client.get.return_value = AsyncMock(
            status_code=200,
            json=lambda: {
                "guild_id": guild_id,
                "user_id": user_id,
                "balance": 100,
                "total_received": 150,
                "total_sent": 50,
                "streak_count": 5,
                "last_daily": "2024-01-01",
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-14T12:00:00Z"
            }
        )
        
        # First call hits API
        balance1 = await bytes_service.get_balance(guild_id, user_id)
        assert balance1.balance == 100
        assert mock_api_client.get.call_count == 1
        
        # Second call uses cache
        balance2 = await bytes_service.get_balance(guild_id, user_id)
        assert balance2.balance == 100
        assert mock_api_client.get.call_count == 1  # No additional call
    
    async def test_transfer_validation(self, bytes_service):
        """Test transfer validation as specified in planning document."""
        # Create mock User objects as in planning document
        class MockUser:
            def __init__(self, user_id: str, username: str):
                self.id = user_id
                self._username = username
            def __str__(self):
                return self._username
        
        user1 = MockUser("123456789012345678", "TestUser") 
        user2 = MockUser("987654321098765432", "OtherUser")
        
        # Test self-transfer using User objects (planning document signature)
        result = await bytes_service.transfer_bytes(
            "111111111111111111", user1, user1, 100
        )
        assert not result.success
        assert "yourself" in result.reason
        
        # Test negative amount using User objects
        result = await bytes_service.transfer_bytes(
            "111111111111111111", user1, user2, -50
        )
        assert not result.success
        assert "positive" in result.reason
    
    async def test_calculate_multiplier(self, bytes_service):
        """Test _calculate_multiplier method as specified in planning document."""
        assert bytes_service._calculate_multiplier(0) == 1
        assert bytes_service._calculate_multiplier(7) == 2
        assert bytes_service._calculate_multiplier(14) == 4
        assert bytes_service._calculate_multiplier(30) == 10
        assert bytes_service._calculate_multiplier(60) == 20
        assert bytes_service._calculate_multiplier(100) == 20  # Max multiplier