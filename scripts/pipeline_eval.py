"""Local researcher × author pipeline evaluation harness.

Pipeline shape:

    prompt
       │
       ▼
    [Researcher] — searches `resource_sources` / `resource_tools` for
                   curated entries relevant to the prompt, outputs a
                   compact "research findings" block.
       │
       ▼
    [Author]     — receives prompt + research, produces the final
                   answer markdown.

For each prompt the harness runs every (researcher, author) pair and
captures:
- Each researcher's raw research output.
- Each author's final answer for each research source.
- Per-call usage (input/output tokens) and dollar cost using the
  pricing table baked in below.

Output: a single self-contained HTML report with every transcript
inline plus a per-prompt and grand-total cost table. Each prompt
section is a `<details>` so the matrix (3 researchers × 2 authors per
prompt) doesn't drown in scroll. Researcher + author bodies are
markdown-rendered so citations are clickable.

Usage:

    uv run python scripts/pipeline_eval.py \
        --prompts scripts/eval_prompts.txt \
        --output reports/eval_$(date +%Y%m%d_%H%M%S).html

Prompts file: one prompt per line. Lines starting with `#` are skipped,
blank lines separate runs. Multi-line prompts are also supported via a
`---` separator on its own line.

The DB connection comes from `smarter_dev.shared.config`'s effective
database URL — point it at the same PG you'd run the prod agent
against (the catalog is what we're testing against). Requires
`GEMINI_API_KEY` (Google) and `OPENAI_API_KEY` to be set.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import os
import secrets
import sys
import time
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from smarter_dev.shared.config import get_settings
from smarter_dev.shared.database import convert_postgres_url_for_asyncpg
# Reuse the production resources-agent authoring prompt so the eval
# author's tone, posture, response shape, and rich-block rules match
# what users see in prod. The researcher prompt stays bespoke (the
# researcher's job is a strict subset — find sources, no authoring).
from smarter_dev.web.resources_agent import _SYSTEM_PROMPT as _PROD_AUTHOR_PROMPT

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pricing (per 1M tokens). Update the table here as Google/OpenAI publish
# new tiers. Costs are USD.
# ---------------------------------------------------------------------------

PRICING: dict[str, dict[str, float]] = {
    # OpenAI
    "gpt-5.4-nano":           {"input": 0.20, "output": 1.25},
    # Google
    "gemini-3.1-flash-lite":  {"input": 0.25, "output": 1.50},
    "gemini-3-flash-preview": {"input": 0.50, "output": 3.00},
    # alias the prod resource-agent model uses
    "gemini-3-flash":         {"input": 0.50, "output": 3.00},
}


def cost_for(model_id: str, input_tokens: int, output_tokens: int) -> float:
    rates = PRICING.get(model_id)
    if rates is None:
        return 0.0
    return (
        (input_tokens / 1_000_000.0) * rates["input"]
        + (output_tokens / 1_000_000.0) * rates["output"]
    )


# ---------------------------------------------------------------------------
# Model factories
# ---------------------------------------------------------------------------

def _google_model(model_id: str) -> GoogleModel:
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
    return GoogleModel(model_id, provider=GoogleProvider(api_key=api_key))


def _openai_model(model_id: str):
    """Pick the right OpenAI client for the model.

    gpt-5 family + reasoning effort + function tools is only supported
    on the Responses API (`/v1/responses`); the Chat Completions
    endpoint rejects the combo. Default to Responses for gpt-5*, keep
    Chat for older families.
    """
    api_key = os.getenv("OPENAI_API_KEY") or ""
    provider = OpenAIProvider(api_key=api_key)
    if model_id.startswith("gpt-5"):
        return OpenAIResponsesModel(model_id, provider=provider)
    return OpenAIChatModel(model_id, provider=provider)


def build_model(model_id: str):
    if model_id.startswith("gpt-") or model_id.startswith("openai/"):
        return _openai_model(model_id.removeprefix("openai/"))
    return _google_model(model_id)


# ---------------------------------------------------------------------------
# Opaque document-ID catalog addressing
#
# To stop researchers from "guessing" URLs they recognise from training
# data, the harness hides real URLs behind opaque document IDs of the
# shape `doc_<6 hex>`. `search_curated` results expose only the
# `document_id`; `open_document` accepts only IDs that came back from a
# `search_curated` call in the same run. Before the author runs, IDs
# are resolved back to real URLs + titles so the prose author writes
# against the same catalog the prod agent does.
# ---------------------------------------------------------------------------


def _new_document_id(taken: set[str]) -> str:
    while True:
        candidate = f"doc_{secrets.token_hex(3)}"
        if candidate not in taken:
            return candidate


# ---------------------------------------------------------------------------
# Curated catalog search — same query the production resource agent uses,
# inlined so the harness doesn't import the production agent (which would
# pull in Skrift's worker runtime).
# ---------------------------------------------------------------------------

_SEARCH_SQL_SOURCES = text(
    """
    SELECT s.title, s.url, s.byline, s.blurb, s.learning_type,
           d.slug AS directory, COALESCE(c.slug, '') AS category,
           GREATEST(
             similarity(s.title, :q),
             similarity(coalesce(s.blurb, ''), :q),
             CASE WHEN to_tsvector('english',
                    coalesce(s.title,'') || ' ' || coalesce(s.blurb,'')
                  ) @@ plainto_tsquery('english', :q) THEN 0.4 ELSE 0 END
           ) AS score
    FROM resource_sources s
    LEFT JOIN resource_directory_spine dsp ON dsp.source_id = s.id
    LEFT JOIN resource_directories d ON d.id = dsp.directory_id
    LEFT JOIN resource_tool_sources ts ON ts.source_id = s.id
    LEFT JOIN resource_tools t ON t.id = ts.tool_id
    LEFT JOIN resource_categories c ON c.id = t.category_id
    WHERE
      to_tsvector('english',
        coalesce(s.title,'') || ' ' || coalesce(s.blurb,'')
      ) @@ plainto_tsquery('english', :q)
      OR similarity(s.title, :q) > 0.15
      OR similarity(coalesce(s.blurb, ''), :q) > 0.15
    ORDER BY score DESC NULLS LAST, s.first_indexed_at DESC
    LIMIT :limit
    """
)

_SEARCH_SQL_TOOLS = text(
    """
    SELECT t.name AS title, t.url, '' AS byline, t.blurb,
           'Tool' AS learning_type, d.slug AS directory, c.slug AS category,
           GREATEST(
             similarity(t.name, :q),
             similarity(coalesce(t.blurb, ''), :q),
             CASE WHEN to_tsvector('english',
                    coalesce(t.name,'') || ' ' || coalesce(t.blurb,'')
                  ) @@ plainto_tsquery('english', :q) THEN 0.4 ELSE 0 END
           ) AS score
    FROM resource_tools t
    JOIN resource_categories c ON c.id = t.category_id
    JOIN resource_directories d ON d.id = c.directory_id
    WHERE
      to_tsvector('english',
        coalesce(t.name,'') || ' ' || coalesce(t.blurb,'')
      ) @@ plainto_tsquery('english', :q)
      OR similarity(t.name, :q) > 0.15
      OR similarity(coalesce(t.blurb, ''), :q) > 0.15
    ORDER BY score DESC NULLS LAST, t.name
    LIMIT :tool_limit
    """
)


_READ_SQL = text(
    """
    SELECT title, url, jina_content
    FROM resource_sources
    WHERE url = :url
    LIMIT 1
    """
)


async def read_curated(url: str, max_chars: int = 10_000) -> dict:
    """Return ``{title, url, content}`` for a catalog URL.

    Mirrors the production agent's `read_source`: try the cached Jina
    body in `resource_sources.jina_content` first; on miss, fall back
    to a live `jina_read` and write the result back to the cache so
    subsequent runs are fast. The harness needs real bodies to test
    research quality — a 90% cold-cache catalog otherwise corrupts
    every quality signal.
    """
    if not url or not url.strip():
        return {"error": "empty url"}
    settings = get_settings()
    engine = create_async_engine(
        convert_postgres_url_for_asyncpg(settings.effective_database_url),
        poolclass=NullPool,
    )
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SET search_path TO skrift, public"))
            row = (
                await conn.execute(_READ_SQL, {"url": url})
            ).mappings().first()
    finally:
        await engine.dispose()
    if row is None:
        return {"error": f"url not in catalog: {url}"}
    title = row["title"]
    body = (row["jina_content"] or "").strip()
    if not body:
        body = await _live_jina_fetch(url, max_chars)
        if not body:
            return {"error": f"jina fetch failed for {url}", "title": title}
        await _write_jina_cache(url, body)
    if len(body) > max_chars:
        body = body[: max_chars - 1] + "…"
    return {"title": title, "url": url, "content": body}


async def _live_jina_fetch(url: str, max_chars: int) -> str | None:
    """Pull `url` through Jina Reader. Returns plain body or None on
    error. Imports lazily so the harness's module-load path doesn't
    drag in httpx until we actually need it."""
    import httpx
    from smarter_dev.web.scan.tools import jina_read

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            result = await jina_read(client, url)
    except Exception as exc:  # noqa: BLE001
        logger.warning("jina_read raised for %s: %s", url, exc)
        return None
    if "error" in result:
        logger.warning("jina_read error for %s: %s", url, result["error"])
        return None
    title = result.get("title", "") or ""
    content = result.get("content", "") or ""
    body = f"{title}\n\n{content}".strip()
    return body[:max_chars] if body else None


async def _write_jina_cache(url: str, body: str) -> None:
    """Persist a freshly fetched Jina body to `resource_sources` so the
    next harness run hits the cache."""
    from datetime import datetime, timezone

    settings = get_settings()
    engine = create_async_engine(
        convert_postgres_url_for_asyncpg(settings.effective_database_url),
        poolclass=NullPool,
    )
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SET search_path TO skrift, public"))
            await conn.execute(
                text(
                    "UPDATE resource_sources "
                    "SET jina_content = :body, jina_fetched_at = :now "
                    "WHERE url = :url"
                ),
                {
                    "body": body,
                    "now": datetime.now(timezone.utc),
                    "url": url,
                },
            )
            await conn.commit()
    finally:
        await engine.dispose()


async def search_curated(query: str, limit: int = 8) -> list[dict]:
    if not query or not query.strip():
        return []
    limit = max(1, min(limit, 20))
    settings = get_settings()
    engine = create_async_engine(
        convert_postgres_url_for_asyncpg(settings.effective_database_url),
        poolclass=NullPool,
    )
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SET search_path TO skrift, public"))
            src_rows = (
                await conn.execute(
                    _SEARCH_SQL_SOURCES, {"q": query, "limit": limit}
                )
            ).mappings().all()
            tool_rows = (
                await conn.execute(
                    _SEARCH_SQL_TOOLS,
                    {"q": query, "tool_limit": max(2, limit // 2)},
                )
            ).mappings().all()
    finally:
        await engine.dispose()

    hits: list[dict] = []
    seen: set[str] = set()
    for row in src_rows:
        if row["url"] in seen:
            continue
        seen.add(row["url"])
        hits.append({
            "kind": "source", "title": row["title"], "url": row["url"],
            "byline": row["byline"] or "", "blurb": row["blurb"] or "",
            "learning_type": row["learning_type"],
            "directory": row["directory"], "category": row["category"] or "",
        })
    for row in tool_rows:
        if row["url"] in seen:
            continue
        seen.add(row["url"])
        hits.append({
            "kind": "tool", "title": row["title"], "url": row["url"],
            "byline": "", "blurb": row["blurb"] or "",
            "learning_type": "Tool",
            "directory": row["directory"], "category": row["category"] or "",
        })
    return hits[:limit]


# ---------------------------------------------------------------------------
# Typed schema — researcher returns a list of these, author consumes them
# ---------------------------------------------------------------------------


class Excerpt(BaseModel):
    """A cited passage from a curated document."""

    purpose: str = Field(
        ...,
        description=(
            "One short sentence stating why this citation matters for "
            "the user's question."
        ),
    )
    excerpt: str = Field(
        ...,
        description="Verbatim excerpt (1-4 sentences) from the document.",
    )
    document_id: str = Field(
        ...,
        description=(
            "Document ID copied verbatim from a `search_curated` hit. "
            "Opaque token — must come from this run's `search_curated` "
            "results; constructed or guessed IDs will be dropped."
        ),
    )


class FurtherReading(BaseModel):
    """A document worth pointing the reader at for a deeper dive after
    the answer, but not directly cited."""

    document_id: str = Field(
        ...,
        description=(
            "Document ID copied verbatim from a `search_curated` hit. "
            "Opaque token — must come from this run's `search_curated` "
            "results."
        ),
    )
    blurb: str = Field(
        ...,
        description=(
            "One sentence on why this is worth a deeper look in the "
            "context of the user's question."
        ),
    )


class Gap(BaseModel):
    """A concept the user's question implicates that the corpus didn't
    cover after at least two distinct search queries."""

    concept: str = Field(
        ...,
        description=(
            "The concept or sub-topic you couldn't find a relevant "
            "document for."
        ),
    )
    tried_queries: list[str] = Field(
        ...,
        description=(
            "The search queries you actually ran while looking for "
            "this concept (at least two distinct attempts)."
        ),
    )
    needed: str = Field(
        ...,
        description=(
            "One sentence describing what kind of source would fill "
            "this gap."
        ),
    )


class ResearchOutput(BaseModel):
    """Structured researcher payload.

    `excerpts` are the cited passages that directly answer the user's
    question — the author weaves these into the prose. `further_reading`
    is the related-but-not-essential pile. `gaps` records concepts the
    question implicates that the corpus didn't cover, so the author can
    note them honestly instead of inventing citations.
    """

    excerpts: list[Excerpt] = Field(default_factory=list)
    further_reading: list[FurtherReading] = Field(default_factory=list)
    gaps: list[Gap] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Researcher & author system prompts
# ---------------------------------------------------------------------------

_RESEARCHER_PROMPT = """\
You're a novice tasked with researching a topic using the Smarter Dev document corpus. Each document is represented by a document ID that you can use to get the full document content. These document IDs are opaque and carry no inherent meaning, so you must carefully copy the IDs provided by the search tool to read the documents you want; guessing only wastes time and can return errors.

