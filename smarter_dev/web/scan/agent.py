"""Pydantic AI research agent for Scan."""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from typing import Literal

import httpx
from pydantic import BaseModel
from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.usage import RunUsage

from smarter_dev.web.scan import tools
from smarter_dev.web.scan.tools import RateLimiter, URLRateLimiter

logger = logging.getLogger(__name__)

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


class SubAgentURL(BaseModel):
    """A URL found and summarized by the research sub-agent."""

    url: str
    title: str
    summary: str


class SubAgentResult(BaseModel):
    """Result from the research sub-agent."""

    answer: str
    urls: list[SubAgentURL]


SYSTEM_PROMPT = """\
You are a research agent. You answer questions by delegating research \
to your `research` tool and writing a clear response the user can \
immediately act on.

## How to research

You have one tool: `research(question)`. It searches the web and reads \
pages to answer a specific question. You can call it multiple times per \
turn and they run in parallel.

### Phase 1 — Survey and orient

Start with 2-3 parallel `research` calls to explore different angles \
of the query. Always include a recency question — if something has \
recently changed that affects the answer, the user needs to know.

After the survey, identify the real question the user is trying to answer. \
It may not be exactly what they asked — understand what they actually need.

### Phase 2 — Deep research

Use additional `research` calls to fill gaps and verify facts. \
Prioritize practical, actionable information over background context.

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

_SUB_AGENT_PROMPT = """\
You are a focused web research assistant. Given a question, search the \
web and read pages to find the answer. Use multiple searches to explore \
different angles and verify information.

Return a clear, factual answer with the URLs you found most useful and \
a brief summary of each.
"""

# Defer model resolution by not passing it at construction time.
# The model is specified at run time via `model=MODEL`.
research_agent = Agent(
    deps_type=ResearchDeps,
    output_type=ResearchResult,
    instructions=SYSTEM_PROMPT,
)


# --- Research sub-agent (search + read tools) ---

_research_sub_agent = Agent(
    deps_type=ResearchDeps,
    output_type=SubAgentResult,
    instructions=_SUB_AGENT_PROMPT,
)


@_research_sub_agent.tool
async def search(
    ctx: RunContext[ResearchDeps], query: str, num_results: int = 5
) -> str:
    """Search the web. Returns results with title, url, description, and page content."""
    await ctx.deps.search_rate_limiter.wait()
    results = await tools.jina_search(
        ctx.deps.http_client, query, min(num_results, 5)
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
        if r.get("content"):
            # Include truncated content so the agent has real page data
            content = r["content"][:3000]
            lines.append(f"   ---")
            lines.append(f"   {content}")
        lines.append("")
    return "\n".join(lines)


@_research_sub_agent.tool
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


def _usage_to_dict(usage: RunUsage) -> dict:
    """Convert RunUsage to a serializable dict, omitting zero values."""
    d = dataclasses.asdict(usage)
    return {k: v for k, v in d.items() if v}


def _extract_sub_agent_tools(messages: list) -> list[dict]:
    """Extract structured tool calls from sub-agent messages.

    Returns a list of dicts with: tool, input, content, status.
    Suitable for nested rendering in the UI.
    """
    # First pass: collect tool calls keyed by tool_call_id
    calls: dict[str, dict] = {}
    for msg in messages:
        if isinstance(msg, ModelResponse):
            for part in msg.parts:
                if isinstance(part, ToolCallPart):
                    args = part.args
                    if isinstance(args, dict):
                        input_data = args
                    else:
                        input_data = {"raw": str(args) if args else ""}
                    calls[part.tool_call_id] = {
                        "tool": part.tool_name,
                        "input": input_data,
                        "status": "complete",
                    }
        elif isinstance(msg, ModelRequest):
            for part in msg.parts:
                if isinstance(part, ToolReturnPart):
                    if part.tool_call_id in calls:
                        content = str(part.content)
                        if len(content) > 5120:
                            content = content[:5120] + "\n... (truncated)"
                        calls[part.tool_call_id]["content"] = content

    return list(calls.values())


@research_agent.tool
async def research(ctx: RunContext[ResearchDeps], question: str) -> str:
    """Research a specific question by searching the web and reading pages.

    Delegates to a sub-agent that performs multiple searches and reads
    to answer the question. Returns relevant URLs and summaries.
    """
    try:
        result = await _research_sub_agent.run(
            question, deps=ctx.deps, model=MODEL,
        )
        usage = result.usage()
        usage_dict = _usage_to_dict(usage)
        output = result.output

        # Format as text for the main agent
        parts = [output.answer, ""]
        if output.urls:
            parts.append("### Sources found:")
            for u in output.urls:
                parts.append(f"- [{u.title}]({u.url})")
                parts.append(f"  {u.summary}")
                parts.append("")

        if usage_dict:
            parts.append(f"[sub-agent usage: {usage_dict}]")

        # Store sub-agent tool activity and usage for the runner
        sub_tools = _extract_sub_agent_tools(result.all_messages())
        if not hasattr(ctx.deps, "_sub_agent_usage"):
            ctx.deps._sub_agent_usage = []  # type: ignore[attr-defined]
        ctx.deps._sub_agent_usage.append({  # type: ignore[attr-defined]
            "question": question,
            "usage": usage_dict,
            "tools": sub_tools,
        })

        return "\n".join(parts)

    except Exception as e:
        logger.exception("Research sub-agent failed for question: %s", question)
        return f"Research failed: {e}"


# --- Naming agent ---

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
