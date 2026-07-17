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

from smarter_dev.web.api.schemas import SquadCostInfo
from smarter_dev.web.api_native import bytes as bytes_module
from smarter_dev.web.api_native import squads as squads_module
from smarter_dev.web.api_native.bytes import BytesController
from smarter_dev.web.api_native.squads import SquadController, SquadSaleEventController


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


# --------------------------------------------------------------------------- #
# Squad fixtures (unit U3)
# --------------------------------------------------------------------------- #


@pytest.fixture
def role_id() -> str:
    """A valid Discord-snowflake-shaped role id."""
    return "555555555555555555"


@pytest.fixture
def sample_squad_data(guild_id: str, role_id: str) -> dict[str, Any]:
    """Representative squad attributes (parity with the FastAPI suite)."""
    return {
        "guild_id": guild_id,
        "role_id": role_id,
        "name": "Test Squad",
        "description": "A test squad for testing",
        "welcome_message": None,
        "announcement_channel": None,
        "max_members": 10,
        "switch_cost": 50,
        "is_active": True,
        "is_default": False,
    }


async def _bypass_cost_info(switch_cost: int, guild_id: str, session: Any) -> dict[str, Any]:
    """Stand in for ``squads.build_cost_info`` so tests skip sale-event lookups.

    Mirrors the FastAPI suite's ``_add_cost_info_to_squad`` patch.
    """
    no_cost = SquadCostInfo(
        original_cost=switch_cost,
        current_cost=switch_cost,
        discount_percent=None,
        active_sale=None,
        is_on_sale=False,
    )
    return {"join_cost_info": no_cost, "switch_cost_info": no_cost}


@pytest.fixture
def squad_client(session_mock: AsyncMock) -> Iterator[TestClient]:
    """Client serving the squad + sale-event controllers with guards bypassed.

    Both controllers share the ``squads.BOT_API_GUARDS`` list by reference, so
    emptying it before the app is built removes guards for these tests only.
    ``build_cost_info`` is patched to skip sale-event lookups. Auth is covered
    separately by ``test_auth.py`` (the bytes controller's guards).
    """
    original_guards = list(squads_module.BOT_API_GUARDS)
    squads_module.BOT_API_GUARDS.clear()
    try:
        with patch(
            "smarter_dev.web.api_native.squads.build_cost_info", _bypass_cost_info
        ):
            with create_test_client(
                route_handlers=[SquadController, SquadSaleEventController],
                plugins=[PydanticPlugin()],
                dependencies={
                    "db_session": Provide(lambda: session_mock, sync_to_thread=False)
                },
            ) as client:
                yield client
    finally:
        squads_module.BOT_API_GUARDS[:] = original_guards


@pytest.fixture
def squad_ops_mock() -> Iterator[Mock]:
    """Patch ``SquadOperations`` in the native squads module."""
    with patch("smarter_dev.web.api_native.squads.SquadOperations") as factory:
        instance = Mock()
        factory.return_value = instance
        instance.get_squad = AsyncMock()
        instance.get_guild_squads = AsyncMock()
        instance.create_squad = AsyncMock()
        instance.join_squad = AsyncMock()
        instance.leave_squad = AsyncMock()
        instance.get_user_squad = AsyncMock()
        instance.get_squad_members = AsyncMock()
        instance._get_squad_member_count = AsyncMock()
        yield instance


@pytest.fixture
def sale_event_ops_mock() -> Iterator[Mock]:
    """Patch ``SquadSaleEventOperations`` in the native squads module."""
    with patch("smarter_dev.web.api_native.squads.SquadSaleEventOperations") as factory:
        instance = Mock()
        factory.return_value = instance
        instance.get_sale_events_by_guild = AsyncMock()
        instance.get_sale_event_by_id = AsyncMock()
        yield instance
