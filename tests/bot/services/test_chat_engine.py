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
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelRequest

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


def _send(
    message: str | None = None,
    *,
    topic: str = "t",
    notes: str | None = "n",
    target_message_id: str = "9001",
    reply_directly: bool = False,
    voice_summary: str | None = None,
    voice_instruction: str | None = None,
    continue_watching: bool = True,
) -> TurnDecision:
    """Build a TurnDecision that carries a populated response.

    Replaces the old ``SendResponse(...)`` test fixture shorthand. Defaults
    score the target at 10/10 so the validators are satisfied without the
    test having to think about rankings.
    """
    return TurnDecision(
        rankings=[
            MessageScore(
                message_id=target_message_id,
                score=10,
                reasoning="test fixture: assumed direct engagement",
            )
        ],
        response_language="english",
        response=ResponseBody(
            target_message_id=target_message_id,
            reply_directly=reply_directly,
            message=message,
            voice_summary=voice_summary,
            voice_instruction=voice_instruction,
        ),
        topic=topic,
        notes=notes,
        continue_watching=continue_watching,
    )


def _no_send(
    *,
    topic: str = "nothing to add",
    target_message_id: str = "9001",
    continue_watching: bool = True,
) -> TurnDecision:
    """Build a TurnDecision with no response — every score below 5."""
    return TurnDecision(
        rankings=[
            MessageScore(
                message_id=target_message_id,
                score=2,
                reasoning="test fixture: assumed bystander",
            )
        ],
        response_language="english",
        response=None,
        topic=topic,
        continue_watching=continue_watching,
    )
