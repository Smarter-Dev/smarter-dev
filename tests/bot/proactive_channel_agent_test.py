"""Channel routing and validation for the guild-wide proactive agent."""

from __future__ import annotations

import inspect
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic_ai.models.test import TestModel

from smarter_dev.bot.agents.chat_tools import GeneratedImage
from smarter_dev.bot.proactive import adapter
from smarter_dev.bot.proactive import agent as agent_module
from smarter_dev.bot.proactive import parity
from smarter_dev.bot.proactive.agent import OPERATING_POLICY_BRIEF
from smarter_dev.bot.proactive.agent import AgentDeps
from smarter_dev.bot.proactive.agent import ToolBudget
from smarter_dev.bot.proactive.agent import build_guild_agent_system_prompt
from smarter_dev.bot.proactive.agent import build_kimi_agent
from smarter_dev.bot.proactive.environment import ChannelEnvironment
from smarter_dev.bot.proactive.environment import InstructionStore
from smarter_dev.bot.proactive.environment import WakeActions
from smarter_dev.bot.proactive.notifications import Notification
from smarter_dev.bot.proactive.notifications import NotificationQueue
from smarter_dev.bot.proactive.types import ActivationContext
from smarter_dev.bot.proactive.types import ChannelMessage

T = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def _message(message_id: str, content: str = "hello") -> ChannelMessage:
    return ChannelMessage(
        id=message_id,
        timestamp=T,
        author_id="user-1",
        author_name="alice",
        author_display="Alice",
        is_bot=False,
        content=content,
        reply_to_id=None,
        mention_user_ids=(),
        mention_everyone=False,
        attachment_count=0,
        sticker_count=0,
        message_type=0,
    )


async def _noop_skim(transcript: str) -> str:
    return "skimmed"


def _agent_deps() -> AgentDeps:
    return AgentDeps(
        enabled_channels={"A": "alpha", "B": "beta"},
        channel_envs={
            "A": ChannelEnvironment([_message("a1")], "bot-1"),
            "B": ChannelEnvironment([_message("b1")], "bot-1"),
        },
        actions=WakeActions(),
        instruction_stores={
            "A": InstructionStore(seed="alpha seed"),
            "B": InstructionStore(seed="beta seed"),
        },
        skim_transcript=_noop_skim,
        budget=ToolBudget(),
    )


async def test_disabled_channel_is_rejected_before_native_tool_budget_spend():
    deps = _agent_deps()
    agent = build_kimi_agent(TestModel(), system_prompt="s")
    tool = agent._function_toolset.tools["channel_history"]

    answer = await tool.function(
        SimpleNamespace(deps=deps), channel_id="not-enabled"
    )

    assert answer.split("\n", 1)[1] == (
        "Channel not-enabled is not enabled for the proactive bot."
    )
    assert deps.budget.used == 0


async def test_watch_instruction_routes_only_to_named_channel():
    deps = _agent_deps()
    agent = build_kimi_agent(TestModel(), system_prompt="s")
    tool = agent._function_toolset.tools["set_watch_instruction"]

    answer = await tool.function(
        SimpleNamespace(deps=deps),
        channel_id="A",
        instruction="watch Alice's deploy",
        ttl_minutes=30,
    )

    assert "set" in answer
    assert [entry.text for entry in deps.instruction_stores["A"].entries] == [
        "watch Alice's deploy"
    ]
    assert deps.instruction_stores["B"].entries == []


async def test_native_send_and_reaction_actions_keep_named_channel():
    deps = _agent_deps()
    agent = build_kimi_agent(TestModel(), system_prompt="s")

    await agent._function_toolset.tools["send_channel_message"].function(
        SimpleNamespace(deps=deps), channel_id="B", content="hello beta"
    )
    await agent._function_toolset.tools["react_to_message"].function(
        SimpleNamespace(deps=deps),
        channel_id="B",
        message_id="b1",
        emoji="👍",
    )

    assert deps.actions.sent[0].channel_id == "B"
    assert deps.actions.reactions[0].channel_id == "B"


