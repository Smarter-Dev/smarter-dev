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
    channel_env = env or _env()
    instruction_store = environment.InstructionStore(seed="SEED")
    return agent.AgentDeps(
        enabled_channels={"a": "test-channel"},
        channel_envs={"a": channel_env},
        actions=environment.WakeActions(),
        instruction_stores={"a": instruction_store},
        skim_transcript=_noop_skim,
        budget=agent.ToolBudget(limit=budget_limit),
    )


# --- watcher -----------------------------------------------------------------


def test_watcher_prompt_carries_instructions_and_blocks():
    prompt = watcher.build_watcher_prompt(
        instructions="WAKE ON X",
        context_transcript="ctx lines",
        new_transcript="new lines",
        bot_user_id="B0",
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
        instructions="i", context_transcript="c", new_transcript="n",
        bot_user_id="B1",
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
    kimi = _kimi(["send_channel_message", "set_watch_instruction"])
    await kimi.run("go", deps=deps)
    assert len(deps.actions.sent) == 1
    assert deps.actions.sent[0].reply_to_id is None
    assert deps.instruction_store.updates == 1
    assert len(deps.instruction_store.entries) == 1
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


def test_default_system_prompt_is_brief_but_complete():
    prompt = agent.build_agent_system_prompt(
        bot_display_name="smarter-bot",
        bot_user_id="B1",
        channel_name="💬general",
        guild_name="Smarter Dev",
    )
    assert len(prompt) < 5000  # still far under prompt-bloat territory
    for load_bearing in (
        "smarter-bot", "B1", "STATELESS", "set_watch_instruction",
        "set_monitoring_mode", "PASSIVE", "ACTIVE", "NOTIFICATIONS",
        "one-off", "higher", "Silence", "Backing off",
    ):
        assert load_bearing in prompt, load_bearing


async def test_extra_tools_register_on_the_agent():
    deps = _deps()
    calls = []

    async def custom_probe(ctx) -> str:
        """A custom probe tool."""
        calls.append(ctx.deps.env.bot_user_id)
        return "probed"

    kimi = agent.build_kimi_agent(
        TestModel(call_tools=["custom_probe"], custom_output_text="done"),
        system_prompt="s",
        extra_tools=[custom_probe],
    )
    await kimi.run("go", deps=deps)
    assert calls == ["B1"]


# --- history compaction ------------------------------------------------------


def _turn(index: int) -> list:
    return [
        ModelRequest(parts=[UserPromptPart(f"wake {index} " + "x" * 200)]),
        ModelResponse(parts=[TextPart(f"note {index} " + "y" * 200)]),
    ]


async def test_history_compacts_over_limit_and_keeps_tail():
    history = [message for index in range(20) for message in _turn(index)]
    summaries = []

    async def summarize(messages) -> str:
        summaries.append(messages)
        return "compact summary"

    compacted = await agent.compact_agent_history(
        history, token_limit=100, summarize=summarize, keep_messages=4
    )
    assert len(summaries) == 1
    assert "wake 19" in str(compacted[-2])  # tail survives verbatim
    assert "compact summary" in str(compacted[0])
    # The injected note is framed as the agent's own memory so its contents
    # are never misread as whatever the next engaging user said.
    assert "NOT a user message" in str(compacted[0])
    assert isinstance(compacted[0], ModelRequest)
    assert len(compacted) < len(history)


async def test_history_untouched_under_limit():
    history = _turn(0)

    async def summarize(messages) -> str:  # pragma: no cover - must not run
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
        deps.instruction_store.set_instruction(
            "watch for follow-up from alice", ttl_seconds=3600
        )
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


def _mention_message(message_id: str, offset: int, mention: str) -> FixtureMessage:
    base = _message(message_id, offset)
    return FixtureMessage(
        **{**base.__dict__, "mention_user_ids": (mention,)}
    )


def test_bot_directed_ids_detect_mentions_and_replies_to_bot():
    bot_message = FixtureMessage(
        **{**_message("5", 5).__dict__, "is_bot": True, "author_id": "B1"}
    )
    reply_to_bot = FixtureMessage(
        **{**_message("6", 6).__dict__, "reply_to_id": "5"}
    )
    mention = _mention_message("7", 7, "B1")
    other_mention = _mention_message("8", 8, "999")
    plain = _message("9", 9)
    env = environment.ChannelEnvironment(
        visible=[bot_message, reply_to_bot, mention, other_mention, plain],
        bot_user_id="B1",
    )
    assert adapter.bot_directed_message_ids(
        [reply_to_bot, mention, other_mention, plain], env, "B1"
    ) == ["6", "7"]


class _ExplodingWatcher:
    async def decide(self, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("watcher must not be consulted on a mention")


async def test_adapter_wakes_deterministically_on_bot_mention():
    messages = [_message("1", 0), _mention_message("2", 5, "B1")]
    runner = _StubAgentRunner()
    two_pass = adapter.TwoPassAdapter(
        watcher=_ExplodingWatcher(),
        agent_runner=runner,
        skim=None,
        instruction_store=environment.InstructionStore(seed="SEED"),
        watcher_model_id="watcher-model",
        agent_model_id="agent-model",
    )
    result = await two_pass.activate(
        ActivationContext(
            channel_name="c", guild_name="g", bot_user_id="B1",
            activated_at=T + timedelta(seconds=10),
            history=[messages[0]], new_messages=[messages[1]],
        )
    )
    assert result.details["watcher"]["deterministic"] is True
    assert result.details["watcher"]["relevant_message_ids"] == ["2"]
    # The wake brief is a mention notification carrying the verbatim message.
    assert "mention" in runner.briefs[0]
    assert "id=2" in runner.briefs[0]
    assert "message 2" in runner.briefs[0]
    assert len(result.responses) == 1  # the stub agent replied
    # No watcher model usage — only the agent spent tokens.
    assert set(result.usage_by_model) == {"agent-model"}


def test_watcher_prompt_names_the_bot_user_id():
    prompt = watcher.build_watcher_prompt(
        instructions="i", context_transcript="c", new_transcript="n",
        bot_user_id="B1", bot_display_name="smarter dev",
    )
    assert "<@B1>" in prompt
    assert 'addressing "smarter dev" by name' in prompt


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
    # The brief is the watcher-summary notification with the relevant ids.
    assert "alice asked the room" in runner.briefs[0]
    assert "2" in runner.briefs[0]
    assert "NOTIFICATIONS" in runner.briefs[0]
    # The instruction update persists for the next wake's watcher call.
    assert "follow-up from alice" in two_pass.instruction_store.current()


async def test_non_wake_summaries_discard_but_queued_items_drain():
    from smarter_dev.bot.proactive.notifications import NotificationQueue

    queue = NotificationQueue()
    runner = _StubAgentRunner()
    quiet_watcher = _StubWatcher(
        watcher.WatcherDecision(
            wake=False, reason="two people chatting",
            summary="keyboard chatter between carol and dave",
        )
    )
    two_pass = adapter.TwoPassAdapter(
        watcher=quiet_watcher,
        agent_runner=runner,
        skim=None,
        instruction_store=environment.InstructionStore(seed="SEED"),
        watcher_model_id="watcher-model",
        agent_model_id="agent-model",
        notification_queue=queue,
    )
    silent = await two_pass.activate(_context())
    assert silent.responses == []
    assert queue.items == []  # non-wake watcher summaries are discarded

    # Queue a mode-change (the kind that DOES queue) to prove drainage.
    from datetime import UTC as _UTC
    from datetime import datetime as _datetime

    from smarter_dev.bot.proactive.notifications import (
        mode_change_notification,
    )

    queue.push(mode_change_notification(
        mode="active", cause="keyboard chatter escalated",
        until=None, created_at=_datetime(2026, 7, 20, 10, 0, tzinfo=_UTC),
        channel_id="c", channel_name="c",
    ))

    # Next wake is deterministic (a mention): the queued item rides along.
    two_pass.watcher = _StubWatcher(
        watcher.WatcherDecision(wake=True, reason="unused")
    )
    messages = [_message(str(n), n) for n in range(3)]
    mention = FixtureMessage(
        **{**_message("9", 9).__dict__, "mention_user_ids": ("B1",)}
    )
    woken = await two_pass.activate(
        ActivationContext(
            channel_name="c", guild_name="g", bot_user_id="B1",
            activated_at=T + timedelta(seconds=20),
            history=messages, new_messages=[mention],
        )
    )
    brief = runner.briefs[-1]
    assert "keyboard chatter escalated" in brief  # drained queue content
    assert "mention" in brief                     # plus the waking notification
    assert queue.items == []                      # drained
    assert woken.details["watcher"]["deterministic"] is True


def test_wake_brief_shows_active_instructions_and_nudges():
    store = environment.InstructionStore(seed="SEED")
    empty_brief = adapter.build_wake_brief([], 0, store)
    assert "YOUR WATCH INSTRUCTIONS: none set." in empty_brief
    assert "set_watch_instruction" in empty_brief

    store.set_instruction("watch for zoe's benchmarks", ttl_seconds=3600)
    brief = adapter.build_wake_brief([], 0, store)
    assert "w1" in brief
    assert "watch for zoe's benchmarks" in brief


def test_engagement_ignores_replies_to_other_bots():
    """A reply to MEE6 is not a reply to us — it must not wake the agent."""
    ours = FixtureMessage(
        **{**_message("5", 5).__dict__, "is_bot": True, "author_id": "B1"}
    )
    other_bot = FixtureMessage(
        **{**_message("6", 6).__dict__, "is_bot": True, "author_id": "OTHERBOT"}
    )
    reply_to_ours = FixtureMessage(
        **{**_message("7", 7).__dict__, "reply_to_id": "5"}
    )
    reply_to_other = FixtureMessage(
        **{**_message("8", 8).__dict__, "reply_to_id": "6"}
    )
    env = environment.ChannelEnvironment(
        visible=[ours, other_bot, reply_to_ours, reply_to_other],
        bot_user_id="B1",
    )

    produced = adapter.engagement_notifications(
        [reply_to_ours, reply_to_other], env, "B1"
    )
    assert [n.message_ids[0] for n in produced] == ["7"]

    assert adapter.bot_directed_message_ids(
        [reply_to_ours, reply_to_other], env, "B1"
    ) == ["7"]
