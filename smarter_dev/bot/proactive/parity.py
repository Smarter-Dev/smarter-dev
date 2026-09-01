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
from copy import copy
from dataclasses import dataclass
from dataclasses import field

from pydantic_ai import Agent
from pydantic_ai.models import Model

from smarter_dev.bot.agents.chat_tools import ChatDeps
from smarter_dev.bot.agents.chat_tools import add_reaction
from smarter_dev.bot.agents.chat_tools import chat_tool_functions
from smarter_dev.bot.agents.chat_tools import generate_image
from smarter_dev.bot.agents.chat_tools import remember
from smarter_dev.bot.agents.chat_tools import report_behavior
from smarter_dev.bot.agents.chat_tools import run_code
from smarter_dev.bot.agents.chat_tools import web_read
from smarter_dev.bot.agents.chat_tools import web_search
from smarter_dev.bot.agents.handler_tools import delete_handler
from smarter_dev.bot.agents.handler_tools import handler_tool_functions
from smarter_dev.bot.agents.handler_tools import list_handlers
from smarter_dev.bot.agents.handler_tools import register_handler
from smarter_dev.bot.proactive.agent import BUDGET_EXHAUSTED
from smarter_dev.bot.proactive.agent import ToolBudget
from smarter_dev.bot.proactive.agent import build_kimi_agent
from smarter_dev.bot.proactive.agent import disabled_channel_error
from smarter_dev.bot.proactive.agent import fetch_channel_env
from smarter_dev.bot.proactive.environment import ChannelEnvironment
from smarter_dev.bot.proactive.environment import InstructionStore
from smarter_dev.bot.proactive.environment import WakeActions


@dataclass(kw_only=True)
class ProactiveDeps(ChatDeps):
    """ChatDeps plus the proactive wake surface."""

    actions: WakeActions
    skim_transcript: Callable[[str], Awaitable[str]]
    budget: ToolBudget
    enabled_channels: dict[str, str] = field(default_factory=dict)
    channel_envs: (
        Callable[[str], ChannelEnvironment | Awaitable[ChannelEnvironment]]
        | dict[str, ChannelEnvironment]
        | None
    ) = None
    instruction_stores: dict[str, InstructionStore] = field(
        default_factory=dict
    )
    env: ChannelEnvironment | None = None
    instruction_store: InstructionStore | None = None
    request_mode: Callable[[str, str, int], str] | None = None
    drain_notifications: Callable[[], str] | None = None
    resolved_channel_envs: dict[str, ChannelEnvironment] = field(
        default_factory=dict, init=False, repr=False
    )

    def __post_init__(self) -> None:
        legacy_channel_id = str(self.channel_id)
        if self.env is not None and self.channel_envs is None:
            self.channel_envs = {legacy_channel_id: self.env}
        if self.instruction_store is not None and not self.instruction_stores:
            self.instruction_stores = {
                legacy_channel_id: self.instruction_store
            }
        if not self.enabled_channels and (
            self.env is not None or self.instruction_store is not None
        ):
            self.enabled_channels = {
                legacy_channel_id: self.channel_name or legacy_channel_id
            }
        if self.env is None and isinstance(self.channel_envs, dict):
            self.env = next(iter(self.channel_envs.values()), None)
        if self.instruction_store is None:
            self.instruction_store = next(
                iter(self.instruction_stores.values()), None
            )

    async def channel_env(self, channel_id: str) -> ChannelEnvironment:
        return await fetch_channel_env(
            self.channel_envs, channel_id, self.resolved_channel_envs
        )

    def channel_instruction_store(self, channel_id: str) -> InstructionStore:
        return self.instruction_stores[channel_id]


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


def _routed_context(ctx, channel_id: str):
    """Copy a tool context with a per-call channel route."""
    routed_deps = copy(ctx.deps)
    routed_deps.channel_id = channel_id
    routed_deps.channel_name = (
        ctx.deps.enabled_channels.get(channel_id) or channel_id
    )
    routed_context = copy(ctx)
    routed_context.deps = routed_deps
    return routed_context


async def _call_routed(ctx, channel_id: str, tool_function, *args, **kwargs):
    """Run a chat tool against a copy of the context routed to one channel.

    The shallow deps copy shares the original's lists (pending_images,
    saved_memory_texts), but a scalar rebind lands on the copy only — sync
    the per-turn memory counter back so MAX_MEMORIES_PER_TURN holds across
    channels.
    """
    routed_context = _routed_context(ctx, channel_id)
    result = await tool_function(routed_context, *args, **kwargs)
    ctx.deps.memories_saved_this_turn = (
        routed_context.deps.memories_saved_this_turn
    )
    return result


def _channel_tool_error(ctx, channel_id: str) -> str | None:
    if error := disabled_channel_error(ctx.deps, channel_id):
        return error
    if not ctx.deps.budget.try_spend():
        return BUDGET_EXHAUSTED
    return None


