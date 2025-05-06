"""
Tests for the API client endpoints.
"""

import os
import sys
import time
import json
import pytest
from datetime import datetime
from unittest.mock import patch, AsyncMock

# Add the project root to the path so we can import the bot package
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from bot.api_client import APIClient
from bot.api_models import (
    Guild, DiscordUser, GuildMember, UserNote, UserWarning,
    ModerationCase, PersistentRole, TemporaryRole, ChannelLock,
    BumpStat, CommandUsage
)

# Base URL for testing
BASE_URL = "http://localhost:8000"
API_KEY = "TESTING"

@pytest.mark.asyncio
async def test_get_guilds():
    """Test getting all guilds"""
    client = APIClient(BASE_URL, API_KEY)

    # Mock the token response
    token_response = AsyncMock()
    token_response.status_code = 200
    token_response.text = "{'token': 'test-token', 'expires_in': 3600}"
    token_response.json = AsyncMock(return_value={
        "token": "test-token",
        "expires_in": 3600
    })

    # Mock the API response
    api_response = AsyncMock()
    api_response.status_code = 200
    api_response.text = json.dumps({
        "guilds": [
            {
                "id": 1,
                "discord_id": 123456789,
                "name": "Test Guild",
                "icon_url": "https://example.com/icon.png",
                "joined_at": "2023-01-01T00:00:00",
                "created_at": "2023-01-01T00:00:00"
            }
        ]
    })
    api_response.json = AsyncMock(return_value={
        "guilds": [
            {
                "id": 1,
                "discord_id": 123456789,
                "name": "Test Guild",
                "icon_url": "https://example.com/icon.png",
                "joined_at": "2023-01-01T00:00:00",
                "created_at": "2023-01-01T00:00:00"
            }
        ]
    })

    try:
        # First mock the token request
        with patch.object(client.client, 'post', new=AsyncMock(return_value=token_response)):
            # Then mock the API request
            with patch.object(client.client, 'request', new=AsyncMock(return_value=api_response)):
                guilds = await client.get_guilds()

                # Check that we got the expected guilds
                assert len(guilds) == 1
                assert guilds[0].id == 1
                assert guilds[0].discord_id == 123456789
                assert guilds[0].name == "Test Guild"
                assert guilds[0].icon_url == "https://example.com/icon.png"
                assert isinstance(guilds[0].joined_at, datetime)
                assert isinstance(guilds[0].created_at, datetime)
    finally:
        await client.close()

@pytest.mark.asyncio
async def test_create_guild():
    """Test creating a guild"""
    client = APIClient(BASE_URL, API_KEY)

    # Create a guild to send
    guild = Guild(
        id=None,  # No ID for creation
        discord_id=123456789,
        name="Test Guild",
        icon_url="https://example.com/icon.png"
    )

    # Mock the token response
    token_response = AsyncMock()
    token_response.status_code = 200
    token_response.text = "{'token': 'test-token', 'expires_in': 3600}"
    token_response.json = AsyncMock(return_value={
        "token": "test-token",
        "expires_in": 3600
    })

    # Mock the API response
    api_response = AsyncMock()
    api_response.status_code = 201
    api_response.text = json.dumps({
        "id": 1,
        "discord_id": 123456789,
        "name": "Test Guild",
        "icon_url": "https://example.com/icon.png",
        "joined_at": "2023-01-01T00:00:00",
        "created_at": "2023-01-01T00:00:00"
    })
    api_response.json = AsyncMock(return_value={
        "id": 1,
        "discord_id": 123456789,
        "name": "Test Guild",
        "icon_url": "https://example.com/icon.png",
        "joined_at": "2023-01-01T00:00:00",
        "created_at": "2023-01-01T00:00:00"
    })

    try:
        # First mock the token request
        with patch.object(client.client, 'post', new=AsyncMock(return_value=token_response)):
            # Then mock the API request
            with patch.object(client.client, 'request', new=AsyncMock(return_value=api_response)):
                created_guild = await client.create_guild(guild)

                # Check that we got the expected guild back
                assert created_guild.id == 1
                assert created_guild.discord_id == 123456789
                assert created_guild.name == "Test Guild"
                assert created_guild.icon_url == "https://example.com/icon.png"
                assert isinstance(created_guild.joined_at, datetime)
                assert isinstance(created_guild.created_at, datetime)
    finally:
        await client.close()

