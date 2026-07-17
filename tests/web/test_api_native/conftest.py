"""Fixtures for the native (Litestar) bot API parity tests.

These tests exercise the ported controllers in isolation — one controller, a
stubbed session, and mocked ``crud`` operations — via
``litestar.testing.create_test_client``. They assert the same status codes and
response bodies the legacy FastAPI ``tests/web/test_api/test_bytes.py`` did, so
the wire contract is proven unchanged before the FastAPI mount is retired.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from litestar.di import Provide
from litestar.plugins.pydantic import PydanticPlugin
from litestar.testing import TestClient, create_test_client
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.api_native import bytes as bytes_module
from smarter_dev.web.api_native.bytes import BytesController


@pytest.fixture
def session_mock() -> AsyncMock:
    """Async session stub shared with the mocked crud operations."""
    session = AsyncMock(spec=AsyncSession)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.flush = AsyncMock()
    session.refresh = AsyncMock()
    session.close = AsyncMock()
    return session


@pytest.fixture
def bytes_client(session_mock: AsyncMock) -> Iterator[TestClient]:
    """Litestar client serving the bytes controller with auth guards bypassed.

    Endpoint-logic and error-parity tests exercise handler behavior, not auth.
    The bytes routes share the ``BOT_API_GUARDS`` list by reference, so emptying
    it before the app is built (and restoring afterward) removes the guards for
    these tests only. Auth is covered separately by ``test_auth.py``.
    """
    original_guards = list(bytes_module.BOT_API_GUARDS)
    bytes_module.BOT_API_GUARDS.clear()
    try:
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


@pytest.fixture
def bytes_ops_mock() -> Iterator[Mock]:
    """Patch ``BytesOperations`` in the controller module."""
    with patch("smarter_dev.web.api_native.bytes.BytesOperations") as factory:
        instance = Mock()
        factory.return_value = instance
        instance.get_balance = AsyncMock()
        instance.get_or_create_balance = AsyncMock()
        instance.create_transaction = AsyncMock()
        instance.get_leaderboard = AsyncMock()
        instance.get_transaction_history = AsyncMock()
        instance.get_sent_transaction_history = AsyncMock()
        instance.update_daily_reward = AsyncMock()
        instance.reset_streak = AsyncMock()
        yield instance


@pytest.fixture
def bytes_config_ops_mock() -> Iterator[Mock]:
    """Patch ``BytesConfigOperations`` in the controller module."""
    with patch("smarter_dev.web.api_native.bytes.BytesConfigOperations") as factory:
        instance = Mock()
        factory.return_value = instance
        instance.get_config = AsyncMock()
        instance.create_config = AsyncMock()
        instance.update_config = AsyncMock()
        instance.delete_config = AsyncMock()
        yield instance


@pytest.fixture
def guild_id() -> str:
    """A valid Discord-snowflake-shaped guild id."""
    return "123456789012345678"


@pytest.fixture
def user_id() -> str:
    """A valid Discord-snowflake-shaped user id."""
    return "987654321098765432"


@pytest.fixture
def user_id_2() -> str:
    """A second valid Discord-snowflake-shaped user id."""
    return "111111111111111111"


@pytest.fixture
def bytes_balance_data(guild_id: str, user_id: str) -> dict[str, Any]:
    """Representative bytes balance attributes."""
    return {
        "guild_id": guild_id,
        "user_id": user_id,
        "balance": 100,
        "total_received": 150,
        "total_sent": 50,
        "streak_count": 3,
        "last_daily": None,
    }


@pytest.fixture
def bytes_config_data(guild_id: str) -> dict[str, Any]:
    """Representative bytes config attributes."""
    return {
        "guild_id": guild_id,
        "daily_amount": 10,
        "starting_balance": 100,
        "max_transfer": 1000,
        "daily_cooldown_hours": 24,
        "transfer_cooldown_hours": 0,
        "streak_bonuses": {"4": 2, "8": 2, "16": 3, "32": 5},
        "role_rewards": {},
        "transfer_tax_rate": 0.0,
        "is_enabled": True,
    }


@pytest.fixture
def transaction_data(guild_id: str, user_id: str, user_id_2: str) -> dict[str, Any]:
    """Representative transfer request body."""
    return {
        "giver_id": user_id,
        "giver_username": "TestUser1",
        "receiver_id": user_id_2,
        "receiver_username": "TestUser2",
        "amount": 25,
        "reason": "Test payment",
    }
