"""
Tests for the bytes plugin functionality, focusing on the daily bytes reward system.
"""

import pytest
import os
import sys
import datetime
from datetime import datetime, timedelta, UTC
import httpx
import asyncio
from unittest.mock import patch, MagicMock, AsyncMock

# Add the parent directory to the path so we can import the modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.api_client import APIClient
from bot.api_models import DiscordUser, GuildMember, Bytes, BytesConfig
import bot.plugins.bytes as bytes_plugin

# Test constants
TEST_API_URL = "http://localhost:8000"
TEST_API_KEY = "TESTING"
TEST_USER_ID = 123456789
TEST_GUILD_ID = 987654321


@pytest.fixture
def api_client():
    """Create an API client for testing."""
    client = MagicMock(spec=APIClient)
    client._request = AsyncMock()
    client._get_json = AsyncMock()
    client._dict_from_model = MagicMock()
    client.get_guild_member = AsyncMock()
    return client


@pytest.fixture
def mock_bot():
    """Create a mock bot for testing."""
    bot = MagicMock()
    bot.d = MagicMock()
    bot.d.api_client = MagicMock()
    bot.rest = AsyncMock()
    return bot


@pytest.fixture
def mock_event(mock_bot):
    """Create a mock event for testing."""
    event = MagicMock()
    event.app = mock_bot
    event.author_id = TEST_USER_ID
    event.guild_id = TEST_GUILD_ID
    event.is_bot = False
    event.channel_id = 111222333
    return event


@pytest.mark.asyncio
async def test_update_user_streak_first_time(api_client):
    """Test updating a user's streak for the first time."""
    # Mock the user response
    now = datetime.now(UTC)
    user_response = httpx.Response(
        200,
        json={
            "users": [
                {
                    "id": 1,
                    "discord_id": TEST_USER_ID,
                    "username": "test_user",
                    "last_active_day": None,
                    "streak_count": 0,
                    "bytes_balance": 100
                }
            ]
        }
    )

    # Mock the update response
    update_response = httpx.Response(
        200,
        json={
            "id": 1,
            "discord_id": TEST_USER_ID,
            "username": "test_user",
            "last_active_day": now.strftime("%Y-%m-%d"),
            "streak_count": 1,
            "bytes_balance": 100
        }
    )

    # Patch the _request method to return our mock responses
    with patch.object(api_client, '_request', side_effect=[user_response, update_response]):
        # Call the function
        result = await bytes_plugin.update_user_streak(api_client, TEST_USER_ID, TEST_GUILD_ID)

        # Verify the result
        assert result is not None
        assert result["streak_count"] == 1
        assert result["is_new_day"] == True


@pytest.mark.asyncio
async def test_update_user_streak_same_day(api_client):
    """Test updating a user's streak when they've already been active today."""
    # Mock the user response
    now = datetime.now(UTC)
    current_day = now.strftime("%Y-%m-%d")
    user_response = httpx.Response(
        200,
        json={
            "users": [
                {
                    "id": 1,
                    "discord_id": TEST_USER_ID,
                    "username": "test_user",
                    "last_active_day": current_day,
                    "streak_count": 1,
                    "bytes_balance": 100
                }
            ]
        }
    )

    # Mock the update response
    update_response = httpx.Response(
        200,
        json={
            "id": 1,
            "discord_id": TEST_USER_ID,
            "username": "test_user",
            "last_active_day": current_day,
            "streak_count": 1,
            "bytes_balance": 100
        }
    )

    # Patch the _request method to return our mock responses
    with patch.object(api_client, '_request', side_effect=[user_response, update_response]):
        # Call the function
        result = await bytes_plugin.update_user_streak(api_client, TEST_USER_ID, TEST_GUILD_ID)

        # Verify the result
        assert result is not None
        assert result["streak_count"] == 1
        assert result["is_new_day"] == False


@pytest.mark.asyncio
async def test_update_user_streak_next_day(api_client):
    """Test updating a user's streak when they were active yesterday."""
    # Mock the user response
    now = datetime.now(UTC)
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    current_day = now.strftime("%Y-%m-%d")
    user_response = httpx.Response(
        200,
        json={
            "users": [
                {
                    "id": 1,
                    "discord_id": TEST_USER_ID,
                    "username": "test_user",
                    "last_active_day": yesterday,
                    "streak_count": 1,
                    "bytes_balance": 100
                }
            ]
        }
    )

    # Mock the update response
    update_response = httpx.Response(
        200,
        json={
            "id": 1,
            "discord_id": TEST_USER_ID,
            "username": "test_user",
            "last_active_day": current_day,
            "streak_count": 2,
            "bytes_balance": 100
        }
    )

    # Patch the _request method to return our mock responses
    with patch.object(api_client, '_request', side_effect=[user_response, update_response]):
        # Call the function
        result = await bytes_plugin.update_user_streak(api_client, TEST_USER_ID, TEST_GUILD_ID)

        # Verify the result
        assert result is not None
        assert result["streak_count"] == 2
        assert result["is_new_day"] == True