@pytest.mark.asyncio
async def test_get_users():
    """Test getting all users"""
    client = APIClient(BASE_URL, API_KEY)

    # Mock the token response
    token_response = AsyncMock()
    token_response.status_code = 200
    token_response.text = "{'token': 'test-token', 'expires_in': 3600}"
    token_response.json = AsyncMock(return_value={
        "token": "test-token",
        "expires_in": 3600
    })

    # Mock the API response
    api_response = AsyncMock()
    api_response.status_code = 200
    api_response.text = json.dumps({
        "users": [
            {
                "id": 1,
                "discord_id": 123456789,
                "username": "TestUser",
                "discriminator": "1234",
                "avatar_url": "https://example.com/avatar.png",
                "created_at": "2023-01-01T00:00:00"
            }
        ]
    })
    api_response.json = AsyncMock(return_value={
        "users": [
            {
                "id": 1,
                "discord_id": 123456789,
                "username": "TestUser",
                "discriminator": "1234",
                "avatar_url": "https://example.com/avatar.png",
                "created_at": "2023-01-01T00:00:00"
            }
        ]
    })

    try:
        # First mock the token request
        with patch.object(client.client, 'post', new=AsyncMock(return_value=token_response)):
            # Then mock the API request
            with patch.object(client.client, 'request', new=AsyncMock(return_value=api_response)):
                users = await client.get_users()

                # Check that we got the expected users
                assert len(users) == 1
                assert users[0].id == 1
                assert users[0].discord_id == 123456789
                assert users[0].username == "TestUser"
                assert users[0].discriminator == "1234"
                assert users[0].avatar_url == "https://example.com/avatar.png"
                assert isinstance(users[0].created_at, datetime)
    finally:
        await client.close()



@pytest.mark.asyncio
async def test_get_warnings():
    """Test getting warnings with filtering"""
    client = APIClient(BASE_URL, API_KEY)

    # Mock the token response
    token_response = AsyncMock()
    token_response.status_code = 200
    token_response.text = "{'token': 'test-token', 'expires_in': 3600}"
    token_response.json = AsyncMock(return_value={
        "token": "test-token",
        "expires_in": 3600
    })

    # Mock the API response
    api_response = AsyncMock()
    api_response.status_code = 200
    api_response.text = json.dumps({
        "warnings": [
            {
                "id": 1,
                "user_id": 2,
                "mod_id": 3,
                "guild_id": 4,
                "reason": "Breaking rules",
                "warned_at": "2023-01-01T00:00:00"
            }
        ]
    })
    api_response.json = AsyncMock(return_value={
        "warnings": [
            {
                "id": 1,
                "user_id": 2,
                "mod_id": 3,
                "guild_id": 4,
                "reason": "Breaking rules",
                "warned_at": "2023-01-01T00:00:00"
            }
        ]
    })

    try:
        # First mock the token request
        with patch.object(client.client, 'post', new=AsyncMock(return_value=token_response)):
            # Then mock the API request
            with patch.object(client.client, 'request', new=AsyncMock(return_value=api_response)):
                warnings = await client.get_warnings(guild_id=4, user_id=2)

                # Check that we got the expected warnings
                assert len(warnings) == 1
                assert warnings[0].id == 1
                assert warnings[0].user_id == 2
                assert warnings[0].mod_id == 3
                assert warnings[0].guild_id == 4
                assert warnings[0].reason == "Breaking rules"
                assert isinstance(warnings[0].warned_at, datetime)
    finally:
        await client.close()

