"""Stage 2 of the blogging pipeline — Scout.

Searches the web for current tech news, reads candidate pages via Jina,
returns 2-3 ScoutTopic suggestions. Scout never sees raw page text — only
Gemini-generated summaries.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import skrift
from pydantic import BaseModel, Field
from pydantic_ai import RunContext
from skrift.agents.models import ResumeContext

from smarter_dev.web.blogging_agent.cache import get_cache
from smarter_dev.web.blogging_agent.summariser import summarise_news_page
from smarter_dev.web.research_tools import brave_search, jina_read

# Runtime import: pydantic-ai resolves the @tool `ctx: RunContext[...]`
# annotations via get_type_hints at materialization on the worker, so it must
# be a real module global (pydantic-ai core only; providers stay worker-side).

SCOUT_MODEL = os.getenv("BLOGGING_SCOUT_MODEL", "gemini-3-flash-preview")
SCOUT_AGENT_NAME = "blogging.scout"
_PROMPT = (Path(__file__).parent / "prompts" / "scout.md").read_text(
    encoding="utf-8"
)


class ScoutTopic(BaseModel):
    """A current-events claim, surfaced for a downstream hypothesis pass.

    Same neutral shape the chat agent's ``BlogTopicCandidate`` uses, so
    Brainstorm sees both inputs uniformly. NOT a pitch — no "the take",
    no editorial.
    """

    headline: str = Field(
        description="Descriptive label, one line. NOT editorial / clickbait."
    )
    observation: str = Field(
        description=(
            "What was actually reported / changed / released. 2-4 "
            "sentences. Faithful paraphrase of the primary source. No "
            "interpretation, no spin."
        ),
    )
    scope: str = Field(
        description=(
            "Neutral surface-area description — what a post on this "
            "would cover. NOT the take."
        ),
    )
    evidence: list[str] = Field(
        default_factory=list,
        description="1-3 primary-source URLs.",
    )
    category: Literal["concept", "misconception", "news"] | None = Field(
        default="news",
        description=(
            "Almost always 'news' for Scout. 'concept' / 'misconception' "
            "allowed if the surfaced item is genuinely one of those."
        ),
    )


class ScoutOutput(BaseModel):
    topics: list[ScoutTopic] = Field(default_factory=list)


@dataclass
class ScoutDeps:
    """Per-run deps for Scout. Carries the run id so tools can look up the cache."""

    run_id: str


def _build_deps(ctx: ResumeContext) -> ScoutDeps:
    return ScoutDeps(run_id=str(ctx.deps_ref.get("run_id", "")))


scout_agent = skrift.Agent(
    f"google-gla:{SCOUT_MODEL}",
    name=SCOUT_AGENT_NAME,
    system_prompt=_PROMPT,
    output_type=ScoutOutput,
    model_settings={"google_thinking_config": {"thinking_level": "MEDIUM"}},
    deps_type=ScoutDeps,
    deps_factory=_build_deps,
)


@scout_agent.tool
async def search_news(
    ctx: RunContext[ScoutDeps], query: str
) -> list[dict]:
    """Search the web via Brave Search and return up to 5 result snippets.

    Bias queries toward Hacker News, Reddit, and reputable publications by
    appending ``site:...`` operators where useful. Returns a list of
    ``{title, url, description}`` dicts. Aim for at most 4-5 calls.

    Args:
        query: Search query string.
    """
    cache = get_cache(ctx.deps.run_id)
    await cache.search_rate_limiter.wait()
    return await brave_search(cache.http_client, query, num_results=5)


@scout_agent.tool
async def read_news(ctx: RunContext[ScoutDeps], url: str) -> dict:
    """Read a news page and return a 3-6 sentence Gemini-generated summary.

    The raw page is cached per-URL for the duration of this run, so a
    repeat call returns the same summary without a network round-trip.

    Args:
        url: A URL returned by a prior ``search_news`` call.

    Returns:
        Dict ``{url, title, summary}``. ``summary`` may say "[summary
        unavailable]" if the page was a paywall / 404 / noise.
    """
    cache = get_cache(ctx.deps.run_id)
    if url in cache.news_summaries:
        # Cached summary hit. Title is not preserved in the summary cache,
        # so re-look up from raw cache if we have it.
        return {
            "url": url,
            "title": _title_from_raw_cache(cache.raw_reads.get(url)),
            "summary": cache.news_summaries[url],
        }

    raw = cache.raw_reads.get(url)
    title: str | None = None
    if raw is None:
        await cache.url_rate_limiter.wait_if_needed(url)
        result = await jina_read(cache.http_client, url)
        if "error" in result:
            return {"url": url, "title": None, "summary": f"[read failed: {result['error']}]"}
        content = result.get("content") or ""
        title = result.get("title")
        cache.raw_reads[url] = content
        raw = content
    else:
        title = _title_from_raw_cache(raw)

    summary = await summarise_news_page(text=raw, url=url, title=title)
    cache.news_summaries[url] = summary
    return {"url": url, "title": title, "summary": summary}


def _title_from_raw_cache(raw: str | None) -> str | None:
    """Best-effort title-of-page extraction from Jina markdown."""
    if not raw:
        return None
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip() or None
        if stripped:
            # Jina sometimes emits "Title: ..." as a header field at the top.
            if stripped.lower().startswith("title:"):
                return stripped.split(":", 1)[1].strip() or None
            break
    return None
