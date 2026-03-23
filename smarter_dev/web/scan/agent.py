"""Pydantic AI research agent for Scan.

Skill-driven 4-mode system:
- **Quick Answer**: Fast retrieval via GPT 5.4 Nano + Flash Lite synthesis.
- **Quick Research**: Contextual retrieval with related context.
- **Standard**: Exploration mode via Gemini Flash + Flash synthesis.
- **Deep**: Investigation mode with comprehensive research.

Each mode loads a research skill and a synthesis skill (markdown documents)
that govern agent behavior.  Tools (Search, Read, YouTubeSearch) are
identical across all modes — what changes is the skill.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPartDelta,
)
from pydantic_ai.usage import RunUsage

from smarter_dev.web.scan import tools
from smarter_dev.web.scan.skills import load_research_skill, load_synthesis_skill
from smarter_dev.web.scan.tools import RateLimiter, URLRateLimiter

logger = logging.getLogger(__name__)

# Type alias for the SSE emit callback used throughout the pipeline.
EmitFn = Callable[..., Coroutine[Any, Any, None]]

# ---------------------------------------------------------------------------
# Model configuration per mode
# ---------------------------------------------------------------------------

FLASH_LITE = "google-gla:gemini-3.1-flash-lite-preview"
FLASH = "google-gla:gemini-3-flash-preview"
NANO = "openai:gpt-5.4-nano"


@dataclass(frozen=True)
class ModeConfig:
    """Model selection and budget for a research mode."""

    mode: str
    research_model: str
    synthesis_model: str
    examples_model: str = FLASH


MODES: dict[str, ModeConfig] = {
    "quick_answer": ModeConfig(
        mode="quick_answer",
        research_model=NANO,
        synthesis_model=FLASH_LITE,
    ),
    "quick_research": ModeConfig(
        mode="quick_research",
        research_model=NANO,
        synthesis_model=FLASH_LITE,
    ),
    "standard": ModeConfig(
        mode="standard",
        research_model=FLASH,
        synthesis_model=FLASH,
    ),
    "deep": ModeConfig(
        mode="deep",
        research_model=FLASH,
        synthesis_model=FLASH,
    ),
}

# Kept for backward compatibility with runner imports.
MODEL = FLASH_LITE
CODE_EXAMPLES_MODEL = FLASH


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def make_slug(name: str) -> str:
    """Generate a URL slug from a session name with a timestamp suffix."""
    import re
    import time

    slug = name.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    slug = slug[:200]
    timestamp = str(int(time.time()))
    return f"{slug}-{timestamp}"


def _usage_to_dict(usage: RunUsage) -> dict:
    """Convert RunUsage to a serializable dict, omitting zero values."""
    d = dataclasses.asdict(usage)
    return {k: v for k, v in d.items() if v}


# ---------------------------------------------------------------------------
# Shared dependencies
# ---------------------------------------------------------------------------


@dataclass
class ResearchDeps:
    session_id: str
    http_client: httpx.AsyncClient
    search_rate_limiter: RateLimiter
    read_rate_limiter: URLRateLimiter
    source_cache: dict[str, dict] = dataclasses.field(default_factory=dict)
    seen_urls: set[str] = dataclasses.field(default_factory=set)
    search_count: int = 0
    youtube_search_count: int = 0


# ---------------------------------------------------------------------------
# Domain / channel quality tiers (used for post-research enrichment sorting)
# ---------------------------------------------------------------------------

_LOW_QUALITY_DOMAINS = {
    "geeksforgeeks.org", "w3schools.com", "tutorialspoint.com",
    "javatpoint.com", "programiz.com", "stackoverflow.com",
    "quora.com", "medium.com", "dev.to",
}

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

_HIGH_QUALITY_CHANNELS = {
    "google", "microsoft", "amazon web services", "aws",
    "github", "mozilla", "linux foundation",
    "python", "pycon", "jsconf", "gophercon", "rustconf",
    "computerphile", "mit opencourseware",
}

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
    return 2


# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------

# -- Final output (consumed by runner for DB persistence) --

class Source(BaseModel):
    url: str
    title: str
    type: Literal["docs", "repo", "article", "video", "forum", "other"] = "other"
    snippet: str = ""
    cited: bool = False


class ResearchResult(BaseModel):
    """Final synthesis output persisted to the database."""
    response: str
    sources: list[Source]
    summary: str


# -- Structured research output contract (research → synthesis handoff) --

class ResearchSource(BaseModel):
    """A source discovered during research."""
    url: str
    title: str
    type: str = Field(
        default="other",
        description="docs, tutorial, blog, comparison, video, reference, case-study, research, or benchmark",
    )
    content: str = Field(description="Extracted or summarized content from this source")
    relevance: str = Field(default="", description="One-sentence relevance note")
    credibility_note: str = Field(default="", description="Author authority or currency note")


class YouTubeResult(BaseModel):
    """A YouTube video found during research."""
    url: str
    title: str
    relevance: str = Field(default="", description="Why this video is relevant")


class ResourceLink(BaseModel):
    """A curated link for sidebar display."""
    url: str
    title: str
    description: str = ""


class ResearchOutput(BaseModel):
    """Structured contract between the research and synthesis stages.

    The research agent returns this as structured output.  Synthesis receives
    it as clean, curated context — no raw conversation history.
    """
    sources: list[ResearchSource] = Field(description="Researched sources with extracted content")
    key_insights: list[str] = Field(description="Critical findings and conclusions")
    outline: list[str] = Field(description="Structural plan for the synthesis response")
    youtube_urls: list[YouTubeResult] = Field(default_factory=list, description="YouTube videos found during research")
    resources: list[ResourceLink] = Field(default_factory=list, description="Curated links for sidebar display")


# ---------------------------------------------------------------------------
# Meta analysis — mode detection
# ---------------------------------------------------------------------------

class SessionMeta(BaseModel):
    """Metadata produced by the meta analysis stage."""
    name: str = Field(description="Brief session title, 2-5 words")
    skill_level: str = Field(description="beginner, intermediate, advanced, or expert")
    topic: str = Field(
        description=(
            "One of: programming, software-engineering, web-dev, app-dev, "
            "backend, full-stack, ai-llm, machine-learning, devops, "
            "data-engineering, security, gamedev, systems, other"
        ),
    )
    query_format: str = Field(description="simple or complex")
    research_mode: str = Field(
        description="quick_answer, quick_research, standard, or deep",
    )


_META_PROMPT = """\
Analyze this user query and produce structured metadata.

