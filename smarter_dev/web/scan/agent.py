"""Pydantic AI research agent for Scan."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import httpx
from pydantic import BaseModel
from pydantic_ai import Agent, RunContext

from smarter_dev.web.scan import tools
from smarter_dev.web.scan.tools import URLRateLimiter

MODEL = "google-gla:gemini-2.0-flash-lite"


@dataclass
class ResearchDeps:
    session_id: str
    http_client: httpx.AsyncClient
    url_rate_limiter: URLRateLimiter


class Source(BaseModel):
    url: str
    title: str
    type: Literal["docs", "repo", "article", "video", "forum", "other"] = "other"
    snippet: str = ""
    cited: bool = False


class ResearchResult(BaseModel):
    response: str
    sources: list[Source]
    summary: str


SYSTEM_PROMPT = """\
You are a research assistant for software developers. Your job is to search the web, \
read relevant sources, and synthesize a comprehensive, well-cited response.

Guidelines:
- Use brave_search to find relevant sources for the query.
- Use jina_read to read the full content of promising URLs.
- Cite sources inline using markdown links [title](url).
- Provide a thorough response with code examples when relevant.
- Include a 2-3 sentence summary suitable for a Discord message.
- Classify each source as docs, repo, article, video, forum, or other.
- Mark sources you actually cited in the response as cited=True.
"""

# Defer model resolution by not passing it at construction time.
# The model is specified at run time via `model=MODEL`.
research_agent = Agent(
    deps_type=ResearchDeps,
    output_type=ResearchResult,
    instructions=SYSTEM_PROMPT,
)


@research_agent.tool
async def brave_search(
    ctx: RunContext[ResearchDeps], query: str, num_results: int = 5
) -> list[dict]:
    """Search the web using Brave Search. Returns a list of results with title, url, and description."""
    return await tools.brave_search(
        ctx.deps.http_client, query, min(num_results, 10)
    )


@research_agent.tool
async def jina_read(ctx: RunContext[ResearchDeps], url: str) -> dict:
    """Read the full content of a URL. Returns the page title, description, and markdown content."""
    await ctx.deps.url_rate_limiter.wait_if_needed(url)
    return await tools.jina_read(ctx.deps.http_client, url)
