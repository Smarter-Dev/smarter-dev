"""Tests for two-stage chat mode wiring in ``ChannelEngine._run_once``.

Two-stage mode is selected per channel by the override's ``drafter_model``: when
it is set (and names a known catalog model) the cheap DRAFTER (worker) runs the
agentic turn on the drafter's wire id and emits a ``BriefingDecision``, then the
tool-less primary WRITER (the channel's ``model_key``) answers. When it is
unset/empty (or stale) the engine keeps today's exact single-agent behaviour.

The drafter/writer agents, memory, input builders, and Redis budget helpers are
all patched — these verify the engine's *wiring*: which agents it builds, when
the writer runs, how the reply/voice is dispatched, and that BOTH models' tokens
are metered.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from smarter_dev.bot.agents.chat_models import (
    Author,
    BriefingDecision,
    ChannelInfo,
    InitialAgentInput,
    Me,
    Message,
    MessageScore,
    WriterBrief,
    WriterOutput,
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


def _brief(
    *,
    send_voice: bool = False,
    reply_directly: bool = False,
    language: str = "english",
) -> WriterBrief:
    return WriterBrief(
        message_summaries=["Alice asked how to use asyncio"],
        search_findings=[],
        questions=['"how do I use asyncio?"'],
        response_language=language,
        send_voice=send_voice,
        reply_directly=reply_directly,
    )


def _briefing(*, brief: WriterBrief | None, max_score: int = 10) -> BriefingDecision:
    return BriefingDecision(
        rankings=[
            MessageScore(message_id="101", score=max_score, reasoning="direct")
        ],
        brief=brief,
        topic="asyncio",
        notes="alice: asyncio",
        continue_watching=True,
    )


def _worker_result(output, *, input_tokens=0, output_tokens=0):
    usage = SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        requests=1,
        model="worker-model",
    )
    return SimpleNamespace(
        output=output,
        usage=lambda: usage,
        all_messages=lambda: [],
        new_messages=lambda: [],
    )


def _writer_result(output, *, input_tokens=0, output_tokens=0):
    usage = SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        requests=1,
        model="writer-model",
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
    drafter_model: str | None = None,
    daily=1000,
    hourly=0,
    reasoning_level=None,
    fallback_model_key=None,
    response_filter=None,
):
    return SimpleNamespace(
        model_key=model_key,
        drafter_model=drafter_model,
        daily_token_budget=daily,
        hourly_token_budget=hourly,
        reasoning_level=reasoning_level,
        auto_respond=False,
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
    r.exists = AsyncMock(return_value=0)
    r.delete = AsyncMock(return_value=0)
    r.get = AsyncMock(return_value=None)
    pipe = MagicMock()
    pipe.execute = AsyncMock(return_value=[0, True])
    pipe.__aenter__ = AsyncMock(return_value=pipe)
    pipe.__aexit__ = AsyncMock(return_value=False)
    r.pipeline = MagicMock(return_value=pipe)
    return r


def _make_engine(override, fake_redis, voice_send=None):
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
        return None

    engine = ChannelEngine(
        bot=bot,
        channel_id=42,
        guild_id=99,
        voice_send=voice_send or _noop_voice,
        on_deactivate=_on_deactivate,
    )
    engine.activation_message = SimpleNamespace(
        id=101, author=SimpleNamespace(id=200, username="alice", is_bot=False)
    )
    return engine, service


def _common_patches(*, fake_memory):
    """Patches shared by every two-stage test (memory, input builder, budget)."""
    return [
        patch(
            "smarter_dev.bot.services.chat_engine.get_chat_memory",
            return_value=fake_memory,
        ),
        patch(
            "smarter_dev.bot.services.chat_engine.build_initial_input",
            new=AsyncMock(return_value=_initial_input()),
        ),
        patch(
            "smarter_dev.bot.services.chat_engine.over_budget_reset_epoch",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "smarter_dev.bot.services.chat_engine.add_usage", new=AsyncMock()
        ),
    ]


# --------------------------------------------------------------------------- #
# Regression: single-stage behaviour is untouched when drafter_model is unset
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_no_drafter_model_runs_single_stage(fake_memory, fake_redis):
    """An override with no drafter_model builds the CHAT agent, never the worker
    or writer — exactly today's single-agent path."""
    from smarter_dev.bot.agents.chat_models import ResponseBody, TurnDecision

    chat_agent = MagicMock()
    chat_agent.run = AsyncMock(
        return_value=_worker_result(
            TurnDecision(
                rankings=[MessageScore(message_id="101", score=10, reasoning="x")],
                response_language="english",
                response=ResponseBody(target_message_id="101", message="hi"),
                topic="t",
                notes="n",
                continue_watching=True,
            )
        )
    )
    engine, _ = _make_engine(_override("gpt-5-4", drafter_model=None), fake_redis)

    with patch(
        "smarter_dev.bot.services.chat_engine.get_chat_agent",
        return_value=chat_agent,
    ) as get_chat, patch(
        "smarter_dev.bot.services.chat_engine.get_worker_agent"
    ) as get_worker, patch(
        "smarter_dev.bot.services.chat_engine.get_writer_agent"
    ) as get_writer, _common_patches(fake_memory=fake_memory)[0], _common_patches(
        fake_memory=fake_memory
    )[1], _common_patches(fake_memory=fake_memory)[2], _common_patches(
        fake_memory=fake_memory
    )[3]:
        await engine._run_once(first_activation=True)

    get_chat.assert_called_once_with("gpt-5.4", None)
    get_worker.assert_not_called()
    get_writer.assert_not_called()
    chat_agent.run.assert_awaited_once()


