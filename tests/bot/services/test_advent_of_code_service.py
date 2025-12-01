"""Tests for the Advent of Code service."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

import pytest

from smarter_dev.bot.services.advent_of_code_service import (
    AdventOfCodeService,
    AOC_START_DAY,
    AOC_END_DAY,
    AOC_MONTH,
    EARLY_POST_SECONDS,
    EST,
)
from smarter_dev.bot.services.models import ServiceHealth


class MockResponse:
    """Mock HTTP response."""

    def __init__(self, status_code: int = 200, json_data: dict[str, Any] | None = None):
        self.status_code = status_code
        self._json_data = json_data or {}

    def json(self) -> dict[str, Any]:
        return self._json_data


@pytest.fixture
def mock_api_client():
    """Create mock API client."""
    client = AsyncMock()
    client.get = AsyncMock()
    client.post = AsyncMock()
    return client


@pytest.fixture
def mock_cache_manager():
    """Create mock cache manager."""
    return AsyncMock()


@pytest.fixture
def mock_bot():
    """Create mock hikari bot."""
    bot = Mock()
    bot.rest = AsyncMock()
    bot.rest.create_forum_post = AsyncMock(return_value=Mock(id=123456789))
    bot.is_alive = True
    return bot


@pytest.fixture
def mock_aoc_agent():
    """Mock the AoC thread agent."""
    with patch('smarter_dev.bot.services.advent_of_code_service.aoc_thread_agent') as mock:
        mock.generate_thread_message = AsyncMock(return_value=("Test AoC message!", 50))
        yield mock


@pytest.fixture
async def service(mock_api_client, mock_cache_manager, mock_bot):
    """Create AdventOfCodeService with mocked dependencies."""
    svc = AdventOfCodeService(mock_api_client, mock_cache_manager, mock_bot)
    # Don't initialize (which starts the scheduler) for unit tests
    yield svc
    # Cleanup
    if svc._scheduler_task:
        await svc.stop_scheduler()


class TestAdventOfCodeServiceHealthCheck:
    """Tests for health check functionality."""

    async def test_health_check_returns_healthy(self, service):
        """Health check returns healthy status."""
        result = await service.health_check()

        assert isinstance(result, ServiceHealth)
        assert result.is_healthy is True
        assert result.service_name == "AdventOfCodeService"
        assert "scheduler_status" in result.details

    async def test_health_check_shows_scheduler_stopped_before_init(self, service):
        """Health check shows scheduler stopped before initialization."""
        result = await service.health_check()

        assert result.details["scheduler_status"] == "stopped"

    @patch('smarter_dev.bot.services.advent_of_code_service.datetime')
    async def test_health_check_detects_aoc_month(self, mock_datetime, service):
        """Health check correctly detects December."""
        mock_now = datetime(2025, 12, 15, 10, 0, 0, tzinfo=EST)
        mock_datetime.now.return_value = mock_now

        result = await service.health_check()

        assert result.details["is_aoc_month"] is True
        assert result.details["current_aoc_day"] == 15

    @patch('smarter_dev.bot.services.advent_of_code_service.datetime')
    async def test_health_check_detects_non_aoc_month(self, mock_datetime, service):
        """Health check correctly detects non-December months."""
        mock_now = datetime(2025, 6, 15, 10, 0, 0, tzinfo=EST)
        mock_datetime.now.return_value = mock_now

        result = await service.health_check()

        assert result.details["is_aoc_month"] is False
        assert result.details["current_aoc_day"] is None


class TestAdventOfCodeServiceScheduler:
    """Tests for scheduler logic."""

    async def test_start_scheduler_creates_task(self, service):
        """start_scheduler creates background task."""
        await service.start_scheduler()

        assert service._running is True
        assert service._scheduler_task is not None

        # Clean up
        await service.stop_scheduler()

    async def test_start_scheduler_is_idempotent(self, service):
        """Calling start_scheduler twice doesn't create duplicate tasks."""
        await service.start_scheduler()
        first_task = service._scheduler_task

        await service.start_scheduler()
        second_task = service._scheduler_task

        assert first_task is second_task

        await service.stop_scheduler()

    async def test_stop_scheduler_cancels_task(self, service):
        """stop_scheduler cancels the background task."""
        await service.start_scheduler()
        assert service._running is True

        await service.stop_scheduler()

        assert service._running is False
        assert service._scheduler_task is None


