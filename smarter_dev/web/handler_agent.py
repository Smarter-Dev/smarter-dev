"""The gathering agent a handler script can spawn (worker tier).

Per the capability model, agents *only gather*: they return plaintext and can
neither send messages nor add reactions. The script takes the returned string
and decides what (if anything) to emit. Two shapes:

- ``has_tools=True`` — a web-search + web-read agent. Both tools draw from the
  fire's shared :class:`~smarter_dev.web.handler_budget.HandlerBudget`, so an
  agent that reads two pages leaves the script one read.
- ``has_tools=False`` — a pure text transform: prompt in, plaintext out, no tools.

Model: Claude Haiku 5.5 by default (GPT-6 Luna until 2026-10-17, #199),
overridable with ``HANDLER_AGENT_MODEL``. A budget cap raised inside a tool propagates
out of ``agent.run`` and stops the fire loud.

Each call's usage goes on the ledger as the guild's Discord spend (``handler_agent``,
and ``handler_media_reader`` for the images and audio its ``web_read`` described),
raised or not: a cap that stops the run after the model answered was billed.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import httpx
from pydantic_ai import Agent
from pydantic_ai import RunContext

from smarter_dev.shared.model_catalog import CatalogModel
from smarter_dev.shared.model_catalog import catalog_model_for_id
from smarter_dev.shared.model_router import build_model_for
from smarter_dev.shared.model_router import model_settings_for
from smarter_dev.shared.redis_client import get_redis_client
from smarter_dev.shared.usage_meter import UsageMeter
from smarter_dev.web.handler_budget import HandlerBudget
from smarter_dev.web.media_read import media_meter
from smarter_dev.web.media_read import read_url
from smarter_dev.web.research_tools import brave_search
from smarter_dev.web.usage_metering import record_meter_now

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-5-5"
MODEL_ENV_VAR = "HANDLER_AGENT_MODEL"

_GATHER_PROMPT = (
    "You are a gathering assistant for a Discord automation. Do exactly what the "
    "instruction asks and reply with PLAIN TEXT only — no markdown scaffolding, no "
    "preamble. You cannot send messages or react; your reply is handed back to the "
    "script that called you. Be concise. Use your tools sparingly: search or read "
    "only when the instruction genuinely needs current or external information. "
    "web_read works on ANY url — web pages, PDFs, images, and audio — so you can "
    "read a screenshot or document by passing its url."
)
_TRANSFORM_PROMPT = (
    "You transform text for a Discord automation. Do exactly what the instruction "
    "asks and reply with PLAIN TEXT only. You have no tools and no external access — "
    "work solely from the text in the prompt. Your reply is handed back to the "
    "script that called you."
)


class _GatherDeps:
    """Carries the shared per-fire budget into the agent's tools."""

    def __init__(self, budget: HandlerBudget, media: UsageMeter | None = None) -> None:
        self.budget = budget
        # The media describes this run's web_read makes, metered on their own.
        self.media = media


@dataclass(frozen=True)
class GatherUsage:
    """Where one fire's agent calls go on the ledger."""

    session_factory: Callable[[], Any]
    job_id: str
    handler_id: str
    handler_kind: str
    guild_id: str
    channel_id: str


def _catalog_model() -> CatalogModel:
    model_id = os.getenv(MODEL_ENV_VAR, DEFAULT_MODEL)
    try:
        return catalog_model_for_id(model_id)
    except ValueError:
        raise RuntimeError(
            f"Handler agent model is not in the catalog: {model_id}"
        ) from None


async def _web_search(ctx: RunContext[_GatherDeps], query: str) -> list[dict[str, str]]:
    """Search the web and return up to 5 result snippets."""
    ctx.deps.budget.spend_web_search()
    async with httpx.AsyncClient(timeout=15.0) as client:
        return await brave_search(client, query, num_results=5)


async def _web_read(ctx: RunContext[_GatherDeps], url: str, instruction: str) -> str:
    """Read any URL — web page, PDF, image, or audio — into instruction-guided text.

    Images and audio are described by a multimodal agent; PDFs are extracted;
    everything else is read as page text. Media descriptions are cached in Redis
    by file content hash + instruction, so the same screenshot posted across many
    messages is only read once.
    """
    ctx.deps.budget.spend_web_read()
    try:
        redis = get_redis_client()
    except Exception:  # noqa: BLE001 — caching is best-effort
        redis = None
    token = media_meter.set(ctx.deps.media)
    try:
        return await read_url(url, instruction, redis=redis)
    finally:
        media_meter.reset(token)


