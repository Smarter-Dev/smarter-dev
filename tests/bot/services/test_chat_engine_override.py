"""Tests for per-channel model override + token-budget enforcement in the engine.

The agent run, memory, input builders, and the Redis budget helpers are all
patched — these verify the engine's *wiring*: which model it selects, when it
skips a turn for budget, and when it meters usage.
"""

from __future__ import annotations

import asyncio
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


def _override(model_key: str, *, daily=0, hourly=0, reasoning_level=None):
    return SimpleNamespace(
        model_key=model_key,
        daily_token_budget=daily,
        hourly_token_budget=hourly,
        reasoning_level=reasoning_level,
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
    # The engine also records per-user message-limit charges through a
    # pipeline on this same Redis — make that path awaitable and inert.
    pipe = MagicMock()
    pipe.execute = AsyncMock(return_value=[0, True])
    pipe.__aenter__ = AsyncMock(return_value=pipe)
    pipe.__aexit__ = AsyncMock(return_value=False)
    r.pipeline = MagicMock(return_value=pipe)
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
        "smarter_dev.bot.services.chat_engine.over_budget_reset_epoch",
        new=AsyncMock(return_value=None),
    ), patch(
        "smarter_dev.bot.services.chat_engine.add_usage", new=AsyncMock()
    ):
        await engine._run_once(first_activation=True)

    get_agent_mock.assert_called_once_with("gpt-5.4", None)
    agent_mock.run.assert_awaited_once()


@pytest.mark.asyncio
async def test_override_reasoning_threads_to_agent(fake_memory, fake_redis):
    """A channel override's reasoning level is passed through to the agent."""
    agent_mock = MagicMock()
    agent_mock.run = AsyncMock(return_value=_result(_send()))
    engine, _ = _make_engine(
        _override("gpt-5-4", reasoning_level="high"), fake_redis
    )

    get_agent = patch(
        "smarter_dev.bot.services.chat_engine.get_chat_agent",
        return_value=agent_mock,
    )
    with get_agent as get_agent_mock, _patches(
        agent_mock=agent_mock, fake_memory=fake_memory
    )[1], _patches(agent_mock=agent_mock, fake_memory=fake_memory)[2], patch(
        "smarter_dev.bot.services.chat_engine.over_budget_reset_epoch",
        new=AsyncMock(return_value=None),
    ), patch(
        "smarter_dev.bot.services.chat_engine.add_usage", new=AsyncMock()
    ):
        await engine._run_once(first_activation=True)

    get_agent_mock.assert_called_once_with("gpt-5.4", "high")


@pytest.mark.asyncio
async def test_over_budget_skips_the_turn(fake_memory, fake_redis):
    """When the budget is spent the engine must not call the model."""
    agent_mock = MagicMock()
    agent_mock.run = AsyncMock(return_value=_result(_send()))
    engine, _ = _make_engine(_override("gpt-5-4", hourly=100), fake_redis)

    with _patches(agent_mock=agent_mock, fake_memory=fake_memory)[0], _patches(
        agent_mock=agent_mock, fake_memory=fake_memory
    )[1], _patches(agent_mock=agent_mock, fake_memory=fake_memory)[2], patch(
        "smarter_dev.bot.services.chat_engine.over_budget_reset_epoch",
        new=AsyncMock(return_value=1_800_000_000),
    ), patch(
        "smarter_dev.bot.services.chat_engine.add_usage", new=AsyncMock()
    ) as add_usage_mock:
        await engine._run_once(first_activation=True)

    agent_mock.run.assert_not_called()
    add_usage_mock.assert_not_called()
    # A single throttled notice is posted (SET NX won the throttle key), and it
    # carries a Discord relative timestamp counting down to the budget reset.
    fake_redis.set.assert_awaited_once()
    engine.bot.rest.create_message.assert_awaited_once()
    notice = engine.bot.rest.create_message.await_args.kwargs["content"]
    assert "<t:1800000000:R>" in notice


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
        "smarter_dev.bot.services.chat_engine.over_budget_reset_epoch",
        new=AsyncMock(return_value=None),
    ), patch(
        "smarter_dev.bot.services.chat_engine.add_usage", new=AsyncMock()
    ) as add_usage_mock:
        await engine._run_once(first_activation=True)

    agent_mock.run.assert_awaited_once()
    add_usage_mock.assert_awaited_once_with(fake_redis, "42", 150)