## Mode Detection

Select the research mode based on query characteristics:

| Mode | Triggers |
|------|----------|
| quick_answer | Short queries (<15 words), "what is," "how to," "syntax for," single-concept lookups, questions with a single obvious answer, error messages |
| quick_research | "How to" with context, "best way to," "explain," simple comparisons, "what should I use for," questions that benefit from 1-2 related angles |
| standard | Complex comparisons ("X vs Y in context of Z"), "how should I architect," multi-part questions, requests for examples with alternatives, "recommend" with constraints |
| deep | "Tradeoffs," "architecture," "current state of," "help me understand," "evaluate," complex/multi-concept queries, follow-up depth requests |

The boundary between quick_answer and quick_research is thin — lean toward \
quick_research when ambiguous since the cost difference is negligible and the \
quality bump is noticeable.

## Query Format

- **simple**: Direct questions with clear answers, single-concept lookups
- **complex**: Multi-part questions, comparisons, architectural decisions

## Skill Level

Infer from query language and complexity:
- **beginner**: Basic syntax, "how do I," simple setup questions
- **intermediate**: Framework usage, best practices, debugging
- **advanced**: Architecture, performance, complex patterns
- **expert**: Low-level systems, advanced optimization, niche topics

## Name

Generate a brief, descriptive title (2-5 words) for this research session. \
Use action phrases: "Set Up CORS FastAPI", "Compare ORM Options".
"""

_meta_agent = Agent(
    output_type=SessionMeta,
    instructions=_META_PROMPT,
)


async def generate_session_meta(query: str) -> tuple[SessionMeta, RunUsage]:
    """Classify query and auto-detect research mode."""
    try:
        result = await _meta_agent.run(
            f"Query: {query}",
            model=FLASH_LITE,
        )
        meta = result.output
        # Validate research_mode
        if meta.research_mode not in MODES:
            meta.research_mode = "quick_research"
        return meta, result.usage()
    except Exception:
        logger.exception("Meta analysis failed, defaulting to quick_research")
        return SessionMeta(
            name="Research",
            skill_level="intermediate",
            topic="other",
            query_format="simple",
            research_mode="quick_research",
        ), RunUsage()


# ---------------------------------------------------------------------------
# Writing instructions (shared by synthesis)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Research agent — skill-driven
# ---------------------------------------------------------------------------

# A single generic agent whose behavior is controlled by the loaded skill.
# Tools are registered once; the system prompt is set at call time.
_research_agent = Agent(
    deps_type=ResearchDeps,
    output_type=ResearchOutput,
)


@_research_agent.tool
async def search(ctx: RunContext[ResearchDeps], query: str) -> str:
    """Search the web. Returns ranked results with titles, URLs, and snippets."""
    deps = ctx.deps
    await deps.search_rate_limiter.wait()
    deps.search_count += 1

    results = await tools.brave_search(deps.http_client, query, num_results=15)
    if not results:
        return "No results found."

    # Track seen URLs for validation
    for r in results:
        deps.seen_urls.add(r["url"])

    # Format as markdown list, stripping any HTML from snippets
    import re
    lines = []
    for i, r in enumerate(results, 1):
        title = re.sub(r"<[^>]+>", "", r.get("title", ""))
        lines.append(f"{i}. [{title}]({r['url']})")
        if r.get("description"):
            desc = re.sub(r"<[^>]+>", "", r["description"][:200])
            lines.append(f"   {desc}")
    return "\n".join(lines)


@_research_agent.tool
async def read(
    ctx: RunContext[ResearchDeps],
    url: str,
    summarization_instructions: str = "",
) -> str:
    """Read a web page. Optionally provide summarization instructions to control depth."""
    deps = ctx.deps
    await deps.read_rate_limiter.wait_if_needed(url)

    # Check cache
    if url in deps.source_cache:
        content = deps.source_cache[url].get("content", "")
        if content:
            return content[:8000]

    result = await tools.jina_read(deps.http_client, url)
    content = result.get("content", "") if isinstance(result, dict) else str(result)
    if content:
        deps.source_cache[url] = {"content": content, "url": url, "title": result.get("title", "") if isinstance(result, dict) else ""}
        deps.seen_urls.add(url)
    return (content or "Failed to read page.")[:8000]


@_research_agent.tool
async def youtube_search(ctx: RunContext[ResearchDeps], query: str) -> str:
    """Search YouTube for relevant videos. Returns video titles, URLs, and descriptions."""
    deps = ctx.deps
    await deps.search_rate_limiter.wait()
    deps.youtube_search_count += 1

    results = await tools.youtube_search(deps.http_client, query, num_results=10)
    if not results:
        return "No YouTube results found."

    for r in results:
        deps.seen_urls.add(r["url"])

    lines = []
    for r in results:
        lines.append(f"- [{r['title']}]({r['url']})")
    return "\n".join(lines)


async def run_research(
    query: str,
    deps: ResearchDeps,
    mode_config: ModeConfig,
    date_context: str,
    emit: EmitFn,
    user_profile: str = "",
) -> tuple[ResearchOutput, list[dict], RunUsage]:
    """Run the skill-driven research agent.

    Returns (ResearchOutput, tool_log, usage).
    """
    skill_text = load_research_skill(mode_config.mode)

    # Build system prompt: skill + date context + user profile
    system_parts = [skill_text, f"\n\n## Current Date\n{date_context}"]
    if user_profile:
        system_parts.append(
            f"\n\n## User Profile\n"
            f"Use this to tailor your research — match their stack, skill level, "
            f"and interests. Do not mention the profile explicitly in output.\n\n"
            f"{user_profile}"
        )
    system_prompt = "".join(system_parts)

    from pydantic_ai import AgentRunResultEvent

    tool_log: list[dict] = []
    result_data: ResearchOutput | None = None
    usage = RunUsage()
    await emit("status", stage="researching")

    async for event in _research_agent.run_stream_events(
        f"Research this query:\n\n{query}",
        deps=deps,
        model=mode_config.research_model,
        instructions=system_prompt,
    ):
        if isinstance(event, FunctionToolCallEvent):
            args = event.part.args
            # Normalize args to a clean dict for frontend display
            if isinstance(args, str):
                tool_input = {"query": args}
            elif isinstance(args, dict):
                tool_input = args
            else:
                tool_input = {"input": str(args)}
            tool_log.append({"tool": event.part.tool_name, "input": tool_input, "status": "running"})
            await emit("tool_use", tool=event.part.tool_name, input=tool_input, status="running")

        elif isinstance(event, FunctionToolResultEvent):
            raw_content = str(event.result.content)[:5120]
            # Strip HTML tags from search result snippets
            import re
            clean_content = re.sub(r"<[^>]+>", "", raw_content)
            # Build a human-readable summary for display
            tool_name = event.result.tool_name
            url = ""
            for entry in reversed(tool_log):
                if entry.get("tool") == tool_name and entry.get("status") == "running":
                    entry["status"] = "complete"
                    entry["content"] = clean_content[:512]
                    url = entry.get("input", {}).get("url", "")
                    break
            if tool_name in ("read", "read_url", "answer_read_url"):
                if clean_content.startswith("Error") or clean_content.startswith("Failed"):
                    display = clean_content[:200]
                else:
                    display = f"Read {len(clean_content)} chars" + (f" from {url}" if url else "")
            else:
                display = clean_content
            await emit("tool_result", tool=tool_name, status="complete", content=display)

        elif isinstance(event, AgentRunResultEvent):
            result_data = event.result.output
            usage = event.result.usage()

    if result_data is None:
        raise RuntimeError("Research agent completed without producing a result")

    return result_data, tool_log, usage


# ---------------------------------------------------------------------------
# Synthesis agent — clean context, single-shot
# ---------------------------------------------------------------------------

_synthesis_agent = Agent(
    output_type=ResearchResult,
)


async def run_synthesis(
    query: str,
    research_output: ResearchOutput,
    mode_config: ModeConfig,
    user_profile: str = "",
    emit: EmitFn | None = None,
) -> tuple[ResearchResult, RunUsage]:
    """Run the synthesis agent with clean, curated context.

    Receives the structured ResearchOutput — no raw conversation history.
    Streams response_chunk events via emit callback.
    """
    skill_text = load_synthesis_skill(mode_config.mode)
    system_prompt = f"{skill_text}\n\n{WRITING_INSTRUCTIONS}"

    # Format the research output as the synthesis input
    input_parts = [f"## User Query\n{query}"]

    if user_profile:
        input_parts.append(f"\n## User Profile\n{user_profile}")

    # Sources
    if research_output.sources:
        input_parts.append("\n## Sources")
        for s in research_output.sources:
            input_parts.append(f"\n### {s.title}\n**URL:** {s.url}\n**Type:** {s.type}")
            if s.relevance:
                input_parts.append(f"**Relevance:** {s.relevance}")
            if s.credibility_note:
                input_parts.append(f"**Credibility:** {s.credibility_note}")
            input_parts.append(f"\n{s.content}")

    # Key insights
    if research_output.key_insights:
        input_parts.append("\n## Key Insights")
        for insight in research_output.key_insights:
            input_parts.append(f"- {insight}")

    # Outline
    if research_output.outline:
        input_parts.append("\n## Outline")
        for item in research_output.outline:
            input_parts.append(f"- {item}")

    # YouTube
    if research_output.youtube_urls:
        input_parts.append("\n## YouTube Videos")
        for yt in research_output.youtube_urls:
            input_parts.append(f"- [{yt.title}]({yt.url})")
            if yt.relevance:
                input_parts.append(f"  {yt.relevance}")

    # Resources
    if research_output.resources:
        input_parts.append("\n## Resources")
        for r in research_output.resources:
            input_parts.append(f"- [{r.title}]({r.url})")
            if r.description:
                input_parts.append(f"  {r.description}")

    synthesis_input = "\n".join(input_parts)

    from pydantic_ai import AgentRunResultEvent

    if emit:
        await emit("status", stage="synthesizing")

    result_data: ResearchResult | None = None
    usage = RunUsage()

    async for event in _synthesis_agent.run_stream_events(
        synthesis_input,
        model=mode_config.synthesis_model,
        instructions=system_prompt,
    ):
        if isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
            if emit:
                await emit("response_chunk", delta=event.delta.content_delta)
        elif isinstance(event, AgentRunResultEvent):
            result_data = event.result.output
            usage = event.result.usage()

    if result_data is None:
        raise RuntimeError("Synthesis agent completed without producing a result")

    return result_data, usage


# ---------------------------------------------------------------------------
# Code examples agent (post-synthesis)
# ---------------------------------------------------------------------------


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
    """Generate code examples that complement a synthesis response.

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


# ---------------------------------------------------------------------------
# User profile agent (unchanged from previous implementation)
# ---------------------------------------------------------------------------


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
- **Query 1-2**: Based on their profile, tech stack, and recent queries. Specific and actionable. Only reference technologies they've actually used or searched for.
- **Query 3**: A boundary-pushing suggestion — something their profile *hints* they might be interested in but haven't explored yet. Infer from the shape of their work (e.g. someone building async web services might benefit from learning about observability, or someone doing lots of data processing might be ready for stream processing). This should feel like a natural next step, not a random topic.
- Make all 3 specific and actionable (e.g. "How to set up database migrations with Alembic and async SQLAlchemy" not "database stuff")
- Don't repeat or rephrase their recent queries
- Write them as natural search queries, not titles or descriptions

## Rules
- Write the narrative in third person ("This user...")
- Be concise but insightful — factual, not flattering. Don't hype or glaze the user. State what they do and research, not how impressive they are.
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
    """Generate/update a user profile based on their latest query."""
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
