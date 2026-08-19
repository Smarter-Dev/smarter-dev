"""The proactive bot honors Discord's length cap like the chat bot does.

Tier 1/2 (<=3000 chars) split into at most two messages at dispatch; over
that the send tools refuse in-loop so the agent rewrites with its own
context, and anything that still slips through is condensed by the shared
summarizer rather than silently truncated.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic_ai.models.test import TestModel

from smarter_dev.bot.agents.response_fitting import (
    DISCORD_MESSAGE_LIMIT,
    SUMMARIZE_THRESHOLD,
)
from smarter_dev.bot.plugins import proactive
from smarter_dev.bot.proactive import agent as proactive_agent
from smarter_dev.bot.proactive.environment import (
    ChannelEnvironment,
    InstructionStore,
    WakeActions,
)
from smarter_dev.bot.proactive.types import ChannelMessage


def _message(message_id: str = "555") -> ChannelMessage:
    return ChannelMessage(
        id=message_id,
        timestamp=datetime(2026, 8, 19, 12, 0, tzinfo=UTC),
        author_id="901",
        author_name="alice",
        author_display="alice",
        is_bot=False,
        content="hello",
        reply_to_id=None,
        mention_user_ids=(),
        mention_everyone=False,
        attachment_count=0,
        sticker_count=0,
        message_type=0,
    )


async def _noop_skim(transcript: str) -> str:
    return "skimmed"


def _deps() -> proactive_agent.AgentDeps:
    return proactive_agent.AgentDeps(
        env=ChannelEnvironment(visible=[_message()], bot_user_id="B1"),
        actions=WakeActions(),
        instruction_store=InstructionStore(seed="SEED"),
        skim_transcript=_noop_skim,
        budget=proactive_agent.ToolBudget(),
    )


# --- tool-level refusal (the agent-rewrite tier, in-loop and free) ----------


async def test_send_tool_refuses_overlong_content_with_rewrite_guidance():
    deps = _deps()
    kimi = proactive_agent.build_kimi_agent(
        TestModel(custom_output_text="done"), system_prompt="s"
    )
    tool = kimi._function_toolset.tools["send_channel_message"]
    answer = await tool.function(
        SimpleNamespace(deps=deps), content="x" * (SUMMARIZE_THRESHOLD + 1)
    )
    assert "too long" in answer.lower()
    assert str(SUMMARIZE_THRESHOLD) in answer
    assert deps.actions.sent == []  # nothing queued; the agent must rewrite


async def test_reply_tool_accepts_content_within_the_fitting_range():
    deps = _deps()
    kimi = proactive_agent.build_kimi_agent(
        TestModel(custom_output_text="done"), system_prompt="s"
    )
    tool = kimi._function_toolset.tools["reply_to_message"]
    answer = await tool.function(
        SimpleNamespace(deps=deps),
        message_id="555",
        content="y" * (DISCORD_MESSAGE_LIMIT + 200),
    )
    assert "sent" in answer.lower()
    assert len(deps.actions.sent) == 1  # dispatch splits it into two messages


# --- dispatch-level splitting ------------------------------------------------


def _fake_bot():
    rest = SimpleNamespace(
        create_message=AsyncMock(),
        add_reaction=AsyncMock(),
    )
    return SimpleNamespace(rest=rest)


async def test_dispatch_splits_a_long_reply_into_two_messages():
    bot = _fake_bot()
    long_text = ("a" * 1400) + "\n" + ("b" * 900)

    sent = await proactive.dispatch_response(
        bot, channel_id=1, content=long_text, reply_to_id="555"
    )

    assert sent == 2
    calls = bot.rest.create_message.await_args_list
    assert len(calls) == 2
    # The reply anchor rides on the first message only.
    assert calls[0].kwargs.get("reply") == 555
    assert "reply" not in calls[1].kwargs
    # Both parts fit the cap and nothing was dropped.
    parts = [call.args[1] for call in calls]
    assert all(len(part) <= DISCORD_MESSAGE_LIMIT for part in parts)
    assert "".join(part.strip() for part in parts).replace("\n", "") == (
        long_text.replace("\n", "")
    )


async def test_dispatch_sends_a_short_reply_as_one_message():
    bot = _fake_bot()
    sent = await proactive.dispatch_response(
        bot, channel_id=1, content="short and sweet", reply_to_id=None
    )
    assert sent == 1
    call = bot.rest.create_message.await_args_list[0]
    assert call.args[1] == "short and sweet"
    assert "reply" not in call.kwargs


async def test_dispatch_condenses_rather_than_truncating_over_the_threshold(
    monkeypatch,
):
    bot = _fake_bot()
    condensed = "condensed version of the reply"

    async def fake_fit(message: str):
        assert len(message) > SUMMARIZE_THRESHOLD
        return SimpleNamespace(text=condensed, method="summarized")

    monkeypatch.setattr(proactive, "fit_writer_message", fake_fit)

    sent = await proactive.dispatch_response(
        bot, channel_id=1, content="z" * (SUMMARIZE_THRESHOLD + 500),
        reply_to_id=None,
    )

    assert sent == 1
    assert bot.rest.create_message.await_args_list[0].args[1] == condensed


async def test_dispatch_returns_zero_when_send_fails():
    bot = _fake_bot()
    bot.rest.create_message.side_effect = RuntimeError("discord is down")
    sent = await proactive.dispatch_response(
        bot, channel_id=1, content="hello", reply_to_id=None
    )
    assert sent == 0
