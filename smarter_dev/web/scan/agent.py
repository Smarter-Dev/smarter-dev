"""Pydantic AI research agent for Scan.

Three implementations:
- **Lite** (default): Two-stage Flash Lite pipeline — fast and cheap.
- **Premium**: Multi-turn agentic pipeline — deeper research, more expensive.
- **Experimental**: History-threaded pipeline — shares conversation context
  across meta, planning, writing, and example generation stages.
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
CODE_EXAMPLES_MODEL = "google-gla:gemini-3-flash-preview"


def make_slug(name: str) -> str:
    """Generate a URL slug from a session name with a timestamp suffix for uniqueness."""
    import re
    import time

    slug = name.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    slug = slug[:200]
    timestamp = str(int(time.time()))
    return f"{slug}-{timestamp}"


@dataclass
class ResearchDeps:
    session_id: str
    http_client: httpx.AsyncClient
    search_rate_limiter: RateLimiter
    read_rate_limiter: URLRateLimiter
    source_cache: dict[str, dict] = dataclasses.field(default_factory=dict)
    search_count: int = 0
    youtube_search_count: int = 0


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

Your answer IS the final output. Use markdown formatting.

Before writing, plan the piece. Decide:
1. **The lead** — what directly answers the user's question? Open with it.
2. **The close** — the actionable conclusion of it all.
3. **The support** — what evidence and context connect the lead to the \
close? Only include what earns its space. Drop anything that doesn't \
serve the answer — irrelevant results should not appear at all.

Then write. Don't summarize what you found — pull out the most important \
details and supporting information, and build a compelling, original \
narrative that informs the user and answers their query. Natural prose, \
not a listicle. Cite every factual claim inline. Use tables when \
comparing parallel items. Keep it tight — say what needs saying and stop.

## Source quality

Evaluate every source before citing it. Prefer primary and authoritative \
sources — official documentation, specs, RFCs, maintainer blogs, and \
project repos carry the most weight. Community Q&A (Stack Overflow, \
Quora) is mid-tier — useful for practical solutions but not authoritative. \
Social/anecdotal sources (Reddit, forums, personal blogs with "I tried X") \
are the weakest — use them only when stronger sources aren't available.

When citing weaker sources, frame the evidence appropriately: \
"community reports suggest…", "anecdotal experience indicates…", etc. \
Never present anecdotal evidence with the same confidence as documented \
facts. If a claim is only supported by anecdotal sources, say so.

## Citations

Cite sources INLINE by placing the full URL inside double square brackets \
immediately after the claim — one URL per pair of brackets. Example: \
The framework now supports streaming responses [[https://docs.example.com/streaming]]. \
For multiple citations, use separate brackets: \
This feature was added in v3 [[https://blog.example.com/v3]] [[https://github.com/example/repo/pull/42]].

Do NOT number sources. Do NOT add a Sources section at the end. \
Every citation must be a real URL from your research — never fabricate URLs. \
Cite the specific page you found the information on, not a homepage.

Avoid unnecessary date anchoring ("as of 2026", "the latest version") \
unless the user asked about recency or the information is specifically \
time-sensitive. Let the content speak for itself.

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
        "from different angles. Only include the current year or 'latest' if "
        "the topic is likely to have meaningful recent changes (new releases, "
        "evolving APIs, recent events). For stable/evergreen topics (core CS "
        "concepts, well-established patterns, mature standards), omit date "
        "qualifiers — they filter out authoritative older content.\n"
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

1. **Triage** — discard any search results or page contents that do NOT \
   help answer the user's question. Off-topic results, tangential mentions, \
   and marketing fluff should be ignored entirely. Do not cite them.
2. **Evaluate** the remaining results and page reads. Assess source \
   authority: official docs and primary sources are strongest, community \
   Q&A (Stack Overflow, Quora) is mid-tier, and social/anecdotal sources \
   (Reddit, forums, personal anecdotes) are weakest. Note which claims \
   are only supported by anecdotal evidence.
3. **Identify gaps** — if you need to read additional pages to fully \
   understand something new, surprising, or to verify important claims, \
   use the `read_url` tool. Focus on filling gaps in your knowledge, \
   especially for things that may have changed since your training cutoff.
4. **Verify numbers** — double-check any math, statistics, prices, dates, \
   or numerical comparisons. Re-read the source to confirm you have the \
   right figures before comparing them. When comparing two numbers, \
   pause and reconsider which is larger/smaller/better before stating \
   a conclusion.
5. **Write** your response following the writing instructions below. \
   Answer the query first, then mention interesting alternatives or \
   related findings only if they add real value.

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
        await emit("tool_use", tool="search", input={"query": q}, status="running")
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
        await emit("tool_use", tool="read", input={"url": url}, status="running")
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
        PartStartEvent,
        TextPart,
        TextPartDelta,
    )
    from pydantic_ai import AgentRunResultEvent

    async for event in _lite_synthesis_agent.run_stream_events(
        synthesis_input, deps=deps, model=MODEL,
    ):
        if isinstance(event, FunctionToolCallEvent):
            # Extract a clean input dict from the tool args
            args = event.part.args
            if isinstance(args, dict):
                tool_input = args
            else:
                tool_input = {"url": str(args)}
            await emit("tool_use", tool=event.part.tool_name, input=tool_input, status="running")
            tool_log.append({"tool": event.part.tool_name, "input": tool_input, "status": "running"})

        elif isinstance(event, FunctionToolResultEvent):
            raw_content = str(event.result.content)[:5120]
            # Summarize the content for human-readable display
            url = ""
            for entry in reversed(tool_log):
                if entry.get("tool") == event.result.tool_name and entry.get("status") == "running":
                    url = entry.get("input", {}).get("url", "")
                    break
            if raw_content.startswith("Error reading"):
                display_content = raw_content
            elif raw_content == "Page had no readable content.":
                display_content = raw_content
            else:
                display_content = f"Read {len(raw_content)} chars from {url}" if url else f"Read {len(raw_content)} chars"
            await emit("tool_result", tool=event.result.tool_name, status="complete", content=display_content)
            for entry in reversed(tool_log):
                if entry.get("tool") == event.result.tool_name and entry.get("status") == "running":
                    entry["status"] = "complete"
                    entry["content"] = display_content
                    break

        elif isinstance(event, PartStartEvent):
            if isinstance(event.part, TextPart) and event.part.content:
                await emit("response_chunk", delta=event.part.content)

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
) -> tuple[str, list[dict], dict]:
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

    return result_text, sub_tools, usage_dict


@research_agent.tool
async def research(ctx: RunContext[ResearchDeps], question: str) -> str:
    """Research a specific question by searching the web and reading pages.

    Runs a pipeline: generates a search query, searches via Brave, reads
    the top 3 results via Jina, and summarizes each with Flash Lite.
    Returns summaries with links.
    """
    try:
        result_text, sub_tools, usage_dict = await _run_research_pipeline(
            question, ctx.deps,
        )

        # Store pipeline activity + token usage for the runner to pick up
        if not hasattr(ctx.deps, "_sub_agent_usage"):
            ctx.deps._sub_agent_usage = []  # type: ignore[attr-defined]
        ctx.deps._sub_agent_usage.append({  # type: ignore[attr-defined]
            "question": question,
            "tools": sub_tools,
            **usage_dict,
        })

        return result_text

    except Exception as e:
        logger.exception("Research pipeline failed for question: %s", question)
        return f"Research failed: {e}"


# ============================================================================
# Session metadata agent (naming + classification)
# ============================================================================


class SessionMeta(BaseModel):
    """Metadata generated for a research session."""

    name: str = Field(
        description="A brief title (2-5 words) for the research query.",
    )
    skill_level: str = Field(
        description=(
            "The skill level assumed by the query. One of: "
            "beginner, intermediate, advanced, expert."
        ),
    )
    topic: str = Field(
        description=(
            "The primary topic category. One of: "
            "programming, software-engineering, web-dev, app-dev, backend, "
            "full-stack, ai-llm, machine-learning, devops, data-engineering, "
            "security, gamedev, systems, other. Use 'programming' for general "
            "CS concepts (OOP, algorithms, data structures, design patterns). "
            "Use 'other' ONLY for non-software topics."
        ),
    )


_meta_agent = Agent(
    output_type=SessionMeta,
    instructions=(
        "Analyze the research query and return structured metadata.\n\n"
        "1. **name**: A brief title (2-5 words) that captures the essence "
        "of the query. No quotes, no punctuation at the end.\n"
        "2. **skill_level**: Infer from the terminology and depth of the "
        "question — beginner, intermediate, advanced, or expert.\n"
        "3. **topic**: Classify into exactly one of: programming, "
        "software-engineering, web-dev, app-dev, backend, full-stack, "
        "ai-llm, machine-learning, devops, data-engineering, security, "
        "gamedev, systems, other. Use 'programming' for general CS concepts "
        "(OOP, algorithms, data structures, design patterns, language features). "
        "Use 'other' ONLY for non-software topics.\n\n"
        "Return structured output only."
    ),
)


async def generate_session_meta(query: str) -> tuple[SessionMeta, RunUsage]:
    """Generate name and classification metadata for a research session."""
    try:
        result = await _meta_agent.run(query, model=MODEL)
        meta = result.output
        meta.name = meta.name[:200]
        return meta, result.usage()
    except Exception:
        return SessionMeta(
            name=query[:200],
            skill_level="intermediate",
            topic="other",
        ), RunUsage()


_youtube_query_agent = Agent(
    output_type=str,
    instructions=(
        "You decide whether a research query would benefit from YouTube "
        "video results, and if so, generate a search query.\n\n"
        "Return NONE (literally the word NONE) if:\n"
        "- The question is too broad or vague to find a useful video\n"
        "- The topic is highly specific to a niche library/tool with "
        "unlikely YouTube coverage\n"
        "- The question is about current events, news, or time-sensitive "
        "information that videos won't cover well\n"
        "- The question is about comparing options, making decisions, or "
        "asking for opinions rather than learning a concept\n"
        "- The topic is non-technical (not software development)\n\n"
        "Return a YouTube search query if:\n"
        "- The question involves a concept, pattern, or technique that is "
        "well-explained visually or in video format\n"
        "- There are likely quality tutorials, conference talks, or "
        "deep-dive videos on the topic\n"
        "- The topic is a popular framework, language, tool, or concept "
        "with strong YouTube coverage\n\n"
        "When generating a query:\n"
        "- IMPORTANT: Over-index on the skill level. A beginner needs "
        "'intro', 'tutorial', 'for beginners', 'explained simply'. An "
        "expert needs 'deep dive', 'advanced', 'internals', 'under the "
        "hood'.\n"
        "- Anchor the query on the classified topic while tailoring to the "
        "specific question.\n"
        "- Keep it under 80 characters.\n"
        "- Return only the search query string or NONE, nothing else."
    ),
)


# ============================================================================
# Code examples agent (post-response)
# ============================================================================


class CodeExample(BaseModel):
    """A single code example."""

    title: str = Field(description="Short descriptive title (3-10 words)")
    language: str = Field(
        description="Programming language for syntax highlighting (e.g. python, javascript, go, rust, sql)"
    )
    code: str = Field(description="The complete, runnable code example")
    explanation: str = Field(
        default="",
        description="1-2 sentence explanation of what the code demonstrates",
    )


class CodeExamplesResult(BaseModel):
    """Output of the code examples agent."""

    examples: list[CodeExample] = Field(
        default_factory=list,
        description="The code examples. Up to 5 short, 2-3 medium, or 1 large.",
    )


_code_examples_agent = Agent(
    output_type=CodeExamplesResult,
    instructions=(
        "You generate practical code examples that complement a research "
        "response about a programming or software engineering topic.\n\n"
        "You will receive:\n"
        "- The original user query\n"
        "- The full research response that was generated\n"
        "- The user's skill level (beginner, intermediate, advanced, expert)\n\n"
        "## Rules\n\n"
        "1. **Tailor to skill level.** Beginners need simple, well-commented "
        "examples. Experts want concise, idiomatic code showing advanced patterns.\n"
        "2. **Choose the right scale:**\n"
        "   - Up to 5 SHORT examples (5-15 lines each) for topics with many "
        "small concepts (e.g. syntax, built-in functions, simple patterns)\n"
        "   - 2-3 MEDIUM examples (15-40 lines each) for topics that need "
        "more context (e.g. design patterns, API usage, algorithms)\n"
        "   - 1 LARGE example (40-100 lines) for topics best shown as a "
        "complete program (e.g. full implementations, project structures)\n"
        "   - Mix scales if appropriate.\n"
        "3. **Be practical.** Examples should be runnable and demonstrate "
        "real usage, not toy examples. Use realistic variable names and "
        "scenarios.\n"
        "4. **Don't repeat the response.** The examples should ADD value "
        "beyond what the written response already covers — show code the "
        "response referenced or alluded to, or illustrate concepts from "
        "a different angle.\n"
        "5. **Return an empty list** if the topic doesn't benefit from code "
        "examples (e.g. purely conceptual discussions, career advice, tool "
        "comparisons with no code).\n"
        "6. **Pick the right language.** Use the language most relevant to "
        "the query. If the query is language-agnostic, prefer Python.\n\n"
        "## Ordering (CRITICAL)\n\n"
        "Order examples to progressively develop the reader's understanding "
        "of the concept. Start with the simplest, most foundational example "
        "that establishes core concepts. Each subsequent example should build "
        "on the previous ones — introducing new complexity, combining ideas, "
        "or showing more advanced usage patterns. The final example should "
        "represent the most complete or sophisticated application of the "
        "concept. Think of it as a mini-tutorial where each example is a "
        "stepping stone.\n\n"
        "## Reflection\n\n"
        "Before finalizing, review each example: is it correct? Does it "
        "follow best practices? Could it teach bad habits? Correct any "
        "issues before returning.\n\n"
        "Return structured output only."
    ),
)


async def generate_code_examples(
    query: str, response: str, skill_level: str,
) -> tuple[CodeExamplesResult, RunUsage]:
    """Generate code examples that complement a research response.

    Uses Gemini 3 Flash with MEDIUM thinking for higher-quality code.
    """
    from google.genai.types import ThinkingLevel

    try:
        prompt = (
            f"## User Query\n{query}\n\n"
            f"## Skill Level\n{skill_level}\n\n"
            f"## Research Response\n{response}"
        )
        result = await _code_examples_agent.run(
            prompt,
            model=CODE_EXAMPLES_MODEL,
            model_settings={
                "google_thinking_config": {
                    "thinking_level": ThinkingLevel.MEDIUM,
                },
            },
        )
        return result.output, result.usage()
    except Exception as exc:
        logger.exception("Failed to generate code examples: %s: %s", type(exc).__name__, exc)
        return CodeExamplesResult(examples=[]), RunUsage()


class YouTubeRanking(BaseModel):
    """Output of the YouTube ranking agent."""

    selected_ids: list[str] = Field(
        description=(
            "Ordered list of exactly 4 video IDs to recommend, most relevant first."
        ),
    )


_youtube_ranking_agent = Agent(
    output_type=YouTubeRanking,
    instructions=(
        "You evaluate YouTube search results and select the most relevant "
        "videos for a user's research query.\n\n"
        "You will receive:\n"
        "- The user's original query\n"
        "- Their skill level\n"
        "- A numbered list of YouTube videos (title, channel, video_id)\n\n"
        "## Selection criteria\n\n"
        "1. **Relevance** — Does the video directly address the query? "
        "Reject tangential or loosely related videos.\n"
        "2. **Quality signals** — Prefer established channels, conference "
        "talks, and well-known educators over random uploads. Channels "
        "with names like 'Fireship', 'Traversy Media', 'The Coding Train', "
        "'freeCodeCamp', 'ArjanCodes', 'Tech With Tim', 'ThePrimeagen', "
        "etc. are strong signals.\n"
        "3. **Skill match** — A beginner query should get beginner-friendly "
        "tutorials, not advanced deep-dives. An expert query should get "
        "advanced content, not intro tutorials.\n"
        "4. **No spam** — Skip videos that look like clickbait, AI-generated "
        "slop, or low-effort content based on their title.\n"
        "5. **Diversity** — If selecting multiple videos, prefer different "
        "angles or channels over redundant content.\n\n"
        "Return exactly 4 video IDs ordered by relevance (best first). "
        "Always pick 4 — choose the best available even if they're not perfect.\n\n"
        "Return structured output only."
    ),
)


async def generate_youtube_query(
    query: str, skill_level: str, topic: str,
) -> tuple[str | None, RunUsage]:
    """Generate a YouTube search query, or None if not suited for video."""
    if topic == "other":
        return None, RunUsage()
    try:
        prompt = (
            f"User query: {query}\n"
            f"Skill level: {skill_level}\n"
            f"Topic: {topic}"
        )
        result = await _youtube_query_agent.run(prompt, model=MODEL)
        output = result.output.strip().strip('"')
        if output.upper() == "NONE":
            return None, result.usage()
        return (output[:80] if output else None), result.usage()
    except Exception:
        logger.exception("Failed to generate YouTube query")
        return None, RunUsage()


async def rank_youtube_results(
    query: str, skill_level: str, videos: list[dict],
) -> tuple[list[dict], RunUsage]:
    """Rank YouTube search results and return the top 1-3 most relevant.

    Uses Flash Lite to evaluate relevance, quality, and skill-level match.
    Returns the selected videos in ranked order.
    """
    if not videos:
        return [], RunUsage()

    try:
        # Build a numbered list for the LLM
        video_lines = []
        for i, v in enumerate(videos, 1):
            video_lines.append(
                f"{i}. [{v.get('video_id', '')}] "
                f"\"{v.get('title', 'Untitled')}\" "
                f"by {v.get('channel', 'Unknown')}"
            )

        prompt = (
            f"## User Query\n{query}\n\n"
            f"## Skill Level\n{skill_level}\n\n"
            f"## YouTube Search Results\n" + "\n".join(video_lines)
        )
        result = await _youtube_ranking_agent.run(prompt, model=MODEL)

        # Map selected IDs back to full video dicts, preserving rank order
        id_to_video = {v.get("video_id", ""): v for v in videos}
        ranked = []
        for vid in result.output.selected_ids[:4]:
            if vid in id_to_video:
                ranked.append(id_to_video[vid])

        return ranked, result.usage()
    except Exception:
        logger.exception("Failed to rank YouTube results")
        # Fallback: return first 4 unranked
        return videos[:4], RunUsage()


# ---------------------------------------------------------------------------
# Resource ranking agent
# ---------------------------------------------------------------------------


class ResourceRanking(BaseModel):
    """Output of the resource ranking agent."""

    selected_indices: list[int] = Field(
        description=(
            "Ordered list of exactly 5 result indices (0-based) to recommend. "
            "Rank by importance: official docs first, then tutorials, guides, "
            "articles, forums."
        ),
    )


_resource_ranking_agent = Agent(
    output_type=ResourceRanking,
    instructions=(
        "You evaluate web search results and select the 5 most useful "
        "resources for a user's research query.\n\n"
        "You will receive:\n"
        "- The user's original query\n"
        "- Their skill level\n"
        "- A numbered list of web results (title, url, description)\n\n"
        "## Selection criteria\n\n"
        "1. **Official documentation** — Always rank official docs highest "
        "when available (e.g. docs.python.org, reactjs.org, MDN).\n"
        "2. **Tutorials & guides** — High-quality tutorials from reputable "
        "sources (Real Python, DigitalOcean, LogRocket, etc.).\n"
        "3. **Skill match** — Match content depth to the user's level. "
        "Beginners need introductions, experts need reference material.\n"
        "4. **Authoritative sources** — Prefer well-known platforms "
        "(GitHub repos, Stack Overflow answers, dev blogs from the "
        "project maintainers) over random blogs.\n"
        "5. **No spam** — Skip SEO-farm articles, AI-generated slop, "
        "listicles with no substance, or paywalled content.\n"
        "6. **Diversity** — Cover different angles: docs, tutorial, "
        "example code, discussion thread, blog post.\n\n"
        "Return exactly 5 indices ordered by importance (most useful first). "
        "If fewer than 5 good results exist, still pick the best 5 available.\n\n"
        "Return structured output only."
    ),
)


async def rank_resource_results(
    query: str, skill_level: str, results: list[dict],
) -> tuple[list[dict], RunUsage]:
    """Rank Brave search results and return the top 5 most useful resources.

    Uses Flash Lite to evaluate relevance, authority, and skill-level match.
    Returns the selected results in ranked order.
    """
    if not results:
        return [], RunUsage()

    try:
        lines = []
        for i, r in enumerate(results):
            lines.append(
                f"{i}. \"{r.get('title', 'Untitled')}\" "
                f"— {r.get('url', '')}\n"
                f"   {r.get('description', '')}"
            )

        prompt = (
            f"## User Query\n{query}\n\n"
            f"## Skill Level\n{skill_level}\n\n"
            f"## Web Search Results\n" + "\n".join(lines)
        )
        result = await _resource_ranking_agent.run(prompt, model=MODEL)

        ranked = []
        for idx in result.output.selected_indices[:5]:
            if 0 <= idx < len(results):
                ranked.append(results[idx])

        return ranked, result.usage()
    except Exception:
        logger.exception("Failed to rank resource results")
        return results[:5], RunUsage()


# ============================================================================
# EXPERIMENTAL IMPLEMENTATION — History-threaded pipeline
# ============================================================================


class ExpMetaQueryPlan(BaseModel):
    """Metadata for the experimental pipeline."""

    name: str = Field(
        description="A concise action phrase (2-4 words) describing what the user and researcher are doing. E.g. 'Finding Notion Alternatives', 'Debugging Docker Memory'.",
    )
    skill_level: str = Field(
        description=(
            "The skill level assumed by the query. One of: "
            "beginner, intermediate, advanced, expert."
        ),
    )
    topic: str = Field(
        description=(
            "The primary topic category. One of: "
            "programming, software-engineering, web-dev, app-dev, backend, "
            "full-stack, ai-llm, machine-learning, devops, data-engineering, "
            "security, gamedev, systems, other. Use 'programming' for general "
            "CS concepts (OOP, algorithms, data structures, design patterns). "
            "Use 'other' ONLY for non-software topics."
        ),
    )


_meta_query_agent = Agent(
    output_type=ExpMetaQueryPlan,
    instructions=(
        "You are a research classifier. You will be given the current date "
        "and the user's question. Produce metadata about the query.\n\n"
        "1. **name**: A concise action phrase (2-4 words) describing what "
        "the user and researcher are doing. Examples: 'Finding Notion "
        "Alternatives', 'Debugging Docker Memory', 'Comparing API "
        "Frameworks'. Keep it short and punchy — no filler words. "
        "No quotes, no punctuation at the end.\n"
        "2. **skill_level**: Infer from the terminology and depth of the "
        "question — beginner, intermediate, advanced, or expert.\n"
        "3. **topic**: Classify into exactly one of: programming, "
        "software-engineering, web-dev, app-dev, backend, full-stack, "
        "ai-llm, machine-learning, devops, data-engineering, security, "
        "gamedev, systems, other. Use 'programming' for general CS concepts "
        "(OOP, algorithms, data structures, design patterns, language features). "
        "Use 'other' ONLY for non-software topics.\n\n"
        "Return structured output only."
    ),
)


# --- Source quality ranking for post-processing ---

# Domains/channels in tier 3 (low quality) — matched as substrings
_LOW_QUALITY_DOMAINS = {
    "geeksforgeeks.org", "w3schools.com", "tutorialspoint.com",
    "javatpoint.com", "programiz.com", "stackoverflow.com",
    "quora.com", "medium.com", "dev.to",
}

# Domains in tier 1 (authoritative) — matched as substrings
_HIGH_QUALITY_DOMAINS = {
    "python.org", "docs.python.org", "developer.mozilla.org", "mdn.mozilla.org",
    "react.dev", "rust-lang.org", "doc.rust-lang.org", "go.dev", "golang.org",
    "nodejs.org", "typescriptlang.org", "kotlinlang.org", "swift.org",
    "docs.oracle.com", "learn.microsoft.com", "developer.apple.com",
    "developer.android.com", "cloud.google.com", "aws.amazon.com",
    "docs.github.com", "wikipedia.org", "arxiv.org",
    "w3.org", "rfc-editor.org", "ietf.org",
    "kernel.org", "linuxfoundation.org",
}

# YouTube channels considered authoritative (lowercase)
_HIGH_QUALITY_CHANNELS = {
    "google", "microsoft", "amazon web services", "aws",
    "github", "mozilla", "linux foundation",
    "python", "pycon", "jsconf", "gophercon", "rustconf",
    "computerphile", "mit opencourseware",
}

# YouTube channels considered mid-tier educators
_MID_QUALITY_CHANNELS = {
    "fireship", "traversy media", "corey schafer", "arjan codes",
    "tech with tim", "the coding train", "sentdex", "ben awad",
    "web dev simplified", "net ninja", "academind",
    "freecodecamp", "freecodecamp.org",
}


def _resource_sort_key(resource: dict) -> int:
    """Return sort tier for a resource: 1 (best) to 3 (worst)."""
    url = resource.get("url", "").lower()
    site = resource.get("site_name", "").lower()
    for domain in _HIGH_QUALITY_DOMAINS:
        if domain in url or domain in site:
            return 1
    for domain in _LOW_QUALITY_DOMAINS:
        if domain in url or domain in site:
            return 3
    return 2


def _video_sort_key(video: dict) -> int:
    """Return sort tier for a YouTube video: 1 (best) to 3 (worst)."""
    channel = video.get("channel", "").lower()
    for ch in _HIGH_QUALITY_CHANNELS:
        if ch in channel:
            return 1
    for ch in _MID_QUALITY_CHANNELS:
        if ch in channel:
            return 2
    return 2  # Unknown channels default to mid-tier


# --- Experimental planner agent ---


class PlannerResource(BaseModel):
    """A selected related resource."""

    title: str
    url: str
    description: str = ""
    site_name: str = ""


class PlannerOutput(BaseModel):
    """Output of the experimental planner agent."""

    youtube_video_ids: list[str] = Field(
        default_factory=list,
        description="Exactly 4 YouTube video IDs to recommend, most relevant first. IDs only — metadata is fetched programmatically.",
    )
    resources: list[PlannerResource] = Field(
        default_factory=list,
        description="Exactly 5 related web resources (tutorials, docs, guides).",
    )
    article_points: list[str] = Field(
        description=(
            "Ordered list of key points the article should cover. "
            "Each point is a concise sentence describing what to address."
        ),
    )


_planner_agent = Agent(
    deps_type=ResearchDeps,
    output_type=PlannerOutput,
    instructions=(
        "You are a research planner. You have the user's question and "
        "metadata from earlier in this conversation.\n\n"
        "## Step 1 — Understand the user\n\n"
        "Before searching, evaluate the user's query and their likely "
        "skill level. What are they trying to do — learn a concept, "
        "understand how something works, fix a bug, compare options, "
        "build something? Decide what approach will help them most and "
        "let that guide your searches and article plan.\n\n"
        "## Step 2 — Research\n\n"
        "Use your tools to find the best possible sources. Keep searching "
        "until you have high-quality results — you can use `search` up to "
        "4 times and `youtube_search` up to 2 times (hard limits).\n\n"
        "1. **Search** — Use the `search` tool to find high-quality web "
        "sources.\n"
        "2. **Verify sources** — Many sites block automated readers, so "
        "you MUST use `source_readable` on each web page you want to "
        "recommend to confirm it's actually accessible. It returns only "
        "a status (readable or not) — you won't see the content, but it "
        "gets cached for the next stage. Do not include a resource you "
        "haven't verified.\n"
        "3. **Find YouTube videos** — Use the `youtube_search` tool to "
        "find relevant videos. Video metadata is fetched programmatically "
        "via the YouTube API, so any video that shows up in search is "
        "accessible. Do NOT use `source_readable` on YouTube video "
        "pages.\n\n"
        "## What to return\n\n"
        "1. **youtube_video_ids** — Exactly 4 YouTube video IDs, most "
        "relevant first. Extract the video ID from YouTube URLs.\n"
        "2. **resources** — Exactly 5 web resources (tutorials, docs, "
        "guides). Use `source_readable` to verify your top picks are "
        "accessible before including them.\n"
        "3. **article_points** — An ordered list of key points the "
        "article should cover, tailored to what the user is trying to "
        "accomplish. Specific, actionable, most important first.\n\n"
        "## Source selection criteria\n\n"
        "Your goal is to find REPUTABLE, AUTHORITATIVE sources — not "
        "listicles, anecdotes, or unreliable user-generated answers.\n\n"
        "**Tier 1 — prefer these**: Official docs, specs, RFCs, "
        "maintainer blogs, project repos, conference talks (PyCon, "
        "JSConf, GopherCon, etc.), channels run by core maintainers.\n\n"
        "**Tier 2 — acceptable**: Reputable tech blogs (Martin Fowler, "
        "Julia Evans), well-known educators (Fireship, Traversy Media, "
        "Corey Schafer, Arjan Codes), thoughtful long-form articles.\n\n"
        "**Tier 3 — avoid or place last**: GeeksForGeeks, W3Schools, "
        "TutorialsPoint, JavaTPoint, Programiz, StackOverflow, Quora, "
        "Medium listicles, generic 'top 10' clickbait, Reddit threads, "
        "forum posts, personal anecdotes.\n\n"
        "When in doubt, prefer the source closest to the people who "
        "actually build or maintain the technology.\n\n"
        "Order both `youtube_video_ids` and `resources` by source "
        "authority — highest quality first.\n\n"
        "## User context\n\n"
        "You may receive background context about the user's technology "
        "stack and skill level. Use it to:\n\n"
        "1. **Tailor resources to their stack.** If they use Python, find "
        "Python-focused resources — don't return .NET, Java, or C# "
        "tutorials unless the query specifically asks about those. Match "
        "the ecosystem: pytest docs for a pytest user, React docs for a "
        "React developer, etc.\n"
        "2. **Tailor videos to their stack.** Same principle — a Python "
        "developer should get Python-oriented video content, not generic "
        "or wrong-language videos.\n"
        "3. **Calibrate depth.** Prefer advanced sources for experienced "
        "users, introductory material for beginners.\n\n"
        "Never mention, reference, or acknowledge the user's profile, "
        "history, or that you know anything about them beyond their "
        "current question.\n\n"
        "Return structured output only."
    ),
)


@_planner_agent.tool
async def search(
    ctx: RunContext[ResearchDeps], query: str,
) -> str:
    """Search the web for pages. Returns titles, URLs, and descriptions."""
    if ctx.deps.search_count >= 4:
        return "ERROR: Search limit reached (4/4). Work with the results you have."
    ctx.deps.search_count += 1
    await ctx.deps.search_rate_limiter.wait()
    results = await tools.brave_search(ctx.deps.http_client, query, num_results=5)
    if not results or (len(results) == 1 and "error" in results[0]):
        return "No results found."
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(
            f"{i}. [{r.get('title', 'Untitled')}]({r.get('url', '')}) "
            f"— {r.get('description', '')}"
        )
    return "\n".join(lines)


@_planner_agent.tool
async def youtube_search(
    ctx: RunContext[ResearchDeps], query: str,
) -> str:
    """Search YouTube for videos. Returns titles, URLs, and descriptions."""
    if ctx.deps.youtube_search_count >= 2:
        return "ERROR: YouTube search limit reached (2/2). Work with the results you have."
    ctx.deps.youtube_search_count += 1
    await ctx.deps.search_rate_limiter.wait()
    results = await tools.brave_search(
        ctx.deps.http_client, f"site:youtube.com {query}", num_results=10,
    )
    if not results or (len(results) == 1 and "error" in results[0]):
        return "No results found."
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(
            f"{i}. [{r.get('title', 'Untitled')}]({r.get('url', '')}) "
            f"— {r.get('description', '')}"
        )
    return "\n".join(lines)


@_planner_agent.tool
async def source_readable(
    ctx: RunContext[ResearchDeps], url: str,
) -> str:
    """Check if a web page is readable and cache its content for later use.
    Returns only a status — you will NOT see the page content.
    Do NOT use this on YouTube video pages."""
    await ctx.deps.read_rate_limiter.wait_if_needed(url)
    page = await tools.jina_read(ctx.deps.http_client, url)
    if "error" in page:
        return f"Not readable: {page['error']}"
    content = page.get("content", "")
    if not content:
        return "Not readable: page had no content."
    ctx.deps.source_cache[url] = page
    return f"Readable ({len(content)} chars)"


# --- Experimental answer writer ---

_EXP_ANSWER_PROMPT = f"""\
You are a research writer. You have the full context of the research so far — \
the user's question, search results, page reads, and a plan with key article \
points from the planner.