async def test_set_monitoring_mode_rejects_disabled_channel_without_budget_spend():
    deps = _agent_deps()
    mode_requests = []

    def request_mode(channel_id, mode, minutes):
        mode_requests.append((channel_id, mode, minutes))
        return "Monitoring mode set."

    deps.request_mode = request_mode
    agent = build_kimi_agent(TestModel(), system_prompt="s")
    tool = agent._function_toolset.tools["set_monitoring_mode"]

    answer = await tool.function(
        SimpleNamespace(deps=deps), channel_id="not-enabled", mode="active"
    )

    assert answer.split("\n", 1)[1] == (
        "Channel not-enabled is not enabled for the proactive bot."
    )
    assert deps.budget.used == 0
    assert mode_requests == []


async def test_set_monitoring_mode_routes_the_named_channel():
    deps = _agent_deps()
    mode_requests = []

    def request_mode(channel_id, mode, minutes):
        mode_requests.append((channel_id, mode, minutes))
        return "Monitoring mode set."

    deps.request_mode = request_mode
    agent = build_kimi_agent(TestModel(), system_prompt="s")
    tool = agent._function_toolset.tools["set_monitoring_mode"]

    answer = await tool.function(
        SimpleNamespace(deps=deps), channel_id="B", mode="active", minutes=5
    )

    assert answer.split("\n", 1)[1] == "Monitoring mode set."
    assert mode_requests == [("B", "active", 5)]


async def test_callable_channel_envs_resolve_once_per_channel():
    fetch_counts = {"A": 0}
    fetched_env = ChannelEnvironment([_message("a1")], "bot-1")

    def fetch_env(channel_id):
        fetch_counts[channel_id] += 1
        return fetched_env

    deps = AgentDeps(
        enabled_channels={"A": "alpha"},
        channel_envs=fetch_env,
        actions=WakeActions(),
        instruction_stores={"A": InstructionStore(seed="seed")},
        skim_transcript=_noop_skim,
        budget=ToolBudget(),
    )
    agent = build_kimi_agent(TestModel(), system_prompt="s")
    tool = agent._function_toolset.tools["channel_history"]

    await tool.function(SimpleNamespace(deps=deps), channel_id="A")
    await tool.function(SimpleNamespace(deps=deps), channel_id="A")

    assert fetch_counts == {"A": 1}


def test_guild_agent_system_prompt_describes_multi_channel_scope():
    prompt = build_guild_agent_system_prompt(
        bot_display_name="smarter-bot",
        bot_user_id="42",
        guild_name="Smarter Dev",
    )

    assert "smarter-bot" in prompt
    assert "<@42>" in prompt
    assert "Smarter Dev" in prompt
    assert "channel_id" in prompt
    # The prompt must point at where the enabled set actually appears: the
    # wake brief's per-channel watch-instruction sections.
    assert "WATCH INSTRUCTIONS BY CHANNEL" in prompt
    assert OPERATING_POLICY_BRIEF in prompt


def _multi_channel_parity_deps() -> parity.ProactiveDeps:
    return parity.ProactiveDeps(
        bot=None,
        channel_id=1,
        guild_id=2,
        channel_name="alpha",
        enabled_channels={"A": "alpha", "B": "beta"},
        channel_envs={
            "A": ChannelEnvironment([_message("a1")], "bot-1"),
            "B": ChannelEnvironment([_message("b1")], "bot-1"),
        },
        actions=WakeActions(),
        instruction_stores={
            "A": InstructionStore(seed="alpha seed"),
            "B": InstructionStore(seed="beta seed"),
        },
        skim_transcript=_noop_skim,
        budget=ToolBudget(),
    )