# --------------------------------------------------------------------------- #
# Two-stage happy path
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_two_stage_worker_briefs_writer_writes_and_sends(
    fake_memory, fake_redis
):
    """drafter_model set: the cheap DRAFTER (worker) runs on the drafter's wire
    id, its brief drives the tool-less primary WRITER (the channel's model_key),
    and the writer's message is sent."""
    worker_agent = MagicMock()
    worker_agent.run = AsyncMock(
        return_value=_worker_result(
            _briefing(brief=_brief()), input_tokens=100, output_tokens=50
        )
    )
    writer_agent = MagicMock()
    writer_agent.run = AsyncMock(
        return_value=_writer_result(
            WriterOutput(message="Use asyncio.run(main())."),
            input_tokens=200,
            output_tokens=80,
        )
    )
    engine, _ = _make_engine(
        _override("gemma-4-31b", drafter_model="gpt-5-4"), fake_redis
    )

    with patch(
        "smarter_dev.bot.services.chat_engine.get_worker_agent",
        return_value=worker_agent,
    ) as get_worker, patch(
        "smarter_dev.bot.services.chat_engine.get_writer_agent",
        return_value=writer_agent,
    ) as get_writer, patch(
        "smarter_dev.bot.services.chat_engine.get_chat_agent"
    ) as get_chat, _common_patches(fake_memory=fake_memory)[0], _common_patches(
        fake_memory=fake_memory
    )[1], _common_patches(fake_memory=fake_memory)[2], patch(
        "smarter_dev.bot.services.chat_engine.add_usage", new=AsyncMock()
    ) as add_usage:
        await engine._run_once(first_activation=True)

    get_chat.assert_not_called()
    # The DRAFTER (worker) runs the cheap drafter model; the primary answers as
    # the WRITER, honouring its reasoning level (None here).
    get_worker.assert_called_once_with("gpt-5.4", None)
    get_writer.assert_called_once_with("google/gemma-4-31b-it", None)
    worker_agent.run.assert_awaited_once()
    writer_agent.run.assert_awaited_once()

    # The writer's message reached the channel.
    engine.bot.rest.create_message.assert_awaited_once()
    content = engine.bot.rest.create_message.await_args.kwargs["content"]
    assert content == "Use asyncio.run(main())."

    # Only the answering WRITER (the primary) counts against the channel token
    # budget (280); the cheap DRAFTER's 150 tokens are excluded from the limit.
    add_usage.assert_awaited_once_with(fake_redis, "42", 280)


