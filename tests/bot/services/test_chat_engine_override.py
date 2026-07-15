"""Tests for per-channel model override + token-budget enforcement in the engine.

The agent run, memory, input builders, and the Redis budget helpers are all
patched — these verify the engine's *wiring*: which model it selects, when it
skips a turn for budget, and when it meters usage.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from smarter_dev.bot.agents.chat_models import (
    Author,
    ChannelInfo,
    InitialAgentInput,
    Me,
    Message,
    MessageScore,
    ResponseBody,
    TurnDecision,
)
from smarter_dev.bot.services.chat_engine import ChannelEngine


def _initial_input() -> InitialAgentInput:
    return InitialAgentInput(
        me=Me(user_id="999", username="bot"),
        channel_history=[
            Message(message_id="100", author_id="200", body="prior message"),
        ],
        activation_message=Message(
            message_id="101",
            author_id="200",
            body="@bot hi",
            mentions_bot=True,
        ),
        authors=[Author(user_id="200", username="alice")],
        channel=ChannelInfo(channel_id="1", name="general"),
        now_utc=datetime.now(UTC),
    )


def _send(message: str = "hi there") -> TurnDecision:
    return TurnDecision(
        rankings=[
            MessageScore(message_id="101", score=10, reasoning="direct")
        ],
        response=ResponseBody(
            target_message_id="101",
            reply_directly=False,
            message=message,
        ),
        topic="t",
        notes="n",
        continue_watching=True,
    )


def _result(output, *, input_tokens=0, output_tokens=0):
    usage = SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        requests=1,
        model="test-model",
    )
    return SimpleNamespace(
        output=output,
        usage=lambda: usage,
        all_messages=lambda: [],
        new_messages=lambda: [],
    )


def _override(model_key: str, *, daily=0, hourly=0):
    return SimpleNamespace(
        model_key=model_key,
        daily_token_budget=daily,
        hourly_token_budget=hourly,
    )


@pytest.fixture
def fake_memory():
    m = MagicMock()
    m.reset_idle_counter = AsyncMock()
    m.write_topic = AsyncMock()
    m.write_notes = AsyncMock()
    m.clear_notes = AsyncMock()
    m.read_history = AsyncMock(return_value=[])
    m.write_history = AsyncMock()
    m.clear_history = AsyncMock()
    return m


@pytest.fixture
def fake_redis():
    r = MagicMock()
    r.set = AsyncMock(return_value=True)
    return r


def _make_engine(override, fake_redis):
    bot = MagicMock()
    bot.rest = MagicMock()
    bot.rest.create_message = AsyncMock()
    service = MagicMock()
    service.get_override = AsyncMock(return_value=override)
    bot.d = {
        "model_override_service": service,
        "chat_memory_redis": fake_redis,
    }

    async def _on_deactivate(channel_id: int) -> None:
        pass

    async def _noop_voice(channel_id, text, reply_to, instruction=None):
        pass

    engine = ChannelEngine(
        bot=bot,
        channel_id=42,
        guild_id=99,
        voice_send=_noop_voice,
        on_deactivate=_on_deactivate,
    )
    engine.activation_message = SimpleNamespace(
        id=101, author=SimpleNamespace(id=200, username="alice")
    )
    return engine, service


def _patches(*, agent_mock, fake_memory):
    return [
        patch(
            "smarter_dev.bot.services.chat_engine.get_chat_agent",
            return_value=agent_mock,
        ),
        patch(
            "smarter_dev.bot.services.chat_engine.get_chat_memory",
            return_value=fake_memory,
        ),
        patch(
            "smarter_dev.bot.services.chat_engine.build_initial_input",
            new=AsyncMock(return_value=_initial_input()),
        ),
    ]


@pytest.mark.asyncio
async def test_override_present_builds_agent_for_override_model(
    fake_memory, fake_redis
):
    """A channel override routes the turn through the override model's wire id."""
    agent_mock = MagicMock()
    agent_mock.run = AsyncMock(return_value=_result(_send()))
    engine, _ = _make_engine(_override("gpt-5-4"), fake_redis)

    get_agent = patch(
        "smarter_dev.bot.services.chat_engine.get_chat_agent",
        return_value=agent_mock,
    )
    with get_agent as get_agent_mock, _patches(
        agent_mock=agent_mock, fake_memory=fake_memory
    )[1], _patches(agent_mock=agent_mock, fake_memory=fake_memory)[2], patch(
        "smarter_dev.bot.services.chat_engine.is_over_budget",
        new=AsyncMock(return_value=False),
    ), patch(
        "smarter_dev.bot.services.chat_engine.add_usage", new=AsyncMock()
    ):
        await engine._run_once(first_activation=True)

    get_agent_mock.assert_called_once_with("gpt-5.4")
    agent_mock.run.assert_awaited_once()