class TestWaitUntilNextCheck:
    """Tests for _wait_until_next_check timing logic."""

    @patch('smarter_dev.bot.services.advent_of_code_service.datetime')
    async def test_wait_not_december_waits_one_hour(self, mock_datetime, service):
        """Outside December, waits 1 hour."""
        mock_now = datetime(2025, 6, 15, 10, 0, 0, tzinfo=EST)
        mock_datetime.now.return_value = mock_now

        with patch.object(asyncio, 'sleep', new_callable=AsyncMock) as mock_sleep:
            await service._wait_until_next_check()
            mock_sleep.assert_called_once_with(3600)

    @patch('smarter_dev.bot.services.advent_of_code_service.datetime')
    async def test_wait_past_day_12_waits_one_hour(self, mock_datetime, service):
        """After December 12 (final day), waits 1 hour."""
        mock_now = datetime(2025, 12, 13, 10, 0, 0, tzinfo=EST)
        mock_datetime.now.return_value = mock_now

        with patch.object(asyncio, 'sleep', new_callable=AsyncMock) as mock_sleep:
            await service._wait_until_next_check()
            mock_sleep.assert_called_once_with(3600)

    @patch('smarter_dev.bot.services.advent_of_code_service.datetime')
    async def test_wait_during_aoc_calculates_next_midnight(self, mock_datetime, service):
        """During AoC, calculates time to next midnight minus early offset."""
        # 10 PM on Dec 5 - should wait about 2 hours minus 2 seconds
        mock_now = datetime(2025, 12, 5, 22, 0, 0, tzinfo=EST)
        mock_datetime.now.return_value = mock_now

        with patch.object(asyncio, 'sleep', new_callable=AsyncMock) as mock_sleep:
            await service._wait_until_next_check()
            # Should be capped at 1 hour
            mock_sleep.assert_called_once()
            sleep_time = mock_sleep.call_args[0][0]
            assert sleep_time <= 3600


class TestCheckAndCreateThreads:
    """Tests for _check_and_create_threads logic."""

    @patch('smarter_dev.bot.services.advent_of_code_service.datetime')
    async def test_skips_if_not_december(self, mock_datetime, service, mock_api_client):
        """Skips processing if not December."""
        mock_now = datetime(2025, 6, 15, 0, 0, 0, tzinfo=EST)
        mock_datetime.now.return_value = mock_now

        await service._check_and_create_threads()

        # API should not be called
        mock_api_client.get.assert_not_called()

    @patch('smarter_dev.bot.services.advent_of_code_service.datetime')
    async def test_skips_if_before_day_1(self, mock_datetime, service, mock_api_client):
        """Skips processing before December 1."""
        # November 30
        mock_now = datetime(2025, 11, 30, 23, 59, 0, tzinfo=EST)
        mock_datetime.now.return_value = mock_now

        await service._check_and_create_threads()

        mock_api_client.get.assert_not_called()

    @patch('smarter_dev.bot.services.advent_of_code_service.datetime')
    async def test_fetches_active_configs(self, mock_datetime, service, mock_api_client):
        """Fetches active configs during December."""
        mock_now = datetime(2025, 12, 5, 0, 0, 0, tzinfo=EST)
        mock_datetime.now.return_value = mock_now
        mock_api_client.get.return_value = MockResponse(200, {"configs": []})

        await service._check_and_create_threads()

        mock_api_client.get.assert_called_with("/advent-of-code/active-configs")

    @patch('smarter_dev.bot.services.advent_of_code_service.datetime')
    async def test_early_posting_uses_next_day(self, mock_datetime, service, mock_api_client, mock_bot, mock_aoc_agent):
        """At 23:59:58, treats time as next day for thread creation.

        This is the critical test: when the scheduler wakes at 23:59:58 (2 seconds
        before midnight), it should create threads for the NEXT day, not the current day.
        """
        # 23:59:58 on Dec 5 - should be treated as Dec 6
        mock_now = datetime(2025, 12, 5, 23, 59, 58, tzinfo=EST)
        mock_datetime.now.return_value = mock_now

        # Return active config
        mock_api_client.get.side_effect = [
            MockResponse(200, {"configs": [{"guild_id": "123", "forum_channel_id": "456"}]}),
            # All days 1-5 exist, day 6 doesn't
            MockResponse(200, {"thread": {"id": "t1"}}),  # Day 1
            MockResponse(200, {"thread": {"id": "t2"}}),  # Day 2
            MockResponse(200, {"thread": {"id": "t3"}}),  # Day 3
            MockResponse(200, {"thread": {"id": "t4"}}),  # Day 4
            MockResponse(200, {"thread": {"id": "t5"}}),  # Day 5
            MockResponse(404, {}),  # Day 6 doesn't exist - should be created!
        ]
        mock_api_client.post.return_value = MockResponse(200, {"success": True})

        await service._check_and_create_threads()

        # Should have created thread for day 6
        mock_bot.rest.create_forum_post.assert_called_once()
        call_kwargs = mock_bot.rest.create_forum_post.call_args[1]
        assert "Day 6" in call_kwargs["name"]

    @patch('smarter_dev.bot.services.advent_of_code_service.datetime')
    async def test_november_30_2359_58_becomes_december_1(self, mock_datetime, service, mock_api_client, mock_bot, mock_aoc_agent):
        """At 23:59:58 on Nov 30, should create thread for Dec 1.

        This tests the month boundary: at 23:59:58 on November 30,
        effective_time becomes December 1, so Day 1 should be created.
        """
        # 23:59:58 on Nov 30 - should be treated as Dec 1
        mock_now = datetime(2025, 11, 30, 23, 59, 58, tzinfo=EST)
        mock_datetime.now.return_value = mock_now

        # Return active config, Day 1 doesn't exist
        mock_api_client.get.side_effect = [
            MockResponse(200, {"configs": [{"guild_id": "123", "forum_channel_id": "456"}]}),
            MockResponse(404, {}),  # Day 1 doesn't exist
        ]
        mock_api_client.post.return_value = MockResponse(200, {"success": True})

        await service._check_and_create_threads()

        # Should have created thread for day 1
        mock_bot.rest.create_forum_post.assert_called_once()
        call_kwargs = mock_bot.rest.create_forum_post.call_args[1]
        assert "Day 1" in call_kwargs["name"]


