"""Per-turn tool budget: stop the tool loop without losing the answer.

A single agent run is otherwise unbounded — nothing in Pydantic AI caps how
many times a model may call tools, and the channel token budget only gates
whether the *next* turn starts. A model that keeps reaching for a tool can
therefore burn a turn's worth of context on every round trip until it either
answers or the run limit trips.

This module bounds that with a budget the engine sizes per turn, enforced in
two independent layers:

1. ``get_tools`` stops advertising the tools once the budget is spent, so the
   model's next request carries no tool definitions at all.
2. ``call_tool`` refuses to execute regardless of what the model sends. Layer
   one only controls what the model is *told*; a model that remembers a tool
   from earlier in the conversation can still emit a call for it. This layer
   is what actually guarantees the tool does not run.

Only the agent's own tools are affected — output tools live in a separate
toolset, so the model keeps its ability to finish the turn. The point is to
take away the model's hands, not its voice: once the budget is gone the only
move left is to answer with what it already has.

The refusal is *returned*, not raised. A raised error becomes a retry prompt
and, twice over, aborts the run into the engine's error path — costing the
user their reply. A returned string reads to the model as an ordinary tool
result, so it can absorb the refusal and write its answer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from dataclasses import replace
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.messages import ModelMessage
from pydantic_ai.messages import ModelRequest
from pydantic_ai.messages import UserPromptPart
from pydantic_ai.toolsets import AbstractToolset
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.toolsets import ToolsetTool
from pydantic_ai.toolsets import WrapperToolset

from smarter_dev.bot.agents.chat_tools import ChatDeps

logger = logging.getLogger(__name__)

# Fallback ceiling for a turn whose channel has no enforced budget. Channels
# without an admin override are unlimited (a 0 budget never blocks), which is
# most of them — without a flat cap they would keep the old unbounded
# behaviour. Sized well above a normal research turn (the heaviest legitimate
# turns observed run a few tens of thousands of tokens) so it only ever bites
# a runaway loop.
TURN_TOOL_TOKEN_CAP = 150_000

TOOL_BUDGET_REFUSAL = (
    "TOOL BUDGET SPENT — this turn has used its entire tool allowance. This "
    "tool did not run and no further tool call will run either. Write your "
    "final answer now using what you already have."
)

TOOL_BUDGET_NOTICE = (
    "You have spent this turn's tool budget, so your tools have been "
    "withdrawn. Do not attempt to call one. Write your final answer now from "
    "what you already gathered — an answer built on partial findings is far "
    "better than no answer."
)


def tool_budget_spent(ctx: RunContext[ChatDeps]) -> bool:
    """Whether this run has consumed the tool budget it was given.

    A ``None`` or non-positive budget means unlimited (matching the channel
    budget convention), so the guard never fires for it. Input and output
    tokens both count: the loop's cost is dominated by re-sending a growing
    history, but a model that reasons at length between calls is just as
    expensive.

    ``tools_disabled`` short-circuits the whole thing — a run that should
    never touch a tool can't express that as a budget, since it starts at zero
    usage and would get one free step.
    """
    if getattr(ctx.deps, "tools_disabled", False):
        return True
    budget = getattr(ctx.deps, "tool_token_budget", None)
    if not budget or budget <= 0:
        return False
    usage = ctx.usage
    spent = (usage.input_tokens or 0) + (usage.output_tokens or 0)
    return spent >= budget


@dataclass
class ToolBudgetGuard(WrapperToolset[ChatDeps]):
    """Wraps a toolset so a spent budget both hides and disables its tools."""

    async def get_tools(
        self, ctx: RunContext[ChatDeps]
    ) -> dict[str, ToolsetTool[ChatDeps]]:
        if tool_budget_spent(ctx):
            return {}
        return await super().get_tools(ctx)

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[ChatDeps],
        tool: ToolsetTool[ChatDeps],
    ) -> Any:
        if tool_budget_spent(ctx):
            # Reached only when the model calls a tool it can no longer see —
            # worth a log line, since it means the model ignored both the
            # missing definitions and the notice.
            logger.warning(
                "Tool %r refused: turn tool budget spent (channel=%s)",
                name,
                getattr(ctx.deps, "channel_id", None),
            )
            return TOOL_BUDGET_REFUSAL
        return await super().call_tool(name, tool_args, ctx, tool)


def budgeted_toolset(functions: list) -> AbstractToolset[ChatDeps]:
    """Bundle ``functions`` into a toolset governed by the turn's budget."""
    return ToolBudgetGuard(wrapped=FunctionToolset(functions))


def tool_budget_notice(
    ctx: RunContext[ChatDeps], messages: list[ModelMessage]
) -> list[ModelMessage]:
    """Explain the vanished tools to the model (a history processor).

    Without this the tools simply disappear mid-turn with no explanation,
    which invites the model to either retry the call or return something
    degenerate. Registered after :func:`compact_history` so it appends to the
    already-compacted history.
    """
    if not tool_budget_spent(ctx):
        return messages
    # The processor's return value replaces the run's history, so appending
    # unconditionally would stack a fresh copy on every subsequent request.
    if any(TOOL_BUDGET_NOTICE in str(message) for message in messages):
        return messages
    return [*messages, ModelRequest(parts=[UserPromptPart(content=TOOL_BUDGET_NOTICE)])]


def resolve_tool_token_budget(remaining_channel_budget: int | None) -> int:
    """The tool budget for a turn, given what its channel has left.

    Takes the tighter of the channel's remaining allowance and the flat cap,
    so an enforced channel can't blow past its own budget in a single turn and
    an unlimited one still can't run away.
    """
    if remaining_channel_budget is None:
        return TURN_TOOL_TOKEN_CAP
    return min(max(remaining_channel_budget, 0), TURN_TOOL_TOKEN_CAP)
