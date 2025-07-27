"""Tests for bytes economy API endpoints."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock
from uuid import uuid4

import pytest
from httpx import AsyncClient

from smarter_dev.web.crud import NotFoundError, ConflictError, DatabaseOperationError


class TestBytesBalance:
    """Test bytes balance endpoints."""
    
    async def test_get_balance_success(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str,
        test_user_id: str,
        mock_bytes_operations,
        sample_bytes_balance_data: dict
    ):
        """Test successful balance retrieval."""
        # Mock the balance response
        balance_mock = Mock()
        for key, value in sample_bytes_balance_data.items():
            setattr(balance_mock, key, value)
        balance_mock.created_at = datetime.now(timezone.utc)
        balance_mock.updated_at = datetime.now(timezone.utc)
        
        mock_bytes_operations.get_or_create_balance.return_value = balance_mock
        
        response = await api_client.get(
            f"/guilds/{test_guild_id}/bytes/balance/{test_user_id}",
            headers=bot_headers
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["guild_id"] == test_guild_id
        assert data["user_id"] == test_user_id
        assert data["balance"] == 100
        assert data["total_received"] == 150
        assert data["total_sent"] == 50
        assert data["streak_count"] == 3
    
    async def test_get_balance_invalid_user_id(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str
    ):
        """Test balance retrieval with invalid user ID."""
        response = await api_client.get(
            f"/guilds/{test_guild_id}/bytes/balance/invalid_id",
            headers=bot_headers
        )
        
        assert response.status_code == 400
        assert "Invalid user ID format" in response.json()["detail"]["detail"]
    
    async def test_get_balance_database_error(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str,
        test_user_id: str,
        mock_bytes_operations
    ):
        """Test balance retrieval with database error."""
        mock_bytes_operations.get_balance.side_effect = DatabaseOperationError("DB Error")
        
        response = await api_client.get(
            f"/guilds/{test_guild_id}/bytes/balance/{test_user_id}",
            headers=bot_headers
        )
        
        assert response.status_code == 500
        assert "Database error" in response.json()["detail"]
    
    async def test_get_balance_unauthorized(
        self,
        api_client: AsyncClient,
        test_guild_id: str,
        test_user_id: str
    ):
        """Test balance retrieval without authorization."""
        response = await api_client.get(
            f"/guilds/{test_guild_id}/bytes/balance/{test_user_id}"
        )
        
        assert response.status_code == 403




class TestDailyClaim:
    """Test daily claim endpoints."""
    
    async def test_claim_daily_success(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str,
        test_user_id: str,
        mock_bytes_operations,
        mock_bytes_config_operations,
        sample_bytes_balance_data: dict,
        sample_bytes_config_data: dict
    ):
        """Test successful daily claim."""
        from unittest.mock import patch
        from smarter_dev.shared.date_provider import MockDateProvider
        
        # Mock date provider to ensure consistent date
        test_date = date(2024, 1, 15)
        mock_date_provider = MockDateProvider(fixed_date=test_date)
        
        with patch('smarter_dev.web.api.routers.bytes.get_date_provider', return_value=mock_date_provider):
            # Mock config
            config_mock = Mock()
            for key, value in sample_bytes_config_data.items():
                setattr(config_mock, key, value)
            mock_bytes_config_operations.get_config.return_value = config_mock
            
            # Mock current balance with yesterday's claim date
            balance_mock = Mock()
            for key, value in sample_bytes_balance_data.items():
                setattr(balance_mock, key, value)
            balance_mock.last_daily = test_date - timedelta(days=1)  # Yesterday relative to test_date
            mock_bytes_operations.get_balance.return_value = balance_mock
            
            # Mock updated balance after claim
            updated_balance = Mock()
            for key, value in sample_bytes_balance_data.items():
                setattr(updated_balance, key, value)
            updated_balance.balance = 120  # Original 100 + 20 (10 * 2 streak bonus)
            updated_balance.streak_count = 4
            updated_balance.last_daily = test_date
            updated_balance.created_at = datetime.now(timezone.utc)
            updated_balance.updated_at = datetime.now(timezone.utc)
            mock_bytes_operations.update_daily_reward.return_value = updated_balance
            
            response = await api_client.post(
                f"/guilds/{test_guild_id}/bytes/daily/{test_user_id}",
                headers=bot_headers
            )
            
            assert response.status_code == 200
            data = response.json()
            assert data["balance"]["balance"] == 120
            assert data["reward_amount"] == 20  # 10 * 2 streak bonus
            assert data["streak_bonus"] == 2
            assert "next_claim_at" in data
    
    async def test_claim_daily_already_claimed(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str,
        test_user_id: str,
        mock_bytes_operations,
        mock_bytes_config_operations,
        sample_bytes_balance_data: dict,
        sample_bytes_config_data: dict
    ):
        """Test daily claim when already claimed today."""
        from unittest.mock import patch
        from smarter_dev.shared.date_provider import MockDateProvider
        
        # Mock date provider to ensure consistent date
        test_date = date(2024, 1, 15)
        mock_date_provider = MockDateProvider(fixed_date=test_date)
        
        with patch('smarter_dev.web.api.routers.bytes.get_date_provider', return_value=mock_date_provider):
            # Mock config
            config_mock = Mock()
            for key, value in sample_bytes_config_data.items():
                setattr(config_mock, key, value)
            mock_bytes_config_operations.get_config.return_value = config_mock
            
            # Mock balance with today's claim
            balance_mock = Mock()
            for key, value in sample_bytes_balance_data.items():
                setattr(balance_mock, key, value)
            balance_mock.last_daily = test_date  # Already claimed today
            mock_bytes_operations.get_balance.return_value = balance_mock
            
            response = await api_client.post(
                f"/guilds/{test_guild_id}/bytes/daily/{test_user_id}",
                headers=bot_headers
            )
            
            assert response.status_code == 409
            response_data = response.json()
            assert "already been claimed" in response_data["detail"]["detail"]
    
    async def test_claim_daily_new_streak(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str,
        test_user_id: str,
        mock_bytes_operations,
        mock_bytes_config_operations,
        sample_bytes_balance_data: dict,
        sample_bytes_config_data: dict
    ):
        """Test daily claim starting new streak."""
        from unittest.mock import patch
        from smarter_dev.shared.date_provider import MockDateProvider
        
        # Mock date provider to ensure consistent date
        test_date = date(2024, 1, 15)
        mock_date_provider = MockDateProvider(fixed_date=test_date)
        
        with patch('smarter_dev.web.api.routers.bytes.get_date_provider', return_value=mock_date_provider):
            # Mock config
            config_mock = Mock()
            for key, value in sample_bytes_config_data.items():
                setattr(config_mock, key, value)
            mock_bytes_config_operations.get_config.return_value = config_mock
            
            # Mock balance with no recent claim (new streak)
            balance_mock = Mock()
            for key, value in sample_bytes_balance_data.items():
                setattr(balance_mock, key, value)
            balance_mock.last_daily = test_date - timedelta(days=5)  # 5 days ago
            mock_bytes_operations.get_balance.return_value = balance_mock
            
            # Mock updated balance
            updated_balance = Mock()
            for key, value in sample_bytes_balance_data.items():
                setattr(updated_balance, key, value)
            updated_balance.balance = 110  # Original + 10 (no streak bonus)
            updated_balance.streak_count = 1
            updated_balance.last_daily = test_date
            updated_balance.created_at = datetime.now(timezone.utc)
            updated_balance.updated_at = datetime.now(timezone.utc)
            mock_bytes_operations.update_daily_reward.return_value = updated_balance
            
            response = await api_client.post(
                f"/guilds/{test_guild_id}/bytes/daily/{test_user_id}",
                headers=bot_headers
            )
            
            assert response.status_code == 200
            data = response.json()
            assert data["reward_amount"] == 10  # Base amount, no streak bonus
            assert data["streak_bonus"] == 1


class TestTransactions:
    """Test transaction endpoints."""
    
    async def test_create_transaction_success(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str,
        mock_bytes_operations,
        mock_bytes_config_operations,
        sample_transaction_data: dict,
        sample_bytes_config_data: dict
    ):
        """Test successful transaction creation."""
        # Mock config
        config_mock = Mock()
        for key, value in sample_bytes_config_data.items():
            setattr(config_mock, key, value)
        mock_bytes_config_operations.get_config.return_value = config_mock
        
        # Mock created transaction
        transaction_mock = Mock()
        transaction_mock.id = uuid4()
        transaction_mock.guild_id = test_guild_id
        for key, value in sample_transaction_data.items():
            setattr(transaction_mock, key, value)
        transaction_mock.created_at = datetime.now(timezone.utc)
        mock_bytes_operations.create_transaction.return_value = transaction_mock
        
        response = await api_client.post(
            f"/guilds/{test_guild_id}/bytes/transactions",
            headers=bot_headers,
            json=sample_transaction_data
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["guild_id"] == test_guild_id
        assert data["amount"] == sample_transaction_data["amount"]
        assert data["giver_id"] == sample_transaction_data["giver_id"]
        assert data["receiver_id"] == sample_transaction_data["receiver_id"]
    
    async def test_create_transaction_self_transfer(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str,
        test_user_id: str,
        mock_bytes_config_operations,
        sample_transaction_data: dict,
        sample_bytes_config_data: dict
    ):
        """Test transaction creation with self-transfer."""
        # Mock config
        config_mock = Mock()
        for key, value in sample_bytes_config_data.items():
            setattr(config_mock, key, value)
        mock_bytes_config_operations.get_config.return_value = config_mock
        
        transaction_data = sample_transaction_data.copy()
        transaction_data["receiver_id"] = transaction_data["giver_id"]  # Self-transfer
        
        response = await api_client.post(
            f"/guilds/{test_guild_id}/bytes/transactions",
            headers=bot_headers,
            json=transaction_data
        )
        
        assert response.status_code == 400
        assert "Cannot transfer bytes to yourself" in response.json()["detail"]["detail"]
    
    async def test_create_transaction_exceeds_limit(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str,
        mock_bytes_config_operations,
        sample_transaction_data: dict,
        sample_bytes_config_data: dict
    ):
        """Test transaction creation exceeding transfer limit."""
        # Mock config with low transfer limit
        config_mock = Mock()
        config_data = sample_bytes_config_data.copy()
        config_data["max_transfer"] = 10  # Lower than transaction amount
        for key, value in config_data.items():
            setattr(config_mock, key, value)
        mock_bytes_config_operations.get_config.return_value = config_mock
        
        response = await api_client.post(
            f"/guilds/{test_guild_id}/bytes/transactions",
            headers=bot_headers,
            json=sample_transaction_data
        )
        
        assert response.status_code == 400
        assert "exceeds maximum limit" in response.json()["detail"]["detail"]
    
    async def test_create_transaction_insufficient_balance(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str,
        mock_bytes_operations,
        mock_bytes_config_operations,
        sample_transaction_data: dict,
        sample_bytes_config_data: dict
    ):
        """Test transaction creation with insufficient balance."""
        # Mock config
        config_mock = Mock()
        for key, value in sample_bytes_config_data.items():
            setattr(config_mock, key, value)
        mock_bytes_config_operations.get_config.return_value = config_mock
        
        # Mock insufficient balance error
        mock_bytes_operations.create_transaction.side_effect = ConflictError(
            "Insufficient balance: 10 < 25"
        )
        
        response = await api_client.post(
            f"/guilds/{test_guild_id}/bytes/transactions",
            headers=bot_headers,
            json=sample_transaction_data
        )
        
        assert response.status_code == 409
        assert "Insufficient balance" in response.json()["detail"]
    
    async def test_create_transaction_invalid_data(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str
    ):
        """Test transaction creation with invalid data."""
        invalid_data = {
            "giver_id": "invalid_id",
            "giver_username": "",  # Empty username
            "receiver_id": "invalid_id",
            "receiver_username": "TestUser2",
            "amount": -10,  # Negative amount
            "reason": "x" * 201  # Too long reason
        }
        
        response = await api_client.post(
            f"/guilds/{test_guild_id}/bytes/transactions",
            headers=bot_headers,
            json=invalid_data
        )
        
        assert response.status_code == 422  # Validation error


class TestLeaderboard:
    """Test leaderboard endpoints."""
    
    async def test_get_leaderboard_success(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str,
        mock_bytes_operations
    ):
        """Test successful leaderboard retrieval."""
        # Mock leaderboard data
        leaderboard_data = []
        for i in range(3):
            balance_mock = Mock()
            balance_mock.guild_id = test_guild_id
            balance_mock.user_id = f"user_{i}"
            balance_mock.balance = 100 - (i * 10)
            balance_mock.total_received = 150 - (i * 15)
            balance_mock.total_sent = 50 - (i * 5)
            balance_mock.streak_count = 5 - i
            balance_mock.last_daily = None
            balance_mock.created_at = datetime.now(timezone.utc)
            balance_mock.updated_at = datetime.now(timezone.utc)
            leaderboard_data.append(balance_mock)
        
        mock_bytes_operations.get_leaderboard.return_value = leaderboard_data
        
        response = await api_client.get(
            f"/guilds/{test_guild_id}/bytes/leaderboard",
            headers=bot_headers
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["guild_id"] == test_guild_id
        assert len(data["users"]) == 3
        assert data["total_users"] == 3
        assert data["users"][0]["balance"] == 100  # Highest balance first
        assert data["users"][1]["balance"] == 90
        assert data["users"][2]["balance"] == 80
    
    async def test_get_leaderboard_with_limit(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str,
        mock_bytes_operations
    ):
        """Test leaderboard with custom limit."""
        mock_bytes_operations.get_leaderboard.return_value = []
        
        response = await api_client.get(
            f"/guilds/{test_guild_id}/bytes/leaderboard?limit=5",
            headers=bot_headers
        )
        
        assert response.status_code == 200
        # Verify the limit was passed to the operation
        mock_bytes_operations.get_leaderboard.assert_called_with(
            mock_bytes_operations.get_leaderboard.call_args[0][0],  # session
            test_guild_id,
            5
        )
    
    async def test_get_leaderboard_invalid_limit(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str
    ):
        """Test leaderboard with invalid limit."""
        response = await api_client.get(
            f"/guilds/{test_guild_id}/bytes/leaderboard?limit=0",
            headers=bot_headers
        )
        
        assert response.status_code == 422  # Validation error


class TestConfiguration:
    """Test configuration endpoints."""
    
    async def test_get_config_success(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str,
        mock_bytes_config_operations,
        sample_bytes_config_data: dict
    ):
        """Test successful config retrieval."""
        config_mock = Mock()
        for key, value in sample_bytes_config_data.items():
            setattr(config_mock, key, value)
        config_mock.created_at = datetime.now(timezone.utc)
        config_mock.updated_at = datetime.now(timezone.utc)
        
        mock_bytes_config_operations.get_config.return_value = config_mock
        
        response = await api_client.get(
            f"/guilds/{test_guild_id}/bytes/config",
            headers=bot_headers
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["guild_id"] == test_guild_id
        assert data["daily_amount"] == 10
        assert data["starting_balance"] == 100
        assert data["is_enabled"] is True
    
    async def test_get_config_not_found_creates_default(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str,
        mock_bytes_config_operations,
        sample_bytes_config_data: dict
    ):
        """Test config retrieval creates default when not found."""
        # First call returns NotFound, second returns created config
        mock_bytes_config_operations.get_config.side_effect = [
            NotFoundError("Config not found"),
            Mock()
        ]
        
        config_mock = Mock()
        for key, value in sample_bytes_config_data.items():
            setattr(config_mock, key, value)
        config_mock.created_at = datetime.now(timezone.utc)
        config_mock.updated_at = datetime.now(timezone.utc)
        mock_bytes_config_operations.create_config.return_value = config_mock
        
        response = await api_client.get(
            f"/guilds/{test_guild_id}/bytes/config",
            headers=bot_headers
        )
        
        assert response.status_code == 200
        mock_bytes_config_operations.create_config.assert_called_once()
    
    async def test_update_config_success(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str,
        mock_bytes_config_operations,
        sample_bytes_config_data: dict
    ):
        """Test successful config update."""
        config_mock = Mock()
        updated_data = sample_bytes_config_data.copy()
        updated_data["daily_amount"] = 15
        for key, value in updated_data.items():
            setattr(config_mock, key, value)
        config_mock.created_at = datetime.now(timezone.utc)
        config_mock.updated_at = datetime.now(timezone.utc)
        
        mock_bytes_config_operations.update_config.return_value = config_mock
        
        update_data = {"daily_amount": 15, "is_enabled": False}
        response = await api_client.put(
            f"/guilds/{test_guild_id}/bytes/config",
            headers=bot_headers,
            json=update_data
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["daily_amount"] == 15
    
    async def test_update_config_empty_data(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str
    ):
        """Test config update with empty data."""
        response = await api_client.put(
            f"/guilds/{test_guild_id}/bytes/config",
            headers=bot_headers,
            json={}
        )
        
        assert response.status_code == 400
        assert "No configuration updates provided" in response.json()["detail"]["detail"]
    
    async def test_delete_config_success(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str,
        mock_bytes_config_operations
    ):
        """Test successful config deletion."""
        response = await api_client.delete(
            f"/guilds/{test_guild_id}/bytes/config",
            headers=bot_headers
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert test_guild_id in data["message"]
        mock_bytes_config_operations.delete_config.assert_called_once()
    
    async def test_delete_config_not_found(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str,
        mock_bytes_config_operations
    ):
        """Test config deletion when config doesn't exist."""
        mock_bytes_config_operations.delete_config.side_effect = NotFoundError(
            "Config not found"
        )
        
        response = await api_client.delete(
            f"/guilds/{test_guild_id}/bytes/config",
            headers=bot_headers
        )
        
        assert response.status_code == 404
        assert "Config not found" in response.json()["detail"]