## Your task

Write the article following the article points from the plan. Cover each \
point in order, but use your judgment to merge, expand, or reorder where \
it makes the piece flow better.

Don't emphasize dates, recency, or "as of [year]" framing unless the user \
specifically asked about recent changes or the topic genuinely requires it. \
Focus on what's true and useful, not when it became true.

Before writing, evaluate the authority of each source in the research. \
Official docs, specs, and maintainer content are strongest. Community Q&A \
is mid-tier. Reddit posts, forum threads, and personal anecdotes are the \
weakest — if you must use them, frame the evidence as anecdotal \
("users report…", "anecdotal experience suggests…"). Build your core \
argument on the strongest available sources.

If you need to read additional pages to fill gaps or verify claims, use \
the `read_url` tool.

{WRITING_INSTRUCTIONS}
"""

_answer_agent = Agent(
    deps_type=ResearchDeps,
    output_type=ResearchResult,
    instructions=_EXP_ANSWER_PROMPT,
)


@_answer_agent.tool
async def answer_read_url(
    ctx: RunContext[ResearchDeps], url: str,
) -> str:
    """Read the full content of a web page. Use this to fill gaps in the
    provided search results or to get details from a page that looks relevant."""
    await ctx.deps.read_rate_limiter.wait_if_needed(url)
    page = await tools.jina_read(ctx.deps.http_client, url)
    if "error" in page:
        return f"Error reading {url}: {page['error']}"
    content = page.get("content", "")[:4000]
    return content if content else "Page had no readable content."


# --- Experimental example planner ---


class ExampleDescription(BaseModel):
    """Description of a single code example to generate."""

    title: str = Field(description="Short descriptive title (3-10 words)")
    language: str = Field(
        description="Programming language (e.g. python, javascript, go, rust, sql)"
    )
    description: str = Field(
        description="What the example should demonstrate and how (2-4 sentences)"
    )
    scale: str = Field(
        description="Expected size: 'short' (5-15 lines), 'medium' (15-40 lines), or 'large' (40-100 lines)"
    )
    instructions: str = Field(
        description=(
            "Detailed instructions for the code writer. Include: the goal "
            "of the example, what concepts it must cover, the expected "
            "skill level of the reader, and any specific patterns or "
            "approaches to use. This is the primary input for the code "
            "generation model — be specific and thorough."
        ),
    )


class ExamplePlan(BaseModel):
    """Plan for code examples to generate."""

    examples: list[ExampleDescription] = Field(
        default_factory=list,
        description=(
            "Descriptions of examples to generate. Up to 5 short, 2-3 medium, "
            "or 1 large. Empty if the topic doesn't benefit from code examples."
        ),
    )


_example_plan_agent = Agent(
    output_type=ExamplePlan,
    instructions=(
        "You plan code examples that complement a research response. You have "
        "the full conversation history including the research response.\n\n"
        "## Step 1 — Think about what's needed\n\n"
        "Consider what the user is trying to do and what concepts from the "
        "research response would benefit from concrete code. Think about "
        "their skill level — what do they already understand vs. what do "
        "they need demonstrated?\n\n"
        "The examples should cover the same scope as the answer — if the "
        "answer covers multiple concepts or techniques, plan examples that "
        "demonstrate each of them, not just one narrow slice.\n\n"
        "## Rules\n\n"
        "1. **Match the user's technology stack (CRITICAL).** If you receive "
        "context about the user's technologies, you MUST use those to pick "
        "languages, frameworks, and libraries for examples. A Python user "
        "should see Python examples, a React developer should see React "
        "examples. If they use pytest, write tests with pytest. If they use "
        "FastAPI, show FastAPI patterns. The user should feel like the "
        "examples were written specifically for their workflow.\n"
        "2. **Tailor to skill level.** Beginners need simple, well-commented "
        "examples. Experts want concise, idiomatic code showing advanced patterns.\n"
        "3. **Choose the right scale:**\n"
        "   - Up to 5 SHORT examples (5-15 lines each) for topics with many "
        "small concepts\n"
        "   - 2-3 MEDIUM examples (15-40 lines each) for topics that need "
        "more context\n"
        "   - 1 LARGE example (40-100 lines) for topics best shown as a "
        "complete program\n"
        "   - Mix scales if appropriate.\n"
        "4. **Don't repeat the response.** Examples should ADD value beyond "
        "what the written response already covers.\n"
        "5. **Return an empty list** if the topic doesn't benefit from code "
        "examples.\n"
        "6. **Pick the right language.** Use the language most relevant to "
        "the query AND the user's stack. Only fall back to Python if "
        "language-agnostic and no user tech context is available.\n\n"
        "## Ordering (CRITICAL)\n\n"
        "Order examples to progressively develop the reader's understanding "
        "of the concept. Start with the simplest, most foundational example "
        "that establishes core concepts. Each subsequent example should build "
        "on the previous ones — introducing new complexity, combining ideas, "
        "or showing more advanced usage patterns. The final example should "
        "represent the most complete or sophisticated application of the "
        "concept. Think of it as a mini-tutorial where each example is a "
        "stepping stone.\n\n"
        "## Instructions field (CRITICAL)\n\n"
        "The `instructions` field is the primary input for the code writer "
        "model, which has NO access to the conversation history. It must "
        "be self-contained and include:\n"
        "- The goal of the example and what it should accomplish\n"
        "- What concepts it needs to cover and demonstrate\n"
        "- The skill level of the reader\n"
        "- The language to use and any specific patterns or approaches\n"
        "- Enough context that the code writer can produce correct, "
        "relevant code without any other information\n\n"
        "Return structured output only."
    ),
)


# --- Experimental single example writer ---

_single_example_agent = Agent(
    output_type=str,
    instructions=(
        "You write a single, practical code example. You will receive "
        "instructions describing what to write.\n\n"
        "## Before writing\n\n"
        "Consider what you're being asked to do. Think carefully about "
        "what concepts need to be covered and write code that actually "
        "does what's asked — not a toy stub, but a real example that "
        "demonstrates the concept in action.\n\n"
        "## Output format\n\n"
        "Write your output in this exact format:\n\n"
        "```language\ncode here\n```\n\n"
        "A 1-2 sentence explanation of what the code does.\n\n"
        "Rules:\n"
        "- The code must be complete and runnable\n"
        "- Use realistic variable names and scenarios\n"
        "- Include comments where helpful for understanding\n"
        "- The explanation should be 1-2 sentences\n"
        "- Return ONLY the code block and explanation, nothing else"
    ),
)


# --- Experimental pipeline orchestrator ---


async def run_experimental_pipeline(
    query: str,
    deps: ResearchDeps,
    date_context: str,
    emit: EmitFn,
    user_profile: str = "",
) -> tuple[ResearchResult, list[dict], RunUsage, list[dict], list[dict], list[dict], ExpMetaQueryPlan, str, str, dict, list[dict]]:
    """History-threaded experimental research pipeline.

    Threads conversation history through sequential stages:
    1. Meta analysis (name, skill level, topic)
    2. Agentic planner with search + source_readable tools
    3. Post-planner content gathering (video metadata, uncached source reads)
    4. Answer writer (streaming)
    5. Example plan
    6. Parallel example generation (streaming)

    Returns (result, tool_log, total_usage, youtube_videos, resources, code_examples, meta_plan, planner_reasoning, meta_slug, planner_output, example_plan).
    """
    from pydantic_ai import AgentRunResultEvent
    from pydantic_ai.messages import (
        FunctionToolCallEvent,
        FunctionToolResultEvent,
        ModelRequest,
        PartDeltaEvent,
        PartStartEvent,
        TextPart,
        TextPartDelta,
        UserPromptPart,
    )

    tool_log: list[dict] = []
    total_usage = RunUsage()
    youtube_videos: list[dict] = []
    resources: list[dict] = []
    code_examples: list[dict] = []
    example_descriptions: list = []

    # ------------------------------------------------------------------
    # Step 1 — Meta analysis
    # ------------------------------------------------------------------
    await emit("status", stage="planning", message="Analyzing query...")

    meta_query_result = await _meta_query_agent.run(
        f"{date_context}\n\n{query}", model=MODEL,
    )
    plan = meta_query_result.output
    plan.name = plan.name[:200]
    total_usage.incr(meta_query_result.usage())

    # Generate slug for URL and emit session meta immediately
    meta_slug = make_slug(plan.name) if plan.name else ""
    await emit(
        "session_meta",
        name=plan.name,
        slug=meta_slug,
        skill_level=plan.skill_level,
        topic=plan.topic,
    )

    # ------------------------------------------------------------------
    # Step 2 — Agentic planner (search + source_readable tools)
    # ------------------------------------------------------------------
    await emit("status", stage="planning_resources", message="Researching and planning article...")

    history = list(meta_query_result.all_messages())
    # Inject user profile as background context — the planner should use it
    # to subtly calibrate depth/focus but never reference it explicitly.
    context_parts: list[str] = [date_context]
    if user_profile:
        context_parts.append(
            f"[INTERNAL CONTEXT — do not mention or reference this directly]\n"
            f"The user's technology stack and background:\n\n{user_profile}\n\n"
            f"Use this to:\n"
            f"- Search for resources in their ecosystem (Python docs, not "
            f".NET docs; React tutorials, not Angular tutorials)\n"
            f"- Find videos that use their stack (Python videos for Python "
            f"developers, etc.)\n"
            f"- Calibrate depth — advanced sources for experienced users, "
            f"beginner-friendly for newcomers\n\n"
            f"Do not mention the user's profile, history, or that you have "
            f"any prior knowledge of them."
        )
    history.append(ModelRequest(parts=[UserPromptPart(content="\n\n".join(context_parts))]))

    planner_result_data: PlannerOutput | None = None
    planner_reasoning_chunks: list[str] = []

    async for event in _planner_agent.run_stream_events(
        "Research the user's question. Search for the best sources, verify "
        "them with source_readable, find YouTube videos, and plan the "
        "key points the article should cover.",
        message_history=history,
        deps=deps,
        model=CODE_EXAMPLES_MODEL,
    ):
        if isinstance(event, FunctionToolCallEvent):
            args = event.part.args
            tool_input = args if isinstance(args, dict) else {"query": str(args)}
            await emit("tool_use", tool=event.part.tool_name, input=tool_input, status="running")
            tool_log.append({"tool": event.part.tool_name, "input": tool_input, "status": "running"})

        elif isinstance(event, FunctionToolResultEvent):
            raw_content = str(event.result.content)[:5120]
            tool_name = event.result.tool_name
            display_content = raw_content
            await emit("tool_result", tool=tool_name, status="complete", content=display_content)
            for entry in reversed(tool_log):
                if entry.get("tool") == tool_name and entry.get("status") == "running":
                    entry["status"] = "complete"
                    entry["content"] = display_content
                    break

        elif isinstance(event, PartDeltaEvent):
            if isinstance(event.delta, TextPartDelta):
                planner_reasoning_chunks.append(event.delta.content_delta)

        elif isinstance(event, PartStartEvent):
            if isinstance(event.part, TextPart) and event.part.content:
                planner_reasoning_chunks.append(event.part.content)

        elif isinstance(event, AgentRunResultEvent):
            planner_result_data = event.result.output
            total_usage.incr(event.result.usage())

    if planner_result_data is None:
        raise RuntimeError("Planner agent completed without producing a result")

    planner_messages = list(event.result.all_messages())  # type: ignore[union-attr]

    # ------------------------------------------------------------------
    # Step 3 — Post-planner: gather content for the answer writer
    # ------------------------------------------------------------------

    # Fetch full video metadata (title, channel, thumbnail, duration) from YouTube API
    if planner_result_data.youtube_video_ids:
        video_ids = planner_result_data.youtube_video_ids[:4]
        youtube_videos = await tools.youtube_video_details(deps.http_client, video_ids)
        youtube_videos.sort(key=_video_sort_key)
        await emit("youtube_videos", videos=youtube_videos)

    # Enrich resources with OG metadata (images, site names, etc.)
    if planner_result_data.resources:
        resources = [r.model_dump() for r in planner_result_data.resources]
        try:
            og_tasks = [
                tools.fetch_og_metadata(deps.http_client, r["url"])
                for r in resources
            ]
            og_results = await asyncio.gather(*og_tasks, return_exceptions=True)
            for r, og in zip(resources, og_results):
                if isinstance(og, dict):
                    if og.get("og_image"):
                        r["og_image"] = og["og_image"]
                    if og.get("og_site_name") and not r.get("site_name"):
                        r["site_name"] = og["og_site_name"]
                    if og.get("favicon"):
                        r["favicon"] = og["favicon"]
                if not r.get("site_name"):
                    from urllib.parse import urlparse
                    r["site_name"] = urlparse(r.get("url", "")).netloc.replace("www.", "")
        except Exception:
            logger.exception("Failed to fetch OG metadata for resources")
        resources.sort(key=_resource_sort_key)
        await emit("resources", resources=resources)

    # Read any resource URLs not already in the source cache
    uncached_urls = [
        (r.url, r.title)
        for r in planner_result_data.resources
        if r.url not in deps.source_cache
    ]
    if uncached_urls:
        await emit("status", stage="reading", message=f"Reading {len(uncached_urls)} uncached sources...")

        async def _read_uncached(url: str, title: str) -> None:
            await deps.read_rate_limiter.wait_if_needed(url)
            page = await tools.jina_read(deps.http_client, url)
            if "error" not in page and page.get("content"):
                deps.source_cache[url] = page

        await asyncio.gather(*(_read_uncached(url, title) for url, title in uncached_urls))

    # Build source content for injection into answer writer history
    source_content_parts = []
    for r in planner_result_data.resources:
        cached = deps.source_cache.get(r.url)
        if cached:
            content = cached.get("content", "")[:4000]
            source_content_parts.append(
                f"### Source: [{r.title}]({r.url})\n{content}"
            )

    video_summary = ""
    if youtube_videos:
        video_lines = [
            f"- [{v.get('title', '')}](https://youtube.com/watch?v={v.get('video_id', '')}) "
            f"by {v.get('channel', '')}"
            for v in youtube_videos
        ]
        video_summary = "### YouTube Videos\n" + "\n".join(video_lines)

    source_context = "## Source Contents\n\n" + "\n\n".join(source_content_parts)
    if video_summary:
        source_context += "\n\n" + video_summary

    planner_messages.append(ModelRequest(parts=[UserPromptPart(content=source_context)]))

    # ------------------------------------------------------------------
    # Step 4 — Answer writer (streaming)
    # ------------------------------------------------------------------
    await emit("status", stage="synthesizing", message="Writing answer...")

    result_data: ResearchResult | None = None
    answer_messages = None

    async for event in _answer_agent.run_stream_events(
        "Write the article based on the planned points. Use the research "
        "context from the conversation history.",
        message_history=planner_messages,
        deps=deps,
        model=MODEL,
    ):
        if isinstance(event, FunctionToolCallEvent):
            args = event.part.args
            tool_input = args if isinstance(args, dict) else {"url": str(args)}
            await emit("tool_use", tool=event.part.tool_name, input=tool_input, status="running")
            tool_log.append({"tool": event.part.tool_name, "input": tool_input, "status": "running"})

        elif isinstance(event, FunctionToolResultEvent):
            raw_content = str(event.result.content)[:5120]
            url = ""
            for entry in reversed(tool_log):
                if entry.get("tool") == event.result.tool_name and entry.get("status") == "running":
                    url = entry.get("input", {}).get("url", "")
                    break
            if raw_content.startswith("Error reading"):
                display_content = raw_content
            elif raw_content == "Page had no readable content.":
                display_content = raw_content
            else:
                display_content = f"Read {len(raw_content)} chars from {url}" if url else f"Read {len(raw_content)} chars"
            await emit("tool_result", tool=event.result.tool_name, status="complete", content=display_content)
            for entry in reversed(tool_log):
                if entry.get("tool") == event.result.tool_name and entry.get("status") == "running":
                    entry["status"] = "complete"
                    entry["content"] = display_content
                    break

        elif isinstance(event, PartStartEvent):
            if isinstance(event.part, TextPart) and event.part.content:
                await emit("response_chunk", delta=event.part.content)

        elif isinstance(event, PartDeltaEvent):
            if isinstance(event.delta, TextPartDelta):
                await emit("response_chunk", delta=event.delta.content_delta)

        elif isinstance(event, AgentRunResultEvent):
            result_data = event.result.output
            answer_messages = list(event.result.all_messages())
            total_usage.incr(event.result.usage())

    if result_data is None:
        raise RuntimeError("Answer writer completed without producing a result")

    # ------------------------------------------------------------------
    # Step 6 — Example plan
    # ------------------------------------------------------------------
    if plan.topic != "other" and answer_messages:
        try:
            example_prompt = "Plan code examples for this response."
            if user_profile:
                example_prompt += (
                    "\n\n[INTERNAL — do not mention this context]\n"
                    "The user's technology profile. You MUST use this to "
                    "pick languages, frameworks, and libraries for your "
                    "examples. The examples should feel familiar to the "
                    "user — use their tools, their patterns, their stack. "
                    "For example: if they use Python + pytest, write Python "
                    "examples with pytest tests. If they use TypeScript + "
                    "React, write TypeScript/React examples.\n\n"
                    f"{user_profile}"
                )
            example_plan_result = await _example_plan_agent.run(
                example_prompt,
                message_history=answer_messages,
                model=MODEL,
            )
            total_usage.incr(example_plan_result.usage())
            example_descriptions = example_plan_result.output.examples

            # ------------------------------------------------------------------
            # Step 7 — Parallel example generation (streaming)
            # ------------------------------------------------------------------
            if example_descriptions:
                await emit("code_examples_status", status="generating")

                async def _gen_example(idx: int, desc: ExampleDescription) -> dict | None:
                    from google.genai.types import ThinkingLevel

                    try:
                        prompt = (
                            f"## Example to Write\n"
                            f"Title: {desc.title}\n"
                            f"Language: {desc.language}\n"
                            f"Scale: {desc.scale}\n\n"
                            f"## Instructions\n{desc.instructions}"
                        )
                        text_chunks: list[str] = []

                        async for ev in _single_example_agent.run_stream_events(
                            prompt,
                            model=CODE_EXAMPLES_MODEL,
                            model_settings={
                                "google_thinking_config": {
                                    "thinking_level": ThinkingLevel.MEDIUM,
                                },
                            },
                        ):
                            if isinstance(ev, PartStartEvent):
                                if isinstance(ev.part, TextPart) and ev.part.content:
                                    text_chunks.append(ev.part.content)
                                    await emit(
                                        "code_example_chunk",
                                        index=idx,
                                        delta=ev.part.content,
                                    )
                            elif isinstance(ev, PartDeltaEvent):
                                if isinstance(ev.delta, TextPartDelta):
                                    text_chunks.append(ev.delta.content_delta)
                                    await emit(
                                        "code_example_chunk",
                                        index=idx,
                                        delta=ev.delta.content_delta,
                                    )
                            elif isinstance(ev, AgentRunResultEvent):
                                total_usage.incr(ev.result.usage())

                        full_text = "".join(text_chunks)
                        # Parse the streamed text into structured example data
                        example_data = _parse_example_text(
                            full_text, desc.title, desc.language,
                        )
                        await emit(
                            "code_example_complete",
                            index=idx,
                            example=example_data,
                        )
                        return example_data
                    except Exception as exc:
                        logger.exception(
                            "Failed to generate example %d: %s", idx, exc,
                        )
                        return None

                example_results = await asyncio.gather(
                    *[_gen_example(i, desc) for i, desc in enumerate(example_descriptions)],
                )
                code_examples = [ex for ex in example_results if ex is not None]
                await emit("code_examples_status", status="done")

        except Exception as exc:
            logger.exception("Example planning/generation failed: %s", exc)
            await emit("code_examples_status", status="done")

    planner_reasoning = "".join(planner_reasoning_chunks)
    planner_output = planner_result_data.model_dump() if planner_result_data else {}
    example_plan_output = [desc.model_dump() for desc in example_descriptions] if example_descriptions else []
    return result_data, tool_log, total_usage, youtube_videos, resources, code_examples, plan, planner_reasoning, meta_slug, planner_output, example_plan_output


def _parse_example_text(text: str, fallback_title: str, fallback_lang: str) -> dict:
    """Parse streamed example text into structured example data.

    Expected format:
    ```language
    code
    ```

    explanation
    """
    import re

    code = ""
    language = fallback_lang
    explanation = ""

    # Extract code block
    code_match = re.search(r"```(\w*)\n(.*?)```", text, re.DOTALL)
    if code_match:
        if code_match.group(1):
            language = code_match.group(1)
        code = code_match.group(2).strip()
        # Everything after the code block is the explanation
        after_code = text[code_match.end():].strip()
        if after_code:
            # Strip any <explanation> tags the model may have output
            explanation = re.sub(r"</?explanation>", "", after_code).strip()
    else:
        # No code block found — treat the whole text as code
        code = text.strip()

    return {
        "title": fallback_title,
        "language": language,
        "code": code,
        "explanation": explanation,
    }


# ============================================================================
# User profiling — background task after each query
# ============================================================================


class UserProfileTechnology(BaseModel):
    """A technology the user works with or is researching."""

    name: str = Field(description="Technology name, e.g. 'Python', 'React', 'PostgreSQL', 'Docker'")
    relationship: Literal["uses", "researching", "both"] = Field(
        description=(
            "'uses' = actively works with it (evidenced by debugging, building, integration queries). "
            "'researching' = exploring or learning about it (conceptual, comparison, getting-started queries). "
            "'both' = actively uses AND currently deepening knowledge."
        ),
    )


class UserProfileOutput(BaseModel):
    """Structured user profile produced by the profiler agent."""

    profile: str = Field(
        description="2-5 paragraph narrative profile of the user in third person.",
    )
    technologies: list[UserProfileTechnology] = Field(
        description=(
            "Technologies the user uses or is researching, extracted from ALL "
            "queries (not just the latest). Carry forward technologies from the "
            "existing profile and add/update based on the new query. "
            "Order by relevance — most central to their work first."
        ),
    )
    suggested_queries: list[str] = Field(
        description=(
            "Exactly 3 research queries the user might want to explore next. "
            "Base these on their profile, recent queries, and current interests. "
            "Make them specific, actionable, and varied — not just rephrasing "
            "their last search. Each should be a natural search query, not a title."
        ),
    )


_user_profile_agent = Agent(
    output_type=UserProfileOutput,
    instructions="""\
