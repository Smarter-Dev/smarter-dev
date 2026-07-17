"""Parity tests for the native (Litestar) Advent of Code API (unit U6).

Assert the wire contract of the ported ``routers/advent_of_code.py`` directly:
exact status codes and JSON bodies for happy paths, the plain ``{"detail": ...}``
error shapes, the 404/409 branches, the 422 on body validation, and the
per-endpoint 500 details on ``DatabaseOperationError``. Paths carry the final
``/api`` prefix because the native controller declares its mounted path (the
FastAPI app was itself mounted at ``/api``). These mirror the exact paths the bot
sends from ``smarter_dev/bot/services/advent_of_code_service.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import Mock
from uuid import uuid4

from litestar.testing import TestClient

from smarter_dev.web.crud import ConflictError, DatabaseOperationError

_GUILD = "123456789012345678"


def _config_mock(**overrides) -> Mock:
    """Build an AdventOfCodeConfig-like mock."""
    config = Mock()
    config.guild_id = overrides.get("guild_id", _GUILD)
    config.forum_channel_id = overrides.get("forum_channel_id", "555555555555555555")
    config.is_active = overrides.get("is_active", True)
    return config


def _thread_mock(**overrides) -> Mock:
    """Build an AdventOfCodeThread-like mock for response serialization."""
    thread = Mock()
    thread.id = overrides.get("id", uuid4())
    thread.guild_id = overrides.get("guild_id", _GUILD)
    thread.year = overrides.get("year", 2025)
    thread.day = overrides.get("day", 1)
    thread.thread_id = overrides.get("thread_id", "thread123")
    thread.thread_title = overrides.get("thread_title", "Day 1")
    thread.created_at = overrides.get(
        "created_at", datetime(2025, 12, 1, tzinfo=timezone.utc)
    )
    return thread


class TestActiveConfigs:
    def test_returns_configs(self, aoc_client: TestClient, aoc_ops_mock):
        aoc_ops_mock.get_active_configs.return_value = [
            _config_mock(),
            _config_mock(guild_id="999", forum_channel_id="888", is_active=True),
        ]

        response = aoc_client.get("/api/advent-of-code/active-configs")

        assert response.status_code == 200
        configs = response.json()["configs"]
        assert len(configs) == 2
        assert configs[0] == {
            "guild_id": _GUILD,
            "forum_channel_id": "555555555555555555",
            "is_active": True,
        }

    def test_returns_empty_list(self, aoc_client: TestClient, aoc_ops_mock):
        aoc_ops_mock.get_active_configs.return_value = []

        response = aoc_client.get("/api/advent-of-code/active-configs")

        assert response.status_code == 200
        assert response.json() == {"configs": []}

    def test_db_error_is_500(self, aoc_client: TestClient, aoc_ops_mock):
        aoc_ops_mock.get_active_configs.side_effect = DatabaseOperationError("boom")

        response = aoc_client.get("/api/advent-of-code/active-configs")

        assert response.status_code == 500
        assert response.json() == {"detail": "Failed to retrieve active configurations"}


class TestGuildConfig:
    def test_returns_config(self, aoc_client: TestClient, aoc_ops_mock):
        aoc_ops_mock.get_or_create_config.return_value = _config_mock()

        response = aoc_client.get(f"/api/advent-of-code/{_GUILD}/config")

        assert response.status_code == 200
        assert response.json() == {
            "guild_id": _GUILD,
            "forum_channel_id": "555555555555555555",
            "is_active": True,
        }

    def test_returns_config_with_null_channel(
        self, aoc_client: TestClient, aoc_ops_mock
    ):
        aoc_ops_mock.get_or_create_config.return_value = _config_mock(
            forum_channel_id=None, is_active=False
        )

        response = aoc_client.get(f"/api/advent-of-code/{_GUILD}/config")

        assert response.status_code == 200
        body = response.json()
        assert body["forum_channel_id"] is None
        assert body["is_active"] is False

    def test_db_error_is_500(self, aoc_client: TestClient, aoc_ops_mock):
        aoc_ops_mock.get_or_create_config.side_effect = DatabaseOperationError("boom")

        response = aoc_client.get(f"/api/advent-of-code/{_GUILD}/config")

        assert response.status_code == 500
        assert response.json() == {"detail": "Failed to retrieve configuration"}


class TestGetPostedThread:
    def test_returns_thread(self, aoc_client: TestClient, aoc_ops_mock):
        thread = _thread_mock()
        aoc_ops_mock.get_posted_thread.return_value = thread

        response = aoc_client.get(f"/api/advent-of-code/{_GUILD}/threads/2025/1")

        assert response.status_code == 200
        body = response.json()["thread"]
        assert body["id"] == str(thread.id)
        assert body["guild_id"] == _GUILD
        assert body["year"] == 2025
        assert body["day"] == 1
        assert body["thread_id"] == "thread123"
        assert body["created_at"] == "2025-12-01T00:00:00+00:00"

    def test_missing_thread_404(self, aoc_client: TestClient, aoc_ops_mock):
        aoc_ops_mock.get_posted_thread.return_value = None

        response = aoc_client.get(f"/api/advent-of-code/{_GUILD}/threads/2025/1")

        assert response.status_code == 404
        assert response.json() == {"detail": "Thread not found"}

    def test_db_error_is_500(self, aoc_client: TestClient, aoc_ops_mock):
        aoc_ops_mock.get_posted_thread.side_effect = DatabaseOperationError("boom")

        response = aoc_client.get(f"/api/advent-of-code/{_GUILD}/threads/2025/1")

        assert response.status_code == 500
        assert response.json() == {"detail": "Failed to check posted thread"}


class TestRecordPostedThread:
    def _body(self, day: int = 1) -> dict:
        return {
            "year": 2025,
            "day": day,
            "thread_id": "thread123",
            "thread_title": "Day 1",
        }

    def test_records_thread(
        self, aoc_client: TestClient, aoc_ops_mock, session_mock
    ):
        thread = _thread_mock()
        aoc_ops_mock.record_posted_thread.return_value = thread

        response = aoc_client.post(
            f"/api/advent-of-code/{_GUILD}/threads", json=self._body()
        )

        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["thread"]["id"] == str(thread.id)
        assert body["thread"]["thread_title"] == "Day 1"
        session_mock.commit.assert_awaited_once()

    def test_conflict_409(self, aoc_client: TestClient, aoc_ops_mock):
        aoc_ops_mock.record_posted_thread.side_effect = ConflictError("dup")

        response = aoc_client.post(
            f"/api/advent-of-code/{_GUILD}/threads", json=self._body()
        )

        assert response.status_code == 409
        assert response.json() == {"detail": "Thread already recorded for this day"}

    def test_day_too_low_422(self, aoc_client: TestClient):
        response = aoc_client.post(
            f"/api/advent-of-code/{_GUILD}/threads", json=self._body(day=0)
        )

        assert response.status_code == 422

    def test_day_too_high_422(self, aoc_client: TestClient):
        response = aoc_client.post(
            f"/api/advent-of-code/{_GUILD}/threads", json=self._body(day=26)
        )

        assert response.status_code == 422

    def test_missing_field_422(self, aoc_client: TestClient):
        response = aoc_client.post(
            f"/api/advent-of-code/{_GUILD}/threads",
            json={"year": 2025, "day": 5, "thread_title": "Day 5"},
        )

        assert response.status_code == 422

    def test_db_error_is_500(self, aoc_client: TestClient, aoc_ops_mock):
        aoc_ops_mock.record_posted_thread.side_effect = DatabaseOperationError("boom")

        response = aoc_client.post(
            f"/api/advent-of-code/{_GUILD}/threads", json=self._body()
        )

        assert response.status_code == 500
        assert response.json() == {"detail": "Failed to record posted thread"}


class TestGetGuildThreads:
    def test_returns_threads(self, aoc_client: TestClient, aoc_ops_mock):
        aoc_ops_mock.get_guild_threads.return_value = [
            _thread_mock(),
            _thread_mock(day=2, thread_id="thread456", thread_title="Day 2"),
        ]

        response = aoc_client.get(f"/api/advent-of-code/{_GUILD}/threads")

        assert response.status_code == 200
        threads = response.json()["threads"]
        assert len(threads) == 2
        assert threads[1]["day"] == 2
        assert threads[1]["thread_id"] == "thread456"

    def test_returns_empty_list(self, aoc_client: TestClient, aoc_ops_mock):
        aoc_ops_mock.get_guild_threads.return_value = []

        response = aoc_client.get(f"/api/advent-of-code/{_GUILD}/threads")

        assert response.status_code == 200
        assert response.json() == {"threads": []}

    def test_year_filter_passed_through(self, aoc_client: TestClient, aoc_ops_mock):
        aoc_ops_mock.get_guild_threads.return_value = []

        response = aoc_client.get(
            f"/api/advent-of-code/{_GUILD}/threads?year=2024"
        )

        assert response.status_code == 200
        _, call_args, _ = aoc_ops_mock.get_guild_threads.mock_calls[0]
        assert call_args[-1] == 2024

    def test_db_error_is_500(self, aoc_client: TestClient, aoc_ops_mock):
        aoc_ops_mock.get_guild_threads.side_effect = DatabaseOperationError("boom")

        response = aoc_client.get(f"/api/advent-of-code/{_GUILD}/threads")

        assert response.status_code == 500
        assert response.json() == {"detail": "Failed to retrieve guild threads"}
