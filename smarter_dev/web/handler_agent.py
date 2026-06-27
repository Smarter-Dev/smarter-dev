"""The gathering agent a handler script can spawn (worker tier).

Per the capability model, agents *only gather*: they return plaintext and can
neither send messages nor add reactions. The script takes the returned string
and decides what (if anything) to emit. Two shapes:

- ``has_tools=True`` — a web-search + web-read agent. Both tools draw from the
  fire's shared :class:`~smarter_dev.web.handler_budget.HandlerBudget`, so an
  agent that reads two pages leaves the script one read.
- ``has_tools=False`` — a pure text transform: prompt in, plaintext out, no tools.

Model: Gemini 3.1 Flash Lite — deliberately a lazy tool user, which is a safety
feature here (it won't spiral into runaway tool loops). A budget cap raised
inside a tool propagates out of ``agent.run`` and stops the fire loud.
"""

from __future__ import annotations

import logging
import os

import httpx
from pydantic_ai import Agent
from pydantic_ai import RunContext
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google import GoogleProvider

from smarter_dev.shared.redis_client import get_redis_client
from smarter_dev.web.handler_budget import HandlerBudget
from smarter_dev.web.media_read import read_url
from smarter_dev.web.research_tools import brave_search

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-3.1-flash-lite"
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

    def __init__(self, budget: HandlerBudget) -> None:
        self.budget = budget


def _build_model() -> GoogleModel:
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
    model_id = os.getenv(MODEL_ENV_VAR, DEFAULT_MODEL)
    return GoogleModel(model_id, provider=GoogleProvider(api_key=api_key))


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
    return await read_url(url, instruction, redis=redis)


_agents: dict[bool, Agent] = {}


def _get_agent(has_tools: bool) -> Agent:
    if has_tools not in _agents:
        _agents[has_tools] = Agent(
            _build_model(),
            deps_type=_GatherDeps,
            system_prompt=_GATHER_PROMPT if has_tools else _TRANSFORM_PROMPT,
            tools=[_web_search, _web_read] if has_tools else [],
        )
    return _agents[has_tools]


async def run_gathering_agent(
    prompt: str, has_tools: bool, budget: HandlerBudget
) -> str:
    """Run a spawned gathering/transform agent and return its plaintext output."""
    agent = _get_agent(has_tools)
    result = await agent.run(prompt, deps=_GatherDeps(budget=budget))
    return str(result.output)
