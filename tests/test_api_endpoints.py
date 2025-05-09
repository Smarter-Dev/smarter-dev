"""
Tests for the API endpoints related to the daily bytes functionality.
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
from bot.api_models import DiscordUser, GuildMember, Bytes

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


@pytest.mark.asyncio
async def test_get_user_with_last_daily_bytes(api_client):
    """Test retrieving a user with last_daily_bytes field."""
    # Mock the response
    now = datetime.now(UTC)
    mock_response = httpx.Response(
        200,
        json={
            "users": [
                {
                    "id": 1,
                    "discord_id": TEST_USER_ID,
                    "username": "test_user",
                    "last_active_day": now.strftime("%Y-%m-%d"),
                    "streak_count": 1,
                    "last_daily_bytes": now.isoformat(),
                    "bytes_balance": 100
                }
            ]
        }
    )

    # Patch the _request method to return our mock response
    with patch.object(api_client, '_request', return_value=mock_response):
        # Call the API
        response = await api_client._request("GET", f"/api/users?discord_id={TEST_USER_ID}")
        data = await api_client._get_json(response)

        # Verify the response
        assert data.get("users") is not None
        assert len(data["users"]) == 1
        assert data["users"][0]["discord_id"] == TEST_USER_ID
        assert data["users"][0]["last_daily_bytes"] == now.isoformat()


@pytest.mark.asyncio
async def test_update_user_last_daily_bytes(api_client):
    """Test updating a user's last_daily_bytes field."""
    # Mock the response
    now = datetime.now(UTC)
    mock_response = httpx.Response(
        200,
        json={
            "id": 1,
            "discord_id": TEST_USER_ID,
            "username": "test_user",
            "last_active_day": now.strftime("%Y-%m-%d"),
            "streak_count": 1,
            "last_daily_bytes": now.isoformat(),
            "bytes_balance": 100
        }
    )

    # Patch the _request method to return our mock response
    with patch.object(api_client, '_request', return_value=mock_response):
        # Call the API
        update_data = {
            "last_daily_bytes": now.isoformat()
        }
        response = await api_client._request("PUT", f"/api/users/1", data=update_data)
        data = await api_client._get_json(response)

        # Verify the response
        assert data["discord_id"] == TEST_USER_ID
        assert data["last_daily_bytes"] == now.isoformat()


@pytest.mark.asyncio
async def test_get_guild_member_with_last_daily_bytes(api_client):
    """Test retrieving a guild member with last_daily_bytes field."""
    # Mock the response
    now = datetime.now(UTC)
    mock_response = httpx.Response(
        200,
        json={
            "id": 1,
            "user_id": 1,
            "guild_id": TEST_GUILD_ID,
            "nickname": "test_nickname",
            "joined_at": now.isoformat(),
            "is_active": True,
            "created_at": now.isoformat(),
            "last_daily_bytes": now.isoformat()
        }
    )

    # Patch the _request method to return our mock response
    with patch.object(api_client, '_request', return_value=mock_response):
        # Call the API
        response = await api_client._request("GET", f"/api/users/{TEST_USER_ID}/guilds/{TEST_GUILD_ID}")
        data = await api_client._get_json(response)

        # Verify the response
        assert data["guild_id"] == TEST_GUILD_ID
        assert data["last_daily_bytes"] == now.isoformat()


@pytest.mark.asyncio
async def test_update_guild_member_last_daily_bytes(api_client):
    """Test updating a guild member's last_daily_bytes field."""
    # Mock the response
    now = datetime.now(UTC)
    mock_response = httpx.Response(
        200,
        json={
            "id": 1,
            "user_id": 1,
            "guild_id": TEST_GUILD_ID,
            "nickname": "test_nickname",
            "joined_at": now.isoformat(),
            "is_active": True,
            "created_at": now.isoformat(),
            "last_daily_bytes": now.isoformat()
        }
    )

    # Create a guild member object
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

    # Patch the _request method to return our mock response
    with patch.object(api_client, '_request', return_value=mock_response):
        # Call the API
        guild_member_dict = api_client._dict_from_model(guild_member)
        response = await api_client._request(
            "PUT",
            f"/api/users/{TEST_USER_ID}/guilds/{TEST_GUILD_ID}",
            data=guild_member_dict
        )
        data = await api_client._get_json(response)

        # Verify the response
        assert data["guild_id"] == TEST_GUILD_ID
        assert data["last_daily_bytes"] == now.isoformat()


@pytest.mark.asyncio
async def test_create_bytes_transaction(api_client):
    """Test creating a bytes transaction."""
    # Mock the response
    now = datetime.now(UTC)
    mock_response = httpx.Response(
        201,
        json={
            "id": 1,
            "giver_id": 0,  # System user
            "receiver_id": TEST_USER_ID,
            "guild_id": TEST_GUILD_ID,
            "amount": 10,
            "reason": "Daily bytes for 1 day streak",
            "awarded_at": now.isoformat(),
            "giver_balance": 0,
            "receiver_balance": 110
        }
    )

    # Create a bytes object
    bytes_obj = Bytes(
        giver_id=0,  # System user
        receiver_id=TEST_USER_ID,
        guild_id=TEST_GUILD_ID,
        amount=10,
        reason="Daily bytes for 1 day streak"
    )

    # Patch the _request method to return our mock response
    with patch.object(api_client, '_request', return_value=mock_response):
        # Call the API
        bytes_dict = api_client._dict_from_model(bytes_obj)
        response = await api_client._request("POST", "/api/bytes", data=bytes_dict)
        data = await api_client._get_json(response)

        # Verify the response
        assert data["giver_id"] == 0
        assert data["receiver_id"] == TEST_USER_ID
        assert data["guild_id"] == TEST_GUILD_ID
        assert data["amount"] == 10
        assert data["reason"] == "Daily bytes for 1 day streak"


if __name__ == "__main__":
    pytest.main(["-xvs", __file__])
