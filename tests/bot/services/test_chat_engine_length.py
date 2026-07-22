"""Engine wiring tests for overlong-reply handling.

The agent run, memory, and input builders are patched — these verify the
engine splits a 2000–3000 char reply into two messages, and routes a >3000
char reply through ``fit_overlong_response`` (metering the rewrite's extra
tokens) before sending.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from smarter_dev.bot.agents.chat_models import Author
from smarter_dev.bot.agents.chat_models import ChannelInfo
from smarter_dev.bot.agents.chat_models import InitialAgentInput
from smarter_dev.bot.agents.chat_models import Me
from smarter_dev.bot.agents.chat_models import Message
from smarter_dev.bot.agents.chat_models import MessageScore
from smarter_dev.bot.agents.chat_models import ResponseBody
from smarter_dev.bot.agents.chat_models import TurnDecision
from smarter_dev.bot.agents.chat_tools import GeneratedImage
from smarter_dev.bot.agents.response_fitting import FitResult
from smarter_dev.bot.services.chat_engine import ChannelEngine


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


def _send(message: str) -> TurnDecision:
    return TurnDecision(
        rankings=[MessageScore(message_id="101", score=10, reasoning="direct")],
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


def _result(output, *, input_tokens=100, output_tokens=50):
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
    r.exists = AsyncMock(return_value=0)
    r.delete = AsyncMock(return_value=0)
    r.get = AsyncMock(return_value=None)
    pipe = MagicMock()
    pipe.execute = AsyncMock(return_value=[0, True])
    pipe.__aenter__ = AsyncMock(return_value=pipe)
    pipe.__aexit__ = AsyncMock(return_value=False)
    r.pipeline = MagicMock(return_value=pipe)
    return r


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
    engine.activation_message = SimpleNamespace(
        id=101, author=SimpleNamespace(id=200, username="alice")
    )
    return engine


def _patches(agent_mock, fake_memory):
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
        patch("smarter_dev.bot.services.chat_engine.add_usage", new=AsyncMock()),
    ]


@pytest.mark.asyncio
async def test_reply_between_2k_and_3k_is_sent_as_two_messages(
    fake_memory, fake_redis
):
    long_reply = "A" * 1200 + "\n" + "B" * 1290  # 2491 chars
    agent_mock = MagicMock()
    agent_mock.run = AsyncMock(return_value=_result(_send(long_reply)))
    engine = _make_engine(fake_redis)

    patches = _patches(agent_mock, fake_memory)
    with patches[0], patches[1], patches[2], patches[3]:
        await engine._run_once(first_activation=True)

    calls = engine.bot.rest.create_message.await_args_list
    assert len(calls) == 2
    contents = [call.kwargs["content"] for call in calls]
    assert contents[0] == "A" * 1200
    assert contents[1] == "B" * 1290
    assert all(len(content) <= 2000 for content in contents)


@pytest.mark.asyncio
async def test_reply_under_2k_is_sent_as_one_message(fake_memory, fake_redis):
    agent_mock = MagicMock()
    agent_mock.run = AsyncMock(return_value=_result(_send("short reply")))
    engine = _make_engine(fake_redis)

    patches = _patches(agent_mock, fake_memory)
    with patches[0], patches[1], patches[2], patches[3]:
        await engine._run_once(first_activation=True)

    engine.bot.rest.create_message.assert_awaited_once()
    assert (
        engine.bot.rest.create_message.await_args.kwargs["content"]
        == "short reply"
    )


@pytest.mark.asyncio
async def test_overlong_reply_is_fitted_and_extra_tokens_metered(
    fake_memory, fake_redis
):
    agent_mock = MagicMock()
    agent_mock.run = AsyncMock(return_value=_result(_send("z" * 3500)))
    engine = _make_engine(fake_redis)

    fit_mock = AsyncMock(
        return_value=FitResult("fitted reply", 20, 10, "summarized")
    )
    persist_turn = AsyncMock()
    patches = _patches(agent_mock, fake_memory)
    with patches[0], patches[1], patches[2], patch(
        "smarter_dev.bot.services.chat_engine.add_usage", new=AsyncMock()
    ) as add_usage_mock, patch(
        "smarter_dev.bot.services.chat_engine.fit_overlong_response", new=fit_mock
    ), patch(
        "smarter_dev.bot.services.chat_engine.start_engagement",
        new=AsyncMock(return_value="engagement-1"),
    ), patch(
        "smarter_dev.bot.services.chat_engine.persist_turn", new=persist_turn
    ):
        await engine._run_once(first_activation=True)

    fit_mock.assert_awaited_once()
    assert fit_mock.await_args.args[0] == "z" * 3500
    # The fitted text is what gets sent, in one message.
    engine.bot.rest.create_message.assert_awaited_once()
    assert (
        engine.bot.rest.create_message.await_args.kwargs["content"]
        == "fitted reply"
    )
    # Rewrite spend is metered (100+50 base + 20+10 extra) and persisted.
    add_usage_mock.assert_awaited_once_with(fake_redis, "42", 180)
    assert persist_turn.await_args.kwargs["chat_tokens_input"] == 120
    assert persist_turn.await_args.kwargs["chat_tokens_output"] == 60


@pytest.mark.asyncio
async def test_reply_at_threshold_is_not_rewritten(fake_memory, fake_redis):
    # Exactly 3000 chars: split into two messages, no model rewrite.
    reply = "C" * 1400 + "\n" + "D" * 1599  # 3000 chars
    agent_mock = MagicMock()
    agent_mock.run = AsyncMock(return_value=_result(_send(reply)))
    engine = _make_engine(fake_redis)

    fit_mock = AsyncMock()
    patches = _patches(agent_mock, fake_memory)
    with patches[0], patches[1], patches[2], patches[3], patch(
        "smarter_dev.bot.services.chat_engine.fit_overlong_response", new=fit_mock
    ):
        await engine._run_once(first_activation=True)

    fit_mock.assert_not_called()
    assert len(engine.bot.rest.create_message.await_args_list) == 2


@pytest.mark.asyncio
async def test_split_reply_puts_attachments_on_the_last_message(fake_redis):
    """Images ride on the second message of a split reply so an attachment
    never visually interrupts the text."""
    engine = _make_engine(fake_redis)
    image = GeneratedImage(
        data=b"PNGDATA", mime_type="image/png", filename="diagram.png"
    )

    ok = await engine._send_text(
        "A" * 1200 + "\n" + "B" * 1290, reply_to=101, images=[image]
    )

    assert ok is True
    calls = engine.bot.rest.create_message.await_args_list
    assert len(calls) == 2
    first, second = (call.kwargs for call in calls)
    assert first["content"] == "A" * 1200
    assert first["reply"] == 101
    assert "attachments" not in first
    assert second["content"] == "B" * 1290
    assert "reply" not in second
    assert len(second["attachments"]) == 1


@pytest.mark.asyncio
async def test_single_message_reply_keeps_attachments_on_it(fake_redis):
    engine = _make_engine(fake_redis)
    image = GeneratedImage(
        data=b"PNGDATA", mime_type="image/png", filename="diagram.png"
    )

    ok = await engine._send_text("short reply", reply_to=None, images=[image])

    assert ok is True
    engine.bot.rest.create_message.assert_awaited_once()
    kwargs = engine.bot.rest.create_message.await_args.kwargs
    assert kwargs["content"] == "short reply"
    assert len(kwargs["attachments"]) == 1


@pytest.mark.asyncio
async def test_failed_lead_message_fails_the_send(fake_redis):
    engine = _make_engine(fake_redis)
    engine.bot.rest.create_message = AsyncMock(side_effect=RuntimeError("down"))

    ok = await engine._send_text("A" * 1200 + "\n" + "B" * 1290, reply_to=None)

    assert ok is False
    engine.bot.rest.create_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_continuation_still_counts_as_sent(fake_redis):
    engine = _make_engine(fake_redis)
    engine.bot.rest.create_message = AsyncMock(
        side_effect=[None, RuntimeError("down")]
    )

    ok = await engine._send_text("A" * 1200 + "\n" + "B" * 1290, reply_to=None)

    assert ok is True
    assert len(engine.bot.rest.create_message.await_args_list) == 2
