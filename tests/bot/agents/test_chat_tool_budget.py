"""Tests for the chat agent's per-turn tool budget.

The guard has two independent layers and both are covered here: hiding the
tool definitions from the model (``get_tools``) and refusing execution even
when a call arrives anyway (``call_tool``). The second layer is the one that
matters — a model that remembers a tool from earlier context can emit a call
for it after the definitions are gone.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from smarter_dev.bot.agents.chat_tool_budget import (
    TOOL_BUDGET_REFUSAL,
    TOOL_BUDGET_NOTICE,
    ToolBudgetGuard,
    tool_budget_notice,
    tool_budget_spent,
)
from smarter_dev.bot.agents.chat_tools import ChatDeps


def _ctx(
    budget: int | None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    tools_disabled: bool = False,
):
    """A stand-in RunContext carrying just what the guard reads."""
    return SimpleNamespace(
        deps=ChatDeps(
            bot=None,
            channel_id=1,
            guild_id=2,
            tool_token_budget=budget,
            tools_disabled=tools_disabled,
        ),
        usage=SimpleNamespace(
            input_tokens=input_tokens, output_tokens=output_tokens
        ),
    )


@dataclass
class _StubToolset:
    """Minimal wrapped toolset recording whether it was actually invoked."""

    called: list[str]

    async def get_tools(self, ctx: Any) -> dict[str, Any]:
        return {"run_code": object()}

    async def call_tool(
        self, name: str, tool_args: dict, ctx: Any, tool: Any
    ) -> Any:
        self.called.append(name)
        return "executed for real"


# -- the spent predicate --------------------------------------------------


def test_budget_none_is_unlimited():
    assert tool_budget_spent(_ctx(None, input_tokens=10_000_000)) is False


def test_budget_zero_is_unlimited():
    assert tool_budget_spent(_ctx(0, input_tokens=10_000_000)) is False


def test_under_budget_is_not_spent():
    assert tool_budget_spent(_ctx(1000, 400, 400)) is False


def test_budget_counts_input_and_output_together():
    # 600 + 500 crosses 1000 even though neither alone does.
    assert tool_budget_spent(_ctx(1000, 600, 500)) is True


def test_budget_exactly_reached_is_spent():
    assert tool_budget_spent(_ctx(1000, 600, 400)) is True


def test_tools_disabled_applies_from_the_very_first_step():
    """A fresh run has zero usage, so only an explicit flag can pre-empt it.

    The overlong-reply rewrite re-runs the tool-carrying agent with a prompt
    stating an exact character count — the exact bait that started the
    incident — and it must not be able to reach for a tool to check it.
    """
    assert tool_budget_spent(_ctx(None, 0, 0, tools_disabled=True)) is True


# -- layer 1: tool definitions disappear ----------------------------------


@pytest.mark.asyncio
async def test_tools_visible_while_under_budget():
    guard = ToolBudgetGuard(wrapped=_StubToolset(called=[]))
    tools = await guard.get_tools(_ctx(1000, 100))
    assert "run_code" in tools


@pytest.mark.asyncio
async def test_tools_hidden_once_budget_spent():
    guard = ToolBudgetGuard(wrapped=_StubToolset(called=[]))
    tools = await guard.get_tools(_ctx(1000, 5000))
    assert tools == {}


# -- layer 2: execution is blocked even if a call arrives anyway ----------


@pytest.mark.asyncio
async def test_tool_executes_while_under_budget():
    stub = _StubToolset(called=[])
    guard = ToolBudgetGuard(wrapped=stub)
    result = await guard.call_tool("run_code", {}, _ctx(1000, 100), None)
    assert result == "executed for real"
    assert stub.called == ["run_code"]


@pytest.mark.asyncio
async def test_spent_budget_blocks_execution_entirely():
    """The load-bearing guarantee: the wrapped tool never runs."""
    stub = _StubToolset(called=[])
    guard = ToolBudgetGuard(wrapped=stub)
    result = await guard.call_tool("run_code", {}, _ctx(1000, 5000), None)
    assert result == TOOL_BUDGET_REFUSAL
    assert stub.called == []


@pytest.mark.asyncio
async def test_refusal_is_returned_not_raised():
    """A return keeps the turn alive; an exception would abort it."""
    guard = ToolBudgetGuard(wrapped=_StubToolset(called=[]))
    result = await guard.call_tool("web_search", {}, _ctx(1000, 5000), None)
    assert isinstance(result, str)
    assert "did not run" in result.lower()


# -- the notice telling the model why its tools vanished ------------------


def _history() -> list:
    return [
        ModelRequest(parts=[UserPromptPart(content="hi")]),
        ModelResponse(parts=[TextPart(content="hello")]),
    ]


def test_no_notice_while_under_budget():
    messages = _history()
    assert tool_budget_notice(_ctx(1000, 100), messages) == messages


def test_notice_appended_once_budget_spent():
    result = tool_budget_notice(_ctx(1000, 5000), _history())
    assert len(result) == 3
    assert TOOL_BUDGET_NOTICE in str(result[-1])


def test_notice_is_not_duplicated_across_steps():
    ctx = _ctx(1000, 5000)
    once = tool_budget_notice(ctx, _history())
    twice = tool_budget_notice(ctx, once)
    assert once == twice


def test_notice_does_not_mutate_caller_history():
    messages = _history()
    original_length = len(messages)
    tool_budget_notice(_ctx(1000, 5000), messages)
    assert len(messages) == original_length
