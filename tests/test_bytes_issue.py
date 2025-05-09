"""
Focused test for the daily bytes issue.
"""

import pytest
import os
import sys
from datetime import datetime, timedelta, UTC
from unittest.mock import patch, MagicMock, AsyncMock

# Add the parent directory to the path so we can import the modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.api_models import DiscordUser, GuildMember, Bytes, BytesConfig

# Test constants
TEST_USER_ID = 123456789
TEST_GUILD_ID = 987654321


class MockAPIClient:
    """Mock API client for testing."""

    def __init__(self):
        self.get_guild_member_calls = []
        self.request_calls = []
        self.last_daily_bytes = None
        self.last_active_day = None
        self.streak_count = 1
        self.bytes_transactions = []
        self.is_new_day = True

    async def get_guild_member(self, user_id, guild_id):
        """Mock get_guild_member method."""
        self.get_guild_member_calls.append((user_id, guild_id))

        if self.last_daily_bytes:
            return GuildMember(
                id=1,
                user_id=1,
                guild_id=guild_id,
                nickname="test_user",
                joined_at=datetime.now(UTC),
                is_active=True,
                created_at=datetime.now(UTC),
                last_daily_bytes=self.last_daily_bytes
            )
        return None

    async def get_bytes_config(self, guild_id):
        """Mock get_bytes_config method."""
        return BytesConfig(
            id=1,
            guild_id=guild_id,
            starting_balance=100,
            daily_earning=10,
            max_give_amount=50,
            cooldown_minutes=1440
        )

    def _dict_from_model(self, model):
        """Mock _dict_from_model method."""
        if isinstance(model, Bytes):
            return {
                "giver_id": model.giver_id,
                "receiver_id": model.receiver_id,
                "guild_id": model.guild_id,
                "amount": model.amount,
                "reason": model.reason
            }
        elif isinstance(model, GuildMember):
            return {
                "id": model.id,
                "user_id": model.user_id,
                "guild_id": model.guild_id,
                "nickname": model.nickname,
                "joined_at": model.joined_at.isoformat() if model.joined_at else None,
                "is_active": model.is_active,
                "created_at": model.created_at.isoformat() if model.created_at else None,
                "last_daily_bytes": model.last_daily_bytes.isoformat() if model.last_daily_bytes else None
            }
        return {}

    def _model_from_dict(self, model_class, data):
        """Mock _model_from_dict method."""
        if model_class == BytesConfig:
            return BytesConfig(
                id=data.get("id", 1),
                guild_id=data.get("guild_id", TEST_GUILD_ID),
                starting_balance=data.get("starting_balance", 100),
                daily_earning=data.get("daily_earning", 10),
                max_give_amount=data.get("max_give_amount", 50),
                cooldown_minutes=data.get("cooldown_minutes", 1440)
            )
        return None

    async def _request(self, method, path, data=None, params=None):
        """Mock _request method."""
        self.request_calls.append((method, path, data, params))

        # Mock user response
        if path.startswith("/api/users?discord_id="):
            return {
                "users": [
                    {
                        "id": 1,
                        "discord_id": TEST_USER_ID,
                        "username": "test_user",
                        "last_active_day": self.last_active_day,
                        "streak_count": self.streak_count,
                        "last_daily_bytes": self.last_daily_bytes.isoformat() if self.last_daily_bytes else None,
                        "bytes_balance": 100
                    }
                ]
            }

        # Mock update response
        if method == "PUT" and path.startswith("/api/users/"):
            if data and "last_daily_bytes" in data:
                self.last_daily_bytes = datetime.fromisoformat(data["last_daily_bytes"])
            if data and "last_active_day" in data:
                self.last_active_day = data["last_active_day"]
            if data and "streak_count" in data:
                self.streak_count = data["streak_count"]

            return {
                "id": 1,
                "discord_id": TEST_USER_ID,
                "username": "test_user",
                "last_active_day": self.last_active_day,
                "streak_count": self.streak_count,
                "last_daily_bytes": self.last_daily_bytes.isoformat() if self.last_daily_bytes else None,
                "bytes_balance": 100
            }

        # Mock bytes transaction
        if method == "POST" and path == "/api/bytes":
            self.bytes_transactions.append(data)
            now = datetime.now(UTC)
            return {
                "id": len(self.bytes_transactions),
                "giver_id": data.get("giver_id", 0),
                "receiver_id": data.get("receiver_id", TEST_USER_ID),
                "guild_id": data.get("guild_id", TEST_GUILD_ID),
                "amount": data.get("amount", 10),
                "reason": data.get("reason", ""),
                "awarded_at": now.isoformat(),
                "giver_balance": 0,
                "receiver_balance": 110
            }

        # Mock system user response
        if path == "/api/users?discord_id=0":
            return {
                "users": [
                    {
                        "id": 0,
                        "discord_id": 0,
                        "username": "System"
                    }
                ]
            }

        return {}

    async def _get_json(self, response):
        """Mock _get_json method."""
        return response