You are a user profiler for a developer research tool called Scan.

You will be given a user's existing profile (may be empty for new users) and
their latest research query. Produce an updated structured profile.

## Profile narrative (2-5 paragraphs)

Cover:
- Their apparent technical interests and domains (e.g. web dev, DevOps, ML)
- Their estimated skill level and how it's evolving
- Patterns in their research (e.g. always debugging, learning new frameworks, comparing tools)
- Any notable traits (e.g. prefers self-hosted solutions, works with specific languages)

## Technologies list

Track every technology the user works with or is researching:
- **uses**: they actively build with it (debugging, integration, deployment queries)
- **researching**: they're exploring or learning about it (conceptual, comparison, intro queries)
- **both**: they use it AND are currently deepening their knowledge of it

Carry forward all technologies from the existing profile. Add new ones from the
latest query. Update the relationship if evidence changes (e.g. a user who was
"researching" React and now asks about debugging a React component → "both").

Be specific with technology names — "FastAPI" not "Python web framework",
"PostgreSQL" not "SQL database". Include languages, frameworks, libraries,
platforms, and tools.

## Suggested queries

Generate exactly 3 research queries the user might want to explore next:
- Base them on their profile, tech stack, recent queries, and apparent interests
- Make them specific and actionable (e.g. "How to set up database migrations with Alembic and async SQLAlchemy" not "database stuff")
- Vary them — cover different aspects of their interests, don't cluster around one topic
- Don't repeat or rephrase their recent queries — suggest genuinely new directions
- Write them as natural search queries, not titles or descriptions

