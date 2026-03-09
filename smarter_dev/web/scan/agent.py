"""Pydantic AI research agent for Scan.

Two implementations:
- **Lite** (default): Two-stage Flash Lite pipeline — fast and cheap.
- **Premium**: Multi-turn agentic pipeline — deeper research, more expensive.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field
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


def _usage_to_dict(usage: RunUsage) -> dict:
    """Convert RunUsage to a serializable dict, omitting zero values."""
    d = dataclasses.asdict(usage)
    return {k: v for k, v in d.items() if v}


# ============================================================================
# LITE IMPLEMENTATION — Two-stage Flash Lite pipeline (default)
# ============================================================================

WRITING_INSTRUCTIONS = """\
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
Include a 2-3 sentence summary suitable for a short notification."""


# --- Stage 1: Query generation ---


class LiteQueryPlan(BaseModel):
    """Output of the lite Stage 1 query-generation call."""

    search_queries: list[str] = Field(
        description="Exactly 2 web search queries that directly address the user's question.",
        min_length=2,
        max_length=2,
    )
    gap_queries: list[str] = Field(
        default_factory=list,
        description=(
            "0-3 additional search queries for things that may have changed "
            "since your training cutoff — recent releases, breaking changes, "
            "new APIs, newly relevant projects, etc. Leave empty if the topic "
            "is unlikely to have changed."
        ),
    )


_lite_query_agent = Agent(
    output_type=LiteQueryPlan,
    instructions=(
        "You are a search query planner. You will be given the current date "
        "and the user's question.\n\n"
        "## Query quality guidelines\n\n"
        "Your queries must dig deep. The goal is to find PRIMARY SOURCES — "
        "official documentation, GitHub issues/discussions, RFCs, benchmarks, "
        "technical blog posts from maintainers, and detailed teardowns.\n\n"
        "AVOID queries that will surface:\n"
        "- PR/marketing announcements and press releases\n"
        "- 'Top 5/10/N' listicles and roundup articles\n"
        "- Generic tutorials that repeat the same surface-level information\n"
        "- SEO-optimized filler content\n\n"
        "INSTEAD, craft queries that find:\n"
        "- Official docs, changelogs, and migration guides\n"
        "- GitHub issues, discussions, and commit messages with real details\n"
        "- Technical deep-dives, benchmarks, and architecture discussions\n"
        "- Stack Overflow answers with actual solutions\n"
        "- Author/maintainer blog posts with insider knowledge\n\n"
        "Use specific technical terms, library names, function/API names, "
        "and error messages when relevant. Add site: filters (e.g. "
        "site:github.com, site:docs.X.com) when a primary source is obvious.\n\n"
        "## What to produce\n\n"
        "1. Exactly 2 web search queries that directly address their question "
        "from different angles. Include the current year (or 'latest') in "
        "queries unless the user is specifically asking about a different time "
        "period. This ensures search results are current.\n"
        "2. A list of 0-3 additional queries for anything that may have changed "
        "since your training data cutoff — recent releases, breaking changes, "
        "new tools, newly relevant information, etc. Include the current year "
        "in these queries. Only include these if the topic is likely to have "
        "evolved.\n\n"
        "Return structured output only."
    ),
)


# --- Stage 3: Synthesis with read_url tool ---

_LITE_SYNTHESIS_PROMPT = f"""\
You are a research synthesis agent. You have been given web search results \
and page contents from reading key results.

## Your task

1. **Evaluate** the search results and page reads provided below.
2. **Identify gaps** — if you need to read additional pages to fully \
   understand something new, surprising, or to verify important claims, \
   use the `read_url` tool. Focus on filling gaps in your knowledge, \
   especially for things that may have changed since your training cutoff.
3. **Write** your response following the writing instructions below.