@pytest.mark.asyncio
async def test_over_budget_skips_the_turn(fake_memory, fake_redis):
    """When the budget is spent the engine must not call the model."""
    agent_mock = MagicMock()
    agent_mock.run = AsyncMock(return_value=_result(_send()))
    engine, _ = _make_engine(_override("gpt-5-4", hourly=100), fake_redis)

    with _patches(agent_mock=agent_mock, fake_memory=fake_memory)[0], _patches(
        agent_mock=agent_mock, fake_memory=fake_memory
    )[1], _patches(agent_mock=agent_mock, fake_memory=fake_memory)[2], patch(
        "smarter_dev.bot.services.chat_engine.is_over_budget",
        new=AsyncMock(return_value=True),
    ), patch(
        "smarter_dev.bot.services.chat_engine.add_usage", new=AsyncMock()
    ) as add_usage_mock:
        await engine._run_once(first_activation=True)

    agent_mock.run.assert_not_called()
    add_usage_mock.assert_not_called()
    # A single throttled notice is posted (SET NX won the throttle key).
    fake_redis.set.assert_awaited_once()
    engine.bot.rest.create_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_under_budget_runs_and_meters_usage(fake_memory, fake_redis):
    """Under budget: the model runs and input+output tokens are recorded."""
    agent_mock = MagicMock()
    agent_mock.run = AsyncMock(
        return_value=_result(_send(), input_tokens=100, output_tokens=50)
    )
    engine, _ = _make_engine(_override("gpt-5-4", daily=1000), fake_redis)

    with _patches(agent_mock=agent_mock, fake_memory=fake_memory)[0], _patches(
        agent_mock=agent_mock, fake_memory=fake_memory
    )[1], _patches(agent_mock=agent_mock, fake_memory=fake_memory)[2], patch(
        "smarter_dev.bot.services.chat_engine.is_over_budget",
        new=AsyncMock(return_value=False),
    ), patch(
        "smarter_dev.bot.services.chat_engine.add_usage", new=AsyncMock()
    ) as add_usage_mock:
        await engine._run_once(first_activation=True)

    agent_mock.run.assert_awaited_once()
    add_usage_mock.assert_awaited_once_with(fake_redis, "42", 150)


@pytest.mark.asyncio
async def test_no_override_uses_default_and_skips_budget(fake_memory, fake_redis):
    """No override → default agent, no budget check, no usage metering."""
    agent_mock = MagicMock()
    agent_mock.run = AsyncMock(return_value=_result(_send()))
    engine, service = _make_engine(None, fake_redis)

    get_agent = patch(
        "smarter_dev.bot.services.chat_engine.get_chat_agent",
        return_value=agent_mock,
    )
    with get_agent as get_agent_mock, _patches(
        agent_mock=agent_mock, fake_memory=fake_memory
    )[1], _patches(agent_mock=agent_mock, fake_memory=fake_memory)[2], patch(
        "smarter_dev.bot.services.chat_engine.is_over_budget",
        new=AsyncMock(return_value=False),
    ) as over_budget_mock, patch(
        "smarter_dev.bot.services.chat_engine.add_usage", new=AsyncMock()
    ) as add_usage_mock:
        await engine._run_once(first_activation=True)

    get_agent_mock.assert_called_once_with(None)
    over_budget_mock.assert_not_called()
    add_usage_mock.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_model_key_falls_back_to_default(fake_memory, fake_redis):
    """A stale/unknown stored model_key falls back to the default model
    without crashing the turn (budget still enforced since an override exists)."""
    agent_mock = MagicMock()
    agent_mock.run = AsyncMock(return_value=_result(_send()))
    engine, _ = _make_engine(_override("this-model-was-removed"), fake_redis)

    get_agent = patch(
        "smarter_dev.bot.services.chat_engine.get_chat_agent",
        return_value=agent_mock,
    )
    with get_agent as get_agent_mock, _patches(
        agent_mock=agent_mock, fake_memory=fake_memory
    )[1], _patches(agent_mock=agent_mock, fake_memory=fake_memory)[2], patch(
        "smarter_dev.bot.services.chat_engine.is_over_budget",
        new=AsyncMock(return_value=False),
    ), patch(
        "smarter_dev.bot.services.chat_engine.add_usage", new=AsyncMock()
    ):
        await engine._run_once(first_activation=True)

    get_agent_mock.assert_called_once_with(None)
    agent_mock.run.assert_awaited_once()
