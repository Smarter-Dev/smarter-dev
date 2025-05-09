"""
Test the daily bytes eligibility check function.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timedelta, UTC

# Constants for testing
TEST_USER_ID = 123456789
TEST_GUILD_ID = 987654321

@pytest.fixture(autouse=True)
def clear_caches():
    """Clear all caches before each test to avoid state sharing between tests."""
    from bot.plugins.bytes import daily_bytes_eligibility_cache, guild_member_cache, user_cache, bytes_balance_cache, bytes_config_cache

    # Clear all caches
    daily_bytes_eligibility_cache.clear()
    guild_member_cache.clear()
    user_cache.clear()
    bytes_balance_cache.clear()
    bytes_config_cache.clear()

    yield  # This allows the test to run

    # Clear again after the test
    daily_bytes_eligibility_cache.clear()
    guild_member_cache.clear()
    user_cache.clear()
    bytes_balance_cache.clear()
    bytes_config_cache.clear()

@pytest.mark.asyncio
async def test_check_daily_bytes_eligibility_eligible():
    """Test that a user is eligible for daily bytes when they haven't received any yet."""
    from bot.plugins.bytes import check_daily_bytes_eligibility

    # Create mock API client
    client = AsyncMock()

    # Mock get_cached_guild_member to return None (no guild member record)
    with patch('bot.plugins.bytes.get_cached_guild_member', return_value=None):
        # Call the function
        is_eligible, next_eligible_time = await check_daily_bytes_eligibility(client, TEST_USER_ID, TEST_GUILD_ID)

        # Verify that the user is eligible
        assert is_eligible is True
        assert next_eligible_time is None

@pytest.mark.asyncio
async def test_check_daily_bytes_eligibility_not_eligible():
    """Test that a user is not eligible for daily bytes when they've received them recently."""
    from bot.plugins.bytes import check_daily_bytes_eligibility, daily_bytes_eligibility_cache

    # Create mock API client
    client = AsyncMock()

    # Create a mock guild member with recent last_daily_bytes
    now = datetime.now(UTC)
    mock_guild_member = MagicMock()
    mock_guild_member.last_daily_bytes = now

    # Mock get_cached_guild_member to return our mock guild member
    with patch('bot.plugins.bytes.get_cached_guild_member', return_value=mock_guild_member):
        # Call the function
        is_eligible, next_eligible_time = await check_daily_bytes_eligibility(client, TEST_USER_ID, TEST_GUILD_ID)

        # Verify that the user is not eligible
        assert is_eligible is False
        assert next_eligible_time is not None

        # Verify that the next eligible time is about 24 hours from now
        time_diff = (next_eligible_time - now).total_seconds()
        assert 23.9 * 3600 < time_diff < 24.1 * 3600  # Allow small margin for test execution time

        # Verify that the cache was updated
        cache_key = (TEST_USER_ID, TEST_GUILD_ID)
        assert cache_key in daily_bytes_eligibility_cache
        next_eligible_ts, _ = daily_bytes_eligibility_cache[cache_key]
        assert abs(next_eligible_ts - next_eligible_time.timestamp()) < 1  # Allow small margin for test execution time

@pytest.mark.asyncio
async def test_check_daily_bytes_eligibility_from_cache():
    """Test that the function uses the cache when available."""
    from bot.plugins.bytes import check_daily_bytes_eligibility, daily_bytes_eligibility_cache

    # Create mock API client
    client = AsyncMock()

    # Set up the cache with a future eligibility time
    now = datetime.now(UTC)
    next_eligible_time = now + timedelta(hours=12)  # Eligible in 12 hours
    cache_key = (TEST_USER_ID, TEST_GUILD_ID)
    daily_bytes_eligibility_cache[cache_key] = (next_eligible_time.timestamp(), now.timestamp())

    # Call the function (should use cache and not call get_cached_guild_member)
    with patch('bot.plugins.bytes.get_cached_guild_member') as mock_get_guild_member:
        is_eligible, returned_next_eligible_time = await check_daily_bytes_eligibility(client, TEST_USER_ID, TEST_GUILD_ID)

        # Verify that get_cached_guild_member was not called
        mock_get_guild_member.assert_not_called()

        # Verify that the user is not eligible
        assert is_eligible is False
        assert returned_next_eligible_time is not None

        # Verify that the returned next eligible time matches what we put in the cache
        time_diff = (returned_next_eligible_time - next_eligible_time).total_seconds()
        assert abs(time_diff) < 1  # Allow small margin for test execution time

    # Now test with an eligibility time in the past but with a fresh cache timestamp
    past_eligible_time = now - timedelta(hours=1)  # Eligible 1 hour ago
    daily_bytes_eligibility_cache[cache_key] = (past_eligible_time.timestamp(), now.timestamp())

    # Mock guild member for the API call that will happen
    mock_guild_member = MagicMock()
    mock_guild_member.last_daily_bytes = None  # No last_daily_bytes, so eligible

    # Call the function (should NOT proceed to API call since cache is fresh)
    with patch('bot.plugins.bytes.get_cached_guild_member', return_value=mock_guild_member) as mock_get_guild_member:
        is_eligible, returned_next_eligible_time = await check_daily_bytes_eligibility(client, TEST_USER_ID, TEST_GUILD_ID)

        # Verify that get_cached_guild_member was NOT called (using cache)
        mock_get_guild_member.assert_not_called()

        # Verify that the user is eligible (based on cache)
        assert is_eligible is True
        assert returned_next_eligible_time is None