async def test_memory_turn_counter_survives_channel_routing(monkeypatch):
    async def counting_remember(ctx, text):
        ctx.deps.memories_saved_this_turn += 1
        return "saved"

    monkeypatch.setattr(parity, "remember", counting_remember)
    deps = _multi_channel_parity_deps()
    agent = parity.build_proactive_agent(TestModel(), system_prompt="s")
    tool = agent._function_toolset.tools["remember"]

    await tool.function(SimpleNamespace(deps=deps), channel_id="A", text="one")
    await tool.function(SimpleNamespace(deps=deps), channel_id="B", text="two")

    assert deps.memories_saved_this_turn == 2


async def test_routed_context_carries_the_named_channels_name(monkeypatch):
    observed_routes = []

    async def recording_remember(ctx, text):
        observed_routes.append(
            (str(ctx.deps.channel_id), ctx.deps.channel_name)
        )
        return "saved"

    monkeypatch.setattr(parity, "remember", recording_remember)
    deps = _multi_channel_parity_deps()
    agent = parity.build_proactive_agent(TestModel(), system_prompt="s")
    tool = agent._function_toolset.tools["remember"]

    await tool.function(
        SimpleNamespace(deps=deps), channel_id="B", text="a note"
    )

    assert observed_routes == [("B", "beta")]
    assert deps.channel_name == "alpha"


async def test_disabled_generate_image_rejected_without_budget_or_work(
    monkeypatch,
):
    called = False

    async def recording_generate_image(ctx, prompt):
        nonlocal called
        called = True
        return "generated"

    monkeypatch.setattr(parity, "generate_image", recording_generate_image)
    deps = parity.ProactiveDeps(
        bot=None,
        channel_id=1,
        guild_id=2,
        enabled_channels={"A": "alpha"},
        channel_envs={"A": ChannelEnvironment([_message("a1")], "bot-1")},
        actions=WakeActions(),
        instruction_stores={"A": InstructionStore(seed="seed")},
        skim_transcript=_noop_skim,
        budget=ToolBudget(),
    )
    agent = parity.build_proactive_agent(TestModel(), system_prompt="s")
    tool = agent._function_toolset.tools["generate_image"]

    answer = await tool.function(
        SimpleNamespace(deps=deps), channel_id="B", prompt="a diagram"
    )

    assert answer.split("\n", 1)[1] == "Channel B is not enabled for the proactive bot."
    assert deps.budget.used == 0
    assert called is False


CHANNEL_PARITY_CALLS = {
    "web_search": {"query": "python"},
    "web_read": {"url": "https://example.com", "instruction": "summarize"},
    "add_reaction": {"message_id": "123", "emoji": "👍"},
    "report_behavior": {"classification": "spam"},
    "run_code": {"reason": "calculate", "code": "1 + 1"},
    "generate_image": {"prompt": "a software diagram"},
    "remember": {"text": "Alice likes parsers"},
    "register_handler": {
        "description": "react to deploys",
        "trigger_type": "new message",
    },
    "list_handlers": {},
    "delete_handler": {"handler_id": "handler-1"},
}


@pytest.mark.parametrize(
    ("tool_name", "arguments"), CHANNEL_PARITY_CALLS.items()
)
async def test_disabled_channel_rejects_channel_parity_tools_without_budget(
    tool_name,
    arguments,
):
    deps = parity.ProactiveDeps(
        bot=None,
        channel_id=1,
        guild_id=2,
        enabled_channels={"A": "alpha"},
        channel_envs={"A": ChannelEnvironment([_message("a1")], "bot-1")},
        actions=WakeActions(),
        instruction_stores={"A": InstructionStore(seed="seed")},
        skim_transcript=_noop_skim,
        budget=ToolBudget(),
    )
    agent = parity.build_proactive_agent(TestModel(), system_prompt="s")

    answer = await agent._function_toolset.tools[tool_name].function(
        SimpleNamespace(deps=deps), channel_id="B", **arguments
    )

    assert answer.split("\n", 1)[1] == "Channel B is not enabled for the proactive bot."
    assert deps.budget.used == 0


