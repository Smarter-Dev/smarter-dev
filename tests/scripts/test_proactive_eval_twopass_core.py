"""Tests for the two-pass watcher, agent tools, compaction and adapter.

Model calls use pydantic-ai's TestModel; nothing touches the network.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models.test import TestModel

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.proactive_eval.simulation import (  # noqa: E402
    ActivationContext,
    ActivationResult,
    FixtureMessage,
    ProposedResponse,
)
from scripts.proactive_eval.twopass import adapter, agent, environment, watcher  # noqa: E402

T = datetime(2026, 7, 20, 10, 0, 0, tzinfo=UTC)


def _message(message_id: str, offset: int, *, author_id: str = "1",
             display: str = "alice") -> FixtureMessage:
    return FixtureMessage(
        id=message_id,
        timestamp=T + timedelta(seconds=offset),
        author_id=author_id,
        author_name=display,
        author_display=display,
        is_bot=False,
        content=f"message {message_id}",
        reply_to_id=None,
        mention_user_ids=(),
        mention_everyone=False,
        attachment_count=0,
        sticker_count=0,
        message_type=0,
    )


def _env(count: int = 5) -> environment.ChannelEnvironment:
    return environment.ChannelEnvironment(
        visible=[_message(str(n), n) for n in range(count)],
        bot_user_id="B1",
    )


async def _noop_skim(transcript: str) -> str:
    return "skimmed"


def _deps(env=None, budget_limit: int = 8) -> agent.AgentDeps:
    return agent.AgentDeps(
        env=env or _env(),
        actions=environment.WakeActions(),
        instruction_store=environment.InstructionStore(seed="SEED"),
        skim_transcript=_noop_skim,
        budget=agent.ToolBudget(limit=budget_limit),
    )


# --- watcher -----------------------------------------------------------------


def test_watcher_prompt_carries_instructions_and_blocks():
    prompt = watcher.build_watcher_prompt(
        instructions="WAKE ON X",
        context_transcript="ctx lines",
        new_transcript="new lines",
    )
    assert "WAKE ON X" in prompt
    assert prompt.index("ctx lines") < prompt.index("new lines")


async def test_watcher_runner_returns_decision_and_usage():
    runner = watcher.WatcherRunner(
        TestModel(
            custom_output_args={
                "wake": True,
                "reason": "bot mentioned",
                "relevant_message_ids": ["2"],
                "summary": "s",
            }
        ),
        prompted_output=False,
    )
    decision, usage = await runner.decide(
        instructions="i", context_transcript="c", new_transcript="n"
    )
    assert decision.wake is True
    assert decision.relevant_message_ids == ["2"]
    assert usage["input_tokens"] > 0


# --- agent tools -------------------------------------------------------------


def _kimi(call_tools: list[str]) -> agent.Agent:
    return agent.build_kimi_agent(
        TestModel(call_tools=call_tools, custom_output_text="done"),
        system_prompt="test system prompt",
    )


async def test_send_and_instruction_tools_record_actions():
    deps = _deps()
    kimi = _kimi(["send_channel_message", "update_watch_instructions"])
    await kimi.run("go", deps=deps)
    assert len(deps.actions.sent) == 1
    assert deps.actions.sent[0].reply_to_id is None
    assert deps.instruction_store.updates == 1
    assert deps.budget.used == 2


async def test_reply_and_react_validate_message_ids():
    deps = _deps()
    kimi = _kimi(["reply_to_message", "react_to_message"])
    await kimi.run("go", deps=deps)
    # TestModel generates 'a' as the message id — invalid, so nothing records.
    assert deps.actions.sent == []
    assert deps.actions.reactions == []


async def test_tool_budget_blocks_after_limit():
    deps = _deps(budget_limit=1)
    kimi = _kimi(["channel_history", "send_channel_message"])
    await kimi.run("go", deps=deps)
    # First tool spends the budget; the send is refused.
    assert deps.actions.sent == []
    assert deps.budget.used == 1


async def test_send_cap_stops_at_two_messages():
    deps = _deps()
    kimi = agent.build_kimi_agent(
        TestModel(call_tools=["send_channel_message"], custom_output_text="done"),
        system_prompt="s",
    )
    # Drive three sends by hand through the tool function via three runs.
    for _ in range(3):
        await kimi.run("go", deps=deps)
    assert len(deps.actions.sent) == 2


# --- history compaction ------------------------------------------------------


def _turn(index: int) -> list:
    return [
        ModelRequest(parts=[UserPromptPart(f"wake {index} " + "x" * 200)]),
        ModelResponse(parts=[TextPart(f"note {index} " + "y" * 200)]),
    ]


async def test_history_compacts_over_limit_and_keeps_tail():
    history = [message for index in range(20) for message in _turn(index)]
    summaries = []

    async def summarize(text: str) -> str:
        summaries.append(text)
        return "compact summary"

    compacted = await agent.compact_agent_history(
        history, token_limit=100, summarize=summarize, keep_messages=4
    )
    assert len(summaries) == 1
    assert "wake 19" in str(compacted[-2])  # tail survives verbatim
    assert "compact summary" in str(compacted[0])
    assert isinstance(compacted[0], ModelRequest)
    assert len(compacted) < len(history)


async def test_history_untouched_under_limit():
    history = _turn(0)

    async def summarize(text: str) -> str:  # pragma: no cover - must not run
        raise AssertionError("should not summarize")

    assert (
        await agent.compact_agent_history(
            history, token_limit=1_000_000, summarize=summarize
        )
        == history
    )


# --- adapter -----------------------------------------------------------------


class _StubWatcher:
    def __init__(self, decision: watcher.WatcherDecision):
        self.decision = decision

    async def decide(self, **kwargs):
        return self.decision, {"input_tokens": 100, "output_tokens": 10,
                                "cache_read_tokens": 0}


class _StubAgentRunner:
    def __init__(self):
        self.briefs: list[str] = []

    async def wake(self, brief: str, deps: agent.AgentDeps):
        self.briefs.append(brief)
        deps.actions.sent.append(
            ProposedResponse(reply_to_id="2", content="happy to help")
        )
        deps.instruction_store.update("watch for follow-up from alice")
        return "replied to the open question", {
            "input_tokens": 500, "output_tokens": 50, "cache_read_tokens": 0,
        }


def _context(count: int = 5) -> ActivationContext:
    messages = [_message(str(n), n) for n in range(count)]
    return ActivationContext(
        channel_name="💬general",
        guild_name="Smarter Dev",
        bot_user_id="B1",
        activated_at=T + timedelta(seconds=count),
        history=messages[:2],
        new_messages=messages[2:],
    )


def _adapter(decision: watcher.WatcherDecision, runner=None) -> adapter.TwoPassAdapter:
    return adapter.TwoPassAdapter(
        watcher=_StubWatcher(decision),
        agent_runner=runner or _StubAgentRunner(),
        skim=None,
        instruction_store=environment.InstructionStore(seed="SEED"),
        watcher_model_id="watcher-model",
        agent_model_id="agent-model",
    )


async def test_adapter_stays_silent_when_watcher_declines():
    result = await _adapter(
        watcher.WatcherDecision(wake=False, reason="two-person exchange")
    ).activate(_context())
    assert result.responses == []
    assert result.usage_by_model == {
        "watcher-model": {"input_tokens": 100, "output_tokens": 10,
                           "cache_read_tokens": 0}
    }
    assert result.details["watcher"]["wake"] is False
    assert "agent" not in result.details


async def test_adapter_wakes_agent_and_merges_usage():
    runner = _StubAgentRunner()
    two_pass = _adapter(
        watcher.WatcherDecision(
            wake=True, reason="open question",
            relevant_message_ids=["2"], summary="alice asked the room",
        ),
        runner,
    )
    result = await two_pass.activate(_context())

    assert [r.reply_to_id for r in result.responses] == ["2"]
    assert result.input_tokens == 600  # watcher 100 + agent 500
    assert result.usage_by_model["agent-model"]["output_tokens"] == 50
    assert result.details["agent"]["note"].startswith("replied")
    assert result.details["watch_instruction_updates"] == 1
    # The brief carried the verbatim snippet for the relevant id.
    assert "[id=2]" in runner.briefs[0]
    assert "alice asked the room" in runner.briefs[0]
    # The instruction update persists for the next wake's watcher call.
    assert "follow-up from alice" in two_pass.instruction_store.current()