You should look for documents you can cite. For each citation, you must have read the document, then provide a brief (single sentence) purpose for the citation, the verbatim excerpt (1-4 sentences), and the exact (copy carefully) document ID for reference. Each source can be cited multiple times. You'll want to shoot for 4-8 citations in total.

You'll also want to note down further reading that would likely add depth but wasn't directly within the scope of the topic. For each, carefully copy over the document ID and provide a blurb explaining why you think it would be relevant for further reading. Shoot for 2 to 5 uncited further reading entries.

If, after at least two distinct search queries for a particular concept, you can't find a relevant document in the corpus, report the gap in `gaps` (with the missing `concept`, the `tried_queries` you ran, and what kind of source would be `needed`) and move on. Do not keep searching endlessly, and do not invent citations to paper over a gap.

**[CRITICAL] DOCUMENT IDs NOT PRESENT IN SEARCH RESULTS WILL NOT OPEN**
All document IDs passed to `open_document` must have been present in the `search_curated` results or it will return an error. This is to prevent abuse."""

# The author runs the production resources-agent system prompt verbatim
# (imported above as `_PROD_AUTHOR_PROMPT`) so we're testing the same
# voice + response-shape + rich-block rules we ship. The harness author
# has no tools — instead the user turn carries the research findings as
# structured JSON.
_AUTHOR_PROMPT = _PROD_AUTHOR_PROMPT


# ---------------------------------------------------------------------------
# Gap-filler stage
#
# Runs only when the researcher reported `gaps`. For each gap, the
# gap-filler agent runs 2 web searches targeting authoritative/primary
# sources, then reads the single best source via `read_url`. Returns
# one citation per gap. Same tool-discipline pattern as the researcher:
# `read_url` only accepts URLs from a prior `web_search` in this run.
# ---------------------------------------------------------------------------


class GapCitation(BaseModel):
    """The single best citation the web searcher found for one gap."""

    gap_concept: str = Field(
        ...,
        description=(
            "The `concept` of the gap this citation fills — copy "
            "verbatim from the input gap."
        ),
    )
    source_title: str = Field(
        ...,
        description="Title of the cited source (verbatim from web_search).",
    )
    source_url: str = Field(
        ...,
        description=(
            "URL of the cited source. Must be one of the URLs returned "
            "by `web_search` in this run."
        ),
    )
    excerpt: str = Field(
        ...,
        description="1-4 sentence verbatim excerpt that addresses the gap.",
    )
    rationale: str = Field(
        ...,
        description=(
            "One sentence on why this is an authoritative source for "
            "the gap (e.g., 'official Postgres docs', 'canonical paper "
            "by X')."
        ),
    )


class GapFillerOutput(BaseModel):
    """One citation per input gap, drawn from the open web."""

    citations: list[GapCitation] = Field(default_factory=list)


_GAP_FILLER_PROMPT = """\
You are filling specific gaps in a curated document corpus. The user turn lists `gaps`, each describing a concept the corpus didn't cover. For each gap, your job is:

1. Run exactly 2 `web_search` queries targeting **primary or authoritative** sources for the gap's concept. Prefer official documentation (e.g., postgresql.org/docs, kubernetes.io/docs), RFCs, canonical academic papers, or domain-expert deep-dives. Avoid SEO blogspam, vendor marketing, and listicles.
2. Skim the search results and pick the **single highest-quality** URL that genuinely fills the gap.
3. Read that URL with `read_url`, then write one `GapCitation` for that gap. The `excerpt` must be a verbatim 1-4 sentence quote from the source you actually read.

Return exactly one `GapCitation` per input gap.

**[CRITICAL] URLs NOT PRESENT IN web_search RESULTS WILL NOT READ**
All URLs passed to `read_url` must have been returned by `web_search` in this run. URLs you didn't get from `web_search` will error. Do not guess URLs."""


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

RESEARCHER_CONFIGS = [
    # (model_id, thinking_level)
    ("gpt-5.4-nano", "medium"),
]

AUTHOR_CONFIGS: list[tuple[str, str]] = []

GAP_FILLER_CONFIGS = [
    # (model_id, thinking_level)
    ("gpt-5.4-nano", "medium"),
    ("gemini-3.1-flash-lite", "medium"),
    ("gemini-3-flash-preview", "low"),
]


def _label(model_id: str, thinking: str) -> str:
    return f"{model_id} · think={thinking}"


_researcher_label = _label


@dataclasses.dataclass
class ToolCall:
    tool: str           # "search_curated" or "read_source"
    args: dict          # {query, limit} or {url}
    result: str         # short summary, e.g. "8 hits" or "ok · 9876 chars"
    elapsed_s: float


@dataclasses.dataclass
class ResearcherResult:
    model: str
    thinking: str  # 'low' | 'high' | '' — reasoning/thinking level
    research: ResearchOutput | None
    tool_calls: list[ToolCall]
    input_tokens: int
    output_tokens: int
    elapsed_s: float
    error: str = ""
    # per-run map populated by `search_curated` results; used to resolve
    # document-id citations back to real URLs before the author runs.
    id_to_url: dict[str, str] = dataclasses.field(default_factory=dict)
    id_to_title: dict[str, str] = dataclasses.field(default_factory=dict)
    # how many excerpts + further-reading entries cited a document_id
    # that wasn't in `id_to_url` (i.e. the model invented an ID).
    unresolved_paths: int = 0

    @property
    def cost_usd(self) -> float:
        return cost_for(self.model, self.input_tokens, self.output_tokens)

    @property
    def label(self) -> str:
        return (
            _researcher_label(self.model, self.thinking)
            if self.thinking else self.model
        )

    @property
    def has_content(self) -> bool:
        return bool(self.research and (
            self.research.excerpts or self.research.further_reading
        ))


@dataclasses.dataclass
class AuthorResult:
    model: str
    thinking: str  # 'minimal'|'low'|'medium'|'high'|''
    output: str  # markdown — the final answer
    input_tokens: int
    output_tokens: int
    elapsed_s: float
    error: str = ""

    @property
    def cost_usd(self) -> float:
        return cost_for(self.model, self.input_tokens, self.output_tokens)

    @property
    def label(self) -> str:
        return _label(self.model, self.thinking) if self.thinking else self.model


@dataclasses.dataclass
class GapFillerResult:
    model: str
    thinking: str
    output: GapFillerOutput | None
    tool_calls: list[ToolCall]
    input_tokens: int
    output_tokens: int
    elapsed_s: float
    error: str = ""
    # per-run web_search URL set; used to gate read_url and to flag
    # citations that point at URLs the model didn't actually search for.
    seen_urls: set[str] = dataclasses.field(default_factory=set)
    unresolved_citations: int = 0

    @property
    def cost_usd(self) -> float:
        return cost_for(self.model, self.input_tokens, self.output_tokens)

    @property
    def label(self) -> str:
        return _label(self.model, self.thinking) if self.thinking else self.model


def _coerce_usage(usage) -> tuple[int, int]:
    if usage is None:
        return 0, 0
    if callable(usage):
        try:
            usage = usage()
        except Exception:  # noqa: BLE001
            return 0, 0
    return (
        int(getattr(usage, "input_tokens", 0) or 0),
        int(getattr(usage, "output_tokens", 0) or 0),
    )


def _model_settings(model_id: str, thinking: str):
    """Map a shared `thinking` knob to each provider's settings type.

    - Gemini: `google_thinking_config={'thinking_level':
      'MINIMAL'|'LOW'|'MEDIUM'|'HIGH'}`
    - OpenAI gpt-5 family: `openai_reasoning_effort` (Responses API)
    - Other OpenAI: chat-completions reasoning_effort
    """
    if model_id.startswith("gemini-"):
        from pydantic_ai.models.google import GoogleModelSettings
        return GoogleModelSettings(
            google_thinking_config={"thinking_level": thinking.upper()},
        )
    if model_id.startswith("gpt-5") or model_id.startswith("openai/gpt-5"):
        from pydantic_ai.models.openai import OpenAIResponsesModelSettings
        return OpenAIResponsesModelSettings(
            openai_reasoning_effort=thinking.lower(),
        )
    if model_id.startswith("gpt-") or model_id.startswith("openai/"):
        from pydantic_ai.models.openai import OpenAIChatModelSettings
        return OpenAIChatModelSettings(openai_reasoning_effort=thinking.lower())
    return None


_researcher_settings = _model_settings
_author_settings = _model_settings


