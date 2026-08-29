"""Prod-parity tool wiring: the proactive agent gets the chat agent's tools.

`ProactiveDeps` satisfies both tool surfaces — the chat tools read the
ChatDeps fields (bot, channel_id, api_client, …) and the proactive
tools read the wake fields (env, actions, budget, …). Every parity tool is
wrapped so it spends the same per-wake budget as the native tools.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass

from pydantic_ai import Agent
from pydantic_ai.models import Model

from smarter_dev.bot.agents.chat_tools import ChatDeps
from smarter_dev.bot.agents.chat_tools import chat_tool_functions
from smarter_dev.bot.agents.handler_tools import handler_tool_functions
from smarter_dev.bot.proactive.agent import BUDGET_EXHAUSTED
from smarter_dev.bot.proactive.agent import ToolBudget
from smarter_dev.bot.proactive.agent import build_kimi_agent
from smarter_dev.bot.proactive.environment import ChannelEnvironment
from smarter_dev.bot.proactive.environment import InstructionStore
from smarter_dev.bot.proactive.environment import WakeActions


@dataclass(kw_only=True)
class ProactiveDeps(ChatDeps):
    """ChatDeps plus the proactive wake surface."""

    env: ChannelEnvironment
    actions: WakeActions
    instruction_store: InstructionStore
    skim_transcript: Callable[[str], Awaitable[str]]
    budget: ToolBudget
    request_mode: Callable[[str, int], str] | None = None
    drain_notifications: Callable[[], str] | None = None


def parity_tool_functions() -> list:
    """The chat agent's full tool set, in its own registration order."""
    return chat_tool_functions() + handler_tool_functions()


def _budgeted(tool_function):
    """Make a chat tool spend the proactive wake's tool budget."""

    @functools.wraps(tool_function)
    async def budgeted_tool(ctx, *args, **kwargs):
        if not ctx.deps.budget.try_spend():
            return BUDGET_EXHAUSTED
        return await tool_function(ctx, *args, **kwargs)

    return budgeted_tool


def build_proactive_agent(
    model: Model | str, *, system_prompt: str
) -> Agent:
    """The production proactive agent: native wake tools + full chat parity."""
    return build_kimi_agent(
        model,
        system_prompt=system_prompt,
        extra_tools=[_budgeted(f) for f in parity_tool_functions()],
        deps_type=ProactiveDeps,
    )