@pytest.mark.asyncio
async def test_check_daily_bytes_eligible(api_client, mock_bot):
    """Test checking daily bytes when the user is eligible."""
    # Mock the streak info
    now = datetime.now(UTC)
    streak_info = {
        "user": {
            "id": 1,
            "discord_id": TEST_USER_ID,
            "username": "test_user",
            "last_active_day": now.strftime("%Y-%m-%d"),
            "streak_count": 1,
            "bytes_balance": 100
        },
        "streak_count": 1,
        "is_new_day": True
    }

    # Mock the guild member response
    guild_member_response = None  # No guild member found

    # Mock the bytes config response
    bytes_config_response = httpx.Response(
        200,
        json={
            "id": 1,
            "guild_id": TEST_GUILD_ID,
            "starting_balance": 100,
            "daily_earning": 10,
            "max_give_amount": 50,
            "cooldown_minutes": 1440
        }
    )

    # Mock the system user response
    system_user_response = httpx.Response(
        200,
        json={
            "users": [
                {
                    "id": 0,
                    "discord_id": 0,
                    "username": "System"
                }
            ]
        }
    )

    # Mock the bytes transaction response
    bytes_transaction_response = httpx.Response(
        201,
        json={
            "id": 1,
            "giver_id": 0,
            "receiver_id": TEST_USER_ID,
            "guild_id": TEST_GUILD_ID,
            "amount": 10,
            "reason": "Daily bytes for 1 day streak",
            "awarded_at": now.isoformat(),
            "giver_balance": 0,
            "receiver_balance": 110
        }
    )

    # Mock the user update response
    user_update_response = httpx.Response(
        200,
        json={
            "id": 1,
            "discord_id": TEST_USER_ID,
            "username": "test_user",
            "last_active_day": now.strftime("%Y-%m-%d"),
            "streak_count": 1,
            "bytes_balance": 110,
            "last_daily_bytes": now.isoformat()
        }
    )

    # Mock the Discord user
    discord_user = MagicMock()
    discord_user.id = TEST_USER_ID
    discord_user.username = "test_user"
    discord_user.mention = "@test_user"

    # Mock the guild
    guild = MagicMock()
    guild.id = TEST_GUILD_ID
    guild.name = "Test Guild"
    guild.system_channel_id = 111222333

    # Patch the necessary methods
    with patch.object(bytes_plugin, 'update_user_streak', return_value=streak_info), \
         patch.object(api_client, 'get_guild_member', return_value=guild_member_response), \
         patch.object(bytes_plugin, 'get_bytes_config', return_value=BytesConfig(
             id=1, guild_id=TEST_GUILD_ID, daily_earning=10, starting_balance=100, max_give_amount=50, cooldown_minutes=1440
         )), \
         patch.object(api_client, '_request', side_effect=[system_user_response, bytes_transaction_response, user_update_response]), \
         patch.object(mock_bot.rest, 'fetch_user', return_value=discord_user), \
         patch.object(mock_bot.rest, 'fetch_guild', return_value=guild), \
         patch.object(mock_bot.rest, 'create_message', return_value=None), \
         patch.object(bytes_plugin, 'check_for_earned_roles', return_value=None):

        # Call the function
        await bytes_plugin.check_daily_bytes(api_client, mock_bot, TEST_USER_ID, TEST_GUILD_ID)

        # Verify that the create_message method was called (indicating bytes were awarded)
        mock_bot.rest.create_message.assert_called_once()


@pytest.mark.asyncio
async def test_check_daily_bytes_not_eligible_already_received(api_client, mock_bot):
    """Test checking daily bytes when the user has already received bytes today."""
    # Mock the streak info
    now = datetime.now(UTC)
    streak_info = {
        "user": {
            "id": 1,
            "discord_id": TEST_USER_ID,
            "username": "test_user",
            "last_active_day": now.strftime("%Y-%m-%d"),
            "streak_count": 1,
            "bytes_balance": 100,
            "last_daily_bytes": now.isoformat()
        },
        "streak_count": 1,
        "is_new_day": True
    }

    # Mock the guild member response with recent last_daily_bytes
    guild_member = GuildMember(
        id=1,
        user_id=1,
        guild_id=TEST_GUILD_ID,
        nickname="test_nickname",
        joined_at=now,
        is_active=True,
        created_at=now,
        last_daily_bytes=now
    )

    # Patch the necessary methods
    with patch.object(bytes_plugin, 'update_user_streak', return_value=streak_info), \
         patch.object(api_client, 'get_guild_member', return_value=guild_member):

        # Call the function
        await bytes_plugin.check_daily_bytes(api_client, mock_bot, TEST_USER_ID, TEST_GUILD_ID)

        # Verify that the create_message method was not called (indicating bytes were not awarded)
        mock_bot.rest.create_message.assert_not_called()