class TestProcessConfig:
    """Tests for _process_config logic."""

    async def test_skips_if_no_forum_channel(self, service, mock_api_client):
        """Skips processing if no forum channel configured."""
        config = {
            "guild_id": "123",
            "forum_channel_id": None,
        }

        await service._process_config(config, 2025, 5)

        mock_api_client.get.assert_not_called()

    async def test_catches_up_missing_days(self, service, mock_api_client, mock_bot, mock_aoc_agent):
        """Creates threads for all missing days up to max_day."""
        config = {
            "guild_id": "123",
            "forum_channel_id": "456",
        }

        # Day 1, 3 exist; Day 2, 4, 5 missing
        async def mock_get_thread(url):
            if "/threads/2025/1" in url or "/threads/2025/3" in url:
                return MockResponse(200, {"thread": {"id": "existing"}})
            return MockResponse(404, {})

        mock_api_client.get.side_effect = mock_get_thread
        mock_api_client.post.return_value = MockResponse(200, {"success": True})

        await service._process_config(config, 2025, 5)

        # Should have created threads for days 2, 4, 5
        assert mock_bot.rest.create_forum_post.call_count == 3


class TestCreateAoCThread:
    """Tests for _create_aoc_thread logic."""

    async def test_creates_regular_day_thread(self, service, mock_api_client, mock_bot, mock_aoc_agent):
        """Creates thread with correct format for regular days."""
        mock_api_client.post.return_value = MockResponse(200, {"success": True})

        await service._create_aoc_thread("123", "456", 2025, 5)

        mock_bot.rest.create_forum_post.assert_called_once()
        call_kwargs = mock_bot.rest.create_forum_post.call_args[1]
        assert call_kwargs["name"] == "Day 5 - Advent of Code"
        assert "Test AoC message!" in call_kwargs["content"]
        assert "adventofcode.com/2025/day/5" in call_kwargs["content"]

    async def test_creates_final_day_special_thread(self, service, mock_api_client, mock_bot, mock_aoc_agent):
        """Creates thread with special format for the final day (Day 12)."""
        mock_api_client.post.return_value = MockResponse(200, {"success": True})

        await service._create_aoc_thread("123", "456", 2025, 12)

        call_kwargs = mock_bot.rest.create_forum_post.call_args[1]
        assert "Day 12" in call_kwargs["name"]
        # Final day uses H1 heading
        assert "# Advent of Code 2025 - Day 12 (Final Day!)" in call_kwargs["content"]
        assert "final challenge" in call_kwargs["content"]
        assert "Merry Christmas" in call_kwargs["content"]

    async def test_uses_fallback_on_llm_error(self, service, mock_api_client, mock_bot):
        """Uses fallback message when LLM fails."""
        mock_api_client.post.return_value = MockResponse(200, {"success": True})

        with patch('smarter_dev.bot.services.advent_of_code_service.aoc_thread_agent') as mock_agent:
            mock_agent.generate_thread_message = AsyncMock(side_effect=Exception("LLM error"))

            await service._create_aoc_thread("123", "456", 2025, 10)

        # Thread should still be created
        mock_bot.rest.create_forum_post.assert_called_once()

    async def test_records_thread_in_database(self, service, mock_api_client, mock_bot, mock_aoc_agent):
        """Records created thread in database via API."""
        mock_api_client.post.return_value = MockResponse(200, {"success": True})

        await service._create_aoc_thread("123", "456", 2025, 5)

        mock_api_client.post.assert_called_once()
        post_call = mock_api_client.post.call_args
        assert "/advent-of-code/123/threads" in post_call[0][0]
        json_data = post_call[1]["json_data"]
        assert json_data["year"] == 2025
        assert json_data["day"] == 5


