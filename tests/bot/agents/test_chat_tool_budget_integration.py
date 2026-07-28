"""End-to-end proof that a spent tool budget really stops tool execution.

The unit tests exercise the guard's own logic; these drive a real ``Agent``
with a model that is *deliberately hostile* — it emits a tool call on every
step, including after the tools have been withdrawn. That is the case the
guard exists for: a model with earlier tool calls in its context can keep
emitting them long after the definitions are gone.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from smarter_dev.bot.agents.chat_tool_budget import budgeted_toolset
from smarter_dev.bot.agents.chat_tools import ChatDeps


@dataclass
class _Spy:
    """Records every real execution of the guarded tool."""

    executions: list[str] = field(default_factory=list)
    tools_seen_per_step: list[int] = field(default_factory=list)


@pytest.fixture
def spy() -> _Spy:
    return _Spy()


def _build_agent(spy: _Spy, *, steps: int) -> Agent:
    """An agent whose model calls ``expensive_tool`` on the first ``steps``."""

    async def expensive_tool(ctx: RunContext[ChatDeps], note: str) -> str:
        spy.executions.append(note)
        return "did expensive work"

    def model_function(
        messages: list[ModelMessage], info: AgentInfo
    ) -> ModelResponse:
        # How many tools the model was offered on this step — 0 once withdrawn.
        spy.tools_seen_per_step.append(len(info.function_tools))
        if len(spy.tools_seen_per_step) <= steps:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="expensive_tool", args={"note": "burn tokens"}
                    )
                ]
            )
        return ModelResponse(parts=[TextPart(content="final answer")])

    return Agent(
        FunctionModel(model_function),
        deps_type=ChatDeps,
        output_type=str,
        toolsets=[budgeted_toolset([expensive_tool])],
    )


def _deps(budget: int | None) -> ChatDeps:
    return ChatDeps(bot=None, channel_id=1, guild_id=2, tool_token_budget=budget)


@pytest.mark.asyncio
async def test_tool_runs_normally_when_budget_is_unlimited(spy):
    agent = _build_agent(spy, steps=1)
    result = await agent.run("go", deps=_deps(None))
    assert result.output == "final answer"
    assert spy.executions == ["burn tokens"]
    # Tools stayed advertised throughout.
    assert all(count == 1 for count in spy.tools_seen_per_step)


@pytest.mark.asyncio
async def test_spent_budget_withdraws_the_tool_definitions(spy):
    # A 1-token budget is spent the moment the first response lands.
    agent = _build_agent(spy, steps=1)
    await agent.run("go", deps=_deps(1))
    # First step was offered the tool; every later step saw none.
    assert spy.tools_seen_per_step[0] == 1
    assert spy.tools_seen_per_step[1:] == [0] * len(spy.tools_seen_per_step[1:])


@pytest.mark.asyncio
async def test_forced_tool_call_never_executes_once_budget_is_spent(spy):
    """The guarantee: even called explicitly, the tool body does not run."""
    agent = _build_agent(spy, steps=1)
    await agent.run("go", deps=_deps(1))
    assert spy.executions == []


@pytest.mark.asyncio
async def test_model_still_produces_an_answer_after_tools_are_withdrawn(spy):
    """Losing tools must not cost the user their reply."""
    agent = _build_agent(spy, steps=1)
    result = await agent.run("go", deps=_deps(1))
    assert result.output == "final answer"


@pytest.mark.asyncio
async def test_relentless_tool_calling_cannot_burn_the_turn_down(spy):
    """A model that never stops calling tools still executes none of them."""
    agent = _build_agent(spy, steps=50)
    with pytest.raises(Exception):
        # It never yields an answer, so pydantic_ai eventually gives up —
        # the point is that it does so without doing any real work.
        await agent.run("go", deps=_deps(1))
    assert spy.executions == []
