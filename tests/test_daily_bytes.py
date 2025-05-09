"""
Test for the daily bytes functionality.
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


class MockAPIClient:
    """Mock API client for testing."""
    
    def __init__(self):
        self.requests = []
        self.last_daily_bytes = None
        self.last_active_day = None
        self.streak_count = 1
        self.bytes_transactions = []
        
    async def get_guild_member(self, user_id, guild_id):
        """Mock get_guild_member method."""
        self.requests.append(("get_guild_member", user_id, guild_id))
        
        if self.last_daily_bytes:
            return MagicMock(
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
    
    async def _request(self, method, path, data=None):
        """Mock _request method."""
        self.requests.append((method, path, data))
        
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
        
        # Mock bytes transaction
        if method == "POST" and path == "/api/bytes":
            self.bytes_transactions.append(data)
            return {"id": 1}
        
        # Mock system user
        if path == "/api/users?discord_id=0":
            return {"users": [{"id": 0, "discord_id": 0, "username": "System"}]}
        
        return {}
    
    async def _get_json(self, response):
        """Mock _get_json method."""
        return response
    
    def _dict_from_model(self, model):
        """Mock _dict_from_model method."""
        return {"id": 1}


@pytest.mark.asyncio
async def test_daily_bytes_awarded_only_once_per_day():
    """Test that daily bytes are only awarded once per day."""
    from bot.plugins.bytes import on_message
    
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
    
    # Set up the initial state - user has not been active today and has not received daily bytes
    api_client.last_active_day = None
    api_client.last_daily_bytes = None
    
    # Mock the necessary functions
    with patch('bot.plugins.bytes.update_user_streak', return_value={
        "user": {"id": 1},
        "streak_count": 1,
        "is_new_day": True
    }), patch('bot.plugins.bytes.award_daily_bytes', AsyncMock()):
        
        # Call the function for the first time
        await on_message(event)
        
        # Verify that award_daily_bytes was called
        from bot.plugins.bytes import award_daily_bytes
        award_daily_bytes.assert_called_once()
        
        # Reset the mock
        award_daily_bytes.reset_mock()
        
        # Set up the state as if the user has received daily bytes
        api_client.last_daily_bytes = datetime.now(UTC)
        api_client.last_active_day = datetime.now(UTC).strftime("%Y-%m-%d")
        
        # Call the function again
        await on_message(event)
        
        # Verify that award_daily_bytes was NOT called
        award_daily_bytes.assert_not_called()


@pytest.mark.asyncio
async def test_daily_bytes_awarded_next_day():
    """Test that daily bytes are awarded again on the next day."""
    from bot.plugins.bytes import on_message
    
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
    
    # Set up the initial state - user received daily bytes yesterday
    yesterday = datetime.now(UTC) - timedelta(days=1)
    api_client.last_active_day = yesterday.strftime("%Y-%m-%d")
    api_client.last_daily_bytes = yesterday
    
    # Mock the necessary functions
    with patch('bot.plugins.bytes.update_user_streak', return_value={
        "user": {"id": 1},
        "streak_count": 2,  # Streak increased
        "is_new_day": True  # First message of the day
    }), patch('bot.plugins.bytes.award_daily_bytes', AsyncMock()):
        
        # Call the function
        await on_message(event)
        
        # Verify that award_daily_bytes was called
        from bot.plugins.bytes import award_daily_bytes
        award_daily_bytes.assert_called_once()


if __name__ == "__main__":
    pytest.main(["-xvs", __file__])