def test_channel_parity_tools_require_channel_id_and_keep_image_policy():
    agent = parity.build_proactive_agent(TestModel(), system_prompt="s")

    for tool_name in CHANNEL_PARITY_CALLS:
        parameter = inspect.signature(
            agent._function_toolset.tools[tool_name].function
        ).parameters["channel_id"]
        assert parameter.default is inspect.Parameter.empty

    image_description = parity._proactive_generate_image.__doc__ or ""
    assert "ONLY diagrams whose subject is software, CS, or math" in image_description
    assert "rate-limited per server" in image_description
    assert "quota remaining 0" in image_description


async def test_generate_image_routes_with_a_per_call_context_without_mutation(
    monkeypatch,
):
    observed_channel_ids = []

    async def recording_generate_image(ctx, prompt):
        observed_channel_ids.append(str(ctx.deps.channel_id))
        ctx.deps.pending_images.append(
            GeneratedImage(
                b"png",
                "image/png",
                "diagram.png",
                channel_id=str(ctx.deps.channel_id),
            )
        )
        return "generated"

    monkeypatch.setattr(parity, "generate_image", recording_generate_image)
    deps = parity.ProactiveDeps(
        bot=None,
        channel_id=1,
        guild_id=2,
        enabled_channels={"B": "beta"},
        channel_envs={"B": ChannelEnvironment([_message("b1")], "bot-1")},
        actions=WakeActions(),
        instruction_stores={"B": InstructionStore(seed="seed")},
        skim_transcript=_noop_skim,
        budget=ToolBudget(),
    )
    agent = parity.build_proactive_agent(TestModel(), system_prompt="s")

    answer = await agent._function_toolset.tools["generate_image"].function(
        SimpleNamespace(deps=deps), channel_id="B", prompt="a diagram"
    )

    assert answer.split("\n", 1)[1] == "generated"
    assert observed_channel_ids == ["B"]
    assert deps.channel_id == 1
    assert deps.pending_images[0].channel_id == "B"


async def test_enabled_parity_actions_and_statuses_route_without_deps_mutation():
    rest = SimpleNamespace(
        add_reaction=AsyncMock(),
        create_message=AsyncMock(),
    )
    deps = parity.ProactiveDeps(
        bot=SimpleNamespace(rest=rest),
        channel_id=1,
        guild_id=2,
        enabled_channels={"B": "beta"},
        channel_envs={"B": ChannelEnvironment([_message("123")], "bot-1")},
        actions=WakeActions(),
        instruction_stores={"B": InstructionStore(seed="seed")},
        skim_transcript=_noop_skim,
        budget=ToolBudget(),
    )
    agent = parity.build_proactive_agent(TestModel(), system_prompt="s")

    await agent._function_toolset.tools["add_reaction"].function(
        SimpleNamespace(deps=deps),
        channel_id="B",
        message_id="123",
        emoji="👍",
    )
    await agent._function_toolset.tools["run_code"].function(
        SimpleNamespace(deps=deps),
        channel_id="B",
        reason="calculate",
        code="1 + 1",
    )

    rest.add_reaction.assert_awaited_once_with("B", 123, "👍")
    assert rest.create_message.await_args.args[0] == "B"
    assert deps.channel_id == 1


def test_legacy_agent_deps_requires_an_explicit_enabled_channel_key():
    with pytest.raises(ValueError, match="enabled_channels"):
        AgentDeps(
            env=ChannelEnvironment([_message("a1")], "bot-1"),
            actions=WakeActions(),
            instruction_store=InstructionStore(seed="seed"),
            skim_transcript=_noop_skim,
            budget=ToolBudget(),
        )


def test_multi_channel_brief_requires_enabled_channel_labels():
    with pytest.raises(ValueError, match="enabled_channels"):
        adapter.build_wake_brief(
            [],
            0,
            instruction_stores={"A": InstructionStore(seed="seed")},
        )


