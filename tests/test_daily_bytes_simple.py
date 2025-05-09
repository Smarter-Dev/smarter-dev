"""
Simple test for the daily bytes functionality.
"""

import pytest
import os
import sys
from datetime import datetime, timedelta, UTC
from unittest.mock import patch, MagicMock, AsyncMock

# Add the parent directory to the path so we can import the modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Test constants
TEST_USER_ID = 123456789
TEST_GUILD_ID = 987654321


@pytest.mark.asyncio
async def test_on_message_with_last_daily_bytes_today():
    """Test that daily bytes are not awarded if the user has already received them today."""
    from bot.plugins.bytes import on_message

    # Create mock objects
    client = AsyncMock()
    bot = MagicMock()
    bot.d = MagicMock()
    bot.d.api_client = client
    bot.rest = AsyncMock()

    # Create mock event
    event = MagicMock()
    event.app = bot
    event.author_id = TEST_USER_ID
    event.guild_id = TEST_GUILD_ID
    event.is_bot = False
    event.channel_id = 111222333

    # Set up the mock API client
    now = datetime.now(UTC)
    today = now.strftime("%Y-%m-%d")

    # Mock user response with last_daily_bytes from today
    user_response = MagicMock()
    user_response.status_code = 200
    client._get_json.return_value = {
        "users": [
            {
                "id": 1,
                "discord_id": TEST_USER_ID,
                "username": "test_user",
                "last_active_day": today,
                "streak_count": 1,
                "last_daily_bytes": now.isoformat(),
                "bytes_balance": 100
            }
        ]
    }

    # Mock update_user_streak to return a valid response
    update_streak_mock = AsyncMock(return_value={
        "user": {
            "id": 1,
            "discord_id": TEST_USER_ID,
            "username": "test_user",
            "last_active_day": today,
            "streak_count": 1,
            "last_daily_bytes": now.isoformat(),
            "bytes_balance": 100
        },
        "streak_count": 1,
        "is_new_day": False
    })

    # Patch the necessary functions
    with patch('bot.plugins.bytes.update_user_streak', update_streak_mock):
        # Call the function
        await on_message(event)

        # Verify that update_user_streak was called
        update_streak_mock.assert_called_once()

        # Verify that no bytes transaction was created
        assert not any(call[0] == "POST" and call[1] == "/api/bytes" for call in client._request.call_args_list)


@pytest.mark.asyncio
async def test_on_message_with_last_daily_bytes_yesterday():
    """Test that daily bytes are awarded if the user received them yesterday."""
    from bot.plugins.bytes import on_message

    # Create mock objects
    client = AsyncMock()
    bot = MagicMock()
    bot.d = MagicMock()
    bot.d.api_client = client
    bot.rest = AsyncMock()

    # Create mock event
    event = MagicMock()
    event.app = bot
    event.author_id = TEST_USER_ID
    event.guild_id = TEST_GUILD_ID
    event.is_bot = False
    event.channel_id = 111222333

    # Set up the mock API client
    now = datetime.now(UTC)
    today = now.strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    yesterday_datetime = now - timedelta(days=1)

    # Mock user response with last_daily_bytes from yesterday
    client._request = AsyncMock()
    client._request.return_value = MagicMock(status_code=200)

    # Set up the user response
    client._get_json.side_effect = [
        # First call - user lookup
        {
            "users": [
                {
                    "id": 1,
                    "discord_id": TEST_USER_ID,
                    "username": "test_user",
                    "last_active_day": yesterday,
                    "streak_count": 1,
                    "last_daily_bytes": yesterday_datetime.isoformat(),
                    "bytes_balance": 100
                }
            ]
        },
        # Second call - system user lookup
        {
            "users": [
                {
                    "id": 0,
                    "discord_id": 0,
                    "username": "System"
                }
            ]
        },
        # Third call - bytes transaction result
        {
            "id": 1,
            "giver_id": 0,
            "receiver_id": TEST_USER_ID,
            "guild_id": TEST_GUILD_ID,
            "amount": 10,
            "reason": "Daily bytes for 2 day streak"
        }
    ]

    # Mock update_user_streak to return a valid response
    update_streak_mock = AsyncMock(return_value={
        "user": {
            "id": 1,
            "discord_id": TEST_USER_ID,
            "username": "test_user",
            "last_active_day": today,
            "streak_count": 2,
            "last_daily_bytes": yesterday_datetime.isoformat(),
            "bytes_balance": 100
        },
        "streak_count": 2,
        "is_new_day": True
    })

    # Mock get_bytes_config
    get_bytes_config_mock = AsyncMock(return_value=MagicMock(daily_earning=10))

    # Patch the necessary functions
    with patch('bot.plugins.bytes.update_user_streak', update_streak_mock), \
         patch('bot.plugins.bytes.get_bytes_config', get_bytes_config_mock):
        # Call the function
        await on_message(event)

        # Verify that update_user_streak was called
        update_streak_mock.assert_called_once()

        # Verify that a bytes transaction was created
        assert any(call[0] == "POST" and call[1] == "/api/bytes" for call in client._request.call_args_list)


if __name__ == "__main__":
    pytest.main(["-xvs", __file__])
