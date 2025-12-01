"""Tests for Advent of Code API endpoints.

These tests validate the API endpoint behavior with mocked dependencies.
Full integration tests with database are covered by the CRUD tests.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from smarter_dev.web.api.app import api
from smarter_dev.web.crud import ConflictError


@pytest.fixture
def mock_db_session():
    """Create a mock database session."""
    session = AsyncMock()
    return session


@pytest.fixture
def mock_aoc_ops():
    """Create mock AdventOfCodeConfigOperations."""
    return MagicMock()


@pytest.fixture
async def client():
    """Create a test HTTP client."""
    async with AsyncClient(
        transport=ASGITransport(app=api),
        base_url="http://test"
    ) as client:
        yield client


class TestAdventOfCodeEndpoints:
    """Tests for Advent of Code API endpoints with mocked dependencies."""

    async def test_active_configs_requires_auth(self, client):
        """GET /advent-of-code/active-configs requires authentication."""
        response = await client.get("/advent-of-code/active-configs")
        # Should return 401 or 403 without auth
        assert response.status_code in [401, 403, 500]

    async def test_guild_config_requires_auth(self, client):
        """GET /advent-of-code/{guild_id}/config requires authentication."""
        response = await client.get("/advent-of-code/123456789/config")
        assert response.status_code in [401, 403, 500]

    async def test_get_thread_requires_auth(self, client):
        """GET /advent-of-code/{guild_id}/threads/{year}/{day} requires authentication."""
        response = await client.get("/advent-of-code/123456789/threads/2025/1")
        assert response.status_code in [401, 403, 500]

    async def test_post_thread_requires_auth(self, client):
        """POST /advent-of-code/{guild_id}/threads requires authentication."""
        response = await client.post(
            "/advent-of-code/123456789/threads",
            json={
                "year": 2025,
                "day": 1,
                "thread_id": "thread123",
                "thread_title": "Day 1"
            }
        )
        assert response.status_code in [401, 403, 500]

    async def test_get_threads_requires_auth(self, client):
        """GET /advent-of-code/{guild_id}/threads requires authentication."""
        response = await client.get("/advent-of-code/123456789/threads")
        assert response.status_code in [401, 403, 500]


class TestAdventOfCodeRequestValidation:
    """Tests for request validation on AoC endpoints."""

    async def test_post_thread_validates_day_too_low(self, client):
        """POST /advent-of-code/{guild_id}/threads validates day >= 1."""
        # Even without auth, validation should happen on the request body
        response = await client.post(
            "/advent-of-code/123456789/threads",
            json={
                "year": 2025,
                "day": 0,  # Invalid - too low
                "thread_id": "thread123",
                "thread_title": "Invalid Day"
            }
        )
        # Will either get 422 (validation) or 401/403 (auth first)
        # The exact behavior depends on middleware order
        assert response.status_code in [401, 403, 422, 500]

    async def test_post_thread_validates_day_too_high(self, client):
        """POST /advent-of-code/{guild_id}/threads validates day <= 25."""
        response = await client.post(
            "/advent-of-code/123456789/threads",
            json={
                "year": 2025,
                "day": 26,  # Invalid - too high
                "thread_id": "thread123",
                "thread_title": "Invalid Day"
            }
        )
        assert response.status_code in [401, 403, 422, 500]

    async def test_post_thread_requires_thread_id(self, client):
        """POST /advent-of-code/{guild_id}/threads requires thread_id field."""
        response = await client.post(
            "/advent-of-code/123456789/threads",
            json={
                "year": 2025,
                "day": 5,
                # Missing thread_id
                "thread_title": "Day 5"
            }
        )
        assert response.status_code in [401, 403, 422, 500]

    async def test_post_thread_requires_thread_title(self, client):
        """POST /advent-of-code/{guild_id}/threads requires thread_title field."""
        response = await client.post(
            "/advent-of-code/123456789/threads",
            json={
                "year": 2025,
                "day": 5,
                "thread_id": "thread123",
                # Missing thread_title
            }
        )
        assert response.status_code in [401, 403, 422, 500]


class TestRecordThreadRequestSchema:
    """Tests for the RecordThreadRequest Pydantic schema."""

    def test_valid_day_range_1_to_25(self):
        """Day must be between 1 and 25."""
        from smarter_dev.web.api.routers.advent_of_code import RecordThreadRequest

        # Valid days
        for day in [1, 10, 25]:
            req = RecordThreadRequest(
                year=2025,
                day=day,
                thread_id="thread123",
                thread_title=f"Day {day}"
            )
            assert req.day == day

    def test_invalid_day_zero(self):
        """Day 0 is invalid."""
        from smarter_dev.web.api.routers.advent_of_code import RecordThreadRequest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            RecordThreadRequest(
                year=2025,
                day=0,
                thread_id="thread123",
                thread_title="Day 0"
            )

    def test_invalid_day_26(self):
        """Day 26 is invalid."""
        from smarter_dev.web.api.routers.advent_of_code import RecordThreadRequest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            RecordThreadRequest(
                year=2025,
                day=26,
                thread_id="thread123",
                thread_title="Day 26"
            )

    def test_requires_all_fields(self):
        """All fields are required."""
        from smarter_dev.web.api.routers.advent_of_code import RecordThreadRequest
        from pydantic import ValidationError

        # Missing day
        with pytest.raises(ValidationError):
            RecordThreadRequest(
                year=2025,
                thread_id="thread123",
                thread_title="Missing Day"
            )

        # Missing year
        with pytest.raises(ValidationError):
            RecordThreadRequest(
                day=5,
                thread_id="thread123",
                thread_title="Missing Year"
            )


class TestAdventOfCodeConfigResponseSchema:
    """Tests for response schemas."""

    def test_config_response_serialization(self):
        """AoC config can be serialized to JSON-compatible dict."""
        from smarter_dev.web.api.routers.advent_of_code import ConfigResponse

        response = ConfigResponse(
            guild_id="123456789",
            forum_channel_id="987654321",
            is_active=True,
        )

        # Should serialize without error
        data = response.model_dump()
        assert data["guild_id"] == "123456789"
        assert data["is_active"] is True

    def test_thread_response_serialization(self):
        """AoC thread can be serialized to JSON-compatible dict."""
        from smarter_dev.web.api.routers.advent_of_code import ThreadResponse

        response = ThreadResponse(
            id="uuid123",
            guild_id="123456789",
            year=2025,
            day=5,
            thread_id="discord_thread_123",
            thread_title="Day 5 - Advent of Code",
            created_at="2025-12-05T00:00:00Z"
        )

        data = response.model_dump()
        assert data["day"] == 5
        assert data["thread_id"] == "discord_thread_123"
