"""Parity tests for the native (Litestar) bytes economy API.

Ported one-for-one from ``tests/web/test_api/test_bytes.py`` (the FastAPI
suite). Paths are prefixed with ``/api`` because the native controller carries
its final mounted path; the FastAPI app was itself mounted at ``/api``.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any
from unittest.mock import Mock, patch
from uuid import uuid4

from litestar.testing import TestClient

from smarter_dev.shared.date_provider import MockDateProvider
from smarter_dev.web.crud import ConflictError, DatabaseOperationError, NotFoundError

DATE_PROVIDER_PATH = "smarter_dev.web.api_native.bytes.get_date_provider"


def _balance_mock(data: dict[str, Any], **overrides: Any) -> Mock:
    """Build a balance-like mock with timestamps for schema validation."""
    balance = Mock()
    for key, value in {**data, **overrides}.items():
        setattr(balance, key, value)
    balance.created_at = datetime.now(timezone.utc)
    balance.updated_at = datetime.now(timezone.utc)
    return balance


def _config_mock(data: dict[str, Any], **overrides: Any) -> Mock:
    """Build a config-like mock with timestamps for schema validation."""
    config = Mock()
    for key, value in {**data, **overrides}.items():
        setattr(config, key, value)
    config.created_at = datetime.now(timezone.utc)
    config.updated_at = datetime.now(timezone.utc)
    return config


class TestBytesBalance:
    def test_get_balance_success(
        self, bytes_client: TestClient, guild_id, user_id, bytes_ops_mock, bytes_balance_data
    ):
        bytes_ops_mock.get_or_create_balance.return_value = _balance_mock(bytes_balance_data)

        response = bytes_client.get(f"/api/guilds/{guild_id}/bytes/balance/{user_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["guild_id"] == guild_id
        assert data["user_id"] == user_id
        assert data["balance"] == 100
        assert data["total_received"] == 150
        assert data["total_sent"] == 50
        assert data["streak_count"] == 3

    def test_get_balance_invalid_user_id(self, bytes_client: TestClient, guild_id):
        response = bytes_client.get(f"/api/guilds/{guild_id}/bytes/balance/invalid_id")

        assert response.status_code == 400
        assert "Invalid user ID format" in response.json()["detail"]["detail"]

    def test_get_balance_database_error(
        self, bytes_client: TestClient, guild_id, user_id, bytes_ops_mock
    ):
        bytes_ops_mock.get_or_create_balance.side_effect = DatabaseOperationError("DB Error")

        response = bytes_client.get(f"/api/guilds/{guild_id}/bytes/balance/{user_id}")

        assert response.status_code == 500
        assert response.json()["type"] == "database_error"
        assert "Database error" in response.json()["detail"]

    def test_get_balance_invalid_guild_id(self, bytes_client: TestClient, user_id):
        response = bytes_client.get(f"/api/guilds/not-a-guild/bytes/balance/{user_id}")

        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid guild ID"


class TestDailyClaim:
    def test_claim_daily_success(
        self, bytes_client, guild_id, user_id, bytes_ops_mock, bytes_config_ops_mock,
        bytes_balance_data, bytes_config_data
    ):
        test_date = date(2024, 1, 15)
        with patch(DATE_PROVIDER_PATH, return_value=MockDateProvider(fixed_date=test_date)):
            bytes_config_ops_mock.get_config.return_value = _config_mock(bytes_config_data)
            bytes_ops_mock.get_balance.return_value = _balance_mock(
                bytes_balance_data, last_daily=test_date - timedelta(days=1)
            )
            updated = _balance_mock(
                bytes_balance_data, balance=120, streak_count=4, last_daily=test_date
            )
            bytes_ops_mock.update_daily_reward.return_value = (updated, None)

            response = bytes_client.post(
                f"/api/guilds/{guild_id}/bytes/daily", json={"user_id": user_id}
            )

        assert response.status_code == 200
        data = response.json()
        assert data["balance"]["balance"] == 120
        assert data["reward_amount"] == 20
        assert data["streak_bonus"] == 2
        assert "next_claim_at" in data

    def test_claim_daily_already_claimed(
        self, bytes_client, guild_id, user_id, bytes_ops_mock, bytes_config_ops_mock,
        bytes_balance_data, bytes_config_data
    ):
        test_date = date(2024, 1, 15)
        with patch(DATE_PROVIDER_PATH, return_value=MockDateProvider(fixed_date=test_date)):
            bytes_config_ops_mock.get_config.return_value = _config_mock(bytes_config_data)
            bytes_ops_mock.get_balance.return_value = _balance_mock(
                bytes_balance_data, last_daily=test_date
            )

            response = bytes_client.post(
                f"/api/guilds/{guild_id}/bytes/daily", json={"user_id": user_id}
            )

        assert response.status_code == 409
        assert "already been claimed" in response.json()["detail"]["detail"]

    def test_claim_daily_new_streak(
        self, bytes_client, guild_id, user_id, bytes_ops_mock, bytes_config_ops_mock,
        bytes_balance_data, bytes_config_data
    ):
        test_date = date(2024, 1, 15)
        with patch(DATE_PROVIDER_PATH, return_value=MockDateProvider(fixed_date=test_date)):
            bytes_config_ops_mock.get_config.return_value = _config_mock(bytes_config_data)
            bytes_ops_mock.get_balance.return_value = _balance_mock(
                bytes_balance_data, last_daily=test_date - timedelta(days=5)
            )
            updated = _balance_mock(
                bytes_balance_data, balance=110, streak_count=1, last_daily=test_date
            )
            bytes_ops_mock.update_daily_reward.return_value = (updated, None)

            response = bytes_client.post(
                f"/api/guilds/{guild_id}/bytes/daily", json={"user_id": user_id}
            )

        assert response.status_code == 200
        data = response.json()
        assert data["reward_amount"] == 10
        assert data["streak_bonus"] == 1


class TestTransactions:
    def test_create_transaction_success(
        self, bytes_client, guild_id, bytes_ops_mock, bytes_config_ops_mock,
        transaction_data, bytes_config_data
    ):
        bytes_config_ops_mock.get_config.return_value = _config_mock(bytes_config_data)

        tx = Mock()
        tx.id = uuid4()
        tx.guild_id = guild_id
        for key, value in transaction_data.items():
            setattr(tx, key, value)
        tx.created_at = datetime.now(timezone.utc)
        bytes_ops_mock.create_transaction.return_value = tx

        response = bytes_client.post(
            f"/api/guilds/{guild_id}/bytes/transactions", json=transaction_data
        )

        assert response.status_code == 200
        data = response.json()
        assert data["guild_id"] == guild_id
        assert data["amount"] == transaction_data["amount"]
        assert data["giver_id"] == transaction_data["giver_id"]
        assert data["receiver_id"] == transaction_data["receiver_id"]

    def test_create_transaction_self_transfer(
        self, bytes_client, guild_id, bytes_config_ops_mock, transaction_data, bytes_config_data
    ):
        bytes_config_ops_mock.get_config.return_value = _config_mock(bytes_config_data)
        body = transaction_data.copy()
        body["receiver_id"] = body["giver_id"]

        response = bytes_client.post(
            f"/api/guilds/{guild_id}/bytes/transactions", json=body
        )

        assert response.status_code == 400
        assert "Cannot transfer bytes to yourself" in response.json()["detail"]["detail"]

    def test_create_transaction_exceeds_limit(
        self, bytes_client, guild_id, bytes_config_ops_mock, transaction_data, bytes_config_data
    ):
        bytes_config_ops_mock.get_config.return_value = _config_mock(
            bytes_config_data, max_transfer=10
        )

        response = bytes_client.post(
            f"/api/guilds/{guild_id}/bytes/transactions", json=transaction_data
        )

        assert response.status_code == 400
        assert "exceeds maximum limit" in response.json()["detail"]["detail"]

    def test_create_transaction_insufficient_balance(
        self, bytes_client, guild_id, bytes_ops_mock, bytes_config_ops_mock,
        transaction_data, bytes_config_data
    ):
        bytes_config_ops_mock.get_config.return_value = _config_mock(bytes_config_data)
        bytes_ops_mock.create_transaction.side_effect = ConflictError(
            "Insufficient balance: 10 < 25"
        )

        response = bytes_client.post(
            f"/api/guilds/{guild_id}/bytes/transactions", json=transaction_data
        )

        assert response.status_code == 409
        assert "Insufficient balance" in response.json()["detail"]

    def test_create_transaction_invalid_data(self, bytes_client, guild_id):
        invalid_data = {
            "giver_id": "invalid_id",
            "giver_username": "",
            "receiver_id": "invalid_id",
            "receiver_username": "TestUser2",
            "amount": -10,
            "reason": "x" * 201,
        }

        response = bytes_client.post(
            f"/api/guilds/{guild_id}/bytes/transactions", json=invalid_data
        )

        assert response.status_code == 422


class TestLeaderboard:
    def test_get_leaderboard_success(self, bytes_client, guild_id, bytes_ops_mock):
        leaderboard = []
        for i in range(3):
            leaderboard.append(
                _balance_mock(
                    {
                        "guild_id": guild_id,
                        "user_id": f"user_{i}",
                        "balance": 100 - (i * 10),
                        "total_received": 150 - (i * 15),
                        "total_sent": 50 - (i * 5),
                        "streak_count": 5 - i,
                        "last_daily": None,
                    }
                )
            )
        bytes_ops_mock.get_leaderboard.return_value = leaderboard

        response = bytes_client.get(f"/api/guilds/{guild_id}/bytes/leaderboard")

        assert response.status_code == 200
        data = response.json()
        assert data["guild_id"] == guild_id
        assert len(data["users"]) == 3
        assert data["total_users"] == 3
        assert data["users"][0]["balance"] == 100
        assert data["users"][1]["balance"] == 90
        assert data["users"][2]["balance"] == 80

    def test_get_leaderboard_with_limit(self, bytes_client, guild_id, bytes_ops_mock):
        bytes_ops_mock.get_leaderboard.return_value = []

        response = bytes_client.get(f"/api/guilds/{guild_id}/bytes/leaderboard?limit=5")

        assert response.status_code == 200
        bytes_ops_mock.get_leaderboard.assert_called_with(
            bytes_ops_mock.get_leaderboard.call_args[0][0], guild_id, 5
        )

    def test_get_leaderboard_invalid_limit(self, bytes_client, guild_id):
        response = bytes_client.get(f"/api/guilds/{guild_id}/bytes/leaderboard?limit=0")

        assert response.status_code == 422


class TestConfiguration:
    def test_get_config_success(
        self, bytes_client, guild_id, bytes_config_ops_mock, bytes_config_data
    ):
        bytes_config_ops_mock.get_config.return_value = _config_mock(bytes_config_data)

        response = bytes_client.get(f"/api/guilds/{guild_id}/bytes/config")

        assert response.status_code == 200
        data = response.json()
        assert data["guild_id"] == guild_id
        assert data["daily_amount"] == 10
        assert data["starting_balance"] == 100

    def test_get_config_not_found_creates_default(
        self, bytes_client, guild_id, bytes_config_ops_mock, bytes_config_data
    ):
        bytes_config_ops_mock.get_config.side_effect = NotFoundError("Config not found")
        bytes_config_ops_mock.create_config.return_value = _config_mock(bytes_config_data)

        response = bytes_client.get(f"/api/guilds/{guild_id}/bytes/config")

        assert response.status_code == 200
        bytes_config_ops_mock.create_config.assert_called_once()

    def test_update_config_success(
        self, bytes_client, guild_id, bytes_config_ops_mock, bytes_config_data
    ):
        bytes_config_ops_mock.update_config.return_value = _config_mock(
            bytes_config_data, daily_amount=15
        )

        response = bytes_client.put(
            f"/api/guilds/{guild_id}/bytes/config",
            json={"daily_amount": 15, "is_enabled": False},
        )

        assert response.status_code == 200
        assert response.json()["daily_amount"] == 15

    def test_update_config_empty_data(self, bytes_client, guild_id):
        response = bytes_client.put(f"/api/guilds/{guild_id}/bytes/config", json={})

        assert response.status_code == 400
        assert "No configuration updates provided" in response.json()["detail"]["detail"]

    def test_delete_config_success(self, bytes_client, guild_id, bytes_config_ops_mock):
        response = bytes_client.delete(f"/api/guilds/{guild_id}/bytes/config")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert guild_id in data["message"]
        bytes_config_ops_mock.delete_config.assert_called_once()

    def test_delete_config_not_found(self, bytes_client, guild_id, bytes_config_ops_mock):
        bytes_config_ops_mock.delete_config.side_effect = NotFoundError("Config not found")

        response = bytes_client.delete(f"/api/guilds/{guild_id}/bytes/config")

        assert response.status_code == 404
        assert "Config not found" in response.json()["detail"]


class TestStreakReset:
    def test_reset_streak_success(
        self, bytes_client, guild_id, user_id, bytes_ops_mock, bytes_balance_data
    ):
        bytes_ops_mock.reset_streak.return_value = _balance_mock(
            bytes_balance_data, streak_count=0
        )

        response = bytes_client.post(
            f"/api/guilds/{guild_id}/bytes/reset-streak/{user_id}"
        )

        assert response.status_code == 200
        assert response.json()["streak_count"] == 0
        bytes_ops_mock.reset_streak.assert_called_once()


class TestTransactionHistory:
    def test_get_transaction_history_success(self, bytes_client, guild_id, bytes_ops_mock):
        transactions = []
        for i in range(3):
            tx = Mock()
            tx.id = uuid4()
            tx.guild_id = guild_id
            tx.giver_id = f"giver_{i}"
            tx.giver_username = f"GiverUser{i}"
            tx.receiver_id = f"receiver_{i}"
            tx.receiver_username = f"ReceiverUser{i}"
            tx.amount = 10 * (i + 1)
            tx.reason = f"Transaction {i}"
            tx.created_at = datetime.now(timezone.utc)
            transactions.append(tx)
        bytes_ops_mock.get_transaction_history.return_value = transactions

        response = bytes_client.get(f"/api/guilds/{guild_id}/bytes/transactions")

        assert response.status_code == 200
        data = response.json()
        assert data["guild_id"] == guild_id
        assert len(data["transactions"]) == 3
        assert data["total_count"] == 3
        assert data["user_id"] is None

    def test_get_transaction_history_with_user_filter(
        self, bytes_client, guild_id, user_id, bytes_ops_mock
    ):
        bytes_ops_mock.get_transaction_history.return_value = []

        response = bytes_client.get(
            f"/api/guilds/{guild_id}/bytes/transactions?user_id={user_id}"
        )

        assert response.status_code == 200
        assert response.json()["user_id"] == user_id
        bytes_ops_mock.get_transaction_history.assert_called_with(
            bytes_ops_mock.get_transaction_history.call_args[0][0], guild_id, user_id, 20
        )
