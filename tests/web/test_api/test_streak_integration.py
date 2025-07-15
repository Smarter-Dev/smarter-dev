"""Integration tests for streak system across API, service, and database layers.

These tests ensure the complete streak workflow functions correctly from
API endpoint through to database storage, with proper UTC date handling.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta, datetime
from typing import Dict, Any, AsyncGenerator
from unittest.mock import Mock, patch, AsyncMock

import pytest
from httpx import AsyncClient

from smarter_dev.shared.date_provider import MockDateProvider, set_date_provider, reset_date_provider
from smarter_dev.web.crud import BytesOperations, BytesConfigOperations


class TestStreakIntegration:
    """Integration tests for complete streak workflow."""
    
    @pytest.fixture(autouse=True)
    def setup_and_teardown(self):
        """Setup and teardown for each test."""
        # Reset date provider before each test
        reset_date_provider()
        yield
        # Reset date provider after each test
        reset_date_provider()
    
    @pytest.fixture
    def mock_date_provider(self) -> MockDateProvider:
        """Create mock date provider for deterministic testing."""
        provider = MockDateProvider(fixed_date=date(2024, 1, 15))
        set_date_provider(provider)
        return provider
    
    @pytest.fixture
    def standard_config_data(self, test_guild_id: str) -> Dict[str, Any]:
        """Standard bytes configuration with streak bonuses."""
        return {
            "guild_id": test_guild_id,
            "daily_amount": 10,
            "starting_balance": 100,
            "max_transfer": 1000,
            "daily_cooldown_hours": 24,
            "streak_bonuses": {"7": 2, "14": 3, "30": 5},
            "transfer_tax_rate": 0.0,
            "is_enabled": True
        }
    
    async def test_new_user_first_daily_claim(
        self,
        api_client: AsyncClient,
        bot_headers: Dict[str, str],
        test_guild_id: str,
        test_user_id: str,
        mock_bytes_operations,
        mock_bytes_config_operations,
        standard_config_data: Dict[str, Any],
        mock_date_provider: MockDateProvider
    ):
        """Test complete workflow for new user's first daily claim."""
        # Setup mocks
        config_mock = Mock()
        for key, value in standard_config_data.items():
            setattr(config_mock, key, value)
        mock_bytes_config_operations.get_config.return_value = config_mock
        
        # New user balance (no previous claims)
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        
        balance_mock = Mock()
        balance_mock.guild_id = test_guild_id
        balance_mock.user_id = test_user_id
        balance_mock.balance = 100  # Starting balance
        balance_mock.total_received = 0
        balance_mock.total_sent = 0
        balance_mock.streak_count = 0
        balance_mock.last_daily = None  # Never claimed before
        balance_mock.created_at = now
        balance_mock.updated_at = now
        
        # Updated balance after claim
        updated_balance_mock = Mock()
        updated_balance_mock.guild_id = test_guild_id
        updated_balance_mock.user_id = test_user_id
        updated_balance_mock.balance = 110  # 100 + 10 daily reward
        updated_balance_mock.total_received = 10
        updated_balance_mock.total_sent = 0
        updated_balance_mock.streak_count = 1  # First day of streak
        updated_balance_mock.last_daily = date(2024, 1, 15)
        updated_balance_mock.created_at = datetime(2024, 1, 1, 0, 0, 0)
        updated_balance_mock.updated_at = datetime(2024, 1, 15, 0, 0, 0)
        
        mock_bytes_operations.get_balance.return_value = balance_mock
        mock_bytes_operations.update_daily_reward.return_value = updated_balance_mock
        
        # Make API call
        response = await api_client.post(
            f"/guilds/{test_guild_id}/bytes/daily/{test_user_id}",
            headers=bot_headers
        )
        
        # Verify response
        assert response.status_code == 200
        data = response.json()
        
        assert data["reward_amount"] == 10  # Base daily amount
        assert data["streak_bonus"] == 1    # No bonus for first day
        assert data["balance"]["streak_count"] == 1
        assert data["balance"]["balance"] == 110
        
        # Verify CRUD was called with correct parameters
        mock_bytes_operations.update_daily_reward.assert_called_once()
        call_args = mock_bytes_operations.update_daily_reward.call_args
        # Args: (db, guild_id, user_id, daily_amount, streak_bonus, new_streak_count, claim_date)
        assert call_args[0][5] == 1  # new_streak_count
        assert call_args[0][6] == date(2024, 1, 15)  # claim_date
    
    # @pytest.mark.skip(reason="Complex integration test - skipping for core functionality focus") 
    async def test_consecutive_days_streak_building(
        self,
        api_client: AsyncClient,
        bot_headers: Dict[str, str],
        test_guild_id: str,
        test_user_id: str,
        mock_bytes_operations,
        mock_bytes_config_operations,
        standard_config_data: Dict[str, Any],
        mock_date_provider: MockDateProvider
    ):
        """Test streak building over consecutive days."""
        # Setup config
        config_mock = Mock()
        for key, value in standard_config_data.items():
            setattr(config_mock, key, value)
        mock_bytes_config_operations.get_config.return_value = config_mock
        
        # Day 6 of streak, claiming on day 7
        current_date = date(2024, 1, 15)
        yesterday = date(2024, 1, 14)
        mock_date_provider.set_date(current_date)
        
        balance_mock = Mock()
        balance_mock.guild_id = test_guild_id
        balance_mock.user_id = test_user_id
        balance_mock.balance = 160  # Previous balance
        balance_mock.total_received = 60
        balance_mock.total_sent = 0
        balance_mock.streak_count = 6  # 6-day streak
        balance_mock.last_daily = yesterday  # Claimed yesterday
        balance_mock.created_at = datetime(2024, 1, 1, 0, 0, 0)
        balance_mock.updated_at = datetime(2024, 1, 14, 0, 0, 0)
        
        # Updated balance after 7th day claim (gets 7-day bonus)
        updated_balance_mock = Mock()
        updated_balance_mock.guild_id = test_guild_id
        updated_balance_mock.user_id = test_user_id
        updated_balance_mock.balance = 180  # 160 + (10 * 2) = 180
        updated_balance_mock.total_received = 80
        updated_balance_mock.total_sent = 0
        updated_balance_mock.streak_count = 7  # 7-day streak
        updated_balance_mock.last_daily = current_date
        updated_balance_mock.created_at = datetime(2024, 1, 1, 0, 0, 0)
        updated_balance_mock.updated_at = datetime(2024, 1, 15, 0, 0, 0)
        
        mock_bytes_operations.get_balance.return_value = balance_mock
        mock_bytes_operations.update_daily_reward.return_value = updated_balance_mock
        
        # Make API call
        response = await api_client.post(
            f"/guilds/{test_guild_id}/bytes/daily/{test_user_id}",
            headers=bot_headers
        )
        
        # Verify response
        assert response.status_code == 200
        data = response.json()
        
        assert data["reward_amount"] == 20  # 10 * 2 (7-day bonus)
        assert data["streak_bonus"] == 2    # 7-day bonus
        assert data["balance"]["streak_count"] == 7
        assert data["balance"]["balance"] == 180
        
        # Verify CRUD was called with correct parameters
        call_args = mock_bytes_operations.update_daily_reward.call_args
        # Args: (session, guild_id, user_id, daily_amount, streak_bonus, new_streak_count, claim_date)
        assert call_args[0][5] == 7  # new_streak_count
        assert call_args[0][4] == 2  # streak_bonus
    
    # @pytest.mark.skip(reason="Complex integration test - skipping for core functionality focus")
    async def test_streak_breaks_after_missing_day(
        self,
        api_client: AsyncClient,
        bot_headers: Dict[str, str],
        test_guild_id: str,
        test_user_id: str,
        mock_bytes_operations,
        mock_bytes_config_operations,
        standard_config_data: Dict[str, Any],
        mock_date_provider: MockDateProvider
    ):
        """Test streak resets after missing a day."""
        # Setup config
        config_mock = Mock()
        for key, value in standard_config_data.items():
            setattr(config_mock, key, value)
        mock_bytes_config_operations.get_config.return_value = config_mock
        
        # Current date, but last claim was 2 days ago (missed yesterday)
        current_date = date(2024, 1, 15)
        two_days_ago = date(2024, 1, 13)
        mock_date_provider.set_date(current_date)
        
        balance_mock = Mock()
        balance_mock.guild_id = test_guild_id
        balance_mock.user_id = test_user_id
        balance_mock.balance = 500  # Had a good balance from long streak
        balance_mock.total_received = 400
        balance_mock.total_sent = 0
        balance_mock.streak_count = 20  # Had a 20-day streak
        balance_mock.last_daily = two_days_ago  # But missed yesterday
        balance_mock.created_at = datetime(2024, 1, 1, 0, 0, 0)
        balance_mock.updated_at = datetime(2024, 1, 13, 0, 0, 0)
        
        # Updated balance after claim (streak resets to 1)
        updated_balance_mock = Mock()
        updated_balance_mock.guild_id = test_guild_id
        updated_balance_mock.user_id = test_user_id
        updated_balance_mock.balance = 510  # 500 + 10 (no bonus)
        updated_balance_mock.total_received = 410
        updated_balance_mock.total_sent = 0
        updated_balance_mock.streak_count = 1  # Streak reset
        updated_balance_mock.last_daily = current_date
        updated_balance_mock.created_at = datetime(2024, 1, 1, 0, 0, 0)
        updated_balance_mock.updated_at = datetime(2024, 1, 15, 0, 0, 0)
        
        mock_bytes_operations.get_balance.return_value = balance_mock
        mock_bytes_operations.update_daily_reward.return_value = updated_balance_mock
        
        # Make API call
        response = await api_client.post(
            f"/guilds/{test_guild_id}/bytes/daily/{test_user_id}",
            headers=bot_headers
        )
        
        # Verify response
        assert response.status_code == 200
        data = response.json()
        
        assert data["reward_amount"] == 10  # Base amount (no bonus)
        assert data["streak_bonus"] == 1    # No bonus
        assert data["balance"]["streak_count"] == 1  # Reset to 1
        assert data["balance"]["balance"] == 510
        
        # Verify CRUD was called with streak reset
        call_args = mock_bytes_operations.update_daily_reward.call_args
        # Args: (session, guild_id, user_id, daily_amount, streak_bonus, new_streak_count, claim_date)
        assert call_args[0][5] == 1  # new_streak_count
        assert call_args[0][4] == 1  # streak_bonus
    
    # @pytest.mark.skip(reason="Complex integration test - skipping for core functionality focus")
    async def test_duplicate_claim_blocked(
        self,
        api_client: AsyncClient,
        bot_headers: Dict[str, str],
        test_guild_id: str,
        test_user_id: str,
        mock_bytes_operations,
        mock_bytes_config_operations,
        standard_config_data: Dict[str, Any],
        mock_date_provider: MockDateProvider
    ):
        """Test that duplicate claims on same day are blocked."""
        # Setup config
        config_mock = Mock()
        for key, value in standard_config_data.items():
            setattr(config_mock, key, value)
        mock_bytes_config_operations.get_config.return_value = config_mock
        
        # User already claimed today
        current_date = date(2024, 1, 15)
        mock_date_provider.set_date(current_date)
        
        balance_mock = Mock()
        balance_mock.guild_id = test_guild_id
        balance_mock.user_id = test_user_id
        balance_mock.balance = 120
        balance_mock.total_received = 20
        balance_mock.total_sent = 0
        balance_mock.streak_count = 2
        balance_mock.last_daily = current_date  # Already claimed today
        balance_mock.created_at = datetime(2024, 1, 1, 0, 0, 0)
        balance_mock.updated_at = datetime(2024, 1, 15, 0, 0, 0)
        
        mock_bytes_operations.get_balance.return_value = balance_mock
        
        # Make API call
        response = await api_client.post(
            f"/guilds/{test_guild_id}/bytes/daily/{test_user_id}",
            headers=bot_headers
        )
        
        # Verify response is conflict error
        assert response.status_code == 409
        # Just check that the response contains the error message somewhere
        response_text = response.text
        assert "Daily reward has already been claimed today" in response_text
        
        # Verify CRUD update was not called
        mock_bytes_operations.update_daily_reward.assert_not_called()
    
    # @pytest.mark.skip(reason="Complex integration test - skipping for core functionality focus")
    async def test_high_streak_bonus_calculation(
        self,
        api_client: AsyncClient,
        bot_headers: Dict[str, str],
        test_guild_id: str,
        test_user_id: str,
        mock_bytes_operations,
        mock_bytes_config_operations,
        mock_date_provider: MockDateProvider
    ):
        """Test calculation with high streak reaching 30-day bonus."""
        # Setup config with 30-day bonus
        config_data = {
            "guild_id": test_guild_id,
            "daily_amount": 25,
            "starting_balance": 100,
            "max_transfer": 1000,
            "daily_cooldown_hours": 24,
            "streak_bonuses": {"7": 2, "14": 3, "30": 5, "60": 10},
            "transfer_tax_rate": 0.0,
            "is_enabled": True
        }
        
        config_mock = Mock()
        for key, value in config_data.items():
            setattr(config_mock, key, value)
        mock_bytes_config_operations.get_config.return_value = config_mock
        
        # Day 30 of streak
        current_date = date(2024, 1, 15)
        yesterday = date(2024, 1, 14)
        mock_date_provider.set_date(current_date)
        
        balance_mock = Mock()
        balance_mock.guild_id = test_guild_id
        balance_mock.user_id = test_user_id
        balance_mock.balance = 2000
        balance_mock.total_received = 1900
        balance_mock.total_sent = 0
        balance_mock.streak_count = 29  # 29-day streak, claiming 30th
        balance_mock.last_daily = yesterday
        balance_mock.created_at = datetime(2024, 1, 1, 0, 0, 0)
        balance_mock.updated_at = datetime(2024, 1, 14, 0, 0, 0)
        
        # Updated balance after 30th day claim (gets 30-day bonus)
        updated_balance_mock = Mock()
        updated_balance_mock.guild_id = test_guild_id
        updated_balance_mock.user_id = test_user_id
        updated_balance_mock.balance = 2125  # 2000 + (25 * 5) = 2125
        updated_balance_mock.total_received = 2025
        updated_balance_mock.total_sent = 0
        updated_balance_mock.streak_count = 30
        updated_balance_mock.last_daily = current_date
        updated_balance_mock.created_at = datetime(2024, 1, 1, 0, 0, 0)
        updated_balance_mock.updated_at = datetime(2024, 1, 15, 0, 0, 0)
        
        mock_bytes_operations.get_balance.return_value = balance_mock
        mock_bytes_operations.update_daily_reward.return_value = updated_balance_mock
        
        # Make API call
        response = await api_client.post(
            f"/guilds/{test_guild_id}/bytes/daily/{test_user_id}",
            headers=bot_headers
        )
        
        # Verify response
        assert response.status_code == 200
        data = response.json()
        
        assert data["reward_amount"] == 125  # 25 * 5 (30-day bonus)
        assert data["streak_bonus"] == 5     # 30-day bonus
        assert data["balance"]["streak_count"] == 30
        assert data["balance"]["balance"] == 2125


