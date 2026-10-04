"""Tests for the ported multi-tier rate limiter on the bytes controller.

Parity contract (docs/v2/legacy-sunset/04-api-rewrite.md "Rate-limiting
parity"): windows 10/s, 180/min, 2500/15 min per key, ``x-ratelimit-*``
headers on success, 429 with the legacy ``{"detail": ...}`` body and
escalated ``retry-after`` on violation. Usage is counted in a Redis sorted
set per key (#81); only violations emit a security event. The bot's
``api_client`` reads the headers to self-throttle, so they are part of the
wire contract.
"""

from __future__ import annotations

import logging
import socket
import time
from collections.abc import Iterator
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch
from uuid import uuid4

import fakeredis.aioredis as fakeredis_aioredis
import pytest
from litestar.di import Provide
from litestar.plugins.pydantic import PydanticPlugin
from litestar.testing import TestClient
from litestar.testing import create_test_client
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.redis_client import create_redis_client
from smarter_dev.web.api_native import bytes as bytes_module
from smarter_dev.web.api_native import rate_limiting
from smarter_dev.web.api_native.bytes import BytesController
from smarter_dev.web.api_native.rate_limiting import RATE_LIMIT_PER_SECOND
from smarter_dev.web.api_native.rate_limiting import RateLimitedKey
from smarter_dev.web.api_native.rate_limiting import check_rate_limits
from smarter_dev.web.api_native.rate_limiting import rate_limit_redis_key
from smarter_dev.web.api_native.rate_limiting import rate_limited_key_from_skrift

GUILD_ID = "123456789012345678"
CONFIG_PATH = f"/api/guilds/{GUILD_ID}/bytes/config"
VALID_SKRIFT_TOKEN = "sk_" + "a" * 43


class FakeSkriftKeyRow:
    """The attribute slice of a Skrift APIKey row the limiter consumes."""

    def __init__(self) -> None:
        self.id = uuid4()
        self.key_prefix = VALID_SKRIFT_TOKEN[:12]
        self.service_name = "discord-bot"
        self.display_name = "discord-bot"
        self.user_id = uuid4()


class _NullSessionContext:
    """Stands in for the key-lookup session; the stubbed verify ignores it."""

    async def __aenter__(self):
        return Mock()

    async def __aexit__(self, *exc_info):
        return False


@pytest.fixture(autouse=True)
def _reset_redis_warning_throttle(monkeypatch):
    monkeypatch.setattr(rate_limiting, "_last_redis_warning_at", None)
    monkeypatch.setattr(rate_limiting, "_redis_failures_since_warning", 0)


@pytest.fixture
def skrift_key_row() -> FakeSkriftKeyRow:
    return FakeSkriftKeyRow()


