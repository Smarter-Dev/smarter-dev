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
from smarter_dev.web.api_native import advent_of_code as advent_of_code_module
from smarter_dev.web.api_native import bytes as bytes_module
from smarter_dev.web.api_native import challenges as challenges_module
from smarter_dev.web.api_native import messages as messages_module
from smarter_dev.web.api_native import quests as quests_module
from smarter_dev.web.api_native import squads as squads_module
from smarter_dev.web.api_native.advent_of_code import AdventOfCodeController
from smarter_dev.web.api_native.bytes import BytesController
from smarter_dev.web.api_native.challenges import ChallengeController
from smarter_dev.web.api_native.messages import (
    RepeatingMessageController,
    ScheduledMessageController,
)
from smarter_dev.web.api_native.quests import QuestController
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


# --------------------------------------------------------------------------- #
# Quest fixtures (unit U5)
# --------------------------------------------------------------------------- #


@pytest.fixture
def quest_client(session_mock: AsyncMock) -> Iterator[TestClient]:
    """Client serving the quest controller with auth guards bypassed.

    The quest routes share the ``quests.BOT_API_GUARDS`` list by reference, so
    emptying it before the app is built removes guards for these tests only.
    Auth is covered separately by ``test_auth.py`` (the bytes controller).
    """
    original_guards = list(quests_module.BOT_API_GUARDS)
    quests_module.BOT_API_GUARDS.clear()
    try:
        with create_test_client(
            route_handlers=[QuestController],
            plugins=[PydanticPlugin()],
            dependencies={
                "db_session": Provide(lambda: session_mock, sync_to_thread=False)
            },
        ) as client:
            yield client
    finally:
        quests_module.BOT_API_GUARDS[:] = original_guards


@pytest.fixture
def quest_ops_mock() -> Iterator[Mock]:
    """Patch ``QuestOperations`` in the native quests module."""
    with patch("smarter_dev.web.api_native.quests.QuestOperations") as factory:
        instance = Mock()
        factory.return_value = instance
        instance.get_daily_quest = AsyncMock()
        instance.get_daily_quest_by_id = AsyncMock()
        instance.get_upcoming_daily_quests = AsyncMock()
        instance.mark_daily_quest_announced = AsyncMock()
        instance.mark_daily_quest_active = AsyncMock()
        yield instance


@pytest.fixture
def quest_input_ops_mock() -> Iterator[Mock]:
    """Patch ``QuestInputOperations`` in the native quests module."""
    with patch("smarter_dev.web.api_native.quests.QuestInputOperations") as factory:
        instance = Mock()
        factory.return_value = instance
        instance.get_or_create_input = AsyncMock()
        yield instance


@pytest.fixture
def quest_submission_ops_mock() -> Iterator[Mock]:
    """Patch ``QuestSubmissionOperations`` in the native quests module."""
    with patch("smarter_dev.web.api_native.quests.QuestSubmissionOperations") as factory:
        instance = Mock()
        factory.return_value = instance
        instance.submit_solution = AsyncMock()
        instance.get_daily_quest_scoreboard = AsyncMock()
        yield instance


@pytest.fixture
def quest_squad_ops_mock() -> Iterator[Mock]:
    """Patch ``SquadOperations`` in the native quests module."""
    with patch("smarter_dev.web.api_native.quests.SquadOperations") as factory:
        instance = Mock()
        factory.return_value = instance
        instance.get_user_squad = AsyncMock()
        yield instance


# --------------------------------------------------------------------------- #
# Advent of Code fixtures (unit U6 — advent_of_code router)
# --------------------------------------------------------------------------- #


@pytest.fixture
def aoc_client(session_mock: AsyncMock) -> Iterator[TestClient]:
    """Client serving the Advent of Code controller with auth guards bypassed.

    The AoC routes share the ``advent_of_code.BOT_API_GUARDS`` list by reference,
    so emptying it before the app is built removes guards for these tests only.
    Auth is covered separately by ``test_auth.py``.
    """
    original_guards = list(advent_of_code_module.BOT_API_GUARDS)
    advent_of_code_module.BOT_API_GUARDS.clear()
    try:
        with create_test_client(
            route_handlers=[AdventOfCodeController],
            plugins=[PydanticPlugin()],
            dependencies={
                "db_session": Provide(lambda: session_mock, sync_to_thread=False)
            },
        ) as client:
            yield client
    finally:
        advent_of_code_module.BOT_API_GUARDS[:] = original_guards


@pytest.fixture
def aoc_ops_mock() -> Iterator[Mock]:
    """Patch ``AdventOfCodeConfigOperations`` in the native AoC module."""
    with patch(
        "smarter_dev.web.api_native.advent_of_code.AdventOfCodeConfigOperations"
    ) as factory:
        instance = Mock()
        factory.return_value = instance
        instance.get_active_configs = AsyncMock()
        instance.get_or_create_config = AsyncMock()
        instance.get_posted_thread = AsyncMock()
        instance.record_posted_thread = AsyncMock()
        instance.get_guild_threads = AsyncMock()
        yield instance


# --------------------------------------------------------------------------- #
# Challenge fixtures (unit U4)
# --------------------------------------------------------------------------- #


@pytest.fixture
def challenge_client(session_mock: AsyncMock) -> Iterator[TestClient]:
    """Client serving the challenge controller with auth guards bypassed.

    The challenge routes share the ``challenges.BOT_API_GUARDS`` list by
    reference, so emptying it before the app is built removes guards for these
    tests only. Auth is covered separately by ``test_auth.py``.
    """
    original_guards = list(challenges_module.BOT_API_GUARDS)
    challenges_module.BOT_API_GUARDS.clear()
    try:
        with create_test_client(
            route_handlers=[ChallengeController],
            plugins=[PydanticPlugin()],
            dependencies={
                "db_session": Provide(lambda: session_mock, sync_to_thread=False)
            },
        ) as client:
            yield client
    finally:
        challenges_module.BOT_API_GUARDS[:] = original_guards