@pytest.mark.asyncio
async def test_check_daily_bytes_eligibility_cache_expired():
    """Test that the function refreshes the cache when it's expired."""
    from bot.plugins.bytes import check_daily_bytes_eligibility, daily_bytes_eligibility_cache, CACHE_TIMEOUT

    # Create mock API client
    client = AsyncMock()

    # Set up the cache with an expired entry for a past eligibility time
    now = datetime.now(UTC)
    past_eligible_time = now - timedelta(hours=1)  # Eligible 1 hour ago
    cache_key = (TEST_USER_ID, TEST_GUILD_ID)
    daily_bytes_eligibility_cache[cache_key] = (
        past_eligible_time.timestamp(),
        now.timestamp() - CACHE_TIMEOUT - 10  # Expired cache entry
    )

    # Create a mock guild member with recent last_daily_bytes
    mock_guild_member = MagicMock()
    mock_guild_member.last_daily_bytes = now

    # Call the function (should refresh the cache)
    with patch('bot.plugins.bytes.get_cached_guild_member', return_value=mock_guild_member):
        is_eligible, returned_next_eligible_time = await check_daily_bytes_eligibility(client, TEST_USER_ID, TEST_GUILD_ID)

        # Verify that the user is not eligible
        assert is_eligible is False
        assert returned_next_eligible_time is not None

        # Verify that the cache was updated with a fresh timestamp
        next_eligible_ts, cache_ts = daily_bytes_eligibility_cache[cache_key]
        assert now.timestamp() - 1 < cache_ts < now.timestamp() + 1  # Fresh cache timestamp

@pytest.mark.asyncio
async def test_check_daily_bytes_eligibility_eligible_after_24_hours():
    """Test that a user is eligible for daily bytes after 24 hours."""
    from bot.plugins.bytes import check_daily_bytes_eligibility

    # Create mock API client
    client = AsyncMock()

    # Create a mock guild member with last_daily_bytes more than 24 hours ago
    now = datetime.now(UTC)
    yesterday = now - timedelta(hours=25)  # 25 hours ago

    # Create a proper dictionary-like object instead of a MagicMock
    class GuildMember:
        def __init__(self, last_daily_bytes):
            self.last_daily_bytes = last_daily_bytes

        def get(self, key, default=None):
            if key == "last_daily_bytes":
                return self.last_daily_bytes
            return default

    # Create a mock guild member with last_daily_bytes from 25 hours ago
    mock_guild_member = GuildMember(yesterday)

    # Calculate time difference for verification
    time_diff_hours = (now - yesterday).total_seconds() / 3600
    assert time_diff_hours >= 24, f"Test setup error: Time difference should be at least 24 hours, got {time_diff_hours} hours"

    # Mock get_cached_guild_member to return our mock guild member
    with patch('bot.plugins.bytes.get_cached_guild_member', return_value=mock_guild_member):
        # Call the function
        is_eligible, next_eligible_time = await check_daily_bytes_eligibility(client, TEST_USER_ID, TEST_GUILD_ID)

        # Verify the results

        # Verify that the user is eligible
        assert is_eligible is True
        assert next_eligible_time is None

if __name__ == "__main__":
    pytest.main(["-xvs", __file__])