## Rules
- Write the narrative in third person ("This user...")
- Be concise but insightful
- Synthesize — don't just append the new query to the existing profile
- If the existing profile is empty, create everything from scratch
- Don't speculate wildly — stick to what the queries reveal
""",
)


async def generate_user_profile(
    query: str,
    existing_profile: str,
    query_count: int,
    existing_technologies: list[dict] | None = None,
    recent_queries: list[str] | None = None,
) -> tuple[UserProfileOutput, RunUsage]:
    """Generate/update a user profile based on their latest query.

    Returns (UserProfileOutput, usage).
    """
    prompt = f"Existing profile ({query_count} previous queries):\n"
    if existing_profile:
        prompt += existing_profile
    else:
        prompt += "(New user — no existing profile)"

    if existing_technologies:
        prompt += "\n\nExisting technologies:\n"
        for tech in existing_technologies:
            prompt += f"- {tech['name']} ({tech['relationship']})\n"

    if recent_queries:
        prompt += "\n\nRecent searches (most recent first):\n"
        for i, q in enumerate(recent_queries, 1):
            prompt += f"{i}. {q}\n"

    prompt += f"\n\nLatest query:\n{query}"

    result = await _user_profile_agent.run(prompt, model=CODE_EXAMPLES_MODEL)
    return result.output, result.usage()