async def _run_researcher(
    model_id: str, prompt: str, thinking: str
) -> ResearcherResult:
    agent = Agent(
        build_model(model_id),
        system_prompt=_RESEARCHER_PROMPT,
        output_type=ResearchOutput,
        model_settings=_researcher_settings(model_id, thinking),
    )
    tool_calls: list[ToolCall] = []
    # Per-run document-id ↔ url maps. Populated as `search_curated`
    # hits arrive, consulted by `open_document` and (post-run) by
    # id→url resolution before the author sees the research.
    id_to_url: dict[str, str] = {}
    url_to_id: dict[str, str] = {}
    id_to_title: dict[str, str] = {}

    def _hit_to_id(hit: dict) -> str:
        existing = url_to_id.get(hit["url"])
        if existing is not None:
            return existing
        doc_id = _new_document_id(set(id_to_url.keys()))
        id_to_url[doc_id] = hit["url"]
        url_to_id[hit["url"]] = doc_id
        id_to_title[doc_id] = hit.get("title", "") or ""
        return doc_id

    @agent.tool_plain
    async def search_curated_tool(query: str, limit: int = 8) -> list[dict]:
        """Search the Smarter Dev document corpus for entries relevant
        to the query. Each hit includes a `document_id` you can pass
        to `open_document` to read the full document."""
        t0 = time.monotonic()
        hits = await search_curated(query, limit=limit)
        rewritten = []
        for h in hits:
            rewritten.append({
                "kind": h["kind"], "title": h["title"],
                "document_id": _hit_to_id(h),
                "byline": h["byline"], "blurb": h["blurb"],
                "learning_type": h["learning_type"],
                "directory": h["directory"], "category": h["category"],
            })
        tool_calls.append(ToolCall(
            tool="search_curated",
            args={"query": query, "limit": limit},
            result=f"{len(rewritten)} hit{'s' if len(rewritten) != 1 else ''}",
            elapsed_s=time.monotonic() - t0,
        ))
        return rewritten

    @agent.tool_plain
    async def open_document(document_id: str) -> dict:
        """Open a document by `document_id` and return its title and
        full body (truncated to ~10k characters).

        **[CRITICAL] DOCUMENT IDs NOT PRESENT IN SEARCH RESULTS WILL NOT OPEN**
        All document IDs passed to `open_document` must have been
        present in the `search_curated` results or it will return an
        error. This is to prevent abuse."""
        t0 = time.monotonic()
        url = id_to_url.get(document_id)
        if url is None:
            result = {
                "error": (
                    f"unknown document_id: {document_id} — only IDs "
                    f"returned by `search_curated` in this run are "
                    f"valid. Run `search_curated` first."
                ),
            }
            summary = "error · unknown id"
        else:
            raw = await read_curated(url)
            if "error" in raw:
                result = {"error": raw["error"]}
                summary = f"error · {raw['error'][:60]}"
            else:
                result = {
                    "title": raw["title"], "document_id": document_id,
                    "content": raw["content"],
                }
                summary = f"ok · {len(raw.get('content',''))} chars"
        tool_calls.append(ToolCall(
            tool="open_document",
            args={"document_id": document_id},
            result=summary,
            elapsed_s=time.monotonic() - t0,
        ))
        return result

    t0 = time.monotonic()
    result = await agent.run(prompt)
    elapsed = time.monotonic() - t0
    research = getattr(result, "output", None)
    if not isinstance(research, ResearchOutput):
        research = ResearchOutput()
    inp, otok = _coerce_usage(getattr(result, "usage", None))
    unresolved = 0
    for ex in research.excerpts:
        if ex.document_id not in id_to_url:
            unresolved += 1
    for fr in research.further_reading:
        if fr.document_id not in id_to_url:
            unresolved += 1
    return ResearcherResult(
        model=model_id, thinking=thinking, research=research,
        tool_calls=tool_calls,
        input_tokens=inp, output_tokens=otok, elapsed_s=elapsed,
        id_to_url=id_to_url, id_to_title=id_to_title,
        unresolved_paths=unresolved,
    )


def _resolve_research_for_author(
    research: ResearchOutput,
    id_to_url: dict[str, str],
    id_to_title: dict[str, str],
) -> dict:
    """Hydrate the researcher's document-id citations with real title +
    URL drawn from the per-run search map.

    The researcher only returns IDs (+ purpose/excerpt/blurb). The
    author sees fully-resolved citations (title + URL) — mirroring the
    production agent's worldview. Excerpts/FR entries whose
    `document_id` can't be resolved (the model invented an ID) are
    silently dropped — they're already counted on
    `ResearcherResult.unresolved_paths` for the report.
    """
    excerpts: list[dict] = []
    for ex in research.excerpts:
        url = id_to_url.get(ex.document_id)
        if url is None:
            continue
        excerpts.append({
            "purpose": ex.purpose, "excerpt": ex.excerpt,
            "source_title": id_to_title.get(ex.document_id, ""),
            "source_url": url,
        })
    further_reading: list[dict] = []
    for fr in research.further_reading:
        url = id_to_url.get(fr.document_id)
        if url is None:
            continue
        further_reading.append({
            "title": id_to_title.get(fr.document_id, ""),
            "url": url, "blurb": fr.blurb,
        })
    return {"excerpts": excerpts, "further_reading": further_reading}


async def _run_author(
    model_id: str,
    prompt: str,
    research: ResearchOutput,
    id_to_url: dict[str, str],
    id_to_title: dict[str, str],
    thinking: str = "",
) -> AuthorResult:
    agent = Agent(
        build_model(model_id),
        system_prompt=_AUTHOR_PROMPT,
        model_settings=_author_settings(model_id, thinking) if thinking else None,
    )
    # Production resources_agent expects to gather sources itself via
    # `search_resources` / `read_source`. In the harness, the researcher
    # already did that step; we feed the typed payload back in as JSON
    # in the user turn so the author treats it as a verified catalog
    # snapshot. Document IDs the researcher cited are resolved back to
    # real URLs here, so the author writes prose against URLs just
    # like prod. No `level` hint — prod's prompt infers from the
    # question's phrasing.
    payload = _resolve_research_for_author(research, id_to_url, id_to_title)
    payload_json = json.dumps(payload, indent=2, ensure_ascii=False)
    user_turn = (
        "Pre-fetched research (cite using these URLs only — every URL "
        "is from the verified catalog; do not invent any):\n\n"
        "- `excerpts`: passages with citations that directly answer "
        "the question. `purpose` is why the citation matters; "
        "`excerpt` is the verbatim supporting text.\n"
        "- `further_reading`: related sources for the reader to dig "
        "into after the answer.\n\n"
        "```json\n"
        f"{payload_json}\n"
        "```\n\n"
        "User question:\n\n"
        f"{prompt}"
    )
    t0 = time.monotonic()
    result = await agent.run(user_turn)
    elapsed = time.monotonic() - t0
    output = str(getattr(result, "output", None) or "")
    inp, otok = _coerce_usage(getattr(result, "usage", None))
    return AuthorResult(
        model=model_id, thinking=thinking, output=output,
        input_tokens=inp, output_tokens=otok, elapsed_s=elapsed,
    )


# ---------------------------------------------------------------------------
# Gap-filler runner
# ---------------------------------------------------------------------------


async def _live_jina_search(query: str, num_results: int = 5) -> list[dict]:
    """Call Jina Search and return list of {title, url, description}."""
    import httpx
    from smarter_dev.web.scan.tools import jina_search

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            hits = await jina_search(client, query, num_results=num_results)
    except Exception as exc:  # noqa: BLE001
        logger.warning("jina_search raised for %s: %s", query, exc)
        return []
    return [
        {
            "title": h.get("title", "") or "",
            "url": h.get("url", "") or "",
            "description": h.get("description", "") or "",
        }
        for h in hits
        if "error" not in h and h.get("url")
    ]


