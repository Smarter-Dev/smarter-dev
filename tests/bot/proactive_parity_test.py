"""Tests for the proactive agent's prod-parity tool wiring."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic_ai.models.test import TestModel

from smarter_dev.bot.proactive.agent import BUDGET_EXHAUSTED, ToolBudget
from smarter_dev.bot.proactive.environment import (
    ChannelEnvironment,
    InstructionStore,
    WakeActions,
)
from smarter_dev.bot.proactive.parity import (
    ProactiveDeps,
    build_proactive_agent,
    parity_tool_functions,
)
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
        env=ChannelEnvironment(visible=[message], bot_user_id="B1"),
        actions=WakeActions(),
        instruction_store=InstructionStore(seed="SEED"),
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
        "update_watch_instructions",
    }
    assert native <= registered
    assert PARITY_TOOL_NAMES <= registered


async def test_parity_tools_spend_the_wake_budget():
    deps = _deps(budget_limit=0)  # exhausted from the start
    agent = build_proactive_agent(
        TestModel(call_tools=["list_handlers"], custom_output_text="done"),
        system_prompt="s",
    )
    result = await agent.run("go", deps=deps)
    # The parity tool was refused by the budget wrapper, never executed
    # (executing list_handlers would need an API client and raise).
    messages = str(result.all_messages())
    assert BUDGET_EXHAUSTED in messages
    assert deps.budget.used == 0