@pytest.fixture
def campaign_ops_mock() -> Iterator[Mock]:
    """Patch ``CampaignOperations`` in the native challenges module."""
    with patch("smarter_dev.web.api_native.challenges.CampaignOperations") as factory:
        instance = Mock()
        factory.return_value = instance
        instance.get_upcoming_announcements = AsyncMock()
        instance.get_pending_announcements = AsyncMock()
        instance.mark_challenge_released = AsyncMock()
        instance.mark_challenge_announced = AsyncMock()
        instance.get_most_recent_campaign = AsyncMock()
        instance.get_challenge_with_campaign = AsyncMock()
        instance.get_campaign_challenge_count = AsyncMock()
        yield instance


@pytest.fixture
def challenge_submission_ops_mock() -> Iterator[Mock]:
    """Patch ``ChallengeSubmissionOperations`` in the native challenges module."""
    with patch(
        "smarter_dev.web.api_native.challenges.ChallengeSubmissionOperations"
    ) as factory:
        instance = Mock()
        factory.return_value = instance
        instance.get_campaign_scoreboard = AsyncMock()
        instance.get_detailed_campaign_scoreboard = AsyncMock()
        instance.get_campaign_submission_count = AsyncMock()
        instance.submit_solution = AsyncMock()
        yield instance


@pytest.fixture
def challenge_input_ops_mock() -> Iterator[Mock]:
    """Patch ``ChallengeInputOperations`` in the native challenges module."""
    with patch(
        "smarter_dev.web.api_native.challenges.ChallengeInputOperations"
    ) as factory:
        instance = Mock()
        factory.return_value = instance
        instance.get_existing_input = AsyncMock()
        instance.get_or_create_input = AsyncMock()
        yield instance


@pytest.fixture
def challenge_squad_ops_mock() -> Iterator[Mock]:
    """Patch ``SquadOperations`` in the native challenges module."""
    with patch("smarter_dev.web.api_native.challenges.SquadOperations") as factory:
        instance = Mock()
        factory.return_value = instance
        instance.get_user_squad = AsyncMock()
        yield instance


# --------------------------------------------------------------------------- #
# Messaging fixtures (unit U6 — scheduled + repeating messages)
# --------------------------------------------------------------------------- #


@pytest.fixture
def scheduled_client(session_mock: AsyncMock) -> Iterator[TestClient]:
    """Client serving the scheduled-message controller with guards bypassed.

    The routes share the ``messages.BOT_API_GUARDS`` list by reference, so
    emptying it before the app is built removes guards for these tests only.
    Auth is covered separately by ``test_auth.py``.
    """
    original_guards = list(messages_module.BOT_API_GUARDS)
    messages_module.BOT_API_GUARDS.clear()
    try:
        with create_test_client(
            route_handlers=[ScheduledMessageController],
            plugins=[PydanticPlugin()],
            dependencies={
                "db_session": Provide(lambda: session_mock, sync_to_thread=False)
            },
        ) as client:
            yield client
    finally:
        messages_module.BOT_API_GUARDS[:] = original_guards


@pytest.fixture
def repeating_client(session_mock: AsyncMock) -> Iterator[TestClient]:
    """Client serving the repeating-message controller with guards bypassed.

    The routes share the ``messages.BOT_API_GUARDS`` list by reference, so
    emptying it before the app is built removes guards for these tests only.
    Auth is covered separately by ``test_auth.py``.
    """
    original_guards = list(messages_module.BOT_API_GUARDS)
    messages_module.BOT_API_GUARDS.clear()
    try:
        with create_test_client(
            route_handlers=[RepeatingMessageController],
            plugins=[PydanticPlugin()],
            dependencies={
                "db_session": Provide(lambda: session_mock, sync_to_thread=False)
            },
        ) as client:
            yield client
    finally:
        messages_module.BOT_API_GUARDS[:] = original_guards


@pytest.fixture
def scheduled_ops_mock() -> Iterator[Mock]:
    """Patch ``ScheduledMessageOperations`` in the native messages module."""
    with patch(
        "smarter_dev.web.api_native.messages.ScheduledMessageOperations"
    ) as factory:
        instance = Mock()
        factory.return_value = instance
        instance.get_upcoming_scheduled_messages = AsyncMock()
        instance.get_pending_scheduled_messages = AsyncMock()
        instance.mark_scheduled_message_sent = AsyncMock()
        instance.get_scheduled_message_with_campaign = AsyncMock()
        yield instance


@pytest.fixture
def repeating_ops_mock() -> Iterator[Mock]:
    """Patch ``RepeatingMessageOperations`` in the native messages module."""
    with patch(
        "smarter_dev.web.api_native.messages.RepeatingMessageOperations"
    ) as factory:
        instance = Mock()
        factory.return_value = instance
        instance.get_due_repeating_messages = AsyncMock()
        instance.mark_message_sent = AsyncMock()
        instance.create_repeating_message = AsyncMock()
        instance.get_guild_repeating_messages = AsyncMock()
        instance.get_repeating_message = AsyncMock()
        instance.update_repeating_message = AsyncMock()
        instance.delete_repeating_message = AsyncMock()
        instance.toggle_repeating_message = AsyncMock()
        yield instance
