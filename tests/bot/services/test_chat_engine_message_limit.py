"""Tests for the engine charging bot-directed messages to the per-user limit.

The agent run, memory, and input builders are patched — these verify the
wiring: after a turn, every ranked message scoring at or above the directed
threshold is recorded against its author's rolling message limit, and
nothing else is.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from redis.exceptions import RedisError

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
from smarter_dev.bot.services.chat_engine import ChannelEngine, _QueuedMessage


def _initial_input() -> InitialAgentInput:
    return InitialAgentInput(
        me=Me(user_id="999", username="bot"),
        channel_history=[],
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


def _decision(rankings: list[tuple[str, int]]) -> TurnDecision:
    target_id = next(mid for mid, score in rankings if score >= 5)
    return TurnDecision(
        rankings=[
            MessageScore(message_id=mid, score=score, reasoning="structural")
            for mid, score in rankings
        ],
        response_language="english",
        response=ResponseBody(
            target_message_id=target_id,
            reply_directly=False,
            message="hi there",
        ),
        topic="t",
        notes="n",
        continue_watching=True,
    )


def _result(output):
    usage = SimpleNamespace(
        input_tokens=0, output_tokens=0, requests=1, model="test-model"
    )
    return SimpleNamespace(
        output=output,
        usage=lambda: usage,
        all_messages=lambda: [],
        new_messages=lambda: [],
    )


def _discord_message(message_id: int, author_id: int, *, is_bot: bool = False):
    return SimpleNamespace(
        id=message_id,
        author=SimpleNamespace(id=author_id, username=f"user-{author_id}", is_bot=is_bot),
        created_at=datetime.now(UTC),
    )


def _make_engine(fake_redis) -> ChannelEngine:
    bot = MagicMock()
    bot.rest = MagicMock()
    bot.rest.create_message = AsyncMock()
    service = MagicMock()
    service.get_override = AsyncMock(return_value=None)
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
    engine.activation_message = _discord_message(101, 200)
    return engine


def _fake_memory():
    m = MagicMock()
    m.reset_idle_counter = AsyncMock()
    m.write_topic = AsyncMock()
    m.write_notes = AsyncMock()
    m.clear_notes = AsyncMock()
    m.read_history = AsyncMock(return_value=[])
    m.write_history = AsyncMock()
    m.clear_history = AsyncMock()
    return m


def _run_patches(decision: TurnDecision, record_mock: AsyncMock):
    agent_mock = MagicMock()
    agent_mock.run = AsyncMock(return_value=_result(decision))
    return [
        patch(
            "smarter_dev.bot.services.chat_engine.get_chat_agent",
            return_value=agent_mock,
        ),
        patch(
            "smarter_dev.bot.services.chat_engine.get_chat_memory",
            return_value=_fake_memory(),
        ),
        patch(
            "smarter_dev.bot.services.chat_engine.build_initial_input",
            new=AsyncMock(return_value=_initial_input()),
        ),
        patch(
            "smarter_dev.bot.services.chat_engine.record_directed_messages",
            new=record_mock,
        ),
    ]


async def _run(engine: ChannelEngine, decision: TurnDecision, record_mock: AsyncMock):
    patches = _run_patches(decision, record_mock)
    with patches[0], patches[1], patches[2], patches[3]:
        await engine._run_once(first_activation=True)


@pytest.mark.asyncio
async def test_directed_messages_are_charged_per_author():
    """Rankings at/above the threshold charge their authors; below stays free."""
    fake_redis = MagicMock()
    engine = _make_engine(fake_redis)
    # Two queued follow-ups from different users alongside the activation.
    engine.queue = [
        _QueuedMessage(message=_discord_message(102, 300), enqueued_at=datetime.now(UTC)),
        _QueuedMessage(message=_discord_message(103, 301), enqueued_at=datetime.now(UTC)),
    ]
    record_mock = AsyncMock()

    await _run(
        engine,
        _decision([("101", 10), ("102", 4), ("103", 7)]),
        record_mock,
    )

    charges = {
        call.args[1]: call.args[2] for call in record_mock.await_args_list
    }
    assert set(charges) == {"200", "301"}
    assert list(charges["200"]) == ["101"]
    assert list(charges["301"]) == ["103"]
    for message_epochs in charges.values():
        for epoch in message_epochs.values():
            assert epoch == pytest.approx(datetime.now(UTC).timestamp(), abs=5)


@pytest.mark.asyncio
async def test_rankings_for_unknown_messages_are_skipped():
    """A ranked id outside this turn's messages (e.g. pre-engagement history)
    charges nobody."""
    fake_redis = MagicMock()
    engine = _make_engine(fake_redis)
    record_mock = AsyncMock()

    await _run(engine, _decision([("101", 10), ("777", 9)]), record_mock)

    record_mock.assert_awaited_once()
    assert record_mock.await_args.args[1] == "200"
    assert list(record_mock.await_args.args[2]) == ["101"]


@pytest.mark.asyncio
async def test_bot_authored_messages_are_never_charged():
    fake_redis = MagicMock()
    engine = _make_engine(fake_redis)
    engine.queue = [
        _QueuedMessage(
            message=_discord_message(102, 999, is_bot=True),
            enqueued_at=datetime.now(UTC),
        ),
    ]
    record_mock = AsyncMock()

    await _run(engine, _decision([("101", 10), ("102", 8)]), record_mock)

    charged_users = {call.args[1] for call in record_mock.await_args_list}
    assert charged_users == {"200"}


@pytest.mark.asyncio
async def test_no_redis_skips_charging():
    engine = _make_engine(None)
    engine.bot.d = {"model_override_service": engine.bot.d["model_override_service"]}
    record_mock = AsyncMock()

    await _run(engine, _decision([("101", 10)]), record_mock)

    record_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_redis_error_never_breaks_the_turn():
    fake_redis = MagicMock()
    engine = _make_engine(fake_redis)
    record_mock = AsyncMock(side_effect=RedisError("down"))

    await _run(engine, _decision([("101", 10)]), record_mock)

    # The response still went out despite the failed charge.
    engine.bot.rest.create_message.assert_awaited()