def test_multi_channel_wake_brief_labels_notifications_and_instructions():
    alpha = InstructionStore(seed="alpha seed")
    beta = InstructionStore(seed="beta seed")
    alpha.set_instruction("watch the deploy", now=T)
    beta.set_instruction("watch the incident", now=T)
    notifications = [
        Notification(
            kind="watcher_summary",
            created_at=T,
            body="deploy update",
            channel_id="A",
            channel_name="alpha",
        ),
        Notification(
            kind="mention",
            created_at=T,
            body="incident update",
            channel_id="B",
            channel_name="beta",
        ),
    ]

    brief = adapter.build_wake_brief(
        notifications,
        0,
        instruction_stores={"A": alpha, "B": beta},
        enabled_channels={"A": "alpha", "B": "beta"},
    )

    assert "[#alpha]" in brief
    assert "[#beta]" in brief
    alpha_section = brief.split("WATCH INSTRUCTIONS FOR #alpha", 1)[1].split(
        "WATCH INSTRUCTIONS FOR #beta", 1
    )[0]
    beta_section = brief.split("WATCH INSTRUCTIONS FOR #beta", 1)[1]
    assert "watch the deploy" in alpha_section
    assert "watch the incident" not in alpha_section
    assert "watch the incident" in beta_section


class _RecordingRunner:
    def __init__(self):
        self.brief = ""
        self.deps = None

    async def wake(self, brief, deps):
        self.brief = brief
        self.deps = deps
        return "done", {
            "input_tokens": 1,
            "output_tokens": 1,
            "cache_read_tokens": 0,
        }


async def test_agent_consumer_passes_multi_channel_deps_and_brief():
    stores = {
        "A": InstructionStore(seed="alpha seed"),
        "B": InstructionStore(seed="beta seed"),
    }
    stores["A"].set_instruction("alpha instruction", now=T)
    stores["B"].set_instruction("beta instruction", now=T)
    runner = _RecordingRunner()
    consumer = adapter.AgentConsumer(
        agent_runner=runner,
        skim=None,
        instruction_stores=stores,
        enabled_channels={"A": "alpha", "B": "beta"},
        channel_envs={
            "A": ChannelEnvironment([_message("a1")], "bot-1"),
            "B": ChannelEnvironment([_message("b1")], "bot-1"),
        },
        agent_model_id="agent-model",
        notification_queue=NotificationQueue(),
    )
    context = ActivationContext(
        channel_name="alpha",
        guild_name="guild",
        bot_user_id="bot-1",
        activated_at=T,
        history=[],
        new_messages=[],
        channel_id="A",
    )

    await consumer.consume(context)

    assert "#alpha" in runner.brief
    assert "#beta" in runner.brief
    assert runner.deps.enabled_channels == {"A": "alpha", "B": "beta"}


# --- tool error containment and channel listing ------------------------------


async def test_tool_errors_return_to_the_agent_instead_of_raising():
    async def exploding_tool(ctx, channel_id: str) -> str:
        raise RuntimeError("discord fell over")

    guarded = agent_module.tool_errors_returned(exploding_tool)

    result = await guarded(None, channel_id="1")

    assert "exploding_tool failed" in result
    assert "RuntimeError" in result
    assert "discord fell over" in result


async def test_tool_success_passes_through_the_error_guard():
    async def fine_tool(ctx, value: str) -> str:
        return f"ok:{value}"

    guarded = agent_module.tool_errors_returned(fine_tool)

    assert (await guarded(None, value="x")).split("\n", 1)[1] == "ok:x"


def test_render_channel_list_names_every_enabled_channel():
    rendered = agent_module.render_channel_list(
        {"111": "general", "222": "technical-talk"}
    )
    assert "111 — #general" in rendered
    assert "222 — #technical-talk" in rendered

    assert "not enabled in any" in agent_module.render_channel_list({})
