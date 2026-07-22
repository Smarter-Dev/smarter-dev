"""Tests for per-channel model override + token-budget enforcement in the engine.

The agent run, memory, input builders, and the Redis budget helpers are all
patched — these verify the engine's *wiring*: which model it selects, when it
skips a turn for budget, and when it meters usage.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from smarter_dev.bot.agents.chat_models import (
    Author,
    ChannelInfo,
    FollowupAgentInput,
    InitialAgentInput,
    Me,
    Message,
    MessageScore,
    ResponseBody,
    TurnDecision,
)
from smarter_dev.bot.services.chat_engine import ChannelEngine
from smarter_dev.bot.services.chat_engine import _QueuedMessage


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
        response_language="english",
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


def _override(
    model_key: str,
    *,
    daily=0,
    hourly=0,
    reasoning_level=None,
    auto_respond=False,
    fallback_model_key=None,
    response_filter=None,
):
    return SimpleNamespace(
        model_key=model_key,
        daily_token_budget=daily,
        hourly_token_budget=hourly,
        reasoning_level=reasoning_level,
        auto_respond=auto_respond,
        fallback_model_key=fallback_model_key,
        response_filter=response_filter,
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
    # Fallback flag/marker reads default to "absent" so normal budget flow runs.
    r.exists = AsyncMock(return_value=0)
    r.delete = AsyncMock(return_value=0)
    # The temporary default-model override read defaults to "none active".
    r.get = AsyncMock(return_value=None)
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
async def test_override_without_pinned_model_uses_default_but_enforces_budget(
    fake_memory, fake_redis
):
    """A budgets-only override (model_key None = server default) runs the
    default agent while its token budgets are still enforced."""
    agent_mock = MagicMock()
    agent_mock.run = AsyncMock(return_value=_result(_send()))
    engine, _ = _make_engine(_override(None, hourly=100), fake_redis)

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
    ):
        await engine._run_once(first_activation=True)

    get_agent_mock.assert_called_once_with(None, None)
    over_budget_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_turn_prompt_carries_model_identity(fake_memory, fake_redis):
    """The user prompt handed to the agent names the resolved model and
    reasoning level in its ``<your-model>`` metadata tag."""
    agent_mock = MagicMock()
    agent_mock.run = AsyncMock(return_value=_result(_send()))
    engine, _ = _make_engine(
        _override("gpt-5-4", reasoning_level="high"), fake_redis
    )

    with patch(
        "smarter_dev.bot.services.chat_engine.get_chat_agent",
        return_value=agent_mock,
    ), _patches(agent_mock=agent_mock, fake_memory=fake_memory)[1], _patches(
        agent_mock=agent_mock, fake_memory=fake_memory
    )[2], patch(
        "smarter_dev.bot.services.chat_engine.over_budget_reset_epoch",
        new=AsyncMock(return_value=None),
    ), patch(
        "smarter_dev.bot.services.chat_engine.add_usage", new=AsyncMock()
    ):
        await engine._run_once(first_activation=True)

    user_prompt = agent_mock.run.await_args.kwargs["user_prompt"]
    assert '<your-model id="gpt-5.4"' in user_prompt
    assert 'reasoning-level="high"' in user_prompt


def _temporary_default_payload(
    model_key: str = "gemini-3-6-flash", reasoning_level: str | None = "high"
) -> str:
    return json.dumps(
        {
            "model_key": model_key,
            "reasoning_level": reasoning_level,
            "expires_at_epoch": 1_800_000_000,
        }
    )


@pytest.mark.asyncio
async def test_temporary_default_applies_without_channel_override(
    fake_memory, fake_redis
):
    """With no channel override, an active temporary default-model override
    picks the turn's model + reasoning and is the persisted model name."""
    agent_mock = MagicMock()
    agent_mock.run = AsyncMock(return_value=_result(_send()))
    fake_redis.get = AsyncMock(return_value=_temporary_default_payload())
    engine, _ = _make_engine(None, fake_redis)

    persist_turn = AsyncMock()
    get_agent = patch(
        "smarter_dev.bot.services.chat_engine.get_chat_agent",
        return_value=agent_mock,
    )
    with get_agent as get_agent_mock, _patches(
        agent_mock=agent_mock, fake_memory=fake_memory
    )[1], _patches(agent_mock=agent_mock, fake_memory=fake_memory)[2], patch(
        "smarter_dev.bot.services.chat_engine.add_usage", new=AsyncMock()
    ), patch(
        "smarter_dev.bot.services.chat_engine.start_engagement",
        new=AsyncMock(return_value="engagement-1"),
    ), patch(
        "smarter_dev.bot.services.chat_engine.persist_turn", new=persist_turn
    ):
        await engine._run_once(first_activation=True)

    get_agent_mock.assert_called_once_with("gemini-3.6-flash", "high")
    assert persist_turn.await_args.kwargs["chat_model_name"] == "gemini-3.6-flash"