{WRITING_INSTRUCTIONS}
"""

_lite_synthesis_agent = Agent(
    deps_type=ResearchDeps,
    output_type=ResearchResult,
    instructions=_LITE_SYNTHESIS_PROMPT,
)


@_lite_synthesis_agent.tool
async def read_url(ctx: RunContext[ResearchDeps], url: str) -> str:
    """Read the full content of a web page. Use this to fill gaps in the
    provided search results or to get details from a page that looks relevant."""
    await ctx.deps.read_rate_limiter.wait_if_needed(url)
    page = await tools.jina_read(ctx.deps.http_client, url)
    if "error" in page:
        return f"Error reading {url}: {page['error']}"
    content = page.get("content", "")[:8000]
    return content if content else "Page had no readable content."


# --- Lite pipeline orchestrator ---

# Type alias for the emit callback used by the lite pipeline.
EmitFn = Callable[..., Any]


async def run_lite_pipeline(
    query: str,
    deps: ResearchDeps,
    date_context: str,
    emit: EmitFn,
) -> tuple[ResearchResult, list[dict], RunUsage]:
    """Two-stage Flash Lite research pipeline.

    Stage 1: Generate search queries (Flash Lite call 1).
    Stage 2: Parallel Brave searches + Jina reads (no LLM).
    Stage 3: Synthesis with read_url tool (Flash Lite call 2).

    Returns (result, tool_log, total_usage).
    """
    tool_log: list[dict] = []
    total_usage = RunUsage()

    # ------------------------------------------------------------------
    # Stage 1 — Query generation
    # ------------------------------------------------------------------
    await emit("status", stage="planning", message="Generating search queries...")

    query_result = await _lite_query_agent.run(
        f"{date_context}\n\n{query}", model=MODEL,
    )
    plan = query_result.output
    total_usage.incr(query_result.usage())

    all_queries = plan.search_queries + plan.gap_queries

    tool_log.append({
        "tool": "query_plan",
        "input": {"user_query": query},
        "content": f"Search queries: {plan.search_queries}\nGap queries: {plan.gap_queries}",
        "status": "complete",
    })
    await emit(
        "tool_result",
        tool="query_plan",
        status="complete",
        content=f"Search queries: {plan.search_queries}\nGap queries: {plan.gap_queries}",
    )

    # ------------------------------------------------------------------
    # Stage 2 — Parallel Brave searches
    # ------------------------------------------------------------------
    await emit("status", stage="researching", message=f"Running {len(all_queries)} searches...")

    async def _search(q: str) -> tuple[str, list[dict]]:
        await deps.search_rate_limiter.wait()
        return q, await tools.brave_search(deps.http_client, q, num_results=15)

    search_tasks = [_search(q) for q in all_queries]
    search_results_by_query: list[tuple[str, list[dict]]] = await asyncio.gather(*search_tasks)

    # Emit search results and collect all results + first URLs to read
    all_search_results: list[dict] = []
    first_urls: list[tuple[str, str]] = []  # (url, title)

    for q, results in search_results_by_query:
        has_results = results and not (len(results) == 1 and "error" in results[0])
        display = (
            "\n".join(f"{i}. {r.get('title', 'Untitled')} — {r.get('url', '')}" for i, r in enumerate(results, 1))
            if has_results
            else "No results found."
        )
        tool_log.append({
            "tool": "search",
            "input": {"query": q},
            "content": display,
            "status": "complete",
        })
        await emit("tool_result", tool="search", status="complete", content=display)

        if has_results:
            all_search_results.extend(results)
            # First valid result URL for reading
            for r in results:
                if "error" not in r and r.get("url"):
                    first_urls.append((r["url"], r.get("title", "Untitled")))
                    break

    # Deduplicate first_urls by URL
    seen_urls: set[str] = set()
    unique_first_urls: list[tuple[str, str]] = []
    for url, title in first_urls:
        if url not in seen_urls:
            seen_urls.add(url)
            unique_first_urls.append((url, title))

    # ------------------------------------------------------------------
    # Stage 2b — Parallel Jina reads of first result from each search
    # ------------------------------------------------------------------
    await emit("status", stage="reading", message=f"Reading {len(unique_first_urls)} pages...")

    reads: list[dict] = []

    async def _read(url: str, title: str) -> None:
        await deps.read_rate_limiter.wait_if_needed(url)
        page = await tools.jina_read(deps.http_client, url)
        if "error" in page:
            tool_log.append({
                "tool": "read",
                "input": {"url": url},
                "content": f"Could not read: {page['error']}",
                "status": "error",
            })
            await emit("tool_result", tool="read", status="error", content=f"Could not read: {page['error']}")
            return
        content = page.get("content", "")[:8000]
        reads.append({"url": url, "title": title, "content": content})
        tool_log.append({
            "tool": "read",
            "input": {"url": url},
            "content": f"Read {len(content)} chars from {title}",
            "status": "complete",
        })
        await emit("tool_result", tool="read", status="complete", content=f"Read {len(content)} chars from {title}")

    await asyncio.gather(*(_read(url, title) for url, title in unique_first_urls))

    # ------------------------------------------------------------------
    # Stage 3 — Synthesis (Flash Lite call 2, with read_url tool)
    # ------------------------------------------------------------------
    await emit("status", stage="synthesizing", message="Analyzing and writing...")

    # Build the context message for the synthesis agent
    search_section = "\n\n".join(
        f"### Search: {q}\n" + "\n".join(
            f"- [{r.get('title', 'Untitled')}]({r.get('url', '')}) — {r.get('description', '')}"
            for r in results
            if "error" not in r
        )
        for q, results in search_results_by_query
    )

    read_section = "\n\n".join(
        f"### Page: [{rd['title']}]({rd['url']})\n{rd['content']}"
        for rd in reads
    ) if reads else "No pages could be read."

    synthesis_input = (
        f"## User's Question\n{query}\n\n"
        f"## Date\n{date_context}\n\n"
        f"## Search Results\n{search_section}\n\n"
        f"## Page Contents\n{read_section}"
    )

    result_data: ResearchResult | None = None

    from pydantic_ai.messages import (
        FunctionToolCallEvent,
        FunctionToolResultEvent,
        PartDeltaEvent,
        TextPartDelta,
    )
    from pydantic_ai import AgentRunResultEvent

    async for event in _lite_synthesis_agent.run_stream_events(
        synthesis_input, deps=deps, model=MODEL,
    ):
        if isinstance(event, FunctionToolCallEvent):
            await emit("tool_use", tool=event.part.tool_name, input={"url": event.part.args}, status="running")
            tool_log.append({"tool": event.part.tool_name, "input": event.part.args, "status": "running"})

        elif isinstance(event, FunctionToolResultEvent):
            content = str(event.result.content)[:5120]
            await emit("tool_result", tool=event.result.tool_name, status="complete", content=content)
            for entry in reversed(tool_log):
                if entry.get("tool") == event.result.tool_name and entry.get("status") == "running":
                    entry["status"] = "complete"
                    entry["content"] = content
                    break

        elif isinstance(event, PartDeltaEvent):
            if isinstance(event.delta, TextPartDelta):
                await emit("response_chunk", delta=event.delta.content_delta)

        elif isinstance(event, AgentRunResultEvent):
            result_data = event.result.output
            total_usage.incr(event.result.usage())

    if result_data is None:
        raise RuntimeError("Synthesis agent completed without producing a result")

    return result_data, tool_log, total_usage


# ============================================================================
# PREMIUM IMPLEMENTATION — Multi-turn agentic pipeline
# (This is the deeper, more expensive research path. Will be used for
#  premium tier users in the future.)
# ============================================================================

SYSTEM_PROMPT = f"""\
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

{WRITING_INSTRUCTIONS}
"""

# Defer model resolution by not passing it at construction time.
# The model is specified at run time via `model=MODEL`.
research_agent = Agent(
    deps_type=ResearchDeps,
    output_type=ResearchResult,
    instructions=SYSTEM_PROMPT,
)


# --- Premium pipeline helpers ---

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


async def _run_research_pipeline(
    question: str,
    deps: ResearchDeps,
) -> tuple[str, list[dict]]:
    """Run the premium research pipeline: query generation → search → read → summarize.

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


# ============================================================================
# Naming agent (shared)
# ============================================================================

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
