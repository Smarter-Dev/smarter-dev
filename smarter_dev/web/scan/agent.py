"""Pydantic AI research agent for Scan."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from dataclasses import dataclass
from typing import Literal

import httpx
from pydantic import BaseModel
from pydantic_ai import Agent, RunContext
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

# Defer model resolution by not passing it at construction time.
# The model is specified at run time via `model=MODEL`.
research_agent = Agent(
    deps_type=ResearchDeps,
    output_type=ResearchResult,
    instructions=SYSTEM_PROMPT,
)


# --- Flash Lite pipeline helpers ---

_query_agent = Agent(
    output_type=str,
    instructions=(
        "Generate a single, effective web search query for the given question. "
        "Return only the search query string, nothing else."
    ),
)

_summarize_agent = Agent(
    output_type=str,
    instructions=(
        "Analyze the provided web page content in relation to the user's question. "
        "Provide:\n"
        "1. A brief summary (2-3 sentences) of what this page covers\n"
        "2. All relevant facts, data points, and specifics that relate to the question\n"
        "3. Any caveats, limitations, edge cases, or important details that qualify those facts\n\n"
        "Be thorough — extract everything useful. The reader needs enough detail to make decisions "
        "without visiting the page themselves."
    ),
)


def _usage_to_dict(usage: RunUsage) -> dict:
    """Convert RunUsage to a serializable dict, omitting zero values."""
    d = dataclasses.asdict(usage)
    return {k: v for k, v in d.items() if v}


async def _run_research_pipeline(
    question: str,
    deps: ResearchDeps,
) -> tuple[str, list[dict]]:
    """Run the research pipeline: query generation → search → read → summarize.

    Returns (formatted_result, sub_tools_for_ui).
    """
    total_usage = RunUsage()
    sub_tools: list[dict] = []

    # Step 1: Generate search query from question
    query_result = await _query_agent.run(question, model=MODEL)
    search_query = query_result.output.strip().strip('"\'')
    total_usage.incr(query_result.usage())

    # Step 2: Brave Search
    await deps.search_rate_limiter.wait()
    search_results = await tools.brave_search(
        deps.http_client, search_query, num_results=5,
    )

    # Format search results for UI
    if search_results and not (len(search_results) == 1 and "error" in search_results[0]):
        search_display = "\n".join(
            f"{i}. {r.get('title', 'Untitled')} — {r.get('url', '')}"
            for i, r in enumerate(search_results, 1)
        )
    else:
        search_display = "No results found."

    sub_tools.append({
        "tool": "search",
        "input": {"query": search_query},
        "content": search_display,
        "status": "complete",
    })

    # Step 3: Jina Read top 3 results + Step 4: Summarize each
    top_results = [
        r for r in search_results
        if "error" not in r and r.get("url")
    ][:3]

    parts: list[str] = []

    async def read_and_summarize(result: dict) -> None:
        url = result["url"]
        title = result.get("title", "Untitled")

        await deps.read_rate_limiter.wait_if_needed(url)
        page = await tools.jina_read(deps.http_client, url)

        if "error" in page:
            summary = f"Could not read: {page['error']}"
            sub_tools.append({
                "tool": "read",
                "input": {"url": url},
                "content": summary,
                "status": "error",
            })
            return

        page_content = page.get("content", "")[:6000]
        if not page_content:
            summary = "Page had no readable content."
            sub_tools.append({
                "tool": "read",
                "input": {"url": url},
                "content": summary,
                "status": "complete",
            })
            parts.append(f"[{title}]({url}): {summary}")
            return

        # Summarize with Flash Lite
        summarize_result = await _summarize_agent.run(
            f"Question: {question}\n\nPage ({url}):\n{page_content}",
            model=MODEL,
        )
        summary = summarize_result.output
        total_usage.incr(summarize_result.usage())

        sub_tools.append({
            "tool": "read",
            "input": {"url": url},
            "content": summary,
            "status": "complete",
        })
        parts.append(f"[{title}]({url}): {summary}")

    # Run reads concurrently
    await asyncio.gather(*(read_and_summarize(r) for r in top_results))

    # Format result for main agent
    result_text = "\n\n".join(parts) if parts else "No results could be read."
    usage_dict = _usage_to_dict(total_usage)
    if usage_dict:
        result_text += f"\n\n[pipeline usage: {usage_dict}]"

    return result_text, sub_tools


@research_agent.tool
async def research(ctx: RunContext[ResearchDeps], question: str) -> str:
    """Research a specific question by searching the web and reading pages.

    Runs a pipeline: generates a search query, searches via Brave, reads
    the top 3 results via Jina, and summarizes each with Flash Lite.
    Returns summaries with links.
    """
    try:
        result_text, sub_tools = await _run_research_pipeline(
            question, ctx.deps,
        )

        # Store pipeline activity for the runner to pick up
        if not hasattr(ctx.deps, "_sub_agent_usage"):
            ctx.deps._sub_agent_usage = []  # type: ignore[attr-defined]
        ctx.deps._sub_agent_usage.append({  # type: ignore[attr-defined]
            "question": question,
            "tools": sub_tools,
        })

        return result_text

    except Exception as e:
        logger.exception("Research pipeline failed for question: %s", question)
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