from smarter_dev.bot.services.chat_engine import (
    INACTIVITY_TIMEOUT,
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


def _fake_trigger_message(message_id: int = 9001, author_id: int = 200):
    """A stand-in hikari.Message used as the activation trigger in tests."""
    return SimpleNamespace(
        id=message_id,
        author=SimpleNamespace(id=author_id, username="alice"),
    )


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


def _followup_input() -> FollowupAgentInput:
    return FollowupAgentInput(
        me=Me(user_id="999", username="bot"),
        new_messages=[
            Message(
                message_id="102",
                author_id="200",
                body="follow-up",
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

    async def _noop_voice(channel_id, text, reply_to, instruction=None):
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


def _result(output, all_messages=None, new_messages=None):
    return SimpleNamespace(
        output=output,
        usage=lambda: None,
        all_messages=lambda: all_messages or [],
        new_messages=lambda: new_messages or [],
    )


@pytest.mark.asyncio
async def test_is_expired_reflects_inactivity_window(fake_bot):
    """``is_expired`` flips once last_sent_at is older than the timeout — the
    signal the registry uses to replace a stale engine instead of reusing it."""
    engine, _ = await _build_engine(fake_bot)
    assert engine.is_expired is False
    engine.last_sent_at = datetime.now(UTC) - INACTIVITY_TIMEOUT - timedelta(seconds=1)
    assert engine.is_expired is True


@pytest.mark.asyncio
async def test_initial_activation_fires_agent_exactly_once(fake_bot, fake_memory):
    """Regression: trigger_initial used to leave the fire event set, causing
    the runner to fire a second time immediately after the first activation —
    the bot ended up replying to its own message."""
    runs: list[bool] = []

    async def fake_run(*, user_prompt, message_history, deps):
        runs.append(True)
        return _result(_send("hi there", topic="greeting", notes="user said hi"))

    patches = _patch_engine(agent_run=fake_run, fake_memory=fake_memory)
    with patches[0], patches[1], patches[2], patches[3]:
        engine, _ = await _build_engine(fake_bot)
        engine.start()
        engine.trigger_initial(_fake_trigger_message())
        await asyncio.sleep(0.1)
        await engine.shutdown()

    assert len(runs) == 1, f"agent should fire exactly once; fired {len(runs)} times"


@pytest.mark.asyncio
async def test_initial_activation_calls_initial_builder(fake_bot, fake_memory):
    """First activation uses build_initial_input; the pre-engagement channel
    history is folded into message_history, one ModelRequest per message."""
    captured: dict = {}

    async def fake_run(*, user_prompt, message_history, deps):
        captured["history"] = message_history
        return _result(
            _send("hi", topic="greeting", notes="user said hi"),
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
        engine.trigger_initial(_fake_trigger_message())
        await asyncio.sleep(0.05)
        await engine.shutdown()

    initial_builder.assert_awaited_once()
    followup_builder.assert_not_awaited()
    # build_agent_call folds the builder's channel_history into
    # message_history: the one prior message becomes a ModelRequest, and the
    # activation message becomes the user_prompt.
    [prior_request] = captured["history"]
    assert isinstance(prior_request, ModelRequest)
    assert "prior message" in prior_request.parts[0].content
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
            _send("ack", topic="ongoing", notes="tracking thread"),
            all_messages=["msg1", "msg2"],
        )

    fake_memory.read_history.return_value = ["prior_a", "prior_b"]

    patches = _patch_engine(agent_run=fake_run, fake_memory=fake_memory)
    with patches[0], patches[1], patches[2] as initial_builder, patches[3] as followup_builder:
        engine, _ = await _build_engine(fake_bot)
        engine.start()
        # Initial activation (turn 1)
        engine.trigger_initial(_fake_trigger_message())
        await asyncio.sleep(0.05)
        # Follow-up triggered by queue-threshold
        for i in range(QUEUE_FIRE_THRESHOLD):
            await engine.observe(_make_event(2000 + i, 200, f"chatter {i}"))
        await asyncio.sleep(0.1)
        await engine.shutdown()

    assert initial_builder.await_count == 1
    assert followup_builder.await_count >= 1
    # Turn 1's history is the folded pre-engagement channel_history (one
    # ModelRequest); turn 2 picked up the prior history snapshot from memory.
    [turn_one_request] = history_calls[0]
    assert isinstance(turn_one_request, ModelRequest)
    assert history_calls[1] == ["prior_a", "prior_b"]


@pytest.mark.asyncio
async def test_queue_threshold_fires_agent(fake_bot, fake_memory):
    """Pushing 15 messages onto the queue should fire the agent without waiting 5s."""
    runs: list[bool] = []

    async def fake_run(*, user_prompt, message_history, deps):
        runs.append(True)
        return _result(_no_send(topic="nothing to add"))

    patches = _patch_engine(agent_run=fake_run, fake_memory=fake_memory)
    with patches[0], patches[1], patches[2], patches[3]:
        engine, _ = await _build_engine(fake_bot)
        engine.start()
        engine.trigger_initial(_fake_trigger_message())
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
        return _result(_no_send(topic="quiet channel"))

    patches = _patch_engine(agent_run=fake_run, fake_memory=fake_memory)
    with patches[0], patches[1], patches[2], patches[3]:
        engine, deactivated = await _build_engine(fake_bot)
        engine.start()
        engine.trigger_initial(_fake_trigger_message())
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
            _send(
                "bye",
                topic="farewell",
                notes="user said bye",
                continue_watching=False,
            )
        )

    patches = _patch_engine(agent_run=fake_run, fake_memory=fake_memory)
    with patches[0], patches[1], patches[2], patches[3]:
        engine, deactivated = await _build_engine(fake_bot)
        engine.start()
        engine.trigger_initial(_fake_trigger_message())
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
        return _result(_send("hello", topic="greeting", notes="starting up"))

    patches = _patch_engine(agent_run=fake_run, fake_memory=fake_memory)
    with patches[0], patches[1], patches[2], patches[3]:
        engine, deactivated = await _build_engine(fake_bot)
        engine.start()
        engine.trigger_initial(_fake_trigger_message())
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
            _send(
                "here you go",
                topic="answered",
                notes="answered question",
                target_message_id="9999",
                reply_directly=True,
            )
        )

    patches = _patch_engine(agent_run=fake_run, fake_memory=fake_memory)
    with patches[0], patches[1], patches[2], patches[3]:
        engine, _ = await _build_engine(fake_bot)
        engine.start()
        engine.trigger_initial(_fake_trigger_message())
        await asyncio.sleep(0.05)
        await engine.shutdown()

    call_kwargs = fake_bot.rest.create_message.await_args.kwargs
    assert call_kwargs.get("reply") == 9999


@pytest.mark.asyncio
async def test_voice_only_response_sends_voice_not_text(fake_bot, fake_memory):
    voice_calls: list[tuple] = []

    async def voice_send(channel_id, text, reply_to, instruction=None):
        voice_calls.append((channel_id, text, reply_to))

    async def fake_run(*, user_prompt, message_history, deps):
        return _result(
            _send(
                voice_summary="async/await lets you write concurrent code that reads like sync",
                topic="async basics",
                notes="user wanted a voice explainer",
            )
        )

    patches = _patch_engine(agent_run=fake_run, fake_memory=fake_memory)
    with patches[0], patches[1], patches[2], patches[3]:
        engine, _ = await _build_engine(fake_bot, voice_send=voice_send)
        engine.start()
        engine.trigger_initial(_fake_trigger_message())
        await asyncio.sleep(0.05)
        await engine.shutdown()

    assert voice_calls
    assert "async" in voice_calls[0][1]
    fake_bot.rest.create_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_text_and_voice_dispatched_in_parallel(fake_bot, fake_memory):
    """Both channels populated → both sends happen for the same turn."""
    voice_calls: list[tuple] = []

    async def voice_send(channel_id, text, reply_to, instruction=None):
        voice_calls.append((channel_id, text, reply_to))

    async def fake_run(*, user_prompt, message_history, deps):
        return _result(
            _send(
                "Here's the long explanation with code...\n\n```python\nimport asyncio\n```",
                voice_summary="check the message — there's a Python example",
                topic="explained async with code",
                notes="user wanted both audio and code",
            )
        )

    patches = _patch_engine(agent_run=fake_run, fake_memory=fake_memory)
    with patches[0], patches[1], patches[2], patches[3]:
        engine, _ = await _build_engine(fake_bot, voice_send=voice_send)
        engine.start()
        engine.trigger_initial(_fake_trigger_message())
        await asyncio.sleep(0.05)
        await engine.shutdown()

    assert voice_calls, "expected voice send"
    fake_bot.rest.create_message.assert_awaited()
    assert "Python example" in voice_calls[0][1]


@pytest.mark.asyncio
async def test_fire_now_re_triggers_an_active_engine(fake_bot, fake_memory):
    """Regression: a follow-up @mention on an active engine used to drop
    silently because trigger_initial set the fire event but the queue was
    empty, so _run_once short-circuited. The fix is observe()+fire_now()
    so the mention is queued AND the agent fires immediately."""
    runs: list[str] = []

    async def fake_run(*, user_prompt, message_history, deps):
        runs.append(user_prompt)
        return _result(
            _send("ack", topic="t", notes="n"),
            all_messages=["m1", "m2"],
        )

    patches = _patch_engine(agent_run=fake_run, fake_memory=fake_memory)
    with patches[0], patches[1], patches[2], patches[3]:
        engine, _ = await _build_engine(fake_bot)
        engine.start()
        engine.trigger_initial(_fake_trigger_message())
        await asyncio.sleep(0.05)
        initial_runs = len(runs)

        # Simulate a follow-up @mention: observe + fire_now (what mention.py
        # does when an active engine sees another engagement).
        await engine.observe(_make_event(7777, 200, "@bot follow-up"))
        engine.fire_now()
        await asyncio.sleep(0.05)
        await engine.shutdown()

    assert len(runs) >= initial_runs + 1, (
        "follow-up engagement didn't trigger another agent run — "
        "fire_now/observe path is broken"
    )


@pytest.mark.asyncio
async def test_voice_instruction_forwarded_to_voice_send(fake_bot, fake_memory):
    """SendResponse.voice_instruction reaches voice_send so the TTS service
    can use it as a stage direction."""
    voice_calls: list[tuple] = []

    async def voice_send(channel_id, text, reply_to, instruction=None):
        voice_calls.append((channel_id, text, reply_to, instruction))

    async def fake_run(*, user_prompt, message_history, deps):
        return _result(
            _send(
                voice_summary="bazinga",
                voice_instruction="Say this with mock-serious deadpan delivery",
                topic="t",
                notes="n",
            )
        )

    patches = _patch_engine(agent_run=fake_run, fake_memory=fake_memory)
    with patches[0], patches[1], patches[2], patches[3]:
        engine, _ = await _build_engine(fake_bot, voice_send=voice_send)
        engine.start()
        engine.trigger_initial(_fake_trigger_message())
        await asyncio.sleep(0.05)
        await engine.shutdown()

    assert voice_calls
    assert voice_calls[0][3] == "Say this with mock-serious deadpan delivery"


@pytest.mark.asyncio
async def test_voice_only_failure_posts_fallback(fake_bot, fake_memory):
    """If voice fails and there's no text alongside, the user is told —
    silence after a request would look like the agent ignored them.
    Regression for Discord 50173 (no SEND_VOICE_MESSAGES permission)."""

    async def voice_send(channel_id, text, reply_to, instruction=None):
        raise RuntimeError("Discord error 400: 50173")

    async def fake_run(*, user_prompt, message_history, deps):
        return _result(
            _send(
                voice_summary="here you go",
                topic="t",
                notes="n",
            )
        )

    patches = _patch_engine(agent_run=fake_run, fake_memory=fake_memory)
    with patches[0], patches[1], patches[2], patches[3]:
        engine, _ = await _build_engine(fake_bot, voice_send=voice_send)
        engine.start()
        engine.trigger_initial(_fake_trigger_message())
        await asyncio.sleep(0.05)
        await engine.shutdown()

    # The fallback "Couldn't send a voice message..." post should be the
    # only thing on the channel.
    fake_bot.rest.create_message.assert_awaited()
    posted = fake_bot.rest.create_message.await_args.kwargs.get("content", "")
    assert "voice message" in posted.lower()


@pytest.mark.asyncio
async def test_voice_failure_with_text_does_not_post_extra_fallback(fake_bot, fake_memory):
    """When voice fails but text was sent alongside, no extra error message
    is posted (the user already got the text reply)."""

    async def voice_send(channel_id, text, reply_to, instruction=None):
        raise RuntimeError("Discord error 400")

    async def fake_run(*, user_prompt, message_history, deps):
        return _result(
            _send(
                "here's the text",
                voice_summary="and a voice version",
                topic="t",
                notes="n",
            )
        )

    patches = _patch_engine(agent_run=fake_run, fake_memory=fake_memory)
    with patches[0], patches[1], patches[2], patches[3]:
        engine, _ = await _build_engine(fake_bot, voice_send=voice_send)
        engine.start()
        engine.trigger_initial(_fake_trigger_message())
        await asyncio.sleep(0.05)
        await engine.shutdown()

    # Exactly one create_message — the text reply, not a fallback.
    assert fake_bot.rest.create_message.await_count == 1
    posted = fake_bot.rest.create_message.await_args.kwargs.get("content", "")
    assert posted == "here's the text"


@pytest.mark.asyncio
async def test_agent_run_failure_posts_error_message(fake_bot, fake_memory):
    """If agent.run raises, the user sees a brief "couldn't generate a reply"
    note rather than silent nothing."""

    async def fake_run(*, user_prompt, message_history, deps):
        raise RuntimeError("provider exploded")

    patches = _patch_engine(agent_run=fake_run, fake_memory=fake_memory)
    with patches[0], patches[1], patches[2], patches[3]:
        engine, _ = await _build_engine(fake_bot)
        engine.start()
        engine.trigger_initial(_fake_trigger_message())
        await asyncio.sleep(0.05)
        await engine.shutdown()

    fake_bot.rest.create_message.assert_awaited()
    posted = fake_bot.rest.create_message.await_args.kwargs.get("content", "")
    assert "couldn't" in posted.lower() or "could not" in posted.lower()


@pytest.mark.asyncio
async def test_inference_http_error_posts_generic_admin_link(fake_bot, fake_memory):
    """Members see no provider detail; admins get the protected diagnostic URL."""

    async def fake_run(*, user_prompt, message_history, deps):
        raise ModelHTTPError(
            status_code=503,
            model_name="kimi-k2.6",
            body={"error": {"message": "upstream model is overloaded"}},
        )

    error_url = "https://smarter.dev/admin/chat-errors/error-id"
    persist = AsyncMock(return_value=error_url)
    patches = _patch_engine(agent_run=fake_run, fake_memory=fake_memory)
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patch(
            "smarter_dev.bot.services.chat_engine.persist_error",
            new=persist,
        ),
    ):
        engine, _ = await _build_engine(fake_bot)
        engine.start()
        engine.trigger_initial(_fake_trigger_message())
        await asyncio.sleep(0.05)
        await engine.shutdown()

    posted = fake_bot.rest.create_message.await_args.kwargs.get("content", "")
    assert "couldn't generate a reply" in posted
    assert "Admin diagnostics" in posted
    assert error_url in posted
    assert "kimi-k2.6" not in posted
    assert "HTTP 503" not in posted
    assert "upstream model is overloaded" not in posted
    persist.assert_awaited_once()
    persisted = persist.await_args.kwargs
    assert isinstance(persisted["error"], ModelHTTPError)
    assert persisted["model_name"] == "kimi-k2.6"


def test_response_body_requires_message_or_voice():
    """Validator: dropping both channels raises at construction time."""
    with pytest.raises(ValueError):
        ResponseBody(target_message_id="1")
    with pytest.raises(ValueError):
        ResponseBody(target_message_id="1", message="", voice_summary=None)


def test_turn_decision_rejects_response_without_qualifying_score():
    """response populated but no ranking >=5 must fail at construction."""
    with pytest.raises(ValueError):
        TurnDecision(
            rankings=[
                MessageScore(message_id="1", score=2, reasoning="bystander")
            ],
            response_language="english",
            response=ResponseBody(target_message_id="1", message="hi"),
            topic="x",
        )


def test_turn_decision_rejects_response_pointing_at_unscored_message():
    """target_message_id must match an actual ranking entry."""
    with pytest.raises(ValueError):
        TurnDecision(
            rankings=[
                MessageScore(message_id="1", score=10, reasoning="direct")
            ],
            response_language="english",
            response=ResponseBody(target_message_id="999", message="hi"),
            topic="x",
        )


def test_turn_decision_allows_no_response():
    """Every ranking <5 + response=None must construct cleanly."""
    decision = TurnDecision(
        rankings=[
            MessageScore(message_id="1", score=3, reasoning="not for me")
        ],
        response_language="english",
        response=None,
        topic="x",
    )
    assert decision.response is None


def test_turn_decision_requires_response_language():
    with pytest.raises(ValueError):
        TurnDecision(
            rankings=[
                MessageScore(message_id="1", score=10, reasoning="direct")
            ],
            response=ResponseBody(target_message_id="1", message="hi"),
            topic="x",
        )


def test_turn_decision_allows_non_english_redirect():
    decision = TurnDecision(
        rankings=[MessageScore(message_id="1", score=10, reasoning="direct")],
        response_language=" Spanish ",
        response=ResponseBody(
            target_message_id="1",
            message="Please use English so I can help.",
        ),
        topic="x",
    )

    assert decision.response_language == "spanish"


def test_turn_decision_allows_silence_after_non_english_warning():
    decision = TurnDecision(
        rankings=[MessageScore(message_id="1", score=10, reasoning="direct")],
        response_language="spanish",
        response=None,
        topic="x",
    )

    assert decision.response is None


@pytest.mark.parametrize(
    ("message", "voice_summary"),
    [
        ("Here is how to fix the KeyError.", None),
        ("Please use English. " + "x" * 230, None),
        ("Please use English.", "Please use English."),
    ],
)
def test_turn_decision_rejects_invalid_non_english_response(
    message: str,
    voice_summary: str | None,
):
    with pytest.raises(ValueError):
        TurnDecision(
            rankings=[
                MessageScore(message_id="1", score=10, reasoning="direct")
            ],
            response_language="spanish",
            response=ResponseBody(
                target_message_id="1",
                message=message,
                voice_summary=voice_summary,
            ),
            topic="x",
        )