_agents: dict[bool, Agent] = {}


def _get_agent(has_tools: bool) -> Agent:
    if has_tools not in _agents:
        catalog_model = _catalog_model()
        _agents[has_tools] = Agent(
            build_model_for(catalog_model),
            deps_type=_GatherDeps,
            system_prompt=_GATHER_PROMPT if has_tools else _TRANSFORM_PROMPT,
            tools=[_web_search, _web_read] if has_tools else [],
            # The model's default reasoning. A transform is one request, so
            # only the tool-using agent keeps the message cache breakpoint.
            model_settings=model_settings_for(catalog_model, cache_messages=has_tools),
        )
    return _agents[has_tools]


async def run_gathering_agent(
    prompt: str,
    has_tools: bool,
    budget: HandlerBudget,
    *,
    usage: GatherUsage | None = None,
) -> str:
    """Run a spawned gathering/transform agent and return its plaintext output."""
    agent = _get_agent(has_tools)
    if usage is None:
        result = await agent.run(prompt, deps=_GatherDeps(budget=budget))
        return str(result.output)
    meter, media = UsageMeter(), UsageMeter()
    # The fire's nth agent call, for the details. The key also carries this
    # run's own id: a retried fire job (or two calls a script runs at once)
    # is new spend, never a replay of another run's row.
    call = budget.agent_calls
    run_id = uuid4().hex
    try:
        result = await agent.run(
            prompt,
            deps=_GatherDeps(budget=budget, media=media),
            model=meter.wrap(agent.model),
        )
        return str(result.output)
    finally:
        details = {
            "job_id": usage.job_id,
            "handler_id": usage.handler_id,
            "handler_kind": usage.handler_kind,
            "guild_id": usage.guild_id,
            "channel_id": usage.channel_id,
            "agent_call": call,
        }
        for operation_type, used in (
            ("handler_agent", meter),
            ("handler_media_reader", media),
        ):
            await record_meter_now(
                used,
                session_factory=usage.session_factory,
                operation_key=f"{operation_type}:{usage.job_id}:{call}:{run_id}",
                product_mode="discord",
                operation_type=operation_type,
                details=details,
            )


# -- the opt-out (#100) ------------------------------------------------------------

# Neutral on purpose: scripts often post the agent's answer publicly, and it
# must not say who opted out.
GATHERING_REFUSED = "The AI agent is not available for this event."
# The trigger-context fields (``bot/plugins/handler_events.py``) that name the
# member whose action fired a handler: a message's author, a thread's creator,
# a joining, leaving or updated member, a reaction's user, a slash-command
# invoker.
_TRIGGER_AUTHOR_FIELDS = (
    "author_id",
    "creator_id",
    "member_id",
    "reaction_user_id",
    "interaction_user_id",
)


def gathering_agent_for(
    trigger_context: dict,
    session_factory: Callable[[], Any],
    *,
    usage: GatherUsage | None = None,
) -> Callable[..., Awaitable[str]]:
    """The gathering agent one fire may use, gated on the opt-out (#100).

    Handlers keep firing for everyone: the opt-out covers the AI assistant,
    not scripted automations. When the script calls the agent, the block list
    is read from the database: a fire triggered by someone on it gets
    :data:`GATHERING_REFUSED` and no model call, and any other fire's prompt
    has blocked ids redacted before the model sees it. ``usage`` puts each
    call's model usage on the ledger.
    """

    async def gated(prompt: str, has_tools: bool, budget: HandlerBudget) -> str:
        from smarter_dev.web.privacy_gate import load_gate

        async with session_factory() as session:
            gate = await load_gate(session)
        if gate.refuses(*(trigger_context.get(key) for key in _TRIGGER_AUTHOR_FIELDS)):
            return GATHERING_REFUSED
        return await run_gathering_agent(
            gate.redact(prompt), has_tools, budget, usage=usage
        )

    return gated