class TestErrorHandling:
    """Tests for error handling in thread creation."""

    async def test_handles_permission_error(self, service, mock_api_client, mock_bot, mock_aoc_agent):
        """Handles ForbiddenError when creating thread."""
        from smarter_dev.bot.services.exceptions import ServiceError

        mock_bot.rest.create_forum_post.side_effect = Exception("Forbidden: Missing permissions")

        with pytest.raises(ServiceError) as exc_info:
            await service._create_aoc_thread("123", "456", 2025, 5)

        assert "failed to create" in str(exc_info.value).lower()

    async def test_handles_not_found_error(self, service, mock_api_client, mock_bot, mock_aoc_agent):
        """Handles NotFoundError when channel doesn't exist."""
        from smarter_dev.bot.services.exceptions import ServiceError

        mock_bot.rest.create_forum_post.side_effect = Exception("Not Found: Channel does not exist")

        with pytest.raises(ServiceError) as exc_info:
            await service._create_aoc_thread("123", "456", 2025, 5)

        assert "failed to create" in str(exc_info.value).lower()

    async def test_logs_db_recording_failure_without_raising(self, service, mock_api_client, mock_bot, mock_aoc_agent):
        """Logs error when DB recording fails but doesn't raise."""
        mock_api_client.post.side_effect = Exception("DB connection error")

        # Should not raise - thread was created successfully
        await service._create_aoc_thread("123", "456", 2025, 5)

        # Thread was created
        mock_bot.rest.create_forum_post.assert_called_once()


class TestCatchUpLogic:
    """Tests for catch-up functionality."""

    async def test_creates_all_missing_days_on_startup(self, service, mock_api_client, mock_bot, mock_aoc_agent):
        """When bot starts mid-month, creates all missing threads."""
        config = {
            "guild_id": "123",
            "forum_channel_id": "456",
        }

        # No existing threads
        mock_api_client.get.return_value = MockResponse(404, {})
        mock_api_client.post.return_value = MockResponse(200, {"success": True})

        # Bot starts on day 5
        await service._process_config(config, 2025, 5)

        # Should create threads for days 1-5
        assert mock_bot.rest.create_forum_post.call_count == 5

    async def test_only_creates_missing_days(self, service, mock_api_client, mock_bot, mock_aoc_agent):
        """Only creates threads for days that don't exist."""
        config = {
            "guild_id": "123",
            "forum_channel_id": "456",
        }

        # Days 1, 2, 3 exist; 4, 5 don't
        async def mock_get(url):
            for day in [1, 2, 3]:
                if f"/threads/2025/{day}" in url:
                    return MockResponse(200, {"thread": {"id": f"thread_{day}"}})
            return MockResponse(404, {})

        mock_api_client.get.side_effect = mock_get
        mock_api_client.post.return_value = MockResponse(200, {"success": True})

        await service._process_config(config, 2025, 5)

        # Only days 4 and 5 should be created
        assert mock_bot.rest.create_forum_post.call_count == 2


class TestThreadContentFormat:
    """Tests for thread content formatting."""

    async def test_regular_day_format(self, service, mock_api_client, mock_bot, mock_aoc_agent):
        """Regular days use bold header format."""
        mock_api_client.post.return_value = MockResponse(200, {"success": True})

        await service._create_aoc_thread("123", "456", 2025, 10)

        content = mock_bot.rest.create_forum_post.call_args[1]["content"]
        assert content.startswith("**Advent of Code 2025 - Day 10**")
        assert "Today's challenge:" in content
        assert "spoiler tags" in content

    async def test_final_day_format(self, service, mock_api_client, mock_bot, mock_aoc_agent):
        """Final day (Day 12) uses H1 header and celebration language."""
        mock_api_client.post.return_value = MockResponse(200, {"success": True})

        await service._create_aoc_thread("123", "456", 2025, 12)

        content = mock_bot.rest.create_forum_post.call_args[1]["content"]
        assert content.startswith("# Advent of Code 2025 - Day 12 (Final Day!)")
        assert "final challenge" in content
        assert "celebrate" in content
        assert "Merry Christmas" in content


class TestConstants:
    """Tests for module constants."""

    def test_aoc_constants(self):
        """Verify AoC constants are correct."""
        assert AOC_START_DAY == 1
        assert AOC_END_DAY == 12  # 2024 AoC is 12 days
        assert AOC_MONTH == 12
        assert EARLY_POST_SECONDS == 2

    def test_timezone_is_eastern(self):
        """Verify timezone is America/New_York."""
        assert EST == ZoneInfo("America/New_York")