# @pytest.mark.skip(reason="Complex integration test - skipping for core functionality focus")
class TestDateBoundaryIntegration:
    """Integration tests for date boundary edge cases."""
    
    @pytest.fixture(autouse=True)
    def setup_and_teardown(self):
        """Setup and teardown for each test."""
        reset_date_provider()
        yield
        reset_date_provider()
    
    async def test_month_boundary_claim(
        self,
        api_client: AsyncClient,
        bot_headers: Dict[str, str],
        test_guild_id: str,
        test_user_id: str,
        mock_bytes_operations,
        mock_bytes_config_operations
    ):
        """Test daily claim across month boundary (Jan 31 -> Feb 1)."""
        # Setup date provider for Feb 1
        mock_provider = MockDateProvider(fixed_date=date(2024, 2, 1))
        set_date_provider(mock_provider)
        
        # Setup config
        config_mock = Mock()
        config_mock.guild_id = test_guild_id
        config_mock.daily_amount = 15
        config_mock.streak_bonuses = {"7": 2}
        mock_bytes_config_operations.get_config.return_value = config_mock
        
        # User claimed on Jan 31
        balance_mock = Mock()
        balance_mock.guild_id = test_guild_id
        balance_mock.user_id = test_user_id
        balance_mock.balance = 200
        balance_mock.total_received = 100
        balance_mock.total_sent = 0
        balance_mock.streak_count = 5
        balance_mock.last_daily = date(2024, 1, 31)  # Yesterday (Jan 31)
        balance_mock.created_at = datetime(2024, 1, 1, 0, 0, 0)
        balance_mock.updated_at = datetime(2024, 1, 31, 0, 0, 0)
        
        updated_balance_mock = Mock()
        updated_balance_mock.guild_id = test_guild_id
        updated_balance_mock.user_id = test_user_id
        updated_balance_mock.balance = 215  # 200 + 15
        updated_balance_mock.total_received = 115
        updated_balance_mock.total_sent = 0
        updated_balance_mock.streak_count = 6
        updated_balance_mock.last_daily = date(2024, 2, 1)
        updated_balance_mock.created_at = datetime(2024, 1, 1, 0, 0, 0)
        updated_balance_mock.updated_at = datetime(2024, 2, 1, 0, 0, 0)
        
        mock_bytes_operations.get_balance.return_value = balance_mock
        mock_bytes_operations.update_daily_reward.return_value = updated_balance_mock
        
        # Make API call
        response = await api_client.post(
            f"/guilds/{test_guild_id}/bytes/daily/{test_user_id}",
            headers=bot_headers
        )
        
        # Verify response
        assert response.status_code == 200
        data = response.json()
        
        assert data["reward_amount"] == 15
        assert data["streak_bonus"] == 1
        assert data["balance"]["streak_count"] == 6
        
        # Verify correct date was passed to CRUD
        call_args = mock_bytes_operations.update_daily_reward.call_args
        # Args: (session, guild_id, user_id, daily_amount, streak_bonus, new_streak_count, claim_date)
        assert call_args[0][6] == date(2024, 2, 1)  # claim_date
    
    async def test_leap_year_boundary_claim(
        self,
        api_client: AsyncClient,
        bot_headers: Dict[str, str],
        test_guild_id: str,
        test_user_id: str,
        mock_bytes_operations,
        mock_bytes_config_operations
    ):
        """Test daily claim on leap year Feb 29."""
        # Setup date provider for Feb 29, 2024 (leap year)
        mock_provider = MockDateProvider(fixed_date=date(2024, 2, 29))
        set_date_provider(mock_provider)
        
        # Setup config
        config_mock = Mock()
        config_mock.guild_id = test_guild_id
        config_mock.daily_amount = 20
        config_mock.streak_bonuses = {"14": 3}
        mock_bytes_config_operations.get_config.return_value = config_mock
        
        # User claimed on Feb 28
        balance_mock = Mock()
        balance_mock.guild_id = test_guild_id
        balance_mock.user_id = test_user_id
        balance_mock.balance = 400
        balance_mock.total_received = 300
        balance_mock.total_sent = 0
        balance_mock.streak_count = 14
        balance_mock.last_daily = date(2024, 2, 28)  # Yesterday (Feb 28)
        balance_mock.created_at = datetime(2024, 1, 1, 0, 0, 0)
        balance_mock.updated_at = datetime(2024, 2, 28, 0, 0, 0)
        
        updated_balance_mock = Mock()
        updated_balance_mock.guild_id = test_guild_id
        updated_balance_mock.user_id = test_user_id
        updated_balance_mock.balance = 460  # 400 + (20 * 3) = 460
        updated_balance_mock.total_received = 360
        updated_balance_mock.total_sent = 0
        updated_balance_mock.streak_count = 15
        updated_balance_mock.last_daily = date(2024, 2, 29)
        updated_balance_mock.created_at = datetime(2024, 1, 1, 0, 0, 0)
        updated_balance_mock.updated_at = datetime(2024, 2, 29, 0, 0, 0)
        
        mock_bytes_operations.get_balance.return_value = balance_mock
        mock_bytes_operations.update_daily_reward.return_value = updated_balance_mock
        
        # Make API call
        response = await api_client.post(
            f"/guilds/{test_guild_id}/bytes/daily/{test_user_id}",
            headers=bot_headers
        )
        
        # Verify response
        assert response.status_code == 200
        data = response.json()
        
        assert data["reward_amount"] == 60  # 20 * 3 (14-day bonus)
        assert data["streak_bonus"] == 3
        assert data["balance"]["streak_count"] == 15
        
        # Verify correct leap year date was passed to CRUD
        call_args = mock_bytes_operations.update_daily_reward.call_args
        # Args: (session, guild_id, user_id, daily_amount, streak_bonus, new_streak_count, claim_date)
        assert call_args[0][6] == date(2024, 2, 29)  # claim_date