@pytest.mark.asyncio
async def test_two_stage_advertises_primary_answerer_not_drafter(
    fake_memory, fake_redis
):
    """The ``<your-model>`` metadata shown to the DRAFTER names the primary model —
    the answerer that actually authors the reply — so a "what model are you?" turn
    resolves to the primary, not the cheap drafter running the turn."""
    worker_agent = MagicMock()
    worker_agent.run = AsyncMock(
        return_value=_worker_result(_briefing(brief=_brief()))
    )
    writer_agent = MagicMock()
    writer_agent.run = AsyncMock(
        return_value=_writer_result(WriterOutput(message="hi"))
    )
    engine, _ = _make_engine(
        _override("gemma-4-31b", drafter_model="gpt-5-4"), fake_redis
    )

    with patch(
        "smarter_dev.bot.services.chat_engine.get_worker_agent",
        return_value=worker_agent,
    ), patch(
        "smarter_dev.bot.services.chat_engine.get_writer_agent",
        return_value=writer_agent,
    ), patch(
        "smarter_dev.bot.services.chat_engine.get_chat_agent"
    ), _common_patches(fake_memory=fake_memory)[0], _common_patches(
        fake_memory=fake_memory
    )[1], _common_patches(fake_memory=fake_memory)[2], _common_patches(
        fake_memory=fake_memory
    )[3]:
        await engine._run_once(first_activation=True)

    user_prompt = worker_agent.run.await_args.kwargs["user_prompt"]
    # Primary (answerer) is Gemma 4 31B; the cheap drafter is GPT-5.4. The metadata
    # advertises the primary answerer, not the drafter running the turn.
    assert "Gemma 4 31B" in user_prompt
    assert "GPT-5.4" not in user_prompt


@pytest.mark.asyncio
async def test_two_stage_persists_combined_token_totals(fake_memory, fake_redis):
    """The persisted turn folds the writer's spend into the chat token totals."""
    worker_agent = MagicMock()
    worker_agent.run = AsyncMock(
        return_value=_worker_result(
            _briefing(brief=_brief()), input_tokens=100, output_tokens=50
        )
    )
    writer_agent = MagicMock()
    writer_agent.run = AsyncMock(
        return_value=_writer_result(
            WriterOutput(message="hello"), input_tokens=200, output_tokens=80
        )
    )
    engine, _ = _make_engine(
        _override("gemma-4-31b", drafter_model="gpt-5-4"), fake_redis
    )

    persist_turn = AsyncMock()
    with patch(
        "smarter_dev.bot.services.chat_engine.get_worker_agent",
        return_value=worker_agent,
    ), patch(
        "smarter_dev.bot.services.chat_engine.get_writer_agent",
        return_value=writer_agent,
    ), _common_patches(fake_memory=fake_memory)[0], _common_patches(
        fake_memory=fake_memory
    )[1], _common_patches(fake_memory=fake_memory)[2], _common_patches(
        fake_memory=fake_memory
    )[3], patch(
        "smarter_dev.bot.services.chat_engine.start_engagement",
        new=AsyncMock(return_value="engagement-1"),
    ), patch(
        "smarter_dev.bot.services.chat_engine.persist_turn", new=persist_turn
    ):
        await engine._run_once(first_activation=True)

    persist_turn.assert_awaited_once()
    kwargs = persist_turn.await_args.kwargs
    assert kwargs["chat_tokens_input"] == 300  # 100 worker + 200 writer
    assert kwargs["chat_tokens_output"] == 130  # 50 worker + 80 writer
    assert kwargs["output_kind"] == "send_response"


# --------------------------------------------------------------------------- #
# Two-stage overlong reply fitting
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_two_stage_overlong_writer_reply_is_fitted(fake_memory, fake_redis):
    """A WRITER message over the summarize threshold is condensed via
    ``fit_writer_message`` before dispatch so ``split_for_discord`` never drops
    its tail. Luna's spend is not metered, and neither is the worker's — only the
    WRITER counts against the budget."""
    from smarter_dev.bot.agents.response_fitting import FitResult

    worker_agent = MagicMock()
    worker_agent.run = AsyncMock(
        return_value=_worker_result(
            _briefing(brief=_brief()), input_tokens=100, output_tokens=50
        )
    )
    writer_agent = MagicMock()
    writer_agent.run = AsyncMock(
        return_value=_writer_result(
            WriterOutput(message="z" * 3500), input_tokens=200, output_tokens=80
        )
    )
    engine, _ = _make_engine(
        _override("gemma-4-31b", drafter_model="gpt-5-4"), fake_redis
    )

    fit_mock = AsyncMock(return_value=FitResult("fitted reply", 0, 0, "summarized"))
    with patch(
        "smarter_dev.bot.services.chat_engine.get_worker_agent",
        return_value=worker_agent,
    ), patch(
        "smarter_dev.bot.services.chat_engine.get_writer_agent",
        return_value=writer_agent,
    ), patch(
        "smarter_dev.bot.services.chat_engine.fit_writer_message", new=fit_mock
    ), _common_patches(fake_memory=fake_memory)[0], _common_patches(
        fake_memory=fake_memory
    )[1], _common_patches(fake_memory=fake_memory)[2], patch(
        "smarter_dev.bot.services.chat_engine.add_usage", new=AsyncMock()
    ) as add_usage:
        await engine._run_once(first_activation=True)

    fit_mock.assert_awaited_once_with("z" * 3500)
    engine.bot.rest.create_message.assert_awaited_once()
    assert (
        engine.bot.rest.create_message.await_args.kwargs["content"] == "fitted reply"
    )
    # Only the WRITER (280) is metered; the worker (150) and Luna add nothing.
    add_usage.assert_awaited_once_with(fake_redis, "42", 280)