class TestStreakReset:
    """Test streak reset endpoint."""
    
    async def test_reset_streak_success(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str,
        test_user_id: str,
        mock_bytes_operations,
        sample_bytes_balance_data: dict
    ):
        """Test successful streak reset."""
        balance_mock = Mock()
        reset_data = sample_bytes_balance_data.copy()
        reset_data["streak_count"] = 0
        for key, value in reset_data.items():
            setattr(balance_mock, key, value)
        balance_mock.created_at = datetime.now(timezone.utc)
        balance_mock.updated_at = datetime.now(timezone.utc)
        
        mock_bytes_operations.reset_streak.return_value = balance_mock
        
        response = await api_client.post(
            f"/guilds/{test_guild_id}/bytes/reset-streak/{test_user_id}",
            headers=bot_headers
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["streak_count"] == 0
        mock_bytes_operations.reset_streak.assert_called_once()


class TestTransactionHistory:
    """Test transaction history endpoint."""
    
    async def test_get_transaction_history_success(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str,
        mock_bytes_operations
    ):
        """Test successful transaction history retrieval."""
        # Mock transaction history
        transactions = []
        for i in range(3):
            tx_mock = Mock()
            tx_mock.id = uuid4()
            tx_mock.guild_id = test_guild_id
            tx_mock.giver_id = f"giver_{i}"
            tx_mock.giver_username = f"GiverUser{i}"
            tx_mock.receiver_id = f"receiver_{i}"
            tx_mock.receiver_username = f"ReceiverUser{i}"
            tx_mock.amount = 10 * (i + 1)
            tx_mock.reason = f"Transaction {i}"
            tx_mock.created_at = datetime.now(timezone.utc)
            transactions.append(tx_mock)
        
        mock_bytes_operations.get_transaction_history.return_value = transactions
        
        response = await api_client.get(
            f"/guilds/{test_guild_id}/bytes/transactions",
            headers=bot_headers
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["guild_id"] == test_guild_id
        assert len(data["transactions"]) == 3
        assert data["total_count"] == 3
        assert data["user_id"] is None
    
    async def test_get_transaction_history_with_user_filter(
        self,
        api_client: AsyncClient,
        bot_headers: dict[str, str],
        test_guild_id: str,
        test_user_id: str,
        mock_bytes_operations
    ):
        """Test transaction history with user filter."""
        mock_bytes_operations.get_transaction_history.return_value = []
        
        response = await api_client.get(
            f"/guilds/{test_guild_id}/bytes/transactions?user_id={test_user_id}",
            headers=bot_headers
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["user_id"] == test_user_id
        
        # Verify the user_id was passed to the operation
        mock_bytes_operations.get_transaction_history.assert_called_with(
            mock_bytes_operations.get_transaction_history.call_args[0][0],  # session
            test_guild_id,
            test_user_id,
            20  # default limit
        )