async def _run_gap_filler(
    model_id: str,
    thinking: str,
    prompt: str,
    gaps: list[dict],
) -> GapFillerResult:
    agent = Agent(
        build_model(model_id),
        system_prompt=_GAP_FILLER_PROMPT,
        output_type=GapFillerOutput,
        model_settings=_model_settings(model_id, thinking),
    )
    tool_calls: list[ToolCall] = []
    seen_urls: set[str] = set()
    # Cache results by URL so read_url can return content without a
    # second Jina fetch when possible.
    url_to_body: dict[str, str] = {}

    @agent.tool_plain
    async def web_search(query: str) -> list[dict]:
        """Search the open web for authoritative/primary sources.
        Returns up to 5 hits, each with `title`, `url`, and
        `description`. To read a hit's full body, call `read_url`."""
        t0 = time.monotonic()
        hits = await _live_jina_search(query, num_results=5)
        for h in hits:
            if h["url"]:
                seen_urls.add(h["url"])
        tool_calls.append(ToolCall(
            tool="web_search",
            args={"query": query},
            result=f"{len(hits)} hit{'s' if len(hits) != 1 else ''}",
            elapsed_s=time.monotonic() - t0,
        ))
        return hits

    @agent.tool_plain
    async def read_url(url: str) -> dict:
        """Read the full body of a URL. The URL must have been returned
        by `web_search` in this run; passing any other URL will
        error."""
        t0 = time.monotonic()
        if url not in seen_urls:
            result = {
                "error": (
                    f"unknown url: {url} — only URLs returned by "
                    f"`web_search` in this run are valid. Run "
                    f"`web_search` first."
                ),
            }
            summary = "error · unknown url"
        else:
            body = url_to_body.get(url)
            if body is None:
                body = await _live_jina_fetch(url, max_chars=10_000)
                if body:
                    url_to_body[url] = body
            if not body:
                result = {"error": f"jina read failed for {url}"}
                summary = "error · read failed"
            else:
                result = {"url": url, "content": body}
                summary = f"ok · {len(body)} chars"
        tool_calls.append(ToolCall(
            tool="read_url",
            args={"url": url},
            result=summary,
            elapsed_s=time.monotonic() - t0,
        ))
        return result

    # The user turn presents the gaps as structured JSON so the agent
    # can iterate over them and produce one citation per gap.
    user_turn = (
        "Original user question:\n\n"
        f"{prompt}\n\n"
        "Curated-corpus gaps to fill (one citation each):\n\n"
        "```json\n"
        f"{json.dumps(gaps, indent=2, ensure_ascii=False)}\n"
        "```\n"
    )

    t0 = time.monotonic()
    try:
        result = await agent.run(user_turn)
    except Exception as exc:  # noqa: BLE001
        return GapFillerResult(
            model=model_id, thinking=thinking, output=None,
            tool_calls=tool_calls,
            input_tokens=0, output_tokens=0,
            elapsed_s=time.monotonic() - t0,
            error=str(exc), seen_urls=seen_urls,
        )
    elapsed = time.monotonic() - t0
    output = getattr(result, "output", None)
    if not isinstance(output, GapFillerOutput):
        output = GapFillerOutput()
    inp, otok = _coerce_usage(getattr(result, "usage", None))
    unresolved = sum(
        1 for c in output.citations if c.source_url not in seen_urls
    )
    return GapFillerResult(
        model=model_id, thinking=thinking, output=output,
        tool_calls=tool_calls,
        input_tokens=inp, output_tokens=otok, elapsed_s=elapsed,
        seen_urls=seen_urls, unresolved_citations=unresolved,
    )


@dataclasses.dataclass
class PromptReport:
    prompt: str
    researchers: list[ResearcherResult]
    authors: list[tuple[str, str, AuthorResult]]  # (researcher, author, result)
    gap_fillers: list[GapFillerResult] = dataclasses.field(
        default_factory=list,
    )

    def total_cost(self) -> float:
        return (
            sum(r.cost_usd for r in self.researchers)
            + sum(a.cost_usd for _, _, a in self.authors)
            + sum(g.cost_usd for g in self.gap_fillers)
        )

    def total_tokens(self) -> tuple[int, int]:
        inp = (
            sum(r.input_tokens for r in self.researchers)
            + sum(a.input_tokens for _, _, a in self.authors)
            + sum(g.input_tokens for g in self.gap_fillers)
        )
        out = (
            sum(r.output_tokens for r in self.researchers)
            + sum(a.output_tokens for _, _, a in self.authors)
            + sum(g.output_tokens for g in self.gap_fillers)
        )
        return inp, out


async def run_one_prompt(prompt: str) -> PromptReport:
    print(f"\n=== prompt: {prompt[:80]}...", file=sys.stderr)
    researchers: list[ResearcherResult] = []
    for model_id, thinking in RESEARCHER_CONFIGS:
        label = _researcher_label(model_id, thinking)
        print(f"  researcher: {label} …", file=sys.stderr, end="")
        try:
            r = await _run_researcher(model_id, prompt, thinking)
            researchers.append(r)
            n_ex = len(r.research.excerpts) if r.research else 0
            n_fr = len(r.research.further_reading) if r.research else 0
            unresolved = (
                f", {r.unresolved_paths} unresolved" if r.unresolved_paths else ""
            )
            print(
                f" ok ({r.elapsed_s:.1f}s, {n_ex} excerpts + {n_fr} fr"
                f"{unresolved}, "
                f"{r.input_tokens}+{r.output_tokens} tok, "
                f"${r.cost_usd:.4f})",
                file=sys.stderr,
            )
        except Exception as exc:  # noqa: BLE001
            print(f" FAIL: {exc}", file=sys.stderr)
            researchers.append(ResearcherResult(
                model=model_id, thinking=thinking,
                research=None, tool_calls=[],
                input_tokens=0, output_tokens=0, elapsed_s=0.0,
                error=str(exc),
            ))

    authors: list[tuple[str, str, AuthorResult]] = []
    for researcher in researchers:
        if researcher.error or not researcher.has_content:
            continue
        for author_model_id, author_thinking in AUTHOR_CONFIGS:
            author_label = _label(author_model_id, author_thinking)
            print(
                f"  author: {author_label} on {researcher.label} …",
                file=sys.stderr, end="",
            )
            try:
                a = await _run_author(
                    author_model_id, prompt, researcher.research,
                    researcher.id_to_url, researcher.id_to_title,
                    thinking=author_thinking,
                )
                authors.append((researcher.label, author_label, a))
                print(
                    f" ok ({a.elapsed_s:.1f}s, "
                    f"{a.input_tokens}+{a.output_tokens} tok, "
                    f"${a.cost_usd:.4f})",
                    file=sys.stderr,
                )
            except Exception as exc:  # noqa: BLE001
                print(f" FAIL: {exc}", file=sys.stderr)
                authors.append((researcher.label, author_label, AuthorResult(
                    model=author_model_id, thinking=author_thinking,
                    output="",
                    input_tokens=0, output_tokens=0, elapsed_s=0.0,
                    error=str(exc),
                )))
    return PromptReport(prompt=prompt, researchers=researchers, authors=authors)


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------

def _fmt_meta(c) -> str:
    """Works for both ResearcherResult and AuthorResult."""
    return (
        f"{c.input_tokens:,} in / {c.output_tokens:,} out tok · "
        f"${c.cost_usd:.4f} · {c.elapsed_s:.1f}s"
    )


def _tool_calls_to_html(calls: list[ToolCall]) -> str:
    if not calls:
        return ""
    rows: list[str] = []
    for c in calls:
        if c.tool == "search_curated":
            args = (
                f"<code>{_html_escape(c.args.get('query', ''))}</code>"
                f" · limit={c.args.get('limit', '')}"
            )
        elif c.tool == "web_search":
            args = f"<code>{_html_escape(c.args.get('query', ''))}</code>"
        elif c.tool == "read_url":
            url = c.args.get("url", "")
            args = (
                f"<a href='{_html_escape(url)}' target='_blank' rel='noopener'>"
                f"<code>{_html_escape(url)}</code></a>"
            )
        else:
            doc_id = c.args.get(
                "document_id", c.args.get("path", c.args.get("url", ""))
            )
            args = f"<code>{_html_escape(doc_id)}</code>"
        rows.append(
            "<tr>"
            f"<td><code>{_html_escape(c.tool)}</code></td>"
            f"<td>{args}</td>"
            f"<td>{_html_escape(c.result)}</td>"
            f"<td class='num'>{c.elapsed_s:.2f}s</td>"
            "</tr>"
        )
    return (
        "<h4 class='res-section'>Tool calls "
        f"<span class='res-section-count'>({len(calls)})</span></h4>"
        "<table class='tool-table'>"
        "<thead><tr><th>Tool</th><th>Args</th><th>Result</th><th class='num'>Δ</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _research_to_html(
    research: ResearchOutput | None,
    id_to_url: dict[str, str] | None = None,
    id_to_title: dict[str, str] | None = None,
) -> str:
    if research is None:
        return "<p class='empty'>(no research)</p>"
    i2u = id_to_url or {}
    i2t = id_to_title or {}
    parts: list[str] = []
    if research.excerpts:
        parts.append("<h4 class='res-section'>Excerpts</h4>")
        parts.append("<div class='excerpt-list'>")
        for ex in research.excerpts:
            url = i2u.get(ex.document_id, "")
            title = i2t.get(ex.document_id, "")
            if url:
                cite = (
                    f"<a href='{_html_escape(url)}' target='_blank' rel='noopener'>"
                    f"{_html_escape(title or ex.document_id)}</a>"
                    f" <code class='excerpt-path'>{_html_escape(ex.document_id)}</code>"
                )
            else:
                cite = (
                    f"<code class='excerpt-path excerpt-path-unresolved'>"
                    f"{_html_escape(ex.document_id)} (unresolved)</code>"
                )
            parts.append(
                "<div class='excerpt'>"
                f"<div class='excerpt-point'>{_html_escape(ex.purpose)}</div>"
                f"<div class='excerpt-text'>{_html_escape(ex.excerpt)}</div>"
                f"<div class='excerpt-cite'>— {cite}</div>"
                "</div>"
            )
        parts.append("</div>")
    if research.further_reading:
        parts.append("<h4 class='res-section'>Further reading</h4>")
        parts.append("<ul class='further-list'>")
        for fr in research.further_reading:
            url = i2u.get(fr.document_id, "")
            title = i2t.get(fr.document_id, "")
            if url:
                title_html = (
                    f"<a href='{_html_escape(url)}' target='_blank' rel='noopener'>"
                    f"{_html_escape(title or fr.document_id)}</a>"
                    f" <code class='excerpt-path'>{_html_escape(fr.document_id)}</code>"
                )
            else:
                title_html = (
                    f"<code class='excerpt-path excerpt-path-unresolved'>"
                    f"{_html_escape(fr.document_id)} (unresolved)</code>"
                )
            parts.append(
                "<li>"
                f"{title_html}"
                f" — <span class='further-blurb'>{_html_escape(fr.blurb)}</span>"
                "</li>"
            )
        parts.append("</ul>")
    if research.gaps:
        parts.append("<h4 class='res-section'>Gaps</h4>")
        parts.append("<ul class='gap-list'>")
        for g in research.gaps:
            queries = ", ".join(
                f"<code>{_html_escape(q)}</code>" for q in g.tried_queries
            )
            parts.append(
                "<li>"
                f"<div class='gap-concept'>{_html_escape(g.concept)}</div>"
                f"<div class='gap-needed'>{_html_escape(g.needed)}</div>"
                f"<div class='gap-queries'>tried: {queries}</div>"
                "</li>"
            )
        parts.append("</ul>")
    if not parts:
        return "<p class='empty'>(empty research)</p>"
    return "".join(parts)


