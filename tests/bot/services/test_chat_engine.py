"""Tests for the ``ChannelEngine`` queue/activation/deactivation logic.

The agent run itself is patched out — these tests verify the *plumbing*:
- initial activation fires the agent exactly once (regression: it used to
  fire twice because trigger_initial left the event set)
- queued messages trigger refires at the 15-message threshold
- 3 consecutive NoResponse turns deactivate the engine
- ``continue_watching=False`` deactivates the engine
- a stop-phrase message deactivates silently and arms cooldown
- ``reply_to_message_id`` is forwarded to the Discord REST call
- voice-only and text+voice outputs dispatch correctly
- initial activation uses ``build_initial_input``; follow-ups use
  ``build_followup_input`` with the drained queue
- the engine round-trips Pydantic AI's ``message_history`` through memory
- deactivation clears both notes AND history
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from smarter_dev.bot.agents.chat_models import (
    AgentInput,
    Author,
    ChannelInfo,
    Me,
    Message,
    NoResponse,
    SendResponse,
)
from smarter_dev.bot.services.chat_engine import (
    MAX_NO_RESPONSE_TURNS,
    QUEUE_FIRE_THRESHOLD,
    ChannelEngine,
)


def _make_event(message_id: int, author_id: int, content: str):
    """Build a minimal MessageCreateEvent-like object for ``observe``."""
    return SimpleNamespace(
        message=SimpleNamespace(id=message_id),
        author=SimpleNamespace(id=author_id),
        content=content,
    )


def _initial_input() -> AgentInput:
    return AgentInput(
        me=Me(user_id="999", username="bot"),
        new_messages=[
            Message(
                message_id="100",
                author_id="200",
                body="hi",
            )
        ],
        authors=[Author(user_id="200", username="alice")],
        channel=ChannelInfo(channel_id="1", name="general"),
        now_utc=datetime.now(UTC),
        is_initial_activation=True,
    )


def _followup_input() -> AgentInput:
    return AgentInput(
        me=Me(user_id="999", username="bot"),
        new_messages=[
            Message(
                message_id="101",
                author_id="200",
                body="follow-up",
            )
        ],
        authors=[Author(user_id="200", username="alice")],
        channel=ChannelInfo(channel_id="1", name="general"),
        now_utc=datetime.now(UTC),
        is_initial_activation=False,
    )


@pytest.fixture
def fake_memory():
    """In-memory stand-in for ChatMemory — only the methods the engine uses."""
    m = MagicMock()
    m.reset_idle_counter = AsyncMock()
    m.write_topic = AsyncMock()
    m.write_notes = AsyncMock()
    m.clear_notes = AsyncMock()
    m.read_history = AsyncMock(return_value=[])
    m.write_history = AsyncMock()
    m.clear_history = AsyncMock()
    m.topic_for_activation = AsyncMock(return_value=None)
    m.get_notes = AsyncMock(return_value=None)
    return m


@pytest.fixture
def fake_bot():
    bot = MagicMock()
    bot.rest = MagicMock()
    bot.rest.create_message = AsyncMock()
    return bot


async def _build_engine(bot, voice_send=None):
    deactivated: list[int] = []

    async def _on_deactivate(channel_id: int) -> None:
        deactivated.append(channel_id)

    async def _noop_voice(channel_id, text, reply_to):
        pass

    engine = ChannelEngine(
        bot=bot,
        channel_id=42,
        guild_id=99,
        voice_send=voice_send or _noop_voice,
        on_deactivate=_on_deactivate,
    )
    return engine, deactivated


def _patch_engine(
    *,
    agent_run: AsyncMock | MagicMock,
    fake_memory,
    initial_builder_result=None,
    followup_builder_result=None,
):
    """Patch the three module-level symbols the engine pulls in."""
    agent_mock = MagicMock()
    agent_mock.run = agent_run
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
            new=AsyncMock(return_value=initial_builder_result or _initial_input()),
        ),
        patch(
            "smarter_dev.bot.services.chat_engine.build_followup_input",
            new=AsyncMock(return_value=followup_builder_result or _followup_input()),
        ),
    ]


def _result(output, all_messages=None):
    return SimpleNamespace(
        output=output,
        usage=lambda: None,
        all_messages=lambda: all_messages or [],
    )


@pytest.mark.asyncio
async def test_initial_activation_fires_agent_exactly_once(fake_bot, fake_memory):
    """Regression: trigger_initial used to leave the fire event set, causing
    the runner to fire a second time immediately after the first activation —
    the bot ended up replying to its own message."""
    runs: list[bool] = []

    async def fake_run(*, user_prompt, message_history, deps):
        runs.append(True)
        return _result(
            SendResponse(message="hi there", topic="greeting", notes="user said hi")
        )

    patches = _patch_engine(agent_run=fake_run, fake_memory=fake_memory)
    with patches[0], patches[1], patches[2], patches[3]:
        engine, _ = await _build_engine(fake_bot)
        engine.start()
        engine.trigger_initial()
        await asyncio.sleep(0.1)
        await engine.shutdown()

    assert len(runs) == 1, f"agent should fire exactly once; fired {len(runs)} times"


@pytest.mark.asyncio
async def test_initial_activation_calls_initial_builder(fake_bot, fake_memory):
    """First activation uses build_initial_input with empty history."""
    captured: dict = {}

    async def fake_run(*, user_prompt, message_history, deps):
        captured["history"] = message_history
        return _result(
            SendResponse(message="hi", topic="greeting", notes="user said hi"),
            all_messages=["fake_msg_a", "fake_msg_b"],
        )

    initial_input = _initial_input()
    patches = _patch_engine(
        agent_run=fake_run,
        fake_memory=fake_memory,
        initial_builder_result=initial_input,
    )
    with patches[0], patches[1], patches[2] as initial_builder, patches[3] as followup_builder:
        engine, _ = await _build_engine(fake_bot)
        engine.start()
        engine.trigger_initial()
        await asyncio.sleep(0.05)
        await engine.shutdown()

    initial_builder.assert_awaited_once()
    followup_builder.assert_not_awaited()
    assert captured["history"] == []
    fake_memory.reset_idle_counter.assert_awaited_with(42)
    fake_memory.write_topic.assert_awaited_with(42, "greeting")
    fake_memory.write_notes.assert_awaited_with(42, "user said hi")
    # Engine persists post-processor history after every turn.
    fake_memory.write_history.assert_awaited_with(
        42, ["fake_msg_a", "fake_msg_b"]
    )


@pytest.mark.asyncio
async def test_followup_turn_loads_history_and_uses_followup_builder(
    fake_bot, fake_memory
):
    """A queued-message-triggered fire loads history from memory and uses
    build_followup_input — not build_initial_input."""
    history_calls: list = []

    async def fake_run(*, user_prompt, message_history, deps):
        history_calls.append(list(message_history))
        return _result(
            SendResponse(message="ack", topic="ongoing", notes="tracking thread"),
            all_messages=["msg1", "msg2"],
        )

    fake_memory.read_history.return_value = ["prior_a", "prior_b"]

    patches = _patch_engine(agent_run=fake_run, fake_memory=fake_memory)
    with patches[0], patches[1], patches[2] as initial_builder, patches[3] as followup_builder:
        engine, _ = await _build_engine(fake_bot)
        engine.start()
        # Initial activation (turn 1)
        engine.trigger_initial()
        await asyncio.sleep(0.05)
        # Follow-up triggered by queue-threshold
        for i in range(QUEUE_FIRE_THRESHOLD):
            await engine.observe(_make_event(2000 + i, 200, f"chatter {i}"))
        await asyncio.sleep(0.1)
        await engine.shutdown()

    assert initial_builder.await_count == 1
    assert followup_builder.await_count >= 1
    # Turn 1 had no history; turn 2 picked up the prior history snapshot.
    assert history_calls[0] == []
    assert history_calls[1] == ["prior_a", "prior_b"]


@pytest.mark.asyncio
async def test_queue_threshold_fires_agent(fake_bot, fake_memory):
    """Pushing 15 messages onto the queue should fire the agent without waiting 5s."""
    runs: list[bool] = []

    async def fake_run(*, user_prompt, message_history, deps):
        runs.append(True)
        return _result(NoResponse(topic="nothing to add"))

    patches = _patch_engine(agent_run=fake_run, fake_memory=fake_memory)
    with patches[0], patches[1], patches[2], patches[3]:
        engine, _ = await _build_engine(fake_bot)
        engine.start()
        engine.trigger_initial()
        await asyncio.sleep(0.05)
        initial_runs = len(runs)

        for i in range(QUEUE_FIRE_THRESHOLD):
            await engine.observe(_make_event(1000 + i, 200, f"chatter {i}"))
        await asyncio.sleep(0.05)
        await engine.shutdown()

    assert len(runs) >= initial_runs + 1, "Queue-threshold fire didn't run the agent"


@pytest.mark.asyncio
async def test_no_response_deactivates_after_three_turns(
    fake_bot, fake_memory, monkeypatch
):
    """Three consecutive NoResponse outputs should deactivate the engine."""
    monkeypatch.setattr("smarter_dev.bot.services.chat_engine.IDLE_FIRE_SECONDS", 0)

    async def fake_run(*, user_prompt, message_history, deps):
        return _result(NoResponse(topic="quiet channel"))

    patches = _patch_engine(agent_run=fake_run, fake_memory=fake_memory)
    with patches[0], patches[1], patches[2], patches[3]:
        engine, deactivated = await _build_engine(fake_bot)
        engine.start()
        engine.trigger_initial()
        await asyncio.sleep(0.05)
        for i in range(MAX_NO_RESPONSE_TURNS):
            await engine.observe(_make_event(2000 + i, 200, f"meh {i}"))
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.05)
        await engine.shutdown()

    assert 42 in deactivated
    assert engine.active is False
    fake_memory.clear_notes.assert_awaited_with(42)
    fake_memory.clear_history.assert_awaited_with(42)


@pytest.mark.asyncio
async def test_continue_watching_false_deactivates(fake_bot, fake_memory):
    async def fake_run(*, user_prompt, message_history, deps):
        return _result(
            SendResponse(
                message="bye",
                topic="farewell",
                notes="user said bye",
                continue_watching=False,
            )
        )

    patches = _patch_engine(agent_run=fake_run, fake_memory=fake_memory)
    with patches[0], patches[1], patches[2], patches[3]:
        engine, deactivated = await _build_engine(fake_bot)
        engine.start()
        engine.trigger_initial()
        await asyncio.sleep(0.05)
        await engine.shutdown()

    assert 42 in deactivated
    assert engine.active is False
    fake_memory.clear_history.assert_awaited_with(42)


@pytest.mark.asyncio
async def test_stop_phrase_in_observe_deactivates_and_arms_cooldown(
    fake_bot, fake_memory, monkeypatch
):
    cooldowns: list[int] = []

    def _track_cooldown(channel_id: int, duration_seconds: int = 300) -> None:
        cooldowns.append(channel_id)

    monkeypatch.setattr(
        "smarter_dev.bot.services.chat_engine.set_channel_cooldown", _track_cooldown
    )

    async def fake_run(*, user_prompt, message_history, deps):
        return _result(
            SendResponse(message="hello", topic="greeting", notes="starting up")
        )

    patches = _patch_engine(agent_run=fake_run, fake_memory=fake_memory)
    with patches[0], patches[1], patches[2], patches[3]:
        engine, deactivated = await _build_engine(fake_bot)
        engine.start()
        engine.trigger_initial()
        await asyncio.sleep(0.05)
        await engine.observe(_make_event(5000, 200, "shut up"))
        await asyncio.sleep(0.05)
        await engine.shutdown()

    assert engine.active is False
    assert 42 in deactivated
    assert 42 in cooldowns
    fake_memory.clear_history.assert_awaited_with(42)


@pytest.mark.asyncio
async def test_send_response_reply_to_message_id_passes_through(fake_bot, fake_memory):
    async def fake_run(*, user_prompt, message_history, deps):
        return _result(
            SendResponse(
                message="here you go",
                topic="answered",
                notes="answered question",
                reply_to_message_id="9999",
            )
        )

    patches = _patch_engine(agent_run=fake_run, fake_memory=fake_memory)
    with patches[0], patches[1], patches[2], patches[3]:
        engine, _ = await _build_engine(fake_bot)
        engine.start()
        engine.trigger_initial()
        await asyncio.sleep(0.05)
        await engine.shutdown()

    call_kwargs = fake_bot.rest.create_message.await_args.kwargs
    assert call_kwargs.get("reply") == 9999


@pytest.mark.asyncio
async def test_voice_only_response_sends_voice_not_text(fake_bot, fake_memory):
    voice_calls: list[tuple] = []

    async def voice_send(channel_id, text, reply_to):
        voice_calls.append((channel_id, text, reply_to))

    async def fake_run(*, user_prompt, message_history, deps):
        return _result(
            SendResponse(
                voice_summary="async/await lets you write concurrent code that reads like sync",
                topic="async basics",
                notes="user wanted a voice explainer",
            )
        )

    patches = _patch_engine(agent_run=fake_run, fake_memory=fake_memory)
    with patches[0], patches[1], patches[2], patches[3]:
        engine, _ = await _build_engine(fake_bot, voice_send=voice_send)
        engine.start()
        engine.trigger_initial()
        await asyncio.sleep(0.05)
        await engine.shutdown()

    assert voice_calls
    assert "async" in voice_calls[0][1]
    fake_bot.rest.create_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_text_and_voice_dispatched_in_parallel(fake_bot, fake_memory):
    """Both channels populated → both sends happen for the same turn."""
    voice_calls: list[tuple] = []

    async def voice_send(channel_id, text, reply_to):
        voice_calls.append((channel_id, text, reply_to))

    async def fake_run(*, user_prompt, message_history, deps):
        return _result(
            SendResponse(
                message="Here's the long explanation with code...\n\n```python\nimport asyncio\n```",
                voice_summary="check the message — there's a Python example",
                topic="explained async with code",
                notes="user wanted both audio and code",
            )
        )

    patches = _patch_engine(agent_run=fake_run, fake_memory=fake_memory)
    with patches[0], patches[1], patches[2], patches[3]:
        engine, _ = await _build_engine(fake_bot, voice_send=voice_send)
        engine.start()
        engine.trigger_initial()
        await asyncio.sleep(0.05)
        await engine.shutdown()

    assert voice_calls, "expected voice send"
    fake_bot.rest.create_message.assert_awaited()
    assert "Python example" in voice_calls[0][1]


@pytest.mark.asyncio
async def test_send_response_requires_message_or_voice():
    """Validator: dropping both channels raises at construction time."""
    with pytest.raises(ValueError):
        SendResponse(topic="x", notes="y")
    with pytest.raises(ValueError):
        SendResponse(message="", voice_summary=None, topic="x", notes="y")