# --------------------------------------------------------------------------- #
# Two-stage silence
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_two_stage_silent_brief_never_calls_writer(fake_memory, fake_redis):
    """When the worker's brief is None the writer is never built or run, nothing
    is sent, and the turn burns a no-response strike."""
    worker_agent = MagicMock()
    worker_agent.run = AsyncMock(
        return_value=_worker_result(
            _briefing(brief=None, max_score=3), input_tokens=40, output_tokens=10
        )
    )
    engine, _ = _make_engine(
        _override("gemma-4-31b", drafter_model="gpt-5-4"), fake_redis
    )

    with patch(
        "smarter_dev.bot.services.chat_engine.get_worker_agent",
        return_value=worker_agent,
    ), patch(
        "smarter_dev.bot.services.chat_engine.get_writer_agent"
    ) as get_writer, _common_patches(fake_memory=fake_memory)[0], _common_patches(
        fake_memory=fake_memory
    )[1], _common_patches(fake_memory=fake_memory)[2], patch(
        "smarter_dev.bot.services.chat_engine.add_usage", new=AsyncMock()
    ) as add_usage:
        await engine._run_once(first_activation=True)

    get_writer.assert_not_called()
    worker_agent.run.assert_awaited_once()
    engine.bot.rest.create_message.assert_not_awaited()
    assert engine.consecutive_no_response == 1
    # Only the WRITER counts against the budget, and it never ran — so a silent
    # two-stage turn meters nothing, even though the worker spent tokens deciding.
    add_usage.assert_awaited_once_with(fake_redis, "42", 0)


# --------------------------------------------------------------------------- #
# Voice request
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_two_stage_voice_request_sends_voice(fake_memory, fake_redis):
    """A brief with send_voice=True forwards the writer's voice_summary to the
    voice sender."""
    worker_agent = MagicMock()
    worker_agent.run = AsyncMock(
        return_value=_worker_result(_briefing(brief=_brief(send_voice=True)))
    )
    writer_agent = MagicMock()
    writer_agent.run = AsyncMock(
        return_value=_writer_result(
            WriterOutput(message="Here you go.", voice_summary="Spoken answer here.")
        )
    )
    voice_send = AsyncMock(return_value=None)
    engine, _ = _make_engine(
        _override("gemma-4-31b", drafter_model="gpt-5-4"),
        fake_redis,
        voice_send=voice_send,
    )

    with patch(
        "smarter_dev.bot.services.chat_engine.get_worker_agent",
        return_value=worker_agent,
    ), patch(
        "smarter_dev.bot.services.chat_engine.get_writer_agent",
        return_value=writer_agent,
    ), _common_patches(fake_memory=fake_memory)[0], _common_patches(
        fake_memory=fake_memory
    )[1], _common_patches(fake_memory=fake_memory)[2], _common_patches(
        fake_memory=fake_memory
    )[3]:
        await engine._run_once(first_activation=True)

    voice_send.assert_awaited_once()
    assert voice_send.await_args.args[1] == "Spoken answer here."


