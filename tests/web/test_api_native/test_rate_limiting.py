"""Tests for the ported multi-tier rate limiter on the bytes controller.

Parity contract (docs/v2/legacy-sunset/04-api-rewrite.md "Rate-limiting
parity"): windows 10/s, 180/min, 2500/15 min per key, ``x-ratelimit-*``
headers on success, 429 with the legacy ``{"detail": ...}`` body and
escalated ``retry-after`` on violation, usage counting backed by
``security_logs`` ``api_request`` rows. The bot's ``api_client`` reads the
headers to self-throttle, so they are part of the wire contract.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch
from uuid import uuid4

import pytest
from litestar.di import Provide
from litestar.plugins.pydantic import PydanticPlugin
from litestar.testing import TestClient
from litestar.testing import create_test_client
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from smarter_dev.web.api_native import bytes as bytes_module
from smarter_dev.web.api_native.bytes import BytesController
from smarter_dev.web.api_native.rate_limiting import RATE_LIMIT_PER_SECOND
from smarter_dev.web.api_native.rate_limiting import RateLimitedKey
from smarter_dev.web.api_native.rate_limiting import rate_limited_key_from_skrift
from smarter_dev.web.models import SecurityLog

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


@pytest.fixture
async def security_log_session_maker():
    """In-memory SQLite engine carrying only the security_logs table."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: SecurityLog.metadata.create_all(
                sync_conn, tables=[SecurityLog.__table__]
            )
        )
    maker = async_sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield maker
    finally:
        await engine.dispose()


@pytest.fixture
def skrift_key_row() -> FakeSkriftKeyRow:
    return FakeSkriftKeyRow()


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
    security_log_session_maker, skrift_key_row, config_ops_mock
) -> Iterator[TestClient]:
    """Bytes controller behind the real middleware, guards bypassed.

    The middleware's two session contexts are pointed at the test SQLite DB
    and the Skrift key verification is stubbed: a request bearing
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
                "smarter_dev.web.api_native.rate_limiting.get_skrift_db_session_context",
                side_effect=lambda: security_log_session_maker(),
            ),
            patch(
                "smarter_dev.web.api_native.rate_limiting.get_db_session_context",
                side_effect=lambda: security_log_session_maker(),
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


async def _seed_api_request_rows(
    session_maker, api_key_id, count: int, age_seconds: float = 0.0
) -> None:
    """Insert ``count`` api_request security-log rows for the key."""
    timestamp = datetime.now(UTC) - timedelta(seconds=age_seconds)
    async with session_maker() as session:
        for _ in range(count):
            session.add(
                SecurityLog(
                    action="api_request",
                    api_key_id=api_key_id,
                    success=True,
                    details="seeded",
                    timestamp=timestamp,
                )
            )
        await session.commit()


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
        self, rate_limited_client, security_log_session_maker, skrift_key_row
    ):
        await _seed_api_request_rows(
            security_log_session_maker, skrift_key_row.id, count=3
        )

        response = rate_limited_client.get(
            CONFIG_PATH, headers={"Authorization": f"Bearer {VALID_SKRIFT_TOKEN}"}
        )

        assert response.status_code == 200
        assert response.headers["x-ratelimit-remaining-second"] == "7"
        assert response.headers["x-ratelimit-remaining-minute"] == "177"

    async def test_allowed_request_logs_api_request_row(
        self, rate_limited_client, security_log_session_maker, skrift_key_row
    ):
        rate_limited_client.get(
            CONFIG_PATH, headers={"Authorization": f"Bearer {VALID_SKRIFT_TOKEN}"}
        )

        async with security_log_session_maker() as session:
            count = await session.scalar(
                select(func.count(SecurityLog.id)).where(
                    SecurityLog.api_key_id == skrift_key_row.id,
                    SecurityLog.action == "api_request",
                )
            )
        assert count == 1


class TestRateLimitExceeded:
    """Violations answer the legacy 429 with escalation."""

    async def test_second_window_exceeded_escalates_to_minute(
        self, rate_limited_client, security_log_session_maker, skrift_key_row
    ):
        await _seed_api_request_rows(
            security_log_session_maker, skrift_key_row.id, count=RATE_LIMIT_PER_SECOND
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
        self, rate_limited_client, security_log_session_maker, skrift_key_row
    ):
        await _seed_api_request_rows(
            security_log_session_maker, skrift_key_row.id, count=RATE_LIMIT_PER_SECOND
        )

        rate_limited_client.get(
            CONFIG_PATH, headers={"Authorization": f"Bearer {VALID_SKRIFT_TOKEN}"}
        )

        async with security_log_session_maker() as session:
            api_request_count = await session.scalar(
                select(func.count(SecurityLog.id)).where(
                    SecurityLog.action == "api_request"
                )
            )
            violation_count = await session.scalar(
                select(func.count(SecurityLog.id)).where(
                    SecurityLog.action == "rate_limit_exceeded"
                )
            )
        assert api_request_count == RATE_LIMIT_PER_SECOND  # only the seeds
        assert violation_count == 1

    async def test_rows_outside_window_do_not_count(
        self, rate_limited_client, security_log_session_maker, skrift_key_row
    ):
        # Old enough to fall out of the second window but inside the minute.
        await _seed_api_request_rows(
            security_log_session_maker,
            skrift_key_row.id,
            count=RATE_LIMIT_PER_SECOND,
            age_seconds=5.0,
        )

        response = rate_limited_client.get(
            CONFIG_PATH, headers={"Authorization": f"Bearer {VALID_SKRIFT_TOKEN}"}
        )

        assert response.status_code == 200
        assert response.headers["x-ratelimit-remaining-second"] == "10"
        assert response.headers["x-ratelimit-remaining-minute"] == "170"


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
        self, rate_limited_client, security_log_session_maker
    ):
        response = rate_limited_client.get(
            CONFIG_PATH, headers={"Authorization": "Bearer sk_" + "b" * 43}
        )

        assert response.status_code == 200
        assert "x-ratelimit-limit" not in response.headers

        async with security_log_session_maker() as session:
            count = await session.scalar(select(func.count(SecurityLog.id)))
        assert count == 0


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