# @pytest.mark.skip(reason="Complex integration test - skipping for core functionality focus")
class TestErrorHandlingIntegration:
    """Integration tests for error handling scenarios."""
    
    @pytest.fixture(autouse=True)
    def setup_and_teardown(self):
        """Setup and teardown for each test."""
        reset_date_provider()
        yield
        reset_date_provider()
    
    async def test_database_error_during_claim(
        self,
        api_client: AsyncClient,
        bot_headers: Dict[str, str],
        test_guild_id: str,
        test_user_id: str,
        mock_bytes_operations,
        mock_bytes_config_operations
    ):
        """Test handling of database errors during claim."""
        from smarter_dev.web.crud import DatabaseOperationError
        
        # Setup config
        config_mock = Mock()
        config_mock.guild_id = test_guild_id
        config_mock.daily_amount = 10
        config_mock.streak_bonuses = {"7": 2}
        mock_bytes_config_operations.get_config.return_value = config_mock
        
        # Setup balance
        balance_mock = Mock()
        balance_mock.guild_id = test_guild_id
        balance_mock.user_id = test_user_id
        balance_mock.balance = 100
        balance_mock.total_received = 0
        balance_mock.total_sent = 0
        balance_mock.streak_count = 0
        balance_mock.last_daily = None
        balance_mock.created_at = datetime(2024, 1, 1, 0, 0, 0)
        balance_mock.updated_at = datetime(2024, 1, 1, 0, 0, 0)
        
        mock_bytes_operations.get_balance.return_value = balance_mock
        
        # Make update_daily_reward raise a database error
        mock_bytes_operations.update_daily_reward.side_effect = DatabaseOperationError(
            "Database connection failed"
        )
        
        # Make API call
        response = await api_client.post(
            f"/guilds/{test_guild_id}/bytes/daily/{test_user_id}",
            headers=bot_headers
        )
        
        # Verify error response
        assert response.status_code == 500
        data = response.json()
        assert "detail" in data
        # Should not expose internal database error details in production
    
    async def test_corrupted_streak_data_handling(
        self,
        api_client: AsyncClient,
        bot_headers: Dict[str, str],
        test_guild_id: str,
        test_user_id: str,
        mock_bytes_operations,
        mock_bytes_config_operations
    ):
        """Test handling of corrupted streak data (future last_daily)."""
        # Setup date provider
        mock_provider = MockDateProvider(fixed_date=date(2024, 1, 15))
        set_date_provider(mock_provider)
        
        # Setup config
        config_mock = Mock()
        config_mock.guild_id = test_guild_id
        config_mock.daily_amount = 10
        config_mock.streak_bonuses = {"7": 2}
        mock_bytes_config_operations.get_config.return_value = config_mock
        
        # Corrupted balance: last_daily is in the future
        balance_mock = Mock()
        balance_mock.guild_id = test_guild_id
        balance_mock.user_id = test_user_id
        balance_mock.balance = 100
        balance_mock.total_received = 0
        balance_mock.total_sent = 0
        balance_mock.streak_count = 5  # Inconsistent with future date
        balance_mock.last_daily = date(2024, 1, 16)  # Tomorrow (corrupted)
        balance_mock.created_at = datetime(2024, 1, 1, 0, 0, 0)
        balance_mock.updated_at = datetime(2024, 1, 16, 0, 0, 0)
        
        mock_bytes_operations.get_balance.return_value = balance_mock
        
        # Make API call
        response = await api_client.post(
            f"/guilds/{test_guild_id}/bytes/daily/{test_user_id}",
            headers=bot_headers
        )
        
        # Verify error response (can't claim because "already claimed today")
        assert response.status_code == 409
        # Just check that the response contains the error message somewhere
        response_text = response.text
        assert "Daily reward has already been claimed today" in response_text
        
        # Verify CRUD update was not called due to corruption
        mock_bytes_operations.update_daily_reward.assert_not_called()