@pytest.mark.asyncio
async def test_two_stage_no_voice_when_not_requested(fake_memory, fake_redis):
    """When the brief did NOT ask for voice, a writer voice_summary is dropped —
    no voice message is sent."""
    worker_agent = MagicMock()
    worker_agent.run = AsyncMock(
        return_value=_worker_result(_briefing(brief=_brief(send_voice=False)))
    )
    writer_agent = MagicMock()
    writer_agent.run = AsyncMock(
        return_value=_writer_result(
            WriterOutput(message="Text only.", voice_summary="unsolicited voice")
        )
    )
    voice_send = AsyncMock(return_value=None)
    engine, _ = _make_engine(
        _override("gemma-4-31b", drafter_model="gpt-5-4"),
        fake_redis,
        voice_send=voice_send,
    )

    with patch(
        "smarter_dev.bot.services.chat_engine.get_worker_agent",
        return_value=worker_agent,
    ), patch(
        "smarter_dev.bot.services.chat_engine.get_writer_agent",
        return_value=writer_agent,
    ), _common_patches(fake_memory=fake_memory)[0], _common_patches(
        fake_memory=fake_memory
    )[1], _common_patches(fake_memory=fake_memory)[2], _common_patches(
        fake_memory=fake_memory
    )[3]:
        await engine._run_once(first_activation=True)

    voice_send.assert_not_awaited()
    engine.bot.rest.create_message.assert_awaited_once()
    assert (
        engine.bot.rest.create_message.await_args.kwargs["content"] == "Text only."
    )


@pytest.mark.asyncio
async def test_two_stage_reply_directly_targets_top_ranked_message(
    fake_memory, fake_redis
):
    """A brief with reply_directly sends the writer's message as a Discord reply
    anchored to the highest-scoring ranked message."""
    worker_agent = MagicMock()
    worker_agent.run = AsyncMock(
        return_value=_worker_result(_briefing(brief=_brief(reply_directly=True)))
    )
    writer_agent = MagicMock()
    writer_agent.run = AsyncMock(
        return_value=_writer_result(WriterOutput(message="Replying directly."))
    )
    engine, _ = _make_engine(
        _override("gemma-4-31b", drafter_model="gpt-5-4"), fake_redis
    )

    with patch(
        "smarter_dev.bot.services.chat_engine.get_worker_agent",
        return_value=worker_agent,
    ), patch(
        "smarter_dev.bot.services.chat_engine.get_writer_agent",
        return_value=writer_agent,
    ), _common_patches(fake_memory=fake_memory)[0], _common_patches(
        fake_memory=fake_memory
    )[1], _common_patches(fake_memory=fake_memory)[2], _common_patches(
        fake_memory=fake_memory
    )[3]:
        await engine._run_once(first_activation=True)

    kwargs = engine.bot.rest.create_message.await_args.kwargs
    assert kwargs["reply"] == 101  # the top-ranked message id


# --------------------------------------------------------------------------- #
# Stale writer key degrades to single-stage
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_stale_drafter_model_falls_back_to_single_stage(
    fake_memory, fake_redis
):
    """A drafter_model the catalog no longer knows degrades to today's
    single-agent path (chat agent), never the worker/writer."""
    from smarter_dev.bot.agents.chat_models import ResponseBody, TurnDecision

    chat_agent = MagicMock()
    chat_agent.run = AsyncMock(
        return_value=_worker_result(
            TurnDecision(
                rankings=[MessageScore(message_id="101", score=10, reasoning="x")],
                response_language="english",
                response=ResponseBody(target_message_id="101", message="hi"),
                topic="t",
                notes="n",
                continue_watching=True,
            )
        )
    )
    engine, _ = _make_engine(
        _override("gpt-5-4", drafter_model="this-model-was-removed"), fake_redis
    )

    with patch(
        "smarter_dev.bot.services.chat_engine.get_chat_agent",
        return_value=chat_agent,
    ) as get_chat, patch(
        "smarter_dev.bot.services.chat_engine.get_worker_agent"
    ) as get_worker, patch(
        "smarter_dev.bot.services.chat_engine.get_writer_agent"
    ) as get_writer, _common_patches(fake_memory=fake_memory)[0], _common_patches(
        fake_memory=fake_memory
    )[1], _common_patches(fake_memory=fake_memory)[2], _common_patches(
        fake_memory=fake_memory
    )[3]:
        await engine._run_once(first_activation=True)

    get_chat.assert_called_once_with("gpt-5.4", None)
    get_worker.assert_not_called()
    get_writer.assert_not_called()