@pytest.mark.asyncio
async def test_on_message_first_message_of_day(mock_event):
    """Test the on_message handler for the first message of the day."""
    # Mock the API client
    api_client = AsyncMock()
    mock_event.app.d.api_client = api_client

    # Mock the user response
    now = datetime.now(UTC)
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    user_response = {
        "users": [
            {
                "id": 1,
                "discord_id": TEST_USER_ID,
                "username": "test_user",
                "last_active_day": yesterday,
                "streak_count": 1,
                "bytes_balance": 100
            }
        ]
    }

    # Mock the guild member response with no last_daily_bytes
    guild_member = None

    # Mock the streak info
    streak_info = {
        "user": {
            "id": 1,
            "discord_id": TEST_USER_ID,
            "username": "test_user",
            "last_active_day": now.strftime("%Y-%m-%d"),
            "streak_count": 2,
            "bytes_balance": 100
        },
        "streak_count": 2,
        "is_new_day": True
    }

    # Patch the necessary methods
    api_client._get_json.return_value = user_response
    api_client.get_guild_member.return_value = guild_member

    with patch.object(bytes_plugin, 'update_user_streak', return_value=streak_info), \
         patch.object(bytes_plugin, 'check_daily_bytes', return_value=None):

        # Call the function
        await bytes_plugin.on_message(mock_event)

        # Verify that check_daily_bytes was called
        bytes_plugin.check_daily_bytes.assert_called_once_with(
            api_client, mock_event.app, TEST_USER_ID, TEST_GUILD_ID, mock_event.channel_id
        )


@pytest.mark.asyncio
async def test_on_message_already_active_today(mock_event):
    """Test the on_message handler when the user has already been active today."""
    # Mock the API client
    api_client = AsyncMock()
    mock_event.app.d.api_client = api_client

    # Mock the user response
    now = datetime.now(UTC)
    current_day = now.strftime("%Y-%m-%d")
    user_response = {
        "users": [
            {
                "id": 1,
                "discord_id": TEST_USER_ID,
                "username": "test_user",
                "last_active_day": current_day,
                "streak_count": 1,
                "bytes_balance": 100
            }
        ]
    }

    # Mock the guild member response with no last_daily_bytes
    guild_member = None

    # Mock the streak info
    streak_info = {
        "user": {
            "id": 1,
            "discord_id": TEST_USER_ID,
            "username": "test_user",
            "last_active_day": current_day,
            "streak_count": 1,
            "bytes_balance": 100
        },
        "streak_count": 1,
        "is_new_day": False
    }

    # Patch the necessary methods
    api_client._get_json.return_value = user_response
    api_client.get_guild_member.return_value = guild_member

    with patch.object(bytes_plugin, 'update_user_streak', return_value=streak_info):

        # Call the function
        await bytes_plugin.on_message(mock_event)

        # Verify that check_daily_bytes was not called
        assert not hasattr(bytes_plugin, 'check_daily_bytes.assert_called')


@pytest.mark.asyncio
async def test_on_message_already_received_daily_bytes(mock_event):
    """Test the on_message handler when the user has already received daily bytes today."""
    # Mock the API client
    api_client = AsyncMock()
    mock_event.app.d.api_client = api_client

    # Mock the user response
    now = datetime.now(UTC)
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    user_response = {
        "users": [
            {
                "id": 1,
                "discord_id": TEST_USER_ID,
                "username": "test_user",
                "last_active_day": yesterday,
                "streak_count": 1,
                "bytes_balance": 100,
                "last_daily_bytes": now.isoformat()
            }
        ]
    }

    # Mock the guild member response with recent last_daily_bytes
    guild_member = GuildMember(
        id=1,
        user_id=1,
        guild_id=TEST_GUILD_ID,
        nickname="test_nickname",
        joined_at=now,
        is_active=True,
        created_at=now,
        last_daily_bytes=now
    )

    # Patch the necessary methods
    api_client._get_json.return_value = user_response
    api_client.get_guild_member.return_value = guild_member

    with patch.object(bytes_plugin, 'update_user_streak', return_value=None):

        # Call the function
        await bytes_plugin.on_message(mock_event)

        # Verify that update_user_streak was called
        bytes_plugin.update_user_streak.assert_called_once()

        # Verify that check_daily_bytes was not called
        assert not hasattr(bytes_plugin, 'check_daily_bytes.assert_called')


if __name__ == "__main__":
    pytest.main(["-xvs", __file__])