@pytest.mark.asyncio
async def test_channel_override_wins_over_temporary_default(
    fake_memory, fake_redis
):
    """A channel's own override stays authoritative while a temporary
    bot-wide default override is active."""
    agent_mock = MagicMock()
    agent_mock.run = AsyncMock(return_value=_result(_send()))
    fake_redis.get = AsyncMock(return_value=_temporary_default_payload())
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


@pytest.mark.asyncio
async def test_temporary_default_with_stale_key_uses_configured_default(
    fake_memory, fake_redis
):
    """A temporary default naming a retired catalog key degrades to the
    configured default model instead of breaking the turn."""
    agent_mock = MagicMock()
    agent_mock.run = AsyncMock(return_value=_result(_send()))
    fake_redis.get = AsyncMock(
        return_value=_temporary_default_payload(model_key="gemini-3-5-flash")
    )
    engine, _ = _make_engine(None, fake_redis)

    get_agent = patch(
        "smarter_dev.bot.services.chat_engine.get_chat_agent",
        return_value=agent_mock,
    )
    with get_agent as get_agent_mock, _patches(
        agent_mock=agent_mock, fake_memory=fake_memory
    )[1], _patches(agent_mock=agent_mock, fake_memory=fake_memory)[2], patch(
        "smarter_dev.bot.services.chat_engine.add_usage", new=AsyncMock()
    ):
        await engine._run_once(first_activation=True)

    get_agent_mock.assert_called_once_with(None, None)


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


# --------------------------------------------------------------------------- #
# Feature 1 — response-filter gate
# --------------------------------------------------------------------------- #


