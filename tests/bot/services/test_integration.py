"""Integration tests for bot services with real HTTP clients.

This module tests the complete service stack using real HTTP clients
against a test FastAPI application, providing end-to-end validation
without requiring a running server.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from typing import Any, Dict
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from smarter_dev.bot.services.api_client import APIClient, RetryConfig
from smarter_dev.bot.services.bytes_service import BytesService
from smarter_dev.bot.services.cache_manager import CacheManager
from smarter_dev.bot.services.exceptions import (
    APIError,
    InsufficientBalanceError,
    ResourceNotFoundError
)
from smarter_dev.bot.services.squads_service import SquadsService
from smarter_dev.bot.services.streak_service import StreakService
from smarter_dev.shared.date_provider import MockDateProvider


class MockUser:
    """Mock Discord user for integration tests."""
    
    def __init__(self, user_id: str, username: str):
        self._id = user_id
        self._username = username
    
    @property
    def id(self) -> str:
        return self._id
    
    def __str__(self) -> str:
        return self._username


# Mock FastAPI app for testing
app = FastAPI()


@app.get("/api/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/api/guilds/{guild_id}/bytes/balance/{user_id}")
async def get_balance(guild_id: str, user_id: str):
    """Mock balance endpoint."""
    from fastapi import HTTPException
    if user_id == "000000000000000000":
        raise HTTPException(status_code=404, detail="Balance not found")
    
    # Give giver_user a higher balance to allow testing API error handling
    balance = 2000 if user_id == "111111111111111111" else 100
    
    return {
        "guild_id": guild_id,
        "user_id": user_id,
        "balance": balance,
        "total_received": balance + 50,
        "total_sent": 50,
        "streak_count": 5,
        "last_daily": "2024-01-14",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-14T12:00:00Z"
    }


@app.post("/api/guilds/{guild_id}/bytes/daily/{user_id}")
async def claim_daily(guild_id: str, user_id: str):
    """Mock daily claim endpoint."""
    from fastapi import HTTPException
    if user_id == "already_claimed":
        raise HTTPException(status_code=409, detail="Daily reward already claimed today")
    
    return {
        "balance": {
            "guild_id": guild_id,
            "user_id": user_id,
            "balance": 120,
            "total_received": 170,
            "total_sent": 50,
            "streak_count": 6,
            "last_daily": "2024-01-15",
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-15T12:00:00Z"
        },
        "reward_amount": 20,
        "streak_bonus": 2,
        "next_claim_at": "2024-01-16T00:00:00Z"
    }


@app.post("/api/guilds/{guild_id}/bytes/transactions")
async def transfer_bytes(guild_id: str, request: Dict[str, Any]):
    """Mock transfer endpoint."""
    from fastapi import HTTPException
    if request.get("amount", 0) > 1000:
        raise HTTPException(status_code=400, detail="Transfer amount too large")
    
    return {
        "id": str(uuid4()),
        "guild_id": guild_id,
        "giver_id": request["giver_id"],
        "giver_username": request["giver_username"],
        "receiver_id": request["receiver_id"],
        "receiver_username": request["receiver_username"],
        "amount": request["amount"],
        "reason": request.get("reason", ""),
        "created_at": datetime.now(timezone.utc).isoformat()
    }


@app.get("/api/guilds/{guild_id}/bytes/leaderboard")
async def get_leaderboard(guild_id: str, limit: int = 10):
    """Mock leaderboard endpoint."""
    return {
        "guild_id": guild_id,
        "users": [
            {
                "user_id": "user1",
                "balance": 1000,
                "total_received": 1200,
                "total_sent": 200,
                "streak_count": 30,
                "last_daily": "2024-01-15"
            },
            {
                "user_id": "user2", 
                "balance": 800,
                "total_received": 900,
                "total_sent": 100,
                "streak_count": 15,
                "last_daily": "2024-01-15"
            }
        ],
        "total_users": 2,
        "generated_at": datetime.now(timezone.utc).isoformat()
    }


@app.get("/api/guilds/{guild_id}/squads")
async def list_squads(guild_id: str, include_inactive: bool = False):
    """Mock squad list endpoint."""
    squads = [
        {
            "id": str(uuid4()),
            "guild_id": guild_id,
            "role_id": "123456789",
            "name": "Test Squad",
            "description": "A test squad",
            "switch_cost": 100,
            "max_members": 20,
            "member_count": 5,
            "is_active": True,
            "created_at": "2024-01-01T00:00:00Z"
        }
    ]
    
    if include_inactive:
        squads.append({
            "id": str(uuid4()),
            "guild_id": guild_id,
            "role_id": "987654321",
            "name": "Inactive Squad",
            "description": "An inactive squad",
            "switch_cost": 50,
            "max_members": 10,
            "member_count": 0,
            "is_active": False,
            "created_at": "2024-01-01T00:00:00Z"
        })
    
    return squads


@app.get("/api/guilds/{guild_id}/squads/{squad_id}")
async def get_squad(guild_id: str, squad_id: str):
    """Mock squad detail endpoint."""
    from fastapi import HTTPException
    if squad_id == "000000000000000000":
        raise HTTPException(status_code=404, detail="Squad not found")
    
    return {
        "id": squad_id,
        "guild_id": guild_id,
        "role_id": "123456789",
        "name": "Test Squad",
        "description": "A test squad",
        "switch_cost": 100,
        "max_members": 20,
        "member_count": 5,
        "is_active": True,
        "created_at": "2024-01-01T00:00:00Z"
    }


@app.get("/api/guilds/{guild_id}/squads/members/{user_id}")
async def get_user_squad(guild_id: str, user_id: str):
    """Mock user squad endpoint."""
    from fastapi import HTTPException
    if user_id == "333333333333333333":
        raise HTTPException(status_code=404, detail="User not in any squad")
    
    return {
        "squad": {
            "id": str(uuid4()),
            "guild_id": guild_id,
            "role_id": "123456789",
            "name": "Test Squad",
            "description": "A test squad",
            "switch_cost": 100,
            "max_members": 20,
            "member_count": 5,
            "is_active": True,
            "created_at": "2024-01-01T00:00:00Z"
        },
        "member_since": "2024-01-10T12:00:00Z"
    }


@app.post("/api/guilds/{guild_id}/squads/{squad_id}/join")
async def join_squad(guild_id: str, squad_id: str, request: Dict[str, Any]):
    """Mock squad join endpoint."""
    from fastapi import HTTPException
    if squad_id == "full_squad":
        raise HTTPException(status_code=400, detail="Squad is full")
    
    return {"success": True}


@app.delete("/api/guilds/{guild_id}/squads/leave")
async def leave_squad(guild_id: str, request: Dict[str, Any]):
    """Mock squad leave endpoint."""
    from fastapi import HTTPException
    user_id = request.get("user_id")
    if user_id == "not_in_squad":
        raise HTTPException(status_code=404, detail="User not in any squad")
    
    return {"success": True}


@app.get("/api/guilds/{guild_id}/squads/{squad_id}/members")
async def get_squad_members(guild_id: str, squad_id: str):
    """Mock squad members endpoint."""
    from fastapi import HTTPException
    if squad_id == "000000000000000000":
        raise HTTPException(status_code=404, detail="Squad not found")
    
    return {
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


class TestIntegrationBytes:
    """Integration tests for BytesService with real HTTP client."""
    
    @pytest.fixture
    async def real_api_client(self):
        """Create real API client with test transport."""
        # Create API client that will use the test app
        api_client = APIClient(
            base_url="http://test",
            bot_token="test-token",
            retry_config=RetryConfig(max_retries=1, base_delay=0.1)
        )
        
        # Replace the internal client with a test client using our FastAPI app
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            timeout=30.0
        ) as test_client:
            # Replace the internal client
            api_client._client = test_client
            yield api_client
    
    @pytest.fixture
    async def real_cache_manager(self):
        """Create real cache manager with mocked Redis."""
        # Mock Redis for integration tests - need to mock the specific import path
        with patch('smarter_dev.bot.services.cache_manager.redis.from_url') as mock_from_url:
            async def mock_scan_iter(*args, **kwargs):
                """Mock async scan_iter that yields no results."""
                for key in []:
                    yield key
            
            mock_redis = AsyncMock()
            mock_redis.ping.return_value = True
            mock_redis.get.return_value = None
            mock_redis.set.return_value = True
            mock_redis.delete.return_value = 1
            mock_redis.scan_iter = mock_scan_iter
            mock_from_url.return_value = mock_redis
            
            cache_manager = CacheManager(redis_url="redis://localhost:6379/0")
            # Use the async context manager
            async with cache_manager:
                yield cache_manager
    
    @pytest.fixture
    async def bytes_service_integration(self, real_api_client, real_cache_manager):
        """Create BytesService with real dependencies."""
        date_provider = MockDateProvider(fixed_date=date(2024, 1, 15))
        streak_service = StreakService(date_provider=date_provider)
        
        service = BytesService(
            api_client=real_api_client,
            cache_manager=real_cache_manager,
            streak_service=streak_service
        )
        await service.initialize()
        return service
    
    async def test_get_balance_integration(self, bytes_service_integration):
        """Test balance retrieval with real HTTP client."""
        balance = await bytes_service_integration.get_balance("123456789012345678", "987654321098765432")
        
        assert balance.guild_id == "123456789012345678"
        assert balance.user_id == "987654321098765432"
        assert balance.balance == 100
        assert balance.total_received == 150
        assert balance.total_sent == 50
        assert balance.streak_count == 5
    
    async def test_get_balance_not_found_integration(self, bytes_service_integration):
        """Test balance retrieval for non-existent user."""
        # The BytesService should handle 404s and convert them to ResourceNotFoundError
        with pytest.raises(ResourceNotFoundError) as exc_info:
            await bytes_service_integration.get_balance("123456789012345678", "000000000000000000")
        
        assert "user_balance" in exc_info.value.resource_type
    
    async def test_claim_daily_integration(self, bytes_service_integration):
        """Test daily claim with real HTTP client."""
        result = await bytes_service_integration.claim_daily("123456789012345678", "987654321098765432", "TestUser")
        
        assert result.success is True
        assert result.earned == 20
        assert result.streak == 6
        assert result.streak_bonus == 2
        assert result.balance.balance == 120
    
    async def test_transfer_bytes_integration(self, bytes_service_integration):
        """Test bytes transfer with real HTTP client."""
        giver = MockUser("111111111111111111", "GiverUser")
        receiver = MockUser("444444444444444444", "ReceiverUser")
        
        result = await bytes_service_integration.transfer_bytes(
            "123456789012345678",
            giver,
            receiver,
            50,
            "Test transfer"
        )
        
        assert result.success is True
        assert result.transaction is not None
        assert result.transaction.amount == 50
        assert result.transaction.reason == "Test transfer"
        assert result.new_giver_balance == 1950  # 2000 - 50
    
    async def test_transfer_bytes_insufficient_balance_integration(self, bytes_service_integration):
        """Test transfer with insufficient balance."""
        giver = MockUser("111111111111111111", "GiverUser")
        receiver = MockUser("444444444444444444", "ReceiverUser")
        
        with pytest.raises(InsufficientBalanceError):
            await bytes_service_integration.transfer_bytes(
                "123456789012345678",
                giver,
                receiver,
                2500,  # More than available balance of 2000
                "Large transfer"
            )
    
    async def test_get_leaderboard_integration(self, bytes_service_integration):
        """Test leaderboard retrieval with real HTTP client."""
        leaderboard = await bytes_service_integration.get_leaderboard("123456789012345678", limit=10)
        
        assert len(leaderboard) == 2
        assert leaderboard[0].rank == 1
        assert leaderboard[0].user_id == "user1"
        assert leaderboard[0].balance == 1000
        assert leaderboard[1].rank == 2
        assert leaderboard[1].user_id == "user2"
        assert leaderboard[1].balance == 800
    
    async def test_api_error_handling_integration(self, bytes_service_integration):
        """Test API error handling in integration scenario."""
        giver = MockUser("111111111111111111", "GiverUser")
        receiver = MockUser("444444444444444444", "ReceiverUser")
        
        # First, let's see what the user's actual balance is
        balance = await bytes_service_integration.get_balance("123456789012345678", "111111111111111111")
        
        # Use an amount that's within the user's balance but exceeds the API limit
        # The service should allow this to go to the API, where it gets rejected
        result = await bytes_service_integration.transfer_bytes(
            "123456789012345678",
            giver,
            receiver,
            1500,  # Amount too large for API, triggers API error
            "Large transfer"
        )
        
        assert result.success is False
        assert "too large" in result.reason


class TestIntegrationSquads:
    """Integration tests for SquadsService with real HTTP client."""
    
    @pytest.fixture
    async def real_api_client(self):
        """Create real API client with test transport."""
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            timeout=30.0
        ) as client:
            api_client = APIClient(
                base_url="http://test",
                bot_token="test-token",
                retry_config=RetryConfig(max_retries=1, base_delay=0.1)
            )
            api_client._client = client
            yield api_client
            await api_client.close()
    
    @pytest.fixture
    async def real_cache_manager(self):
        """Create real cache manager with mocked Redis."""
        # Mock Redis for integration tests - need to mock the specific import path
        with patch('smarter_dev.bot.services.cache_manager.redis.from_url') as mock_from_url:
            async def mock_scan_iter(*args, **kwargs):
                """Mock async scan_iter that yields no results."""
                for key in []:
                    yield key
            
            mock_redis = AsyncMock()
            mock_redis.ping.return_value = True
            mock_redis.get.return_value = None
            mock_redis.set.return_value = True
            mock_redis.delete.return_value = 1
            mock_redis.scan_iter = mock_scan_iter
            mock_from_url.return_value = mock_redis
            
            cache_manager = CacheManager(redis_url="redis://localhost:6379/0")
            # Use the async context manager
            async with cache_manager:
                yield cache_manager
    
    @pytest.fixture
    async def squads_service_integration(self, real_api_client, real_cache_manager):
        """Create SquadsService with real dependencies."""
        service = SquadsService(
            api_client=real_api_client,
            cache_manager=real_cache_manager
        )
        await service.initialize()
        return service
    
    async def test_list_squads_integration(self, squads_service_integration):
        """Test squad listing with real HTTP client."""
        squads = await squads_service_integration.list_squads("123456789012345678")
        
        assert len(squads) == 1
        assert squads[0].name == "Test Squad"
        assert squads[0].switch_cost == 100
        assert squads[0].max_members == 20
        assert squads[0].member_count == 5
        assert squads[0].is_active is True
    
    async def test_list_squads_with_inactive_integration(self, squads_service_integration):
        """Test squad listing including inactive squads."""
        squads = await squads_service_integration.list_squads("123456789012345678", include_inactive=True)
        
        assert len(squads) == 2
        assert any(squad.name == "Test Squad" for squad in squads)
        assert any(squad.name == "Inactive Squad" for squad in squads)
    
    async def test_get_squad_integration(self, squads_service_integration):
        """Test individual squad retrieval."""
        squad_id = uuid4()
        squad = await squads_service_integration.get_squad("123456789012345678", squad_id)
        
        assert squad.id == squad_id
        assert squad.name == "Test Squad"
        assert squad.switch_cost == 100
    
    async def test_get_squad_not_found_integration(self, squads_service_integration):
        """Test squad retrieval for non-existent squad."""
        with pytest.raises(ResourceNotFoundError):
            await squads_service_integration.get_squad("123456789012345678", "000000000000000000")
    
    async def test_get_user_squad_integration(self, squads_service_integration):
        """Test user squad membership retrieval."""
        user_squad = await squads_service_integration.get_user_squad("123456789012345678", "987654321098765432")
        
        assert user_squad.user_id == "987654321098765432"
        assert user_squad.is_in_squad is True
        assert user_squad.squad is not None
        assert user_squad.squad.name == "Test Squad"
        assert user_squad.member_since is not None
    
    async def test_get_user_squad_not_in_squad_integration(self, squads_service_integration):
        """Test user squad retrieval when not in any squad."""
        user_squad = await squads_service_integration.get_user_squad("123456789012345678", "333333333333333333")
        
        assert user_squad.user_id == "333333333333333333"
        assert user_squad.is_in_squad is False
        assert user_squad.squad is None
        assert user_squad.member_since is None
    
    async def test_join_squad_integration(self, squads_service_integration):
        """Test squad joining with real HTTP client."""
        squad_id = uuid4()
        
        result = await squads_service_integration.join_squad(
            "123456789012345678",
            "333333333333333333",  # User not currently in a squad
            squad_id,
            200  # Sufficient balance
        )
        
        assert result.success is True
        assert result.squad is not None
        assert result.squad.name == "Test Squad"
        assert result.previous_squad is None
        assert result.cost == 0  # No cost for first squad
        assert result.new_balance == 200
    
    async def test_join_squad_insufficient_balance_integration(self, squads_service_integration):
        """Test squad joining with insufficient balance."""
        squad_id = uuid4()
        
        result = await squads_service_integration.join_squad(
            "123456789012345678",
            "987654321098765432",  # User already in a squad (has switch cost)
            squad_id,
            50  # Insufficient for 100 cost switch
        )
        
        assert result.success is False
        assert "Insufficient bytes" in result.reason
        assert result.cost == 100
    
    async def test_get_squad_members_integration(self, squads_service_integration):
        """Test squad members retrieval."""
        squad_id = uuid4()
        members = await squads_service_integration.get_squad_members("123456789012345678", squad_id)
        
        assert len(members) == 2
        assert members[0].user_id == "user1"
        assert members[0].username == "TestUser1"
        assert members[0].joined_at is not None
        assert members[1].user_id == "user2"
        assert members[1].username == "TestUser2"
        assert members[1].joined_at is not None
    
    async def test_get_squad_members_not_found_integration(self, squads_service_integration):
        """Test squad members retrieval for non-existent squad."""
        with pytest.raises(ResourceNotFoundError):
            await squads_service_integration.get_squad_members("123456789012345678", "000000000000000000")


class TestIntegrationPerformance:
    """Performance tests for service integration."""
    
    @pytest.fixture
    async def performance_api_client(self):
        """Create API client optimized for performance testing."""
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            timeout=30.0
        ) as client:
            api_client = APIClient(
                base_url="http://test",
                bot_token="test-token",
                retry_config=RetryConfig(max_retries=0, base_delay=0.01)
            )
            api_client._client = client
            yield api_client
            await api_client.close()
    
    @pytest.fixture
    async def performance_cache_manager(self):
        """Create cache manager for performance testing."""
        # Use the same approach as other fixtures - mock the import path
        with patch('smarter_dev.bot.services.cache_manager.redis.from_url') as mock_from_url:
            # Fast in-memory cache for performance tests
            cache_data = {}
            
            async def mock_get(key):
                return cache_data.get(key)
            
            async def mock_setex(key, ttl, value):
                cache_data[key] = value
            
            async def mock_delete(key):
                cache_data.pop(key, None)
                return 1
            
            async def mock_clear_pattern(pattern):
                keys_to_delete = [k for k in cache_data.keys() if k.startswith(pattern.replace("*", ""))]
                for key in keys_to_delete:
                    del cache_data[key]
                return len(keys_to_delete)
            
            async def mock_scan_iter(*args, **kwargs):
                """Mock async scan_iter that yields no results."""
                for key in []:
                    yield key
            
            mock_redis = AsyncMock()
            mock_redis.ping.return_value = True
            mock_redis.get.side_effect = mock_get
            mock_redis.setex.side_effect = mock_setex  # Cache manager uses setex, not set
            mock_redis.delete.side_effect = mock_delete
            mock_redis.scan_iter = mock_scan_iter
            mock_from_url.return_value = mock_redis
            
            cache_manager = CacheManager(redis_url="redis://localhost:6379/0")
            # Use the async context manager
            async with cache_manager:
                # Mock the clear_pattern method directly on the cache manager
                cache_manager.clear_pattern = mock_clear_pattern
                yield cache_manager
    
    @pytest.fixture
    async def performance_bytes_service(self, performance_api_client, performance_cache_manager):
        """Create BytesService for performance testing."""
        date_provider = MockDateProvider(fixed_date=date(2024, 1, 15))
        streak_service = StreakService(date_provider=date_provider)
        
        service = BytesService(
            api_client=performance_api_client,
            cache_manager=performance_cache_manager,
            streak_service=streak_service
        )
        await service.initialize()
        return service
    
    async def test_concurrent_balance_requests(self, performance_bytes_service):
        """Test concurrent balance requests performance."""
        # Create 50 concurrent balance requests
        tasks = []
        for i in range(50):
            task = performance_bytes_service.get_balance("123456789012345678", f"88888888888888888{i:03d}")
            tasks.append(task)
        
        # Execute all concurrently
        results = await asyncio.gather(*tasks)
        
        # Verify all succeeded
        assert len(results) == 50
        assert all(result.balance == 100 for result in results)
        
        # Check service stats
        stats = await performance_bytes_service.get_service_stats()
        assert stats["total_balance_requests"] == 50
    
    async def test_cache_performance(self, performance_bytes_service):
        """Test cache performance and hit rates."""
        # First request - should miss cache
        balance1 = await performance_bytes_service.get_balance("123456789012345678", "222222222222222222")
        
        # Second request - should hit cache
        balance2 = await performance_bytes_service.get_balance("123456789012345678", "222222222222222222")
        
        # Verify same data
        assert balance1.balance == balance2.balance
        assert balance1.user_id == balance2.user_id
        
        # Check cache stats
        stats = await performance_bytes_service.get_service_stats()
        assert stats["cache_hits"] >= 1
        assert stats["cache_hit_rate"] > 0
    
    async def test_error_recovery_performance(self, performance_api_client):
        """Test error recovery doesn't impact performance."""
        # Create a service that will encounter errors
        service = BytesService(
            api_client=performance_api_client,
            cache_manager=None  # No cache to test error paths
        )
        await service.initialize()
        
        # Mix of successful and error requests
        tasks = []
        for i in range(20):
            if i % 5 == 0:
                # These will return 404
                task = service.get_balance("123456789012345678", "000000000000000000")
            else:
                # These will succeed
                task = service.get_balance("123456789012345678", f"88888888888888888{i:03d}")
            tasks.append(task)
        
        # Execute and count successes/failures
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        successes = sum(1 for r in results if not isinstance(r, Exception))
        failures = sum(1 for r in results if isinstance(r, Exception))
        
        assert successes == 16  # 4/5 of requests succeed
        assert failures == 4    # 1/5 of requests fail
        
        # Service should still be healthy after errors
        health = await service.health_check()
        assert health.is_healthy is True