def _gap_filler_citations_to_html(
    output: GapFillerOutput | None, seen_urls: set[str]
) -> str:
    if output is None or not output.citations:
        return "<p class='empty'>(no citations)</p>"
    parts: list[str] = ["<h4 class='res-section'>Citations</h4>",
                        "<div class='excerpt-list'>"]
    for c in output.citations:
        valid = c.source_url in seen_urls
        cite_link = (
            f"<a href='{_html_escape(c.source_url)}' target='_blank' rel='noopener'>"
            f"{_html_escape(c.source_title or c.source_url)}</a>"
        )
        if not valid:
            cite_link += (
                " <code class='excerpt-path excerpt-path-unresolved'>"
                "(unresolved url)</code>"
            )
        parts.append(
            "<div class='excerpt'>"
            f"<div class='excerpt-point'>{_html_escape(c.gap_concept)}</div>"
            f"<div class='excerpt-text'>{_html_escape(c.excerpt)}</div>"
            f"<div class='gap-needed'>{_html_escape(c.rationale)}</div>"
            f"<div class='excerpt-cite'>— {cite_link}</div>"
            "</div>"
        )
    parts.append("</div>")
    return "".join(parts)


def _md_to_html(md: str) -> str:
    """Render markdown via Skrift's renderer (the same one /ai/answer
    uses) so links + lists + code fences come through."""
    try:
        from skrift.lib.markdown import render_markdown
        return render_markdown(md or "")
    except Exception:  # noqa: BLE001
        # Fallback: <pre> the raw text.
        import html as _html
        return f"<pre>{_html.escape(md or '')}</pre>"