def _fake_message(message_id, *, author_id=200, username="alice", content="hello"):
    """A minimal hikari-message stand-in for gate/queue plumbing."""
    return SimpleNamespace(
        id=message_id,
        author=SimpleNamespace(id=author_id, username=username, is_bot=False),
        content=content,
        created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_response_filter_drops_activation_skips_turn(fake_memory, fake_redis):
    """The gate dropping the activation skips the turn entirely: no model call,
    no budget spend, no no-response strike, engagement retained, engine active."""
    agent_mock = MagicMock()
    agent_mock.run = AsyncMock(return_value=_result(_send()))
    engine, _ = _make_engine(
        _override("gpt-5-4", response_filter="only python questions"), fake_redis
    )
    start_engagement = AsyncMock(return_value="engagement-1")

    with _patches(agent_mock=agent_mock, fake_memory=fake_memory)[0], _patches(
        agent_mock=agent_mock, fake_memory=fake_memory
    )[1], _patches(agent_mock=agent_mock, fake_memory=fake_memory)[2], patch(
        "smarter_dev.bot.services.chat_engine.over_budget_reset_epoch",
        new=AsyncMock(return_value=None),
    ) as over_budget, patch(
        "smarter_dev.bot.services.chat_engine.add_usage", new=AsyncMock()
    ) as add_usage_mock, patch(
        "smarter_dev.bot.services.chat_engine.filter_messages",
        new=AsyncMock(return_value=[]),
    ), patch(
        "smarter_dev.bot.services.chat_engine.start_engagement", new=start_engagement
    ):
        consumed = await engine._run_once(first_activation=True)

    # first_activation retained (mirrors the over-budget skip contract).
    assert consumed is False
    agent_mock.run.assert_not_called()
    add_usage_mock.assert_not_called()
    over_budget.assert_not_called()  # gate short-circuits before the budget check
    start_engagement.assert_not_awaited()
    assert engine.active is True
    assert engine.consecutive_no_response == 0


@pytest.mark.asyncio
async def test_response_filter_first_activation_starts_from_later_ontopic_message(
    fake_memory, fake_redis
):
    """Regression: when the activating message is off-topic but a message queued
    afterwards is on-topic, the first-activation gate must judge the queued
    messages too and start the engagement from the earliest survivor — not
    freeze forever on the dropped activation trigger."""
    agent_mock = MagicMock()
    agent_mock.run = AsyncMock(return_value=_result(_send()))
    engine, _ = _make_engine(
        _override("gpt-5-4", daily=1000, response_filter="only python questions"),
        fake_redis,
    )
    off_topic_trigger = _fake_message(700, content="what's for lunch")
    on_topic_followup = _fake_message(701, content="how do I use asyncio")
    engine.activation_message = off_topic_trigger
    engine.queue = [
        _QueuedMessage(message=on_topic_followup, enqueued_at=datetime.now(UTC)),
    ]

    with _patches(agent_mock=agent_mock, fake_memory=fake_memory)[0], _patches(
        agent_mock=agent_mock, fake_memory=fake_memory
    )[1], _patches(agent_mock=agent_mock, fake_memory=fake_memory)[2], patch(
        "smarter_dev.bot.services.chat_engine.over_budget_reset_epoch",
        new=AsyncMock(return_value=None),
    ), patch(
        "smarter_dev.bot.services.chat_engine.add_usage", new=AsyncMock()
    ), patch(
        "smarter_dev.bot.services.chat_engine.filter_messages",
        new=AsyncMock(return_value=["701"]),  # only the on-topic follow-up survives
    ):
        consumed = await engine._run_once(first_activation=True)

    # The engagement starts: the model ran and the survivor became the trigger.
    assert consumed is True
    agent_mock.run.assert_awaited_once()
    assert engine.activation_message is on_topic_followup


@pytest.mark.asyncio
async def test_response_filter_partial_drop_only_survivors_reach_agent(
    fake_memory, fake_redis
):
    """On a follow-up, only the messages the gate allows are handed to the
    agent input; the dropped ones never reach the model."""
    agent_mock = MagicMock()
    agent_mock.run = AsyncMock(return_value=_result(_send()))
    engine, _ = _make_engine(
        _override("gpt-5-4", daily=1000, response_filter="only python questions"),
        fake_redis,
    )
    drop_msg = _fake_message(500, content="what's for lunch")
    keep_msg = _fake_message(501, content="how do I use asyncio")
    engine.queue = [
        _QueuedMessage(message=drop_msg, enqueued_at=datetime.now(UTC)),
        _QueuedMessage(message=keep_msg, enqueued_at=datetime.now(UTC)),
    ]

    build_followup = AsyncMock(
        return_value=FollowupAgentInput(
            me=Me(user_id="999", username="bot"),
            new_messages=[
                Message(message_id="501", author_id="200", body="how do I use asyncio")
            ],
            authors=[Author(user_id="200", username="alice")],
            channel=ChannelInfo(channel_id="1", name="general"),
            now_utc=datetime.now(UTC),
        )
    )

    with _patches(agent_mock=agent_mock, fake_memory=fake_memory)[0], _patches(
        agent_mock=agent_mock, fake_memory=fake_memory
    )[1], patch(
        "smarter_dev.bot.services.chat_engine.build_followup_input",
        new=build_followup,
    ), patch(
        "smarter_dev.bot.services.chat_engine.over_budget_reset_epoch",
        new=AsyncMock(return_value=None),
    ), patch(
        "smarter_dev.bot.services.chat_engine.add_usage", new=AsyncMock()
    ), patch(
        "smarter_dev.bot.services.chat_engine.filter_messages",
        new=AsyncMock(return_value=["501"]),
    ):
        await engine._run_once(first_activation=False)

    agent_mock.run.assert_awaited_once()
    queued = build_followup.await_args.kwargs["queued"]
    assert [m.id for m in queued] == [501]


@pytest.mark.asyncio
async def test_response_filter_gate_error_runs_turn_unfiltered(
    fake_memory, fake_redis
):
    """An unexpected gate error must not silence the bot — the turn runs with
    every candidate (fail-open at the engine's call site)."""
    agent_mock = MagicMock()
    agent_mock.run = AsyncMock(return_value=_result(_send()))
    engine, _ = _make_engine(
        _override("gpt-5-4", daily=1000, response_filter="only python questions"),
        fake_redis,
    )

    with _patches(agent_mock=agent_mock, fake_memory=fake_memory)[0], _patches(
        agent_mock=agent_mock, fake_memory=fake_memory
    )[1], _patches(agent_mock=agent_mock, fake_memory=fake_memory)[2], patch(
        "smarter_dev.bot.services.chat_engine.over_budget_reset_epoch",
        new=AsyncMock(return_value=None),
    ), patch(
        "smarter_dev.bot.services.chat_engine.add_usage", new=AsyncMock()
    ), patch(
        "smarter_dev.bot.services.chat_engine.filter_messages",
        new=AsyncMock(side_effect=RuntimeError("gate blew up")),
    ):
        consumed = await engine._run_once(first_activation=True)

    assert consumed is True
    agent_mock.run.assert_awaited_once()


@pytest.mark.asyncio
async def test_gate_receives_channel_name(fake_redis):
    """The gate must see the channel/thread name — for a forum post it is the
    title, often the only statement of what the conversation is about."""
    engine, _ = _make_engine(
        _override("gpt-5-4", response_filter="only python questions"), fake_redis
    )
    gate = AsyncMock(return_value=[])

    with patch(
        "smarter_dev.bot.services.chat_engine.filter_messages", new=gate
    ), patch(
        "smarter_dev.bot.services.chat_engine.fetch_channel_info",
        new=AsyncMock(return_value={"channel_name": "How Does Logits Work?"}),
    ), patch.object(
        engine, "_fetch_gate_grounding", new=AsyncMock(return_value=[])
    ):
        await engine._gate_allows("only python questions", [_fake_message(700)])

    assert gate.await_args.kwargs["channel_name"] == "How Does Logits Work?"


@pytest.mark.asyncio
async def test_gate_channel_name_lookup_failure_degrades_to_none(fake_redis):
    """A channel-info failure must not break the gate — it judges without the
    name rather than erroring the turn."""
    engine, _ = _make_engine(
        _override("gpt-5-4", response_filter="only python questions"), fake_redis
    )
    gate = AsyncMock(return_value=[])

    with patch(
        "smarter_dev.bot.services.chat_engine.filter_messages", new=gate
    ), patch(
        "smarter_dev.bot.services.chat_engine.fetch_channel_info",
        new=AsyncMock(side_effect=RuntimeError("discord hiccup")),
    ), patch.object(
        engine, "_fetch_gate_grounding", new=AsyncMock(return_value=[])
    ):
        await engine._gate_allows("only python questions", [_fake_message(700)])

    assert gate.await_args.kwargs["channel_name"] is None


# --------------------------------------------------------------------------- #
# Feature 2 — fallback model on budget exhaustion
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_fallback_flag_skips_budget_and_uses_fallback_model(
    fake_memory, fake_redis
):
    """With the fallback flag set, the budget check is skipped, the fallback
    model runs, and its tokens are metered into the display-only fallback
    windows (visible in /bot-usage) — never the enforced windows, so the free
    opt-in cannot re-trip the primary's cap when the fallback closes."""
    agent_mock = MagicMock()
    agent_mock.run = AsyncMock(
        return_value=_result(_send(), input_tokens=10, output_tokens=5)
    )
    engine, _ = _make_engine(
        _override("kimi-k2-6", hourly=100, fallback_model_key="gpt-5-4"), fake_redis
    )
    fake_redis.exists = AsyncMock(return_value=1)  # fallback flag present

    get_agent = patch(
        "smarter_dev.bot.services.chat_engine.get_chat_agent",
        return_value=agent_mock,
    )
    over_budget = AsyncMock(return_value=1_800_000_000)  # would block if consulted
    with get_agent as get_agent_mock, _patches(
        agent_mock=agent_mock, fake_memory=fake_memory
    )[1], _patches(agent_mock=agent_mock, fake_memory=fake_memory)[2], patch(
        "smarter_dev.bot.services.chat_engine.over_budget_reset_epoch",
        new=over_budget,
    ), patch(
        "smarter_dev.bot.services.chat_engine.add_usage", new=AsyncMock()
    ) as add_usage_mock, patch(
        "smarter_dev.bot.services.chat_engine.add_fallback_usage", new=AsyncMock()
    ) as add_fallback_usage_mock:
        await engine._run_once(first_activation=True)

    # The fallback model's wire id, reasoning left to its own default.
    get_agent_mock.assert_called_once_with("gpt-5.4", None)
    over_budget.assert_not_called()  # budget enforcement skipped entirely
    agent_mock.run.assert_awaited_once()
    # Free-fallback spend goes to the display-only windows, not the enforced ones.
    add_fallback_usage_mock.assert_awaited_once_with(fake_redis, "42", 15)
    add_usage_mock.assert_not_called()


@pytest.mark.asyncio
async def test_fallback_persisted_turn_records_fallback_model(fake_memory, fake_redis):
    """The persisted turn is priced against the fallback model actually used."""
    agent_mock = MagicMock()
    agent_mock.run = AsyncMock(
        return_value=_result(_send(), input_tokens=10, output_tokens=5)
    )
    engine, _ = _make_engine(
        _override("kimi-k2-6", hourly=100, fallback_model_key="gpt-5-4"), fake_redis
    )
    fake_redis.exists = AsyncMock(return_value=1)

    start_engagement = AsyncMock(return_value="engagement-1")
    persist_turn = AsyncMock()
    with _patches(agent_mock=agent_mock, fake_memory=fake_memory)[0], _patches(
        agent_mock=agent_mock, fake_memory=fake_memory
    )[1], _patches(agent_mock=agent_mock, fake_memory=fake_memory)[2], patch(
        "smarter_dev.bot.services.chat_engine.over_budget_reset_epoch",
        new=AsyncMock(return_value=1_800_000_000),
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
async def test_fallback_stale_key_falls_back_to_primary_model(
    fake_memory, fake_redis
):
    """A stale/unknown fallback key while the window is active degrades to the
    primary override model rather than crashing the turn."""
    agent_mock = MagicMock()
    agent_mock.run = AsyncMock(return_value=_result(_send()))
    engine, _ = _make_engine(
        _override(
            "kimi-k2-6", hourly=100, fallback_model_key="this-model-was-removed"
        ),
        fake_redis,
    )
    fake_redis.exists = AsyncMock(return_value=1)

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

    # Primary override model ("kimi-k2-6" -> wire id "kimi-k2.6"), not the stale
    # fallback key.
    get_agent_mock.assert_called_once_with("kimi-k2.6", None)
    agent_mock.run.assert_awaited_once()


@pytest.mark.asyncio
async def test_ended_marker_notifies_primary_restored_then_runs_primary(
    fake_memory, fake_redis
):
    """The fallback window having ended (flag gone, marker present) posts a
    "primary is back" notice, clears the marker, and runs the primary model."""
    agent_mock = MagicMock()
    agent_mock.run = AsyncMock(return_value=_result(_send()))
    engine, _ = _make_engine(
        _override("gpt-5-4", daily=1000, fallback_model_key="kimi-k2-6"), fake_redis
    )
    fake_redis.exists = AsyncMock(return_value=0)  # flag expired
    fake_redis.delete = AsyncMock(return_value=1)  # marker still present

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

    fake_redis.delete.assert_awaited_once()  # marker cleared
    # The primary model ran (its wire id), not the fallback.
    get_agent_mock.assert_called_once_with("gpt-5.4", None)
    # A "primary is back" notice was posted, naming the primary model.
    notices = [
        call.kwargs.get("content", "")
        for call in engine.bot.rest.create_message.await_args_list
    ]
    assert any("answering again" in text and "GPT-5.4" in text for text in notices)


@pytest.mark.asyncio
async def test_ended_marker_while_over_budget_does_not_announce_restored(
    fake_memory, fake_redis
):
    """Regression: with the fallback flag gone and the ended-marker present but
    the channel still over budget, the turn must NOT announce "primary is back"
    (it would immediately be contradicted by the budget-exhausted notice). The
    marker survives so the genuine restoration is announced on a later turn."""
    agent_mock = MagicMock()
    agent_mock.run = AsyncMock(return_value=_result(_send()))
    engine, _ = _make_engine(
        _override("gpt-5-4", daily=1000, fallback_model_key="kimi-k2-6"), fake_redis
    )
    fake_redis.exists = AsyncMock(return_value=0)  # fallback flag expired
    fake_redis.delete = AsyncMock(return_value=1)  # marker would clear if touched

    with _patches(agent_mock=agent_mock, fake_memory=fake_memory)[0], _patches(
        agent_mock=agent_mock, fake_memory=fake_memory
    )[1], _patches(agent_mock=agent_mock, fake_memory=fake_memory)[2], patch(
        "smarter_dev.bot.services.chat_engine.over_budget_reset_epoch",
        new=AsyncMock(return_value=1_800_000_000),  # still over budget
    ), patch(
        "smarter_dev.bot.services.chat_engine.add_usage", new=AsyncMock()
    ):
        consumed = await engine._run_once(first_activation=True)

    assert consumed is False
    agent_mock.run.assert_not_called()  # turn skipped for budget
    # The ended-marker was never touched (the restoration notice is deferred).
    fake_redis.delete.assert_not_awaited()
    notices = [
        call.kwargs.get("content", "")
        for call in engine.bot.rest.create_message.await_args_list
    ]
    # No "primary is back" notice; only the budget-exhausted one.
    assert not any("answering again" in text for text in notices)
    assert any("token budget is used up" in text for text in notices)


@pytest.mark.asyncio
async def test_budget_exhausted_notice_carries_fallback_button(
    fake_memory, fake_redis
):
    """The over-budget notice carries the fallback-offer button when a fallback
    model is configured; the button's custom_id carries the reset epoch."""
    agent_mock = MagicMock()
    agent_mock.run = AsyncMock(return_value=_result(_send()))
    engine, _ = _make_engine(
        _override("gpt-5-4", hourly=100, fallback_model_key="kimi-k2-6"), fake_redis
    )

    with _patches(agent_mock=agent_mock, fake_memory=fake_memory)[0], _patches(
        agent_mock=agent_mock, fake_memory=fake_memory
    )[1], _patches(agent_mock=agent_mock, fake_memory=fake_memory)[2], patch(
        "smarter_dev.bot.services.chat_engine.over_budget_reset_epoch",
        new=AsyncMock(return_value=1_800_000_000),
    ), patch(
        "smarter_dev.bot.services.chat_engine.add_usage", new=AsyncMock()
    ):
        consumed = await engine._run_once(first_activation=True)

    assert consumed is False
    engine.bot.rest.create_message.assert_awaited_once()
    kwargs = engine.bot.rest.create_message.await_args.kwargs
    row = kwargs["components"][0]
    button = row.components[0]
    assert button.custom_id == "model_budget_fallback:1800000000"


@pytest.mark.asyncio
async def test_budget_exhausted_notice_has_no_button_without_fallback(
    fake_memory, fake_redis
):
    """Without a configured fallback, the over-budget notice posts plain (no
    components), unchanged from the pre-feature behaviour."""
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
    ):
        await engine._run_once(first_activation=True)

    engine.bot.rest.create_message.assert_awaited_once()
    kwargs = engine.bot.rest.create_message.await_args.kwargs
    assert "components" not in kwargs