@pytest.mark.asyncio
async def test_on_message_first_message_no_previous_daily_bytes():
    """Test the on_message handler for the first message with no previous daily bytes."""
    from bot.plugins.bytes import on_message, update_user_streak, check_daily_bytes

    # Create mock objects
    api_client = MockAPIClient()
    bot = MagicMock()
    bot.d = MagicMock()
    bot.d.api_client = api_client
    bot.rest = AsyncMock()

    # Create mock event
    event = MagicMock()
    event.app = bot
    event.author_id = TEST_USER_ID
    event.guild_id = TEST_GUILD_ID
    event.is_bot = False
    event.channel_id = 111222333

    # Set up the initial state
    api_client.last_active_day = None
    api_client.last_daily_bytes = None

    # Mock the update_user_streak and check_daily_bytes functions
    async def mock_update_user_streak(client, user_id, guild_id):
        return {
            "user": {
                "id": 1,
                "discord_id": TEST_USER_ID,
                "username": "test_user",
                "last_active_day": client.last_active_day,
                "streak_count": client.streak_count,
                "last_daily_bytes": client.last_daily_bytes.isoformat() if client.last_daily_bytes else None,
                "bytes_balance": 100
            },
            "streak_count": client.streak_count,
            "is_new_day": client.is_new_day
        }

    with patch('bot.plugins.bytes.update_user_streak', side_effect=mock_update_user_streak), \
         patch('bot.plugins.bytes.check_daily_bytes', side_effect=check_daily_bytes):

        # Call the function
        await on_message(event)

        # Verify that bytes transaction was created (daily bytes awarded)
        assert len(api_client.bytes_transactions) == 1

        # Verify that last_daily_bytes was updated
        assert api_client.last_daily_bytes is not None

        # Now simulate a second message
        # Reset the request calls
        api_client.request_calls = []

        # Call the function again
        await on_message(event)

        # Verify that no new bytes transaction was created
        assert len(api_client.bytes_transactions) == 1


@pytest.mark.asyncio
async def test_on_message_same_day_with_previous_daily_bytes():
    """Test the on_message handler for a message on the same day with previous daily bytes."""
    from bot.plugins.bytes import on_message, update_user_streak, check_daily_bytes

    # Create mock objects
    api_client = MockAPIClient()
    bot = MagicMock()
    bot.d = MagicMock()
    bot.d.api_client = api_client
    bot.rest = AsyncMock()

    # Create mock event
    event = MagicMock()
    event.app = bot
    event.author_id = TEST_USER_ID
    event.guild_id = TEST_GUILD_ID
    event.is_bot = False
    event.channel_id = 111222333

    # Set up the initial state - user has been active today and received daily bytes
    now = datetime.now(UTC)
    api_client.last_active_day = now.strftime("%Y-%m-%d")
    api_client.last_daily_bytes = now

    # Mock the update_user_streak and check_daily_bytes functions
    async def mock_update_user_streak(client, user_id, guild_id):
        return {
            "user": {
                "id": 1,
                "discord_id": TEST_USER_ID,
                "username": "test_user",
                "last_active_day": client.last_active_day,
                "streak_count": client.streak_count,
                "last_daily_bytes": client.last_daily_bytes.isoformat() if client.last_daily_bytes else None,
                "bytes_balance": 100
            },
            "streak_count": client.streak_count,
            "is_new_day": False  # Already active today
        }

    with patch('bot.plugins.bytes.update_user_streak', side_effect=mock_update_user_streak), \
         patch('bot.plugins.bytes.check_daily_bytes', side_effect=check_daily_bytes):

        # Call the function
        await on_message(event)

        # Verify that no bytes transaction was created
        assert len(api_client.bytes_transactions) == 0


@pytest.mark.asyncio
async def test_on_message_next_day_after_daily_bytes():
    """Test the on_message handler for a message on the next day after receiving daily bytes."""
    from bot.plugins.bytes import on_message, update_user_streak, check_daily_bytes

    # Create mock objects
    api_client = MockAPIClient()
    bot = MagicMock()
    bot.d = MagicMock()
    bot.d.api_client = api_client
    bot.rest = AsyncMock()

    # Create mock event
    event = MagicMock()
    event.app = bot
    event.author_id = TEST_USER_ID
    event.guild_id = TEST_GUILD_ID
    event.is_bot = False
    event.channel_id = 111222333

    # Set up the initial state - user was active yesterday and received daily bytes
    now = datetime.now(UTC)
    yesterday = (now - timedelta(days=1))
    api_client.last_active_day = yesterday.strftime("%Y-%m-%d")
    api_client.last_daily_bytes = yesterday

    # Mock the update_user_streak and check_daily_bytes functions
    async def mock_update_user_streak(client, user_id, guild_id):
        # Update the last_active_day to today
        current_day = datetime.now(UTC).strftime("%Y-%m-%d")
        client.last_active_day = current_day

        return {
            "user": {
                "id": 1,
                "discord_id": TEST_USER_ID,
                "username": "test_user",
                "last_active_day": client.last_active_day,
                "streak_count": client.streak_count + 1,  # Increment streak
                "last_daily_bytes": client.last_daily_bytes.isoformat() if client.last_daily_bytes else None,
                "bytes_balance": 100
            },
            "streak_count": client.streak_count + 1,
            "is_new_day": True  # First message of the day
        }

    with patch('bot.plugins.bytes.update_user_streak', side_effect=mock_update_user_streak), \
         patch('bot.plugins.bytes.check_daily_bytes', side_effect=check_daily_bytes):

        # Call the function
        await on_message(event)

        # Verify that bytes transaction was created (daily bytes awarded)
        assert len(api_client.bytes_transactions) == 1

        # Verify that last_daily_bytes was updated
        assert api_client.last_daily_bytes > yesterday

        # Now simulate a second message on the same day
        # Reset the request calls
        api_client.request_calls = []

        # Call the function again
        await on_message(event)

        # Verify that no new bytes transaction was created
        assert len(api_client.bytes_transactions) == 1


if __name__ == "__main__":
    pytest.main(["-xvs", __file__])