@pytest.mark.asyncio
async def test_create_moderation_case():
    """Test creating a moderation case"""
    client = APIClient(BASE_URL, API_KEY)

    # Create a case to send
    case = ModerationCase(
        guild_id=1,
        user_id=2,
        mod_id=3,
        action="ban",
        reason="Breaking rules",
        duration_sec=3600
    )

    # Mock the token response
    token_response = AsyncMock()
    token_response.status_code = 200
    token_response.text = "{'token': 'test-token', 'expires_in': 3600}"
    token_response.json = AsyncMock(return_value={
        "token": "test-token",
        "expires_in": 3600
    })

    # Mock the API response
    api_response = AsyncMock()
    api_response.status_code = 201
    api_response.text = json.dumps({
        "id": 1,
        "case_number": 1,
        "guild_id": 1,
        "user_id": 2,
        "mod_id": 3,
        "action": "ban",
        "reason": "Breaking rules",
        "created_at": "2023-01-01T00:00:00",
        "duration_sec": 3600,
        "resolved_at": None,
        "resolution_note": None
    })
    api_response.json = AsyncMock(return_value={
        "id": 1,
        "case_number": 1,
        "guild_id": 1,
        "user_id": 2,
        "mod_id": 3,
        "action": "ban",
        "reason": "Breaking rules",
        "created_at": "2023-01-01T00:00:00",
        "duration_sec": 3600,
        "resolved_at": None,
        "resolution_note": None
    })

    try:
        # First mock the token request
        with patch.object(client.client, 'post', new=AsyncMock(return_value=token_response)):
            # Then mock the API request
            with patch.object(client.client, 'request', new=AsyncMock(return_value=api_response)):
                created_case = await client.create_moderation_case(case)

                # Check that we got the expected case back
                assert created_case.id == 1
                assert created_case.case_number == 1
                assert created_case.guild_id == 1
                assert created_case.user_id == 2
                assert created_case.mod_id == 3
                assert created_case.action == "ban"
                assert created_case.reason == "Breaking rules"
                assert isinstance(created_case.created_at, datetime)
                assert created_case.duration_sec == 3600
                assert created_case.resolved_at is None
                assert created_case.resolution_note is None
    finally:
        await client.close()

@pytest.mark.asyncio
async def test_update_moderation_case():
    """Test updating a moderation case"""
    client = APIClient(BASE_URL, API_KEY)

    # Create a case to update
    case = ModerationCase(
        id=1,
        case_number=1,
        guild_id=1,
        user_id=2,
        mod_id=3,
        action="ban",
        reason="Breaking rules",
        created_at=datetime(2023, 1, 1),
        duration_sec=3600,
        resolved_at=datetime(2023, 1, 2),
        resolution_note="User apologized"
    )

    # Mock the token response
    token_response = AsyncMock()
    token_response.status_code = 200
    token_response.text = "{'token': 'test-token', 'expires_in': 3600}"
    token_response.json = AsyncMock(return_value={
        "token": "test-token",
        "expires_in": 3600
    })

    # Mock the API response
    api_response = AsyncMock()
    api_response.status_code = 200
    api_response.text = json.dumps({
        "id": 1,
        "case_number": 1,
        "guild_id": 1,
        "user_id": 2,
        "mod_id": 3,
        "action": "ban",
        "reason": "Breaking rules",
        "created_at": "2023-01-01T00:00:00",
        "duration_sec": 3600,
        "resolved_at": "2023-01-02T00:00:00",
        "resolution_note": "User apologized"
    })
    api_response.json = AsyncMock(return_value={
        "id": 1,
        "case_number": 1,
        "guild_id": 1,
        "user_id": 2,
        "mod_id": 3,
        "action": "ban",
        "reason": "Breaking rules",
        "created_at": "2023-01-01T00:00:00",
        "duration_sec": 3600,
        "resolved_at": "2023-01-02T00:00:00",
        "resolution_note": "User apologized"
    })

    try:
        # First mock the token request
        with patch.object(client.client, 'post', new=AsyncMock(return_value=token_response)):
            # Then mock the API request
            with patch.object(client.client, 'request', new=AsyncMock(return_value=api_response)):
                updated_case = await client.update_moderation_case(case)

                # Check that we got the expected case back
                assert updated_case.id == 1
                assert updated_case.case_number == 1
                assert updated_case.guild_id == 1
                assert updated_case.user_id == 2
                assert updated_case.mod_id == 3
                assert updated_case.action == "ban"
                assert updated_case.reason == "Breaking rules"
                assert isinstance(updated_case.created_at, datetime)
                assert updated_case.duration_sec == 3600
                assert isinstance(updated_case.resolved_at, datetime)
                assert updated_case.resolution_note == "User apologized"
    finally:
        await client.close()