@pytest.mark.asyncio
async def test_no_override_uses_default_and_skips_enforcement_but_meters(
    fake_memory, fake_redis
):
    """No override → default agent and no budget *enforcement*, but the turn's
    token usage is still metered so ``/bot-usage`` has numbers everywhere."""
    agent_mock = MagicMock()
    agent_mock.run = AsyncMock(
        return_value=_result(_send(), input_tokens=100, output_tokens=50)
    )
    engine, service = _make_engine(None, fake_redis)

    get_agent = patch(
        "smarter_dev.bot.services.chat_engine.get_chat_agent",
        return_value=agent_mock,
    )
    with get_agent as get_agent_mock, _patches(
        agent_mock=agent_mock, fake_memory=fake_memory
    )[1], _patches(agent_mock=agent_mock, fake_memory=fake_memory)[2], patch(
        "smarter_dev.bot.services.chat_engine.over_budget_reset_epoch",
        new=AsyncMock(return_value=None),
    ) as over_budget_mock, patch(
        "smarter_dev.bot.services.chat_engine.add_usage", new=AsyncMock()
    ) as add_usage_mock:
        await engine._run_once(first_activation=True)

    get_agent_mock.assert_called_once_with(None, None)
    over_budget_mock.assert_not_called()
    add_usage_mock.assert_awaited_once_with(fake_redis, "42", 150)


@pytest.mark.asyncio
async def test_persisted_turn_records_override_model_name(fake_memory, fake_redis):
    """The persisted turn is priced against the override's wire model id, not
    the default model (regression: the model was read off RunUsage, which never
    carries it, so every overridden turn was recorded under the default)."""
    agent_mock = MagicMock()
    agent_mock.run = AsyncMock(
        return_value=_result(_send(), input_tokens=10, output_tokens=5)
    )
    engine, _ = _make_engine(_override("gpt-5-4", daily=1000), fake_redis)

    start_engagement = AsyncMock(return_value="engagement-1")
    persist_turn = AsyncMock()
    with _patches(agent_mock=agent_mock, fake_memory=fake_memory)[0], _patches(
        agent_mock=agent_mock, fake_memory=fake_memory
    )[1], _patches(agent_mock=agent_mock, fake_memory=fake_memory)[2], patch(
        "smarter_dev.bot.services.chat_engine.over_budget_reset_epoch",
        new=AsyncMock(return_value=None),
    ), patch(
        "smarter_dev.bot.services.chat_engine.add_usage", new=AsyncMock()
    ), patch(
        "smarter_dev.bot.services.chat_engine.start_engagement", new=start_engagement
    ), patch(
        "smarter_dev.bot.services.chat_engine.persist_turn", new=persist_turn
    ):
        await engine._run_once(first_activation=True)

    persist_turn.assert_awaited_once()
    assert persist_turn.await_args.kwargs["chat_model_name"] == "gpt-5.4"


@pytest.mark.asyncio
async def test_over_budget_on_first_activation_recovers_when_budget_frees(
    fake_memory, fake_redis
):
    """Over budget on the very first activation must not permanently lose the
    engagement: once the budget window frees, the next fire still starts the
    engagement and persists its turn (regression: first_activation was consumed
    on the skipped turn, so engagement_id stayed None forever)."""
    agent_mock = MagicMock()
    agent_mock.run = AsyncMock(
        return_value=_result(_send(), input_tokens=10, output_tokens=5)
    )
    engine, _ = _make_engine(_override("gpt-5-4", hourly=100), fake_redis)

    budget_reset = AsyncMock(return_value=1_800_000_000)
    start_engagement = AsyncMock(return_value="engagement-1")
    persist_turn = AsyncMock()
    with _patches(agent_mock=agent_mock, fake_memory=fake_memory)[0], _patches(
        agent_mock=agent_mock, fake_memory=fake_memory
    )[1], _patches(agent_mock=agent_mock, fake_memory=fake_memory)[2], patch(
        "smarter_dev.bot.services.chat_engine.over_budget_reset_epoch",
        new=budget_reset,
    ), patch(
        "smarter_dev.bot.services.chat_engine.add_usage", new=AsyncMock()
    ), patch(
        "smarter_dev.bot.services.chat_engine.start_engagement", new=start_engagement
    ), patch(
        "smarter_dev.bot.services.chat_engine.persist_turn", new=persist_turn
    ):
        engine.start()
        # First activation lands while the channel is over budget.
        engine.trigger_initial(engine.activation_message)
        await asyncio.sleep(0.05)
        # Turn skipped: no model call, no engagement, nothing persisted.
        agent_mock.run.assert_not_awaited()
        start_engagement.assert_not_awaited()
        persist_turn.assert_not_awaited()

        # Budget frees; the engine fires again on the next activity.
        budget_reset.return_value = None
        engine.fire_now()
        await asyncio.sleep(0.05)
        await engine.shutdown()

    agent_mock.run.assert_awaited_once()
    start_engagement.assert_awaited_once()
    persist_turn.assert_awaited_once()


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
        "smarter_dev.bot.services.chat_engine.over_budget_reset_epoch",
        new=AsyncMock(return_value=None),
    ), patch(
        "smarter_dev.bot.services.chat_engine.add_usage", new=AsyncMock()
    ):
        await engine._run_once(first_activation=True)

    get_agent_mock.assert_called_once_with(None, None)
    agent_mock.run.assert_awaited_once()
