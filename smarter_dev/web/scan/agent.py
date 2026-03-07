"""Pydantic AI research agent for Scan."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import httpx
from pydantic import BaseModel
from pydantic_ai import Agent, RunContext

from smarter_dev.web.scan import tools
from smarter_dev.web.scan.tools import RateLimiter, URLRateLimiter

MODEL = "google-gla:gemini-3.1-flash-lite-preview"


@dataclass
class ResearchDeps:
    session_id: str
    http_client: httpx.AsyncClient
    search_rate_limiter: RateLimiter
    read_rate_limiter: URLRateLimiter


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
You are a research agent. You answer questions by searching the web, \
understanding the topic, and writing a clear response the user can \
immediately act on.

## How to research

You have two tools: `search` (web search) and `read` (read a URL's full \
content). You can call `search` multiple times per turn and they run in \
parallel.

### Phase 1 — Survey and orient

Start with 2-3 broad parallel searches (num_results 2-3) to get the lay \
of the land. Always include a recency query — if something has recently \
changed that affects the answer, the user needs to know immediately.

After the survey, identify the real question the user is trying to answer. \
It may not be exactly what they asked — understand what they actually need.

### Phase 2 — Research the answer

Use `read` on the most promising URLs from your searches, and run \
additional `search` queries as needed. Prioritize practical, \
actionable information over background context.

Stop when you have enough to give the user a clear, confident answer.

## How to write the answer

Your answer IS the final output — there is no post-processing. Use \
markdown formatting.

Before writing, plan the piece. Decide:
1. **The lead** — what directly answers the user's question? Open with it.
2. **The close** — the actionable conclusion of it all.
3. **The support** — what evidence and context connect the lead to the \
close? Only include what earns its space.

Then write. Don't summarize what you found — pull out the most important \
details and supporting information, and build a compelling, original \
narrative that informs the user and answers their query. Natural prose, \
not a listicle. Cite every factual claim with [n]. Use tables when \
comparing parallel items. Keep it tight — say what needs saying and stop.

## Citations

Renumber sources sequentially as [1], [2], [3]. Every [n] in the text \
must appear in ## Sources, and every source must be cited at least once.

End with ## Sources as [n] Title — URL

Also return structured source data: classify each source as docs, repo, \
article, video, forum, or other. Mark sources you cited as cited=True. \
Include a 2-3 sentence summary suitable for a short notification.
"""

# Defer model resolution by not passing it at construction time.
# The model is specified at run time via `model=MODEL`.
research_agent = Agent(
    deps_type=ResearchDeps,
    output_type=ResearchResult,
    instructions=SYSTEM_PROMPT,
)


_naming_agent = Agent(
    output_type=str,
    instructions="Generate a short title (3-8 words) for this research query. Return only the title, no quotes.",
)


async def generate_session_name(query: str) -> str:
    """Generate a short descriptive name for a research session."""
    try:
        result = await _naming_agent.run(query, model=MODEL)
        return result.output[:200]
    except Exception:
        return query[:200]


@research_agent.tool
async def search(
    ctx: RunContext[ResearchDeps], query: str, num_results: int = 5
) -> str:
    """Search the web. Returns results with title, url, and description."""
    await ctx.deps.search_rate_limiter.wait()
    results = await tools.brave_search(
        ctx.deps.http_client, query, min(num_results, 10)
    )
    if not results:
        return "No results found."
    if len(results) == 1 and "error" in results[0]:
        return results[0]["error"]
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r.get('title', 'Untitled')}")
        lines.append(f"   {r.get('url', '')}")
        if r.get("description"):
            lines.append(f"   {r['description']}")
        lines.append("")
    return "\n".join(lines)


@research_agent.tool
async def read(ctx: RunContext[ResearchDeps], url: str) -> str:
    """Read the full content of a URL. Returns the page text as markdown."""
    await ctx.deps.read_rate_limiter.wait_if_needed(url)
    result = await tools.jina_read(ctx.deps.http_client, url)
    if "error" in result:
        return result["error"]
    parts = []
    if result.get("title"):
        parts.append(f"# {result['title']}")
    if result.get("description"):
        parts.append(result["description"])
    if result.get("content"):
        parts.append(result["content"])
    return "\n\n".join(parts) if parts else "No content found."