_HTML_TEMPLATE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Pipeline eval — {timestamp}</title>
<style>
  :root {{
    --bg: #0e1115;
    --fg: #d8e0ea;
    --muted: #8d97a6;
    --cyan: #4fb8ff;
    --green: #6ce6a0;
    --amber: #ffba6a;
    --border: rgba(255,255,255,.08);
    --card: #161a21;
    --code: #1e242d;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 24px;
    background: var(--bg); color: var(--fg);
    font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }}
  .wrap {{ max-width: 1100px; margin: 0 auto; }}
  h1 {{ margin: 0 0 .25rem; font-size: 1.5rem; }}
  h2 {{ font-size: 1.05rem; margin: 1.25rem 0 .5rem; }}
  h3 {{ font-size: .82rem; margin: 1rem 0 .35rem;
        text-transform: uppercase; letter-spacing: .12em;
        color: var(--muted); }}
  .sub {{ color: var(--muted); font-size: .85rem; margin-bottom: 1rem; }}
  table {{ width: 100%; border-collapse: collapse; margin: .5rem 0 1.25rem;
           font-size: .85rem; }}
  th, td {{ padding: .35rem .55rem; text-align: left;
            border-bottom: 1px solid var(--border); }}
  th {{ font-weight: 600; color: var(--muted); font-size: .72rem;
        text-transform: uppercase; letter-spacing: .12em; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  tr.total td {{ font-weight: 700; border-top: 2px solid var(--border); }}
  code {{ background: var(--code); padding: .1em .35em; border-radius: 3px;
          font-size: .9em; }}
  pre {{ background: var(--code); padding: .75rem .85rem; border-radius: 4px;
         overflow-x: auto; line-height: 1.45; font-size: .85rem; }}
  blockquote {{ border-left: 3px solid var(--cyan); margin: .5rem 0;
                padding: .25rem 0 .25rem .8rem;
                color: var(--muted); font-style: italic; }}
  a {{ color: var(--cyan); }}
  .prompt-card {{
    background: var(--card); border: 1px solid var(--border);
    border-radius: 6px; padding: 1rem 1.25rem; margin-bottom: 1rem;
  }}
  details {{ border: 1px solid var(--border); border-radius: 6px;
             margin: .35rem 0; background: var(--card); }}
  details > summary {{
    cursor: pointer; padding: .55rem .8rem;
    font: 600 .82rem/1 -apple-system, sans-serif;
    list-style: none;
    display: flex; gap: .5rem; align-items: center; flex-wrap: wrap;
  }}
  details > summary::-webkit-details-marker {{ display: none; }}
  details > summary::before {{
    content: "▸"; color: var(--muted); transition: transform .15s;
    font-size: .75rem;
  }}
  details[open] > summary::before {{ transform: rotate(90deg);
    display: inline-block; }}
  .pill {{
    display: inline-block; padding: .12rem .45rem;
    font: 600 .7rem/1.4 ui-monospace, "SF Mono", monospace;
    border-radius: 3px;
    background: rgba(79,184,255,.15); color: var(--cyan);
  }}
  .pill.author {{ background: rgba(108,230,160,.15); color: var(--green); }}
  .pill.cost {{ background: transparent; color: var(--muted);
                font-weight: 400; }}
  details > .body {{ padding: 0 .8rem .9rem;
                     border-top: 1px solid var(--border); }}
  details > .body > .md {{ padding-top: .8rem; }}
  .md p:first-child {{ margin-top: 0; }}
  .md p:last-child  {{ margin-bottom: 0; }}
  .md ul, .md ol {{ margin: .35rem 0 .6rem; padding-left: 1.4rem; }}
  .md li {{ margin: .15rem 0; }}
  .empty {{ color: var(--muted); font-style: italic; }}
  .res-section {{
    font-size: .68rem; letter-spacing: .14em; text-transform: uppercase;
    color: var(--muted); margin: .85rem 0 .4rem; font-weight: 600;
  }}
  .excerpt-list {{ display: flex; flex-direction: column; gap: .55rem; }}
  .excerpt {{
    padding: .55rem .75rem;
    background: rgba(79,184,255,.04);
    border-left: 2px solid rgba(79,184,255,.5);
    border-radius: 0 4px 4px 0;
  }}
  .excerpt-point {{
    font-weight: 600; color: var(--cyan); font-size: .82rem;
    margin-bottom: .25rem;
  }}
  .excerpt-text {{ font-size: .88rem; line-height: 1.5; margin: 0 0 .2rem; }}
  .excerpt-cite {{ font-size: .78rem; color: var(--muted); }}
  .excerpt-path {{ font-size: .72rem; color: var(--muted); opacity: .75; }}
  .excerpt-path-unresolved {{ color: #c54848; opacity: 1; }}
  ul.gap-list {{ list-style: none; padding: 0; margin: 0;
                 display: flex; flex-direction: column; gap: .35rem; }}
  ul.gap-list li {{ font-size: .82rem; line-height: 1.45;
                    border-left: 2px solid #c5a448; padding: .15rem .5rem;
                    background: rgba(197, 164, 72, 0.06); }}
  .gap-concept {{ font-weight: 600; color: #d8b85a; }}
  .gap-needed {{ color: var(--text); margin: .1rem 0; }}
  .gap-queries {{ color: var(--muted); font-size: .76rem; }}
  .gap-queries code {{ font-size: .8em; opacity: .85; }}
  ul.further-list {{
    list-style: none; padding: 0; margin: 0;
    display: flex; flex-direction: column; gap: .25rem;
  }}
  ul.further-list li {{ font-size: .85rem; line-height: 1.45; }}
  .further-blurb {{ color: var(--muted); }}
  table.tool-table {{ font-size: .8rem; margin: 0 0 .25rem; }}
  table.tool-table th {{ font-weight: 600; }}
  table.tool-table td {{ vertical-align: top; padding: .3rem .55rem; }}
  table.tool-table code {{ font-size: .8em; }}
  .res-section-count {{ color: var(--muted); font-weight: 400;
    text-transform: none; letter-spacing: 0; font-size: .68rem; }}
  .row-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: .5rem; }}
  @media (max-width: 800px) {{ .row-grid {{ grid-template-columns: 1fr; }} }}
</style>
</head>
<body>
<div class="wrap">
  <h1>Researcher × Author pipeline eval</h1>
  <p class="sub">Generated {timestamp} · {n_prompts} prompt(s) · {n_runs} model calls</p>

  <h2>Cost totals</h2>
  <table>
    <thead><tr>
      <th>Model</th><th class="num">Input tok</th>
      <th class="num">Output tok</th><th class="num">Cost (USD)</th>
    </tr></thead>
    <tbody>
{cost_rows}
      <tr class="total">
        <td>TOTAL</td>
        <td class="num">{total_in:,}</td>
        <td class="num">{total_out:,}</td>
        <td class="num">${grand_cost:.4f}</td>
      </tr>
    </tbody>
  </table>

{prompt_blocks}

</div>
</body>
</html>
"""


def _html_escape(s: str) -> str:
    import html as _html
    return _html.escape(s)


def render_html(reports: list[PromptReport]) -> str:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

    # Aggregate per-model totals.
    model_in: dict[str, int] = {}
    model_out: dict[str, int] = {}
    n_runs = 0
    for r in reports:
        for c in r.researchers:
            model_in[c.model] = model_in.get(c.model, 0) + c.input_tokens
            model_out[c.model] = model_out.get(c.model, 0) + c.output_tokens
            n_runs += 1
        for _, _, c in r.authors:
            model_in[c.model] = model_in.get(c.model, 0) + c.input_tokens
            model_out[c.model] = model_out.get(c.model, 0) + c.output_tokens
            n_runs += 1
        for c in r.gap_fillers:
            model_in[c.model] = model_in.get(c.model, 0) + c.input_tokens
            model_out[c.model] = model_out.get(c.model, 0) + c.output_tokens
            n_runs += 1

    grand_cost = 0.0
    total_in = 0
    total_out = 0
    cost_rows: list[str] = []
    for m in sorted(set(list(model_in.keys()) + list(model_out.keys()))):
        ci = model_in.get(m, 0)
        co = model_out.get(m, 0)
        cost = cost_for(m, ci, co)
        grand_cost += cost
        total_in += ci
        total_out += co
        cost_rows.append(
            f"      <tr><td><code>{_html_escape(m)}</code></td>"
            f"<td class='num'>{ci:,}</td><td class='num'>{co:,}</td>"
            f"<td class='num'>${cost:.4f}</td></tr>"
        )

    # Per-prompt blocks.
    blocks: list[str] = []
    for i, r in enumerate(reports, 1):
        researcher_html: list[str] = []
        for c in r.researchers:
            if c.error:
                body = (
                    f"<pre class='empty'>[error] {_html_escape(c.error)}</pre>"
                )
                meta_count = "error"
            else:
                body = (
                    _tool_calls_to_html(c.tool_calls)
                    + _research_to_html(c.research, c.id_to_url, c.id_to_title)
                )
                n_ex = len(c.research.excerpts) if c.research else 0
                n_fr = len(c.research.further_reading) if c.research else 0
                n_gaps = len(c.research.gaps) if c.research else 0
                n_tc = len(c.tool_calls)
                meta_count = f"{n_tc} calls · {n_ex} excerpts · {n_fr} further"
                if n_gaps:
                    meta_count += f" · {n_gaps} gaps"
                if c.unresolved_paths:
                    meta_count += f" · {c.unresolved_paths} unresolved"
            researcher_html.append(
                "<details open>"
                f"<summary><span class='pill'>{_html_escape(c.label)}</span> "
                f"<span class='pill cost'>{meta_count} · {_html_escape(_fmt_meta(c))}</span>"
                "</summary>"
                f"<div class='body'>{body}</div>"
                "</details>"
            )

        author_html: list[str] = []
        for researcher, author_model, a in r.authors:
            if a.error:
                body = (
                    f"<pre class='empty'>[error] {_html_escape(a.error)}</pre>"
                )
            else:
                body = f"<div class='md'>{_md_to_html(a.output)}</div>"
            author_html.append(
                "<details open>"
                "<summary>"
                f"<span class='pill author'>{_html_escape(author_model)}</span>"
                " ← research from "
                f"<span class='pill'>{_html_escape(researcher)}</span>"
                f"<span class='pill cost'>{_html_escape(_fmt_meta(a))}</span>"
                "</summary>"
                f"<div class='body'>{body}</div>"
                "</details>"
            )

        gap_filler_html: list[str] = []
        for gf in r.gap_fillers:
            if gf.error:
                body = (
                    f"<pre class='empty'>[error] {_html_escape(gf.error)}</pre>"
                )
                meta_count = "error"
            else:
                body = (
                    _tool_calls_to_html(gf.tool_calls)
                    + _gap_filler_citations_to_html(gf.output, gf.seen_urls)
                )
                n_cite = len(gf.output.citations) if gf.output else 0
                n_search = sum(
                    1 for t in gf.tool_calls if t.tool == "web_search"
                )
                n_read = sum(
                    1 for t in gf.tool_calls if t.tool == "read_url"
                )
                meta_count = (
                    f"{n_search} searches · {n_read} reads · "
                    f"{n_cite} citations"
                )
                if gf.unresolved_citations:
                    meta_count += f" · {gf.unresolved_citations} unresolved"
            gap_filler_html.append(
                "<details open>"
                f"<summary><span class='pill'>{_html_escape(gf.label)}</span> "
                f"<span class='pill cost'>{meta_count} · {_html_escape(_fmt_meta(gf))}</span>"
                "</summary>"
                f"<div class='body'>{body}</div>"
                "</details>"
            )

        per_in, per_out = r.total_tokens()
        subtitle_parts = []
        if r.researchers:
            subtitle_parts.append(
                f"{len(r.researchers)} researchers × {len(AUTHOR_CONFIGS)} authors"
            )
        if r.gap_fillers:
            subtitle_parts.append(f"{len(r.gap_fillers)} gap fillers")
        subtitle = " · ".join(subtitle_parts) or "—"
        blocks.append(
            "<div class='prompt-card'>"
            f"<h2>Prompt {i}</h2>"
            f"<blockquote>{_html_escape(r.prompt)}</blockquote>"
            f"<p class='sub'>${r.total_cost():.4f} · "
            f"{per_in:,} in / {per_out:,} out tok · "
            f"{subtitle}"
            "</p>"
            + (
                "<h3>Researcher outputs</h3>"
                + "".join(researcher_html)
                if researcher_html else ""
            )
            + (
                "<h3>Author outputs (per researcher source)</h3>"
                + "".join(author_html)
                if author_html else ""
            )
            + (
                "<h3>Gap-filler outputs</h3>"
                + "".join(gap_filler_html)
                if gap_filler_html else ""
            )
            + "</div>"
        )

    return _HTML_TEMPLATE.format(
        timestamp=timestamp,
        n_prompts=len(reports),
        n_runs=n_runs,
        cost_rows="\n".join(cost_rows),
        total_in=total_in,
        total_out=total_out,
        grand_cost=grand_cost,
        prompt_blocks="\n".join(blocks),
    )


# ---------------------------------------------------------------------------
# Prompt file parsing
# ---------------------------------------------------------------------------

def parse_prompts(text_blob: str) -> list[str]:
    """One prompt per line, OR sections separated by lines of `---`.
    Lines starting with `#` are skipped. Adjacent blank lines collapse."""
    blocks: list[str] = []
    current: list[str] = []
    for raw in text_blob.splitlines():
        line = raw.rstrip()
        if line.strip().startswith("#"):
            continue
        if line.strip() == "---":
            if current:
                blocks.append("\n".join(current).strip())
                current = []
            continue
        if not line.strip() and not current:
            continue
        current.append(line)
    if current:
        blocks.append("\n".join(current).strip())
    # If no `---` separators were used, treat each non-empty line as its own
    # prompt — the common case.
    if len(blocks) == 1 and "\n" in blocks[0]:
        lines = [l.strip() for l in blocks[0].splitlines() if l.strip()]
        if len(lines) > 1 and all(len(l) < 240 for l in lines):
            return lines
    return [b for b in blocks if b]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

async def run_gap_fillers_from_json(path: Path) -> list[PromptReport]:
    """Replay only the gap-filler stage against gaps recorded in a
    prior run's JSON sidecar. Skips researcher and author."""
    data = json.loads(path.read_text())
    reports: list[PromptReport] = []
    for entry in data:
        prompt = entry["prompt"]
        # Gather gaps across all researchers in this prompt entry,
        # de-duplicated by concept (the field is per-researcher in the
        # JSON, but for the gap-filler stage we treat them as a single
        # input pile per prompt).
        seen_concepts: set[str] = set()
        gaps: list[dict] = []
        for r in entry.get("researchers", []):
            research = r.get("research") or {}
            for g in research.get("gaps", []) or []:
                concept = g.get("concept", "")
                if concept and concept not in seen_concepts:
                    seen_concepts.add(concept)
                    gaps.append(g)
        if not gaps:
            print(
                f"\n=== prompt: {prompt[:80]}… (no gaps, skipping)",
                file=sys.stderr,
            )
            reports.append(PromptReport(prompt=prompt, researchers=[], authors=[]))
            continue

        print(
            f"\n=== prompt: {prompt[:80]}… ({len(gaps)} gap(s))",
            file=sys.stderr,
        )
        gap_fillers: list[GapFillerResult] = []
        for model_id, thinking in GAP_FILLER_CONFIGS:
            lbl = _label(model_id, thinking)
            print(f"  gap_filler: {lbl} …", file=sys.stderr, end="")
            try:
                gf = await _run_gap_filler(model_id, thinking, prompt, gaps)
                gap_fillers.append(gf)
                n_cite = len(gf.output.citations) if gf.output else 0
                n_search = sum(
                    1 for t in gf.tool_calls if t.tool == "web_search"
                )
                n_read = sum(
                    1 for t in gf.tool_calls if t.tool == "read_url"
                )
                n_err = sum(
                    1 for t in gf.tool_calls
                    if t.tool == "read_url" and "error" in t.result
                )
                unresolved = (
                    f", {gf.unresolved_citations} unresolved"
                    if gf.unresolved_citations else ""
                )
                print(
                    f" ok ({gf.elapsed_s:.1f}s, {n_search}s+{n_read}r "
                    f"({n_err} err), {n_cite} citations{unresolved}, "
                    f"{gf.input_tokens}+{gf.output_tokens} tok, "
                    f"${gf.cost_usd:.4f})",
                    file=sys.stderr,
                )
            except Exception as exc:  # noqa: BLE001
                print(f" FAIL: {exc}", file=sys.stderr)
                gap_fillers.append(GapFillerResult(
                    model=model_id, thinking=thinking,
                    output=None, tool_calls=[],
                    input_tokens=0, output_tokens=0, elapsed_s=0.0,
                    error=str(exc),
                ))

        reports.append(PromptReport(
            prompt=prompt, researchers=[], authors=[],
            gap_fillers=gap_fillers,
        ))
    return reports


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--prompts", type=Path, default=None,
        help="Path to a text file containing prompts (ignored in gap-filler-only mode).",
    )
    ap.add_argument(
        "--output", type=Path, required=True,
        help="Path to write the HTML report.",
    )
    ap.add_argument(
        "--json", type=Path, default=None,
        help="Optional path to also write a JSON sidecar with raw data.",
    )
    ap.add_argument(
        "--gap-filler-from", type=Path, default=None,
        dest="gap_filler_from",
        help=(
            "Skip researcher + author, and run only the gap-filler "
            "stage against the gaps recorded in the given prior-run "
            "JSON sidecar."
        ),
    )
    args = ap.parse_args()

    if args.gap_filler_from is not None:
        if not args.gap_filler_from.exists():
            print(
                f"--gap-filler-from path not found: {args.gap_filler_from}",
                file=sys.stderr,
            )
            return 1
        print(
            f"Replaying gap-filler stage from {args.gap_filler_from}…",
            file=sys.stderr,
        )
        reports = await run_gap_fillers_from_json(args.gap_filler_from)
    else:
        if args.prompts is None:
            print("--prompts is required unless --gap-filler-from is set.",
                  file=sys.stderr)
            return 1
        text_blob = args.prompts.read_text()
        prompts = parse_prompts(text_blob)
        if not prompts:
            print("No prompts found.", file=sys.stderr)
            return 1
        print(f"Running {len(prompts)} prompt(s)…", file=sys.stderr)

        reports = []
        for prompt in prompts:
            reports.append(await run_one_prompt(prompt))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_html(reports))
    print(f"Wrote {args.output}", file=sys.stderr)

    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        payload = []
        for r in reports:
            payload.append({
                "prompt": r.prompt,
                "researchers": [
                    {
                        "model": c.model,
                        "thinking": c.thinking,
                        "label": c.label,
                        "id_to_url": c.id_to_url,
                        "id_to_title": c.id_to_title,
                        "research": (
                            c.research.model_dump() if c.research else None
                        ),
                        "tool_calls": [
                            dataclasses.asdict(t) for t in c.tool_calls
                        ],
                        "input_tokens": c.input_tokens,
                        "output_tokens": c.output_tokens,
                        "elapsed_s": c.elapsed_s,
                        "cost_usd": c.cost_usd,
                        "error": c.error,
                    }
                    for c in r.researchers
                ],
                "authors": [
                    {
                        "researcher": rh, "author": ah,
                        "model": a.model, "thinking": a.thinking,
                        "output": a.output,
                        "input_tokens": a.input_tokens,
                        "output_tokens": a.output_tokens,
                        "elapsed_s": a.elapsed_s,
                        "cost_usd": a.cost_usd,
                        "error": a.error,
                    }
                    for rh, ah, a in r.authors
                ],
                "gap_fillers": [
                    {
                        "model": g.model,
                        "thinking": g.thinking,
                        "label": g.label,
                        "output": (
                            g.output.model_dump() if g.output else None
                        ),
                        "tool_calls": [
                            dataclasses.asdict(t) for t in g.tool_calls
                        ],
                        "seen_urls": sorted(g.seen_urls),
                        "unresolved_citations": g.unresolved_citations,
                        "input_tokens": g.input_tokens,
                        "output_tokens": g.output_tokens,
                        "elapsed_s": g.elapsed_s,
                        "cost_usd": g.cost_usd,
                        "error": g.error,
                    }
                    for g in r.gap_fillers
                ],
                "total_cost_usd": r.total_cost(),
            })
        args.json.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
        print(f"Wrote {args.json}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
