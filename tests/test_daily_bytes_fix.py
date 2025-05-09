"""
Test for the fixed daily bytes functionality.
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
        
        # Mock recent bytes transactions
        if path.startswith("/api/bytes/recent"):
            if self.bytes_transactions:
                return {"transactions": self.bytes_transactions}
            return {"transactions": []}
        
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
            self.bytes_transactions.append({
                "id": len(self.bytes_transactions) + 1,
                "giver_id": 0,
                "receiver_id": TEST_USER_ID,
                "guild_id": TEST_GUILD_ID,
                "amount": 10,
                "reason": "Daily bytes for 1 day streak",
                "awarded_at": datetime.now(UTC).isoformat()
            })
            return {"id": len(self.bytes_transactions)}
        
        # Mock system user
        if path == "/api/users?discord_id=0":
            return {"users": [{"id": 0, "discord_id": 0, "username": "System"}]}
        
        # Mock bytes config
        if path.startswith("/api/bytes/config"):
            return {
                "id": 1,
                "guild_id": TEST_GUILD_ID,
                "daily_earning": 10,
                "starting_balance": 100,
                "max_give_amount": 50,
                "cooldown_minutes": 1440
            }
        
        return {}
    
    async def _get_json(self, response):
        """Mock _get_json method."""
        return response
    
    def _dict_from_model(self, model):
        """Mock _dict_from_model method."""
        return {"id": 1}


@pytest.mark.asyncio
async def test_daily_bytes_with_recent_transaction():
    """Test that daily bytes are not awarded if there's a recent transaction."""
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
    
    # Set up the initial state - user has a recent bytes transaction
    api_client.bytes_transactions = [{
        "id": 1,
        "giver_id": 0,
        "receiver_id": TEST_USER_ID,
        "guild_id": TEST_GUILD_ID,
        "amount": 10,
        "reason": "Daily bytes for 1 day streak",
        "awarded_at": datetime.now(UTC).isoformat()
    }]
    
    # Mock the necessary functions
    with patch('bot.plugins.bytes.update_user_streak', return_value={
        "user": {"id": 1},
        "streak_count": 1,
        "is_new_day": True
    }), patch('bot.plugins.bytes.award_daily_bytes', AsyncMock()):
        
        # Call the function
        await on_message(event)
        
        # Verify that award_daily_bytes was NOT called
        from bot.plugins.bytes import award_daily_bytes
        award_daily_bytes.assert_not_called()


@pytest.mark.asyncio
async def test_daily_bytes_with_last_daily_bytes():
    """Test that daily bytes are not awarded if last_daily_bytes is recent."""
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
    
    # Set up the initial state - user has a recent last_daily_bytes
    api_client.last_daily_bytes = datetime.now(UTC)
    api_client.last_active_day = datetime.now(UTC).strftime("%Y-%m-%d")
    
    # Mock the necessary functions
    with patch('bot.plugins.bytes.update_user_streak', return_value={
        "user": {"id": 1},
        "streak_count": 1,
        "is_new_day": True
    }), patch('bot.plugins.bytes.award_daily_bytes', AsyncMock()):
        
        # Call the function
        await on_message(event)
        
        # Verify that award_daily_bytes was NOT called
        from bot.plugins.bytes import award_daily_bytes
        award_daily_bytes.assert_not_called()


@pytest.mark.asyncio
async def test_daily_bytes_eligible():
    """Test that daily bytes are awarded when eligible."""
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
    
    # Set up the initial state - user is eligible for daily bytes
    api_client.bytes_transactions = []
    api_client.last_daily_bytes = None
    api_client.last_active_day = None
    
    # Mock the necessary functions
    with patch('bot.plugins.bytes.update_user_streak', return_value={
        "user": {"id": 1},
        "streak_count": 1,
        "is_new_day": True
    }), patch('bot.plugins.bytes.award_daily_bytes', AsyncMock()):
        
        # Call the function
        await on_message(event)
        
        # Verify that award_daily_bytes was called
        from bot.plugins.bytes import award_daily_bytes
        award_daily_bytes.assert_called_once()


if __name__ == "__main__":
    pytest.main(["-xvs", __file__])