async def _proactive_web_search(ctx, channel_id: str, query: str):
    """Search the web for one enabled channel and post status there."""
    if error := _channel_tool_error(ctx, channel_id):
        return error
    return await _call_routed(ctx, channel_id, web_search, query)


async def _proactive_web_read(
    ctx, channel_id: str, url: str, instruction: str
):
    """Read a URL for one enabled channel and post status there."""
    if error := _channel_tool_error(ctx, channel_id):
        return error
    return await _call_routed(ctx, channel_id, web_read, url, instruction)


async def _proactive_add_reaction(
    ctx, channel_id: str, message_id: str, emoji: str
):
    """React to a message in one enabled channel."""
    if error := _channel_tool_error(ctx, channel_id):
        return error
    return await _call_routed(
        ctx, channel_id, add_reaction, message_id, emoji
    )


async def _proactive_report_behavior(
    ctx, channel_id: str, classification: str
):
    """Report disruptive behavior from one enabled channel."""
    if error := _channel_tool_error(ctx, channel_id):
        return error
    return await _call_routed(
        ctx, channel_id, report_behavior, classification
    )


async def _proactive_run_code(
    ctx, channel_id: str, reason: str, code: str
):
    """Run code for one enabled channel and post status there."""
    if error := _channel_tool_error(ctx, channel_id):
        return error
    return await _call_routed(ctx, channel_id, run_code, reason, code)


async def _proactive_generate_image(ctx, channel_id: str, prompt: str) -> str:
    """Generate an image attached to a reply in `channel_id`. ONLY diagrams whose subject is software, CS, or math — nothing else. `prompt` is a detailed illustrator brief, reviewed before drawing; rate-limited per server — when metadata shows quota remaining 0, don't call until it resets, say images are rate-limited and answer in text."""
    if error := _channel_tool_error(ctx, channel_id):
        return error
    return await _call_routed(ctx, channel_id, generate_image, prompt)


async def _proactive_remember(ctx, channel_id: str, text: str):
    """Keep a memory attributed to one enabled channel."""
    if error := _channel_tool_error(ctx, channel_id):
        return error
    return await _call_routed(ctx, channel_id, remember, text)


async def _proactive_register_handler(
    ctx,
    channel_id: str,
    description: str,
    trigger_type: str,
    settings: dict | None = None,
):
    """File a persistent automation for one enabled channel."""
    if error := _channel_tool_error(ctx, channel_id):
        return error
    return await _call_routed(
        ctx,
        channel_id,
        register_handler,
        description,
        trigger_type,
        settings,
    )


async def _proactive_list_handlers(ctx, channel_id: str):
    """List persistent automations for one enabled channel."""
    if error := _channel_tool_error(ctx, channel_id):
        return error
    return await _call_routed(ctx, channel_id, list_handlers)


async def _proactive_delete_handler(
    ctx, channel_id: str, handler_id: str
):
    """Delete a handler while acting for one enabled channel."""
    if error := _channel_tool_error(ctx, channel_id):
        return error
    return await _call_routed(
        ctx, channel_id, delete_handler, handler_id
    )


_CHANNEL_TOOL_WRAPPERS = {
    "web_search": _proactive_web_search,
    "web_read": _proactive_web_read,
    "add_reaction": _proactive_add_reaction,
    "report_behavior": _proactive_report_behavior,
    "run_code": _proactive_run_code,
    "generate_image": _proactive_generate_image,
    "remember": _proactive_remember,
    "register_handler": _proactive_register_handler,
    "list_handlers": _proactive_list_handlers,
    "delete_handler": _proactive_delete_handler,
}

_CHANNEL_TOOL_FUNCTIONS = {
    "web_search": web_search,
    "web_read": web_read,
    "add_reaction": add_reaction,
    "report_behavior": report_behavior,
    "run_code": run_code,
    "generate_image": generate_image,
    "remember": remember,
    "register_handler": register_handler,
    "list_handlers": list_handlers,
    "delete_handler": delete_handler,
}

for _tool_name, _tool_wrapper in _CHANNEL_TOOL_WRAPPERS.items():
    _tool_wrapper.__name__ = _tool_name
    if _tool_name != "generate_image":
        _original_doc = _CHANNEL_TOOL_FUNCTIONS[_tool_name].__doc__ or ""
        _tool_wrapper.__doc__ = (
            f"{_original_doc} `channel_id` must name an enabled proactive "
            "channel."
        )


def build_proactive_agent(
    model: Model | str, *, system_prompt: str
) -> Agent:
    """The production proactive agent: native wake tools + full chat parity."""
    parity_tools = [
        _CHANNEL_TOOL_WRAPPERS.get(
            tool_function.__name__, _budgeted(tool_function)
        )
        for tool_function in parity_tool_functions()
    ]
    return build_kimi_agent(
        model,
        system_prompt=system_prompt,
        extra_tools=parity_tools,
        deps_type=ProactiveDeps,
    )
