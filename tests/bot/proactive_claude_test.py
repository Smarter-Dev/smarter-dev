"""The bot's embedded proactive agent on Claude Opus 5.5 (#199).

Guilds outside PROACTIVE_AGENT_EXTERNAL_GUILD_IDS run the agent inside the bot,
so this path gets what the proactive-agent worker got in #188: cache writes in
the wake's usage, and Claude thinking dropped wherever its signed prefix no
longer holds. Request shapes come from the recorded Messages API fake.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from pydantic_ai import Agent
from pydantic_ai.messages import ModelRequest
from pydantic_ai.messages import ModelResponse
from pydantic_ai.messages import TextPart
from pydantic_ai.messages import ThinkingPart
from pydantic_ai.messages import UserPromptPart

from smarter_dev.bot.agents.chat_agent import build_agent_model
from smarter_dev.bot.plugins.proactive import DEFAULT_AGENT_MODEL
from smarter_dev.bot.proactive.adapter import _TOTALED
from smarter_dev.bot.proactive.adapter import _merge_usage
from smarter_dev.bot.proactive.agent import KimiAgentRunner
from smarter_dev.bot.proactive.agent import compact_agent_history
from smarter_dev.bot.proactive.agent import self_compaction_summary
from smarter_dev.bot.proactive.contracts import TokenUsage
from smarter_dev.shared.claude_thinking import drop_other_models_claude_thinking
from tests.claude_wire import cache_controls
from tests.claude_wire import serve
from tests.claude_wire import text_reply
from tests.claude_wire import thinking_blocks

ONE_HOUR = {"type": "ephemeral", "ttl": "1h"}


def _thinking(text: str, model: str = "claude-opus-5-5") -> ModelResponse:
    return ModelResponse(
        parts=[
            ThinkingPart(
                content=text, signature=f"sig-{text}", provider_name="anthropic"
            ),
            TextPart(f"answer after {text}"),
        ],
        model_name=model,
        provider_name="anthropic",
    )


def _turn(prompt: str) -> ModelRequest:
    return ModelRequest(parts=[UserPromptPart(prompt)])


async def test_a_wake_on_opus_is_cached_and_reports_its_cache_writes(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    server = serve(
        monkeypatch, text_reply("stayed quiet", cache_read=1_000, cache_write_1h=4_096)
    )
    assert DEFAULT_AGENT_MODEL == "claude-opus-5-5"
    runner = KimiAgentRunner(
        agent=Agent(build_agent_model(DEFAULT_AGENT_MODEL), system_prompt="guild"),
        summarize=AsyncMock(),
    )

    _, usage = await runner.wake("brief", deps=None)

    assert usage == {
        "input_tokens": 10 + 1_000 + 4_096,
        "output_tokens": 5,
        "cache_read_tokens": 1_000,
        "cache_write_tokens": 4_096,
    }
    (body,) = server.requests
    assert body["model"] == "claude-opus-5-5"
    assert body["system"][-1]["cache_control"] == ONE_HOUR
    assert body["messages"][-1]["content"][-1]["cache_control"] == ONE_HOUR


def test_cache_writes_total_up_but_never_reach_watcher_usage():
    usage_by_model: dict[str, dict] = {}
    watcher = {"input_tokens": 50, "output_tokens": 0, "cache_read_tokens": 0}
    _merge_usage(usage_by_model, "typesafe:jev-1.13.0", watcher)
    _merge_usage(
        usage_by_model,
        "claude-opus-5-5",
        {
            "input_tokens": 9_000,
            "output_tokens": 300,
            "cache_read_tokens": 6_000,
            "cache_write_tokens": 2_000,
        },
    )

    totals = {
        key: sum(entry.get(key, 0) for entry in usage_by_model.values())
        for key in _TOTALED
    }
    assert totals["cache_write_tokens"] == 2_000
    # Watcher usage crosses to the worker as a TokenUsage, which forbids
    # extra fields: its entry must not gain the key, not even as 0.
    TokenUsage(**usage_by_model["typesafe:jev-1.13.0"])


async def test_keep_tail_compaction_strips_the_kept_tails_thinking():
    history = [_turn("a"), _thinking("one"), _turn("b"), _thinking("two")]

    compacted = await compact_agent_history(
        history,
        token_limit=0,
        summarize=AsyncMock(return_value="note"),
        keep_messages=2,
    )

    assert compacted[-1].parts == [TextPart("answer after two")]
    assert [part.part_kind for part in history[-1].parts] == ["thinking", "text"]


async def test_self_compaction_sends_no_thinking_and_writes_no_history_cache(
    monkeypatch,
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    server = serve(monkeypatch, text_reply("memory note"))

    note, usage = await self_compaction_summary(
        build_agent_model("claude-opus-5-5"),
        [_turn("a"), _thinking("one"), _turn("b"), _thinking("two")],
    )

    assert note == "memory note"
    (body,) = server.requests
    assert thinking_blocks(body) == []
    # One request whose prefix nothing reuses: a message breakpoint would only
    # bill the folded transcript at Opus's $8 1-hour write rate.
    assert cache_controls(body) == []
    assert usage["cache_write_tokens"] == 0


def test_moving_to_opus_drops_the_earlier_models_leading_thinking():
    history = [
        _turn("a"),
        _thinking("old", model="claude-sonnet-5-5"),
        _turn("b"),
        _thinking("new"),
    ]

    moved = drop_other_models_claude_thinking(history, "claude-opus-5-5")

    assert moved[1].parts == [TextPart("answer after old")]
    assert moved[3] is history[3]
    assert drop_other_models_claude_thinking(moved, "claude-opus-5-5") is moved


async def test_a_wake_on_opus_sends_no_thinking_from_the_earlier_model(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    server = serve(monkeypatch, text_reply("stayed quiet"))
    runner = KimiAgentRunner(
        agent=Agent(build_agent_model("claude-opus-5-5"), system_prompt="guild"),
        summarize=AsyncMock(),
        history=[_turn("a"), _thinking("old", model="claude-sonnet-5-5")],
    )

    await runner.wake("brief", deps=None)

    (body,) = server.requests
    assert thinking_blocks(body) == []
