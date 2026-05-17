"""Tests for the ``ChannelEngine`` queue/activation/deactivation logic.

The agent run itself is patched out — these tests verify the *plumbing*:
- initial activation fires the agent immediately
- queued messages trigger refires at the 5s idle threshold
- queued messages trigger an immediate refire at the 15-message threshold
- 3 consecutive NoResponse turns deactivate the engine
- ``continue_watching=False`` deactivates the engine
- a stop-phrase message deactivates silently and arms cooldown
- ``has_attachments`` flag and reply mapping pass through correctly
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
    IDLE_FIRE_SECONDS,
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


def _empty_input() -> AgentInput:
    return AgentInput(
        me=Me(user_id="999", username="bot"),
        messages=[
            Message(
                message_id="100",
                author_id="200",
                body="hi",
            )
        ],
        authors=[Author(user_id="200", username="alice")],
        channel=ChannelInfo(channel_id="1", name="general"),
        now_utc=datetime.now(UTC),
    )


@pytest.fixture
def fake_memory():
    """In-memory stand-in for ChatMemory — only the methods the engine uses."""
    m = MagicMock()
    m.reset_idle_counter = AsyncMock()
    m.write_topic = AsyncMock()
    m.write_notes = AsyncMock()
    m.clear_notes = AsyncMock()
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


@pytest.mark.asyncio
async def test_initial_activation_fires_agent_exactly_once(fake_bot, fake_memory):
    """Regression: trigger_initial used to leave the fire event set, causing
    the runner to fire a second time immediately after the first activation —
    the bot ended up replying to its own message."""
    runs: list[bool] = []

    async def fake_run(*, user_prompt, deps):
        runs.append(True)
        return SimpleNamespace(
            output=SendResponse(
                message="hi there",
                topic="greeting",
                notes="user said hi",
            ),
            usage=lambda: None,
        )

    agent_mock = MagicMock()
    agent_mock.run = fake_run

    with patch("smarter_dev.bot.services.chat_engine.get_chat_agent", return_value=agent_mock), \
         patch("smarter_dev.bot.services.chat_engine.get_chat_memory", return_value=fake_memory), \
         patch(
             "smarter_dev.bot.services.chat_engine.build_agent_input",
             new=AsyncMock(return_value=_empty_input()),
         ):
        engine, _ = await _build_engine(fake_bot)
        engine.start()
        engine.trigger_initial()
        await asyncio.sleep(0.1)
        await engine.shutdown()

    assert len(runs) == 1, f"agent should fire exactly once on activation; fired {len(runs)} times"


@pytest.mark.asyncio
async def test_initial_activation_fires_agent_with_no_notes(fake_bot, fake_memory):
    captured: dict = {}

    async def fake_run(*, user_prompt, deps):
        captured["prompt"] = user_prompt
        captured["deps"] = deps
        return SimpleNamespace(
            output=SendResponse(
                message="hi there",
                topic="greeting",
                notes="user said hi",
            ),
            usage=lambda: None,
        )

    agent_mock = MagicMock()
    agent_mock.run = fake_run

    with patch("smarter_dev.bot.services.chat_engine.get_chat_agent", return_value=agent_mock), \
         patch("smarter_dev.bot.services.chat_engine.get_chat_memory", return_value=fake_memory), \
         patch(
             "smarter_dev.bot.services.chat_engine.build_agent_input",
             new=AsyncMock(return_value=_empty_input()),
         ) as input_builder:
        engine, _ = await _build_engine(fake_bot)
        engine.start()
        engine.trigger_initial()
        # Let the runner pick up the initial fire.
        await asyncio.sleep(0.05)
        await engine.shutdown()

    assert input_builder.await_count >= 1
    # First call must be the initial activation — no notes.
    assert input_builder.await_args_list[0].kwargs["include_notes"] is False
    fake_memory.reset_idle_counter.assert_awaited_with(42)
    fake_memory.write_topic.assert_awaited_with(42, "greeting")
    fake_memory.write_notes.assert_awaited_with(42, "user said hi")
    fake_bot.rest.create_message.assert_awaited()


@pytest.mark.asyncio
async def test_queue_threshold_fires_agent(fake_bot, fake_memory):
    """Pushing 15 messages onto the queue should fire the agent without waiting 5s."""
    runs: list[bool] = []

    async def fake_run(*, user_prompt, deps):
        runs.append(True)
        return SimpleNamespace(
            output=NoResponse(topic="nothing to add"),
            usage=lambda: None,
        )

    agent_mock = MagicMock()
    agent_mock.run = fake_run

    with patch("smarter_dev.bot.services.chat_engine.get_chat_agent", return_value=agent_mock), \
         patch("smarter_dev.bot.services.chat_engine.get_chat_memory", return_value=fake_memory), \
         patch(
             "smarter_dev.bot.services.chat_engine.build_agent_input",
             new=AsyncMock(return_value=_empty_input()),
         ):
        engine, _ = await _build_engine(fake_bot)
        engine.start()
        engine.trigger_initial()
        # Allow initial activation.
        await asyncio.sleep(0.05)
        initial_runs = len(runs)

        for i in range(QUEUE_FIRE_THRESHOLD):
            await engine.observe(_make_event(1000 + i, 200, f"chatter {i}"))
        # Give the runner a moment to pick up the fire (no 5s sleep).
        await asyncio.sleep(0.05)
        await engine.shutdown()

    assert len(runs) >= initial_runs + 1, "Queue-threshold fire didn't run the agent"


@pytest.mark.asyncio
async def test_no_response_deactivates_after_three_turns(fake_bot, fake_memory, monkeypatch):
    """Three consecutive NoResponse outputs should deactivate the engine."""
    # Disable the 5-second idle wait to keep the test fast.
    monkeypatch.setattr("smarter_dev.bot.services.chat_engine.IDLE_FIRE_SECONDS", 0)

    async def fake_run(*, user_prompt, deps):
        return SimpleNamespace(
            output=NoResponse(topic="quiet channel"),
            usage=lambda: None,
        )

    agent_mock = MagicMock()
    agent_mock.run = fake_run

    with patch("smarter_dev.bot.services.chat_engine.get_chat_agent", return_value=agent_mock), \
         patch("smarter_dev.bot.services.chat_engine.get_chat_memory", return_value=fake_memory), \
         patch(
             "smarter_dev.bot.services.chat_engine.build_agent_input",
             new=AsyncMock(return_value=_empty_input()),
         ):
        engine, deactivated = await _build_engine(fake_bot)
        engine.start()
        engine.trigger_initial()
        # Initial activation = turn 1 (NoResponse).
        await asyncio.sleep(0.05)
        # Two more turns triggered by observed messages.
        for i in range(MAX_NO_RESPONSE_TURNS):
            await engine.observe(_make_event(2000 + i, 200, f"meh {i}"))
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.05)
        await engine.shutdown()

    assert 42 in deactivated
    assert engine.active is False
    fake_memory.clear_notes.assert_awaited_with(42)


@pytest.mark.asyncio
async def test_continue_watching_false_deactivates(fake_bot, fake_memory):
    async def fake_run(*, user_prompt, deps):
        return SimpleNamespace(
            output=SendResponse(
                message="bye",
                topic="farewell",
                notes="user said bye",
                continue_watching=False,
            ),
            usage=lambda: None,
        )

    agent_mock = MagicMock()
    agent_mock.run = fake_run

    with patch("smarter_dev.bot.services.chat_engine.get_chat_agent", return_value=agent_mock), \
         patch("smarter_dev.bot.services.chat_engine.get_chat_memory", return_value=fake_memory), \
         patch(
             "smarter_dev.bot.services.chat_engine.build_agent_input",
             new=AsyncMock(return_value=_empty_input()),
         ):
        engine, deactivated = await _build_engine(fake_bot)
        engine.start()
        engine.trigger_initial()
        await asyncio.sleep(0.05)
        await engine.shutdown()

    assert 42 in deactivated
    assert engine.active is False


@pytest.mark.asyncio
async def test_stop_phrase_in_observe_deactivates_and_arms_cooldown(fake_bot, fake_memory, monkeypatch):
    cooldowns: list[int] = []

    def _track_cooldown(channel_id: int, duration_seconds: int = 300) -> None:
        cooldowns.append(channel_id)

    monkeypatch.setattr("smarter_dev.bot.services.chat_engine.set_channel_cooldown", _track_cooldown)

    async def fake_run(*, user_prompt, deps):
        return SimpleNamespace(
            output=SendResponse(
                message="hello",
                topic="greeting",
                notes="starting up",
            ),
            usage=lambda: None,
        )

    agent_mock = MagicMock()
    agent_mock.run = fake_run

    with patch("smarter_dev.bot.services.chat_engine.get_chat_agent", return_value=agent_mock), \
         patch("smarter_dev.bot.services.chat_engine.get_chat_memory", return_value=fake_memory), \
         patch(
             "smarter_dev.bot.services.chat_engine.build_agent_input",
             new=AsyncMock(return_value=_empty_input()),
         ):
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


@pytest.mark.asyncio
async def test_send_response_reply_to_message_id_passes_through(fake_bot, fake_memory):
    async def fake_run(*, user_prompt, deps):
        return SimpleNamespace(
            output=SendResponse(
                message="here you go",
                topic="answered",
                notes="answered question",
                reply_to_message_id="9999",
            ),
            usage=lambda: None,
        )

    agent_mock = MagicMock()
    agent_mock.run = fake_run

    with patch("smarter_dev.bot.services.chat_engine.get_chat_agent", return_value=agent_mock), \
         patch("smarter_dev.bot.services.chat_engine.get_chat_memory", return_value=fake_memory), \
         patch(
             "smarter_dev.bot.services.chat_engine.build_agent_input",
             new=AsyncMock(return_value=_empty_input()),
         ):
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

    async def fake_run(*, user_prompt, deps):
        return SimpleNamespace(
            output=SendResponse(
                voice_summary="async/await lets you write concurrent code that reads like sync",
                topic="async basics",
                notes="user wanted a voice explainer",
            ),
            usage=lambda: None,
        )

    agent_mock = MagicMock()
    agent_mock.run = fake_run

    with patch("smarter_dev.bot.services.chat_engine.get_chat_agent", return_value=agent_mock), \
         patch("smarter_dev.bot.services.chat_engine.get_chat_memory", return_value=fake_memory), \
         patch(
             "smarter_dev.bot.services.chat_engine.build_agent_input",
             new=AsyncMock(return_value=_empty_input()),
         ):
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

    async def fake_run(*, user_prompt, deps):
        return SimpleNamespace(
            output=SendResponse(
                message="Here's the long explanation with code...\n\n```python\nimport asyncio\n```",
                voice_summary="check the message — there's a Python example",
                topic="explained async with code",
                notes="user wanted both audio and code",
            ),
            usage=lambda: None,
        )

    agent_mock = MagicMock()
    agent_mock.run = fake_run

    with patch("smarter_dev.bot.services.chat_engine.get_chat_agent", return_value=agent_mock), \
         patch("smarter_dev.bot.services.chat_engine.get_chat_memory", return_value=fake_memory), \
         patch(
             "smarter_dev.bot.services.chat_engine.build_agent_input",
             new=AsyncMock(return_value=_empty_input()),
         ):
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
