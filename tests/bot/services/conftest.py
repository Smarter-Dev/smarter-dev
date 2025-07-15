"""Test configuration and fixtures for bot services.

This module provides comprehensive test fixtures for testing bot services
in isolation with proper mocking and dependency injection.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone, timedelta
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest

from smarter_dev.bot.services.api_client import APIClient
from smarter_dev.bot.services.base import APIClientProtocol, CacheManagerProtocol
from smarter_dev.bot.services.bytes_service import BytesService
from smarter_dev.bot.services.cache_manager import CacheManager
from smarter_dev.bot.services.models import (
    BytesBalance,
    BytesTransaction,
    Squad,
    ServiceHealth
)
from smarter_dev.bot.services.squads_service import SquadsService
from smarter_dev.bot.services.streak_service import StreakService
from smarter_dev.shared.date_provider import MockDateProvider


class MockAPIClient(APIClientProtocol):
    """Mock API client for testing."""
    
    def __init__(self):
        # Create mocks that implement both the async methods and provide direct access
        self.get = AsyncMock()
        self.post = AsyncMock()
        self.put = AsyncMock()
        self.delete = AsyncMock()
        self.close = AsyncMock()
        self.health_check = AsyncMock()
        
        # Default health check response
        self.health_check.return_value = ServiceHealth(
            service_name="MockAPIClient",
            is_healthy=True,
            response_time_ms=10.0,
            last_check=datetime.now(timezone.utc)
        )


class MockCacheManager(CacheManagerProtocol):
    """Mock cache manager for testing."""
    
    def __init__(self):
        self._cache: Dict[str, Any] = {}
        self.get = AsyncMock(side_effect=self._get)
        self.set = AsyncMock(side_effect=self._set)
        self.delete = AsyncMock(side_effect=self._delete)
        self.clear_pattern = AsyncMock(side_effect=self._clear_pattern)
        self.health_check = AsyncMock()
        
        # Default health check response
        self.health_check.return_value = ServiceHealth(
            service_name="MockCacheManager",
            is_healthy=True,
            response_time_ms=5.0,
            last_check=datetime.now(timezone.utc)
        )
    
    async def _get(self, key: str) -> Optional[Any]:
        return self._cache.get(key)
    
    async def _set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        self._cache[key] = value
    
    async def _delete(self, key: str) -> None:
        self._cache.pop(key, None)
    
    async def _clear_pattern(self, pattern: str) -> int:
        # Simple pattern matching for tests
        keys_to_delete = []
        pattern_prefix = pattern.replace("*", "")
        
        for key in self._cache:
            if key.startswith(pattern_prefix):
                keys_to_delete.append(key)
        
        for key in keys_to_delete:
            del self._cache[key]
        
        return len(keys_to_delete)
    
    def clear_cache(self) -> None:
        """Helper method to clear all cache for tests."""
        self._cache.clear()


class MockResponse:
    """Mock HTTP response for testing."""
    
    def __init__(
        self,
        status_code: int = 200,
        json_data: Optional[Dict[str, Any]] = None,
        text: str = ""
    ):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.text = text
    
    def json(self) -> Dict[str, Any]:
        return self._json_data


# Test fixtures

@pytest.fixture
def mock_api_client() -> MockAPIClient:
    """Create mock API client for testing."""
    return MockAPIClient()


@pytest.fixture
def mock_cache_manager() -> MockCacheManager:
    """Create mock cache manager for testing."""
    return MockCacheManager()


@pytest.fixture
def mock_date_provider() -> MockDateProvider:
    """Create mock date provider for testing."""
    return MockDateProvider(fixed_date=date(2024, 1, 15))


@pytest.fixture
def test_guild_id() -> str:
    """Test guild ID."""
    return "123456789012345678"


@pytest.fixture
def test_user_id() -> str:
    """Test user ID."""
    return "987654321098765432"


@pytest.fixture
def test_user_id_2() -> str:
    """Second test user ID."""
    return "111222333444555666"


@pytest.fixture
def test_squad_id() -> UUID:
    """Test squad ID."""
    return uuid4()


@pytest.fixture
def sample_bytes_balance(test_guild_id: str, test_user_id: str) -> BytesBalance:
    """Sample bytes balance for testing."""
    return BytesBalance(
        guild_id=test_guild_id,
        user_id=test_user_id,
        balance=100,
        total_received=150,
        total_sent=50,
        streak_count=5,
        last_daily=date(2024, 1, 14),
        created_at=datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
        updated_at=datetime(2024, 1, 14, 12, 0, 0, tzinfo=timezone.utc)
    )


@pytest.fixture
def sample_bytes_transaction(test_guild_id: str, test_user_id: str, test_user_id_2: str) -> BytesTransaction:
    """Sample bytes transaction for testing."""
    return BytesTransaction(
        id=uuid4(),
        guild_id=test_guild_id,
        giver_id=test_user_id,
        giver_username="TestUser1",
        receiver_id=test_user_id_2,
        receiver_username="TestUser2",
        amount=50,
        reason="Test transfer",
        created_at=datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
    )


@pytest.fixture
def sample_squad(test_guild_id: str, test_squad_id: UUID) -> Squad:
    """Sample squad for testing."""
    return Squad(
        id=test_squad_id,
        guild_id=test_guild_id,
        role_id="555666777888999000",
        name="Test Squad",
        description="A test squad for unit testing",
        switch_cost=100,
        max_members=20,
        member_count=5,
        is_active=True,
        created_at=datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    )


@pytest.fixture
async def bytes_service(
    mock_api_client: MockAPIClient,
    mock_cache_manager: MockCacheManager,
    mock_date_provider: MockDateProvider
) -> BytesService:
    """Create bytes service with mocked dependencies."""
    streak_service = StreakService(date_provider=mock_date_provider)
    service = BytesService(
        api_client=mock_api_client,
        cache_manager=mock_cache_manager,
        streak_service=streak_service
    )
    await service.initialize()
    return service


@pytest.fixture
async def squads_service(
    mock_api_client: MockAPIClient,
    mock_cache_manager: MockCacheManager
) -> SquadsService:
    """Create squads service with mocked dependencies."""
    service = SquadsService(
        api_client=mock_api_client,
        cache_manager=mock_cache_manager
    )
    await service.initialize()
    return service


@pytest.fixture
def balance_api_response(sample_bytes_balance: BytesBalance) -> Dict[str, Any]:
    """API response for balance request."""
    return {
        "guild_id": sample_bytes_balance.guild_id,
        "user_id": sample_bytes_balance.user_id,
        "balance": sample_bytes_balance.balance,
        "total_received": sample_bytes_balance.total_received,
        "total_sent": sample_bytes_balance.total_sent,
        "streak_count": sample_bytes_balance.streak_count,
        "last_daily": sample_bytes_balance.last_daily.isoformat() if sample_bytes_balance.last_daily else None,
        "created_at": sample_bytes_balance.created_at.isoformat() if sample_bytes_balance.created_at else None,
        "updated_at": sample_bytes_balance.updated_at.isoformat() if sample_bytes_balance.updated_at else None
    }


@pytest.fixture
def daily_claim_api_response(sample_bytes_balance: BytesBalance) -> Dict[str, Any]:
    """API response for daily claim request."""
    updated_balance = BytesBalance(
        guild_id=sample_bytes_balance.guild_id,
        user_id=sample_bytes_balance.user_id,
        balance=sample_bytes_balance.balance + 20,  # +20 bytes earned
        total_received=sample_bytes_balance.total_received + 20,
        total_sent=sample_bytes_balance.total_sent,
        streak_count=sample_bytes_balance.streak_count + 1,
        last_daily=date(2024, 1, 15),  # Today
        created_at=sample_bytes_balance.created_at,
        updated_at=datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
    )
    
    return {
        "balance": {
            "guild_id": updated_balance.guild_id,
            "user_id": updated_balance.user_id,
            "balance": updated_balance.balance,
            "total_received": updated_balance.total_received,
            "total_sent": updated_balance.total_sent,
            "streak_count": updated_balance.streak_count,
            "last_daily": updated_balance.last_daily.isoformat(),
            "created_at": updated_balance.created_at.isoformat(),
            "updated_at": updated_balance.updated_at.isoformat()
        },
        "reward_amount": 20,
        "streak_bonus": 2,
        "next_claim_at": "2024-01-16T00:00:00Z"
    }


@pytest.fixture
def transaction_api_response(sample_bytes_transaction: BytesTransaction) -> Dict[str, Any]:
    """API response for transaction creation."""
    return {
        "id": str(sample_bytes_transaction.id),
        "guild_id": sample_bytes_transaction.guild_id,
        "giver_id": sample_bytes_transaction.giver_id,
        "giver_username": sample_bytes_transaction.giver_username,
        "receiver_id": sample_bytes_transaction.receiver_id,
        "receiver_username": sample_bytes_transaction.receiver_username,
        "amount": sample_bytes_transaction.amount,
        "reason": sample_bytes_transaction.reason,
        "created_at": sample_bytes_transaction.created_at.isoformat()
    }


@pytest.fixture
def leaderboard_api_response(test_guild_id: str) -> Dict[str, Any]:
    """API response for leaderboard request."""
    return {
        "guild_id": test_guild_id,
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
            },
            {
                "user_id": "user3",
                "balance": 600,
                "total_received": 650,
                "total_sent": 50,
                "streak_count": 10,
                "last_daily": "2024-01-14"
            }
        ],
        "total_users": 3,
        "generated_at": "2024-01-15T12:00:00Z"
    }


@pytest.fixture
def squad_api_response(sample_squad: Squad) -> Dict[str, Any]:
    """API response for squad request."""
    return {
        "id": str(sample_squad.id),
        "guild_id": sample_squad.guild_id,
        "role_id": sample_squad.role_id,
        "name": sample_squad.name,
        "description": sample_squad.description,
        "switch_cost": sample_squad.switch_cost,
        "max_members": sample_squad.max_members,
        "member_count": sample_squad.member_count,
        "is_active": sample_squad.is_active,
        "created_at": sample_squad.created_at.isoformat() if sample_squad.created_at else None
    }


@pytest.fixture
def squads_list_api_response(sample_squad: Squad) -> List[Dict[str, Any]]:
    """API response for squads list request."""
    return [
        {
            "id": str(sample_squad.id),
            "guild_id": sample_squad.guild_id,
            "role_id": sample_squad.role_id,
            "name": sample_squad.name,
            "description": sample_squad.description,
            "switch_cost": sample_squad.switch_cost,
            "max_members": sample_squad.max_members,
            "member_count": sample_squad.member_count,
            "is_active": sample_squad.is_active,
            "created_at": sample_squad.created_at.isoformat() if sample_squad.created_at else None
        },
        {
            "id": str(uuid4()),
            "guild_id": sample_squad.guild_id,
            "role_id": "999888777666555444",
            "name": "Another Squad",
            "description": "Another test squad",
            "switch_cost": 50,
            "max_members": 15,
            "member_count": 3,
            "is_active": True,
            "created_at": "2024-01-02T00:00:00Z"
        }
    ]


# Helper functions for tests

def create_mock_response(
    status_code: int = 200,
    json_data: Optional[Dict[str, Any]] = None,
    text: str = ""
) -> MockResponse:
    """Create a mock HTTP response."""
    return MockResponse(status_code, json_data, text)


def assert_cache_operations(
    cache_manager: MockCacheManager,
    expected_gets: int = 0,
    expected_sets: int = 0,
    expected_deletes: int = 0
) -> None:
    """Assert expected cache operations were performed."""
    assert cache_manager.get.call_count == expected_gets
    assert cache_manager.set.call_count == expected_sets
    assert cache_manager.delete.call_count == expected_deletes