@pytest.fixture
async def fake_redis():
    client = fakeredis_aioredis.FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def config_ops_mock() -> Iterator[Mock]:
    """Serve GET /config from a canned config so handlers need no real DB."""
    with patch("smarter_dev.web.api_native.bytes.BytesConfigOperations") as factory:
        instance = Mock()
        factory.return_value = instance
        config_row = Mock(
            guild_id=GUILD_ID,
            daily_amount=10,
            starting_balance=100,
            max_transfer=1000,
            daily_cooldown_hours=24,
            transfer_cooldown_hours=0,
            streak_bonuses={},
            role_rewards={},
            transfer_tax_rate=0.0,
            is_enabled=True,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        instance.get_config = AsyncMock(return_value=config_row)
        yield instance


@pytest.fixture
def rate_limited_client(
    skrift_key_row, config_ops_mock, fake_redis
) -> Iterator[TestClient]:
    """Bytes controller behind the real middleware, guards bypassed.

    The middleware's Redis is ``fake_redis`` and the Skrift key verification
    is stubbed: a request bearing
    ``VALID_SKRIFT_TOKEN`` resolves to ``skrift_key_row``, anything else to
    ``None``. Guards are emptied (shared-list pattern from ``conftest.py``)
    because auth behavior is covered by the auth tests — here only the
    limiter is under test.
    """

    async def fake_verify(session, token, client_ip=None):
        if token == VALID_SKRIFT_TOKEN:
            return skrift_key_row
        return None

    fake_service = Mock()
    fake_service.verify_api_key = AsyncMock(side_effect=fake_verify)

    original_guards = list(bytes_module.BOT_API_GUARDS)
    bytes_module.BOT_API_GUARDS.clear()
    session_mock = AsyncMock(spec=AsyncSession)
    try:
        with (
            patch(
                "smarter_dev.web.api_native.rate_limiting.skrift_api_key_service",
                fake_service,
            ),
            patch(
                "smarter_dev.web.api_native.rate_limiting.get_db_session_context",
                return_value=_NullSessionContext(),
            ),
            patch(
                "smarter_dev.web.api_native.rate_limiting.get_redis_client",
                return_value=fake_redis,
            ),
        ):
            with create_test_client(
                route_handlers=[BytesController],
                plugins=[PydanticPlugin()],
                dependencies={
                    "db_session": Provide(lambda: session_mock, sync_to_thread=False)
                },
            ) as client:
                yield client
    finally:
        bytes_module.BOT_API_GUARDS[:] = original_guards


async def _seed_requests(
    redis, key_row, count: int, age_seconds: float = 0.0
) -> None:
    """Record ``count`` earlier allowed requests for the key in Redis."""
    timestamp = datetime.now(UTC) - timedelta(seconds=age_seconds)
    score = int(timestamp.timestamp() * 1000)
    await redis.zadd(
        rate_limit_redis_key(rate_limited_key_from_skrift(key_row)),
        {f"seed-{index}": score for index in range(count)},
    )


async def _recorded_requests(redis, key_row) -> int:
    return await redis.zcard(rate_limit_redis_key(rate_limited_key_from_skrift(key_row)))


class TestSuccessHeaders:
    """Allowed requests carry the full legacy header set."""

    async def test_first_request_reports_full_windows(self, rate_limited_client):
        response = rate_limited_client.get(
            CONFIG_PATH, headers={"Authorization": f"Bearer {VALID_SKRIFT_TOKEN}"}
        )

        assert response.status_code == 200
        assert response.headers["x-ratelimit-limit-second"] == "10"
        assert response.headers["x-ratelimit-remaining-second"] == "10"
        assert response.headers["x-ratelimit-limit-minute"] == "180"
        assert response.headers["x-ratelimit-remaining-minute"] == "180"
        assert response.headers["x-ratelimit-limit-15min"] == "2500"
        assert response.headers["x-ratelimit-remaining-15min"] == "2500"
        # Legacy trio mirrors the strictest (per-second) window.
        assert response.headers["x-ratelimit-limit"] == "10"
        assert response.headers["x-ratelimit-remaining"] == "10"
        assert int(response.headers["x-ratelimit-reset"]) > 0

    async def test_usage_decrements_remaining(
        self, rate_limited_client, skrift_key_row, fake_redis
    ):
        await _seed_requests(
            fake_redis, skrift_key_row, count=3
        )

        response = rate_limited_client.get(
            CONFIG_PATH, headers={"Authorization": f"Bearer {VALID_SKRIFT_TOKEN}"}
        )

        assert response.status_code == 200
        assert response.headers["x-ratelimit-remaining-second"] == "7"
        assert response.headers["x-ratelimit-remaining-minute"] == "177"

    async def test_allowed_request_counts_in_redis_and_logs_nothing(
        self, rate_limited_client, skrift_key_row, fake_redis, security_events
    ):
        rate_limited_client.get(
            CONFIG_PATH, headers={"Authorization": f"Bearer {VALID_SKRIFT_TOKEN}"}
        )

        assert await _recorded_requests(fake_redis, skrift_key_row) == 1
        assert security_events == []

    async def test_counter_key_expires_with_the_longest_window(
        self, rate_limited_client, skrift_key_row, fake_redis
    ):
        rate_limited_client.get(
            CONFIG_PATH, headers={"Authorization": f"Bearer {VALID_SKRIFT_TOKEN}"}
        )

        ttl = await fake_redis.ttl(
            rate_limit_redis_key(rate_limited_key_from_skrift(skrift_key_row))
        )
        assert 0 < ttl <= 900


class TestRateLimitExceeded:
    """Violations answer the legacy 429 with escalation."""

    async def test_second_window_exceeded_escalates_to_minute(
        self, rate_limited_client, skrift_key_row, fake_redis
    ):
        await _seed_requests(
            fake_redis, skrift_key_row, count=RATE_LIMIT_PER_SECOND
        )

        response = rate_limited_client.get(
            CONFIG_PATH, headers={"Authorization": f"Bearer {VALID_SKRIFT_TOKEN}"}
        )

        assert response.status_code == 429
        assert response.json() == {
            "detail": (
                "Rate limit of 10 requests per second exceeded. "
                "Must wait until minute window resets."
            )
        }
        assert response.headers["retry-after"] == "60"
        assert response.headers["x-ratelimit-limit-second"] == "10"
        assert response.headers["x-ratelimit-remaining-second"] == "0"
        assert response.headers["x-ratelimit-limit"] == "10"
        assert response.headers["x-ratelimit-remaining"] == "0"

    async def test_blocked_request_not_counted_and_violation_logged(
        self, rate_limited_client, skrift_key_row, fake_redis, security_events
    ):
        await _seed_requests(
            fake_redis, skrift_key_row, count=RATE_LIMIT_PER_SECOND
        )

        rate_limited_client.get(
            CONFIG_PATH, headers={"Authorization": f"Bearer {VALID_SKRIFT_TOKEN}"}
        )

        assert await _recorded_requests(fake_redis, skrift_key_row) == (
            RATE_LIMIT_PER_SECOND  # only the seeds
        )
        assert security_events == [
            {
                "event": "rate_limit_exceeded",
                "success": False,
                "key_id": str(skrift_key_row.id),
                "key_prefix": skrift_key_row.key_prefix,
                "current_usage": RATE_LIMIT_PER_SECOND,
                "rate_limit": RATE_LIMIT_PER_SECOND,
                "window": "second",
                # The route template, never the concrete path with its ids.
                "http.route": "/api/guilds/{guild_id}/bytes/config",
                "http.method": "GET",
            }
        ]

    async def test_rows_outside_window_do_not_count(
        self, rate_limited_client, skrift_key_row, fake_redis
    ):
        # Old enough to fall out of the second window but inside the minute.
        await _seed_requests(
            fake_redis,
            skrift_key_row,
            count=RATE_LIMIT_PER_SECOND,
            age_seconds=5.0,
        )

        response = rate_limited_client.get(
            CONFIG_PATH, headers={"Authorization": f"Bearer {VALID_SKRIFT_TOKEN}"}
        )

        assert response.status_code == 200
        assert response.headers["x-ratelimit-remaining-second"] == "10"
        assert response.headers["x-ratelimit-remaining-minute"] == "170"


class TestRedisUnreachable:
    """Fails open: the bot keeps working, without rate-limit headers."""

    async def test_request_passes_without_headers_or_events(
        self, rate_limited_client, fake_redis, security_events
    ):
        fake_redis.eval = AsyncMock(side_effect=RedisConnectionError("down"))

        response = rate_limited_client.get(
            CONFIG_PATH, headers={"Authorization": f"Bearer {VALID_SKRIFT_TOKEN}"}
        )

        assert response.status_code == 200
        assert "x-ratelimit-limit" not in response.headers
        assert security_events == []

    async def test_hung_redis_does_not_hold_the_request(self, rate_limited_client):
        """A Redis that accepts connections and never answers."""
        with socket.socket() as silent:
            silent.bind(("127.0.0.1", 0))
            silent.listen(8)  # connections queue in the backlog, never served
            host, port = silent.getsockname()
            hung = Redis(host=host, port=port, decode_responses=True)
            with patch(
                "smarter_dev.web.api_native.rate_limiting.get_redis_client",
                return_value=hung,
            ):
                started = time.monotonic()
                response = rate_limited_client.get(
                    CONFIG_PATH,
                    headers={"Authorization": f"Bearer {VALID_SKRIFT_TOKEN}"},
                )
                elapsed = time.monotonic() - started

        assert response.status_code == 200
        assert "x-ratelimit-limit" not in response.headers
        assert elapsed < rate_limiting.REDIS_TIMEOUT_SECONDS + 1.0

    async def test_malformed_redis_url_fails_open_not_500(self, rate_limited_client):
        def malformed_client():
            return create_redis_client(
                SimpleNamespace(effective_redis_url="not-a-redis-url")
            )

        with patch(
            "smarter_dev.web.api_native.rate_limiting.get_redis_client",
            side_effect=malformed_client,
        ):
            response = rate_limited_client.get(
                CONFIG_PATH, headers={"Authorization": f"Bearer {VALID_SKRIFT_TOKEN}"}
            )

        assert response.status_code == 200
        assert "x-ratelimit-limit" not in response.headers

    async def test_warning_is_throttled_per_interval(
        self, fake_redis, skrift_key_row, caplog, monkeypatch
    ):
        caplog.set_level(logging.WARNING, logger=rate_limiting.__name__)
        fake_redis.eval = AsyncMock(side_effect=RedisConnectionError("down"))
        key = rate_limited_key_from_skrift(skrift_key_row)
        request = Mock()

        for _ in range(5):
            await check_rate_limits(key, request, redis=fake_redis)
        assert len(caplog.records) == 1

        monkeypatch.setattr(
            rate_limiting,
            "_last_redis_warning_at",
            time.monotonic() - rate_limiting.REDIS_WARNING_INTERVAL_SECONDS - 1,
        )
        await check_rate_limits(key, request, redis=fake_redis)
        assert len(caplog.records) == 2
        assert "for 5 request(s)" in caplog.records[1].getMessage()

        # Two more go unlimited inside the interval, then Redis recovers: the
        # first good check reports them rather than leaving them unreported.
        for _ in range(2):
            await check_rate_limits(key, request, redis=fake_redis)
        del fake_redis.eval  # back to the real script
        decision = await check_rate_limits(key, request, redis=fake_redis)
        assert decision.headers
        assert len(caplog.records) == 3
        assert "2 more request(s)" in caplog.records[2].getMessage()

        await check_rate_limits(key, request, redis=fake_redis)
        assert len(caplog.records) == 3  # healthy checks log nothing


class TestUnauthenticatedPassthrough:
    """Requests without a verifiable key never consume or report windows."""

    async def test_missing_key_passes_through_without_headers(
        self, rate_limited_client
    ):
        response = rate_limited_client.get(CONFIG_PATH)

        # Guards are emptied in this fixture, so the handler answers 200 —
        # the assertion under test is the absence of rate-limit headers.
        assert response.status_code == 200
        assert "x-ratelimit-limit" not in response.headers

    async def test_unknown_key_passes_through_without_headers(
        self, rate_limited_client, security_events
    ):
        response = rate_limited_client.get(
            CONFIG_PATH, headers={"Authorization": "Bearer sk_" + "b" * 43}
        )

        assert response.status_code == 200
        assert "x-ratelimit-limit" not in response.headers
        assert security_events == []


class TestKeyViewAdapter:
    def test_rate_limited_key_from_skrift_prefers_service_name(self):
        row = FakeSkriftKeyRow()
        key_view = rate_limited_key_from_skrift(row)
        assert key_view == RateLimitedKey(
            id=row.id, key_prefix=row.key_prefix, created_by="discord-bot"
        )

    def test_rate_limited_key_falls_back_to_display_name(self):
        row = FakeSkriftKeyRow()
        row.service_name = None
        key_view = rate_limited_key_from_skrift(row)
        assert key_view.created_by == "discord-bot"
