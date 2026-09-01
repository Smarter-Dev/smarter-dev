"""Tests for the proactive agent's prod-parity tool wiring."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from types import SimpleNamespace

from pydantic_ai.models.test import TestModel

from smarter_dev.bot.proactive.agent import BUDGET_EXHAUSTED
from smarter_dev.bot.proactive.agent import ToolBudget
from smarter_dev.bot.proactive.environment import ChannelEnvironment
from smarter_dev.bot.proactive.environment import InstructionStore
from smarter_dev.bot.proactive.environment import WakeActions
from smarter_dev.bot.proactive.parity import ProactiveDeps
from smarter_dev.bot.proactive.parity import build_proactive_agent
from smarter_dev.bot.proactive.parity import parity_tool_functions
from smarter_dev.bot.proactive.types import ChannelMessage

PARITY_TOOL_NAMES = {
    "web_search",
    "web_read",
    "list_available_reactions",
    "add_reaction",
    "report_behavior",
    "run_code",
    "generate_image",
    "remember",
    "register_handler",
    "list_handlers",
    "delete_handler",
}


async def _noop_skim(transcript: str) -> str:
    return "skimmed"


def _deps(budget_limit: int = 8) -> ProactiveDeps:
    message = ChannelMessage(
        id="1",
        timestamp=datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
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
    return ProactiveDeps(
        bot=None,
        channel_id=1,
        guild_id=2,
        enabled_channels={"1": "general"},
        channel_envs={
            "1": ChannelEnvironment(visible=[message], bot_user_id="B1")
        },
        actions=WakeActions(),
        instruction_stores={"1": InstructionStore(seed="SEED")},
        skim_transcript=_noop_skim,
        budget=ToolBudget(limit=budget_limit),
    )


def test_parity_covers_every_prod_chat_tool():
    assert {f.__name__ for f in parity_tool_functions()} == PARITY_TOOL_NAMES


def test_proactive_agent_registers_native_plus_parity_tools():
    agent = build_proactive_agent(
        TestModel(custom_output_text="done"), system_prompt="s"
    )
    registered = set(agent._function_toolset.tools)
    native = {
        "lookup_message", "channel_history", "skim_messages",
        "send_channel_message", "reply_to_message", "react_to_message",
        "set_watch_instruction", "clear_watch_instruction",
        "list_watch_instructions", "set_monitoring_mode",
        "read_notifications",
    }
    assert native <= registered
    assert PARITY_TOOL_NAMES <= registered


async def test_read_notifications_is_free_and_drains_the_queue():
    deps = _deps(budget_limit=0)  # exhausted: a budgeted tool would refuse
    deps.drain_notifications = lambda: "NOTIFICATIONS: alice mentioned you"
    agent = build_proactive_agent(
        TestModel(call_tools=["read_notifications"], custom_output_text="done"),
        system_prompt="s",
    )
    result = await agent.run("go", deps=deps)
    messages = str(result.all_messages())
    assert "alice mentioned you" in messages
    assert BUDGET_EXHAUSTED not in messages
    assert deps.budget.used == 0


async def test_read_notifications_without_wiring_reports_none():
    deps = _deps()
    agent = build_proactive_agent(
        TestModel(call_tools=["read_notifications"], custom_output_text="done"),
        system_prompt="s",
    )
    result = await agent.run("go", deps=deps)
    assert "No new notifications" in str(result.all_messages())


def test_replay_tool_surface_matches_production():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.proactive_eval.replay_tools import replay_parity_tools

    assert {f.__name__ for f in replay_parity_tools()} == PARITY_TOOL_NAMES


async def test_replay_stub_answers_honestly_and_spends_budget():
    import sys
    from pathlib import Path
    from types import SimpleNamespace

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.proactive_eval import replay_tools

    deps = _deps(budget_limit=8)
    stub = next(
        f for f in replay_tools.replay_parity_tools()
        if f.__name__ == "run_code"
    )
    answer = await stub(SimpleNamespace(deps=deps), reason="r", code="c")
    assert "unavailable in replay" in answer
    assert deps.budget.used == 1


async def test_channel_parity_tool_is_callable_through_the_model():
    deps = _deps(budget_limit=0)  # exhausted: any spend attempt would refuse
    agent = build_proactive_agent(
        TestModel(call_tools=["list_handlers"], custom_output_text="done"),
        system_prompt="s",
    )
    result = await agent.run("go", deps=deps)
    messages = str(result.all_messages())
    # The model synthesizes channel_id; whichever branch it hits, the wrapper
    # bound through the real tool machinery and refused before doing work.
    assert (
        "is not enabled for the proactive bot" in messages
        or BUDGET_EXHAUSTED in messages
    )
    assert deps.budget.used == 0


async def test_parity_tools_spend_the_wake_budget():
    deps = _deps(budget_limit=0)  # exhausted from the start
    agent = build_proactive_agent(
        TestModel(custom_output_text="done"),
        system_prompt="s",
    )
    result = await agent._function_toolset.tools["list_handlers"].function(
        SimpleNamespace(deps=deps), channel_id="1"
    )
    # The parity tool was refused by the budget wrapper, never executed
    # (executing list_handlers would need an API client and raise).
    assert result == BUDGET_EXHAUSTED
    assert deps.budget.used == 0