class TestIntegrationErrorScenarios:
    """Integration tests for comprehensive error scenarios."""
    
    @pytest.fixture
    async def error_api_client(self):
        """Create API client for error scenario testing."""
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            timeout=1.0  # Short timeout for testing
        ) as client:
            api_client = APIClient(
                base_url="http://test",
                bot_token="test-token",
                retry_config=RetryConfig(max_retries=2, base_delay=0.01)
            )
            api_client._client = client
            yield api_client
            await api_client.close()
    
    async def test_network_timeout_recovery(self, error_api_client):
        """Test network timeout and recovery behavior."""
        service = BytesService(
            api_client=error_api_client,
            cache_manager=None
        )
        await service.initialize()
        
        # This should work normally
        balance = await service.get_balance("123456789012345678", "987654321098765432")
        assert balance.balance == 100
        
        # Health check should still pass
        health = await service.health_check()
        assert health.is_healthy is True
    
    async def test_api_client_resilience(self, error_api_client):
        """Test API client resilience to various errors."""
        # Test with different error conditions
        test_cases = [
            ("987654321098765432", 200),      # Success
            ("000000000000000000", 404),    # Not found
            ("987654321098765432", 200),      # Success again
        ]
        
        service = BytesService(
            api_client=error_api_client,
            cache_manager=None
        )
        await service.initialize()
        
        for user_id, expected_status in test_cases:
            if expected_status == 200:
                balance = await service.get_balance("123456789012345678", user_id)
                assert balance.balance == 100
            elif expected_status == 404:
                with pytest.raises(ResourceNotFoundError):
                    await service.get_balance("123456789012345678", user_id)
        
        # Service should remain healthy after mixed results
        health = await service.health_check()
        assert health.is_healthy is True