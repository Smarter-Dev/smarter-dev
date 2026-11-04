"""The Resource Agent — answers /resources questions using the curated DB.

Four-stage pipeline, presented to the user as a single agent:

0. **Reframer** (claude-haiku-5-5 · effort=medium) — interrogates the
   user's raw prompt (is it reasonable, is there a more normal question
   underneath, does it fit the shape of the catalog?) and emits a short
   user-visible message restating what they asked plus structured
   instructions that steer the rest of the pipeline:
   ``corpus_topics`` (researcher's search list), ``web_search_topics``
   (extra targets for the gap-filler), and ``reframing_instructions``
   (angle/emphasis notes for the author).
1. **Researcher** (claude-haiku-5-5 · effort=medium) — searches the curated
   catalog (``search_resources``) for the reframer's corpus topics,
   opens promising sources (``read_source``), and produces a typed
   ``ResearchOutput`` listing distilled excerpts, further-reading, and
   gaps the catalog didn't cover.
2. **Gap-filler** (claude-haiku-5-5 · effort=low) — runs when the
   researcher reported gaps OR the reframer asked for extra web topics.
   For each item, runs ``web_search`` over the open web and ``read_url``
   on the single best result. Returns one ``GapCitation`` per input.
3. **Author** (claude-haiku-5-5 · effort=low) — gets the merged
   research bundle (curated excerpts + web citations) plus the
   reframer's instructions and the original prompt, and writes the
   final markdown answer using the production system prompt.

Tool events from every stage flow into the same ``agent_tool_event``
notification stream so the user sees one continuous chip timeline
without any stage breaks. The reframer's user-visible message is
delivered via a single ``agent_reframe_ready`` notification that the
browser renders as a transient preface above the tool chips; it
collapses alongside the chip stream when ``agent_run_complete`` fires.

Skrift sessions
---------------
Each stage runs inline (``agents.default_subagent_dispatch: inline``) inside
the ``resources.agent.run`` worker job, and Skrift stores each stage's session
in its worker tables while it runs. The product reads only the
``agent_conversations`` / ``agent_messages`` tables, so once the pipeline ends,
however it ends, it deletes every stage's session
(:func:`~smarter_dev.web.agent_session_cleanup.forget_agent_sessions`).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import time
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import httpx
import skrift
from pydantic import BaseModel
from pydantic import Field
from pydantic_ai import RunContext
from skrift.agents.models import ResumeContext

# RunContext is imported at runtime (not under TYPE_CHECKING) because
# pydantic-ai resolves the @tool signatures (`ctx: RunContext[RunDeps]`) via
# get_type_hints when it materializes the agent on the worker — it must be a
# real name in this module's globals. This pulls in pydantic-ai *core* only;
# the heavy provider SDKs (google.genai / openai) still load lazily on the
# worker when the string model ids below materialize, never on the web tier.
from skrift.notifications import notify_user

from smarter_dev.shared.model_catalog import ReasoningLevel
from smarter_dev.shared.model_catalog import catalog_model_for_id
from smarter_dev.shared.skrift_models import agent_model_name
from smarter_dev.shared.skrift_models import agent_model_settings
from smarter_dev.web.agent_session_cleanup import forget_agent_sessions
from smarter_dev.web.agent_session_cleanup import session_billing
from smarter_dev.web.research_tools import brave_search
from smarter_dev.web.resources_corpus import read_corpus
from smarter_dev.web.resources_corpus import search_corpus

logger = logging.getLogger(__name__)

# Claude Haiku 5.5 since 2026-10-17 (#199), replacing GPT-6 Luna (which had
# replaced the Gemini 3 Flash preview on 2026-09-24). Catalog wire ids; any
# other Claude id works too (shared.skrift_models).
REFRAMER_MODEL = os.getenv("RESOURCE_REFRAMER_MODEL", "claude-haiku-5-5")
RESEARCHER_MODEL = os.getenv("RESOURCE_RESEARCHER_MODEL", "claude-haiku-5-5")
GAP_FILLER_MODEL = os.getenv("RESOURCE_GAP_FILLER_MODEL", "claude-haiku-5-5")
AUTHOR_MODEL = os.getenv("RESOURCE_AUTHOR_MODEL", "claude-haiku-5-5")

# Per-stage Agent names — four separate Skrift agents (one per stage),
# all transparent to the end user via the shared tool-event stream.
REFRAMER_AGENT_NAME = "smarter.dev.resources.reframer"
RESEARCHER_AGENT_NAME = "smarter.dev.resources.researcher"
GAP_FILLER_AGENT_NAME = "smarter.dev.resources.gap_filler"
AUTHOR_AGENT_NAME = "smarter.dev.resources.author"

# Per-stage timeouts. Researcher and gap-filler can be slow because of
# cold Jina reads; reframer and author are short.
_REFRAMER_TIMEOUT_S = float(os.getenv("RESOURCE_REFRAMER_TIMEOUT_S", "60"))
_RESEARCHER_TIMEOUT_S = float(os.getenv("RESOURCE_RESEARCHER_TIMEOUT_S", "180"))
_GAP_FILLER_TIMEOUT_S = float(os.getenv("RESOURCE_GAP_FILLER_TIMEOUT_S", "180"))
_AUTHOR_TIMEOUT_S = float(os.getenv("RESOURCE_AUTHOR_TIMEOUT_S", "60"))


_SYSTEM_PROMPT = (
    Path(__file__).parent / "prompts" / "resources_agent_system_prompt.md"
).read_text(encoding="utf-8")


_REFRAMER_PROMPT = (
    Path(__file__).parent / "prompts" / "resources_agent_reframer_prompt.md"
).read_text(encoding="utf-8")


_RESEARCHER_PROMPT = (
    Path(__file__).parent / "prompts" / "resources_agent_researcher_prompt.md"
).read_text(encoding="utf-8")


_GAP_FILLER_PROMPT = (
    Path(__file__).parent / "prompts" / "resources_agent_gap_filler_prompt.md"
).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Typed schemas (reframer + researcher + gap-filler outputs)
# ---------------------------------------------------------------------------


class ReframerOutput(BaseModel):
    """Planning-step output that steers the rest of the pipeline."""

    restated_question: str = Field(
        ...,
        description=(
            "1 to 3 short sentences addressed to the user, second person, "
            "restating what they're asking (or what they're really asking, "
            "if pivoted) and how the agent will frame the research and "
            "response. Conversational, no headers, no lists."
        ),
    )
    reframing_instructions: str = Field(
        ...,
        description=(
            "Terse author-facing notes on angle, emphasis, and what to "
            "skip. Not a draft answer."
        ),
    )
    corpus_topics: list[str] = Field(
        default_factory=list,
        description=(
            "2 to 6 short search-query-shaped strings the researcher will "
            "run through `search_resources`."
        ),
    )
    web_search_topics: list[str] = Field(
        default_factory=list,
        description=(
            "0 to 4 short topic strings the gap-filler should web-search "
            "regardless of catalog coverage."
        ),
    )


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
    source_url: str = Field(
        ...,
        description=(
            "URL copied verbatim from a `search_resources` hit you "
            "opened. URLs you didn't see in `search_resources` results "
            "this run will be dropped."
        ),
    )


class FurtherReading(BaseModel):
    """A document worth pointing the reader at for a deeper dive after
    the answer, but not directly cited."""

    source_url: str = Field(
        ...,
        description=(
            "URL copied verbatim from a `search_resources` hit. Must "
            "come from this run's `search_resources` results."
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
            "The concept or sub-topic you couldn't find a relevant document for."
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
            "One sentence describing what kind of source would fill this gap."
        ),
    )


class ResearchOutput(BaseModel):
    """Structured researcher payload — distilled excerpts the author
    weaves into prose, plus further-reading and any gaps in the
    catalog."""

    excerpts: list[Excerpt] = Field(default_factory=list)
    further_reading: list[FurtherReading] = Field(default_factory=list)
    gaps: list[Gap] = Field(default_factory=list)


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


# A list of dicts (one per tool hit) accumulated during a single run.
# Kept for legacy callers; new pipeline does not depend on it for
# citations (citations are surfaced via the structured Pydantic
# outputs).
_HITS: ContextVar[list[dict] | None] = ContextVar("resource_agent_hits", default=None)


def begin_run() -> list[dict]:
    """Reset the hit log for a new agent invocation. Returns the list to drain later."""
    bucket: list[dict] = []
    _HITS.set(bucket)
    return bucket


def _record_hit(hit: dict) -> None:
    bucket = _HITS.get()
    if bucket is not None:
        bucket.append(hit)


# ---------------------------------------------------------------------------
# Model factories
# ---------------------------------------------------------------------------


# Models are pydantic-ai model-name STRINGS (not Model objects), so this module
# imports no pydantic-ai. Skrift materializes the real model lazily in the
# worker; shared.skrift_models turns a catalog id into the name and plain-dict
# settings. The reframer and the author answer in one request, so they skip
# the message cache breakpoint; the researcher and gap-filler loop over tools,
# where it pays.
def _model_name(model_id: str) -> str:
    return agent_model_name(catalog_model_for_id(model_id))


def _settings(model_id: str, level: ReasoningLevel, *, cache_messages: bool) -> dict:
    return agent_model_settings(
        catalog_model_for_id(model_id), level, cache_messages=cache_messages
    )


def _reframer_model_settings() -> dict:
    return _settings(REFRAMER_MODEL, ReasoningLevel.MEDIUM, cache_messages=False)


def _researcher_model_settings() -> dict:
    return _settings(RESEARCHER_MODEL, ReasoningLevel.MEDIUM, cache_messages=True)


def _gap_filler_model_settings() -> dict:
    return _settings(GAP_FILLER_MODEL, ReasoningLevel.LOW, cache_messages=True)


def _author_model_settings() -> dict:
    return _settings(AUTHOR_MODEL, ReasoningLevel.LOW, cache_messages=False)


@dataclass
class RunDeps:
    """Per-run dependencies injected into tool calls via ``RunContext.deps``.

    Carries the identifiers needed to push real-time ``agent_tool_event``
    notifications back to the asker's browser. ``seen_urls`` is the
    gap-filler's per-run allowlist: `web_search` populates it, `read_url`
    enforces it. The researcher and author tools don't touch it.
    """

    conversation_id: str
    owner_user_id: str
    seen_urls: set[str] = dataclasses.field(default_factory=set)


def _build_deps(ctx: ResumeContext) -> RunDeps:
    return RunDeps(
        conversation_id=str(ctx.deps_ref.get("conversation_id", "")),
        owner_user_id=str(ctx.deps_ref.get("owner_user_id", "")),
    )


# ---------------------------------------------------------------------------
# Skrift agents — one per pipeline stage
# ---------------------------------------------------------------------------


reframer_agent = skrift.Agent(
    _model_name(REFRAMER_MODEL),
    name=REFRAMER_AGENT_NAME,
    system_prompt=_REFRAMER_PROMPT,
    output_type=ReframerOutput,
    model_settings=_reframer_model_settings(),
    deps_type=RunDeps,
    deps_factory=_build_deps,
)

researcher_agent = skrift.Agent(
    _model_name(RESEARCHER_MODEL),
    name=RESEARCHER_AGENT_NAME,
    system_prompt=_RESEARCHER_PROMPT,
    output_type=ResearchOutput,
    model_settings=_researcher_model_settings(),
    deps_type=RunDeps,
    deps_factory=_build_deps,
)

gap_filler_agent = skrift.Agent(
    _model_name(GAP_FILLER_MODEL),
    name=GAP_FILLER_AGENT_NAME,
    system_prompt=_GAP_FILLER_PROMPT,
    output_type=GapFillerOutput,
    model_settings=_gap_filler_model_settings(),
    deps_type=RunDeps,
    deps_factory=_build_deps,
)

author_agent = skrift.Agent(
    _model_name(AUTHOR_MODEL),
    name=AUTHOR_AGENT_NAME,
    system_prompt=_SYSTEM_PROMPT,
    model_settings=_author_model_settings(),
    deps_type=RunDeps,
    deps_factory=_build_deps,
)


async def _emit_tool_event(
    deps: RunDeps | None,
    *,
    tool: str,
    label: str,
    summary: str,
) -> None:
    """Push an `agent_tool_event` to the asker's open answer page.

    Silently no-ops when no owner is set (e.g. tests or replay paths). Any
    notification failure is logged but never propagated — tool callers must
    not see clipboard/SSE issues.
    """
    if not deps or not deps.owner_user_id:
        return
    try:
        await notify_user(
            deps.owner_user_id,
            "agent_tool_event",
            conversation_id=deps.conversation_id,
            tool=tool,
            label=label,
            summary=summary,
        )
    except Exception:  # noqa: BLE001
        logger.exception("agent_tool_event notify_user failed")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@researcher_agent.tool
async def search_resources(
    ctx: RunContext[RunDeps], query: str, limit: int = 8
) -> list[dict]:
    """Search the curated resource catalog.

    Use this first. Returns a ranked list of candidate sources and tools,
    each with a title/byline/blurb you can read before deciding which (if any)
    to fetch via ``read_source``. Aim for 1–3 well-chosen queries per run.

    Args:
        query: A short phrase or keyword. e.g. "postgres replication",
            "feature flags", "incident response runbooks".
        limit: Max number of hits to return. Default 8, cap 20.

    Returns:
        A list of dicts: ``[{kind, title, url, byline, blurb, learning_type,
        directory, category}]``. ``kind`` is "source" for curated articles
        and "tool" for indexed tools.
    """
    if not query or not query.strip():
        return []
    hits = await search_corpus(query, limit)
    for hit in hits:
        _record_hit({"source": "search", **hit})
        if hit.get("url"):
            ctx.deps.seen_urls.add(hit["url"])

    await _emit_tool_event(
        ctx.deps,
        tool="search_resources",
        label=query,
        summary=f"{len(hits)} hit" + ("s" if len(hits) != 1 else ""),
    )
    return hits


@researcher_agent.tool
async def read_source(ctx: RunContext[RunDeps], url: str) -> str:
    """Read the full text of a curated resource.

    Reads come from a tiered cache:

    1. ``resource_sources.jina_content`` — durable Postgres cache. Set by the
       precrawl (``scripts/warm_jina_cache.py``) and refreshed on a 30-day
       TTL. The agent should reach for ``read_source`` freely because curated
       URLs are almost always already hot here.
    2. In-process LRU (60 min) — covers any non-curated URL the agent might
       fetch (rare given the prompt's "only curated URLs" rule).
    3. Live Jina Reader — fallback on miss; writes back into whichever cache
       layer applies before returning.

    Args:
        url: The exact ``url`` field from a ``search_resources`` hit.

    Returns:
        Plain-text body (title + content concatenated), truncated to 10k chars.
        On failure, returns a short error string starting with ``"[error]"``.
    """
    if not url:
        return "[error] no URL provided"

    if url not in ctx.deps.seen_urls:
        # Enforce the per-run allowlist: every URL the model passes to
        # `read_source` must have been returned by `search_resources`
        # in this run. Stops URL fabrication cold.
        await _emit_tool_event(
            ctx.deps,
            tool="read_source",
            label=url,
            summary="[error] URL not in search results",
        )
        return (
            "[error] URL not in search_resources results from this run. "
            "Run `search_resources` first and use a URL from the results."
        )

    read = await read_corpus(url)
    if read.error is not None:
        # Emit a failing tool event so the UI can show the error
        # alongside the chip rather than swallowing silently.
        await _emit_tool_event(
            ctx.deps,
            tool="read_source",
            label=url,
            summary=f"[error] {read.error}",
        )
        return f"[error] {read.error}"
    body = read.body
    _record_hit({"source": "read", "url": url})

    label = body.split("\n", 1)[0].strip() if body else url
    if len(label) > 80:
        label = label[:79].rstrip() + "…"
    await _emit_tool_event(
        ctx.deps,
        tool="read_source",
        label=label or url,
        summary=url,
    )
    return body


# ---------------------------------------------------------------------------
# Gap-filler tools (open-web search + read, with per-run URL allowlist)
# ---------------------------------------------------------------------------


@gap_filler_agent.tool
async def web_search(ctx: RunContext[RunDeps], query: str) -> list[dict]:
    """Search the open web for authoritative/primary sources.

    Returns up to 5 English-language hits, each with `title`, `url`,
    and `description`. To read a hit's full body, call `read_url` with
    the URL.
    """
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            raw = await brave_search(
                client,
                query,
                num_results=5,
                country="US",
                search_lang="en",
                ui_lang="en-US",
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("brave_search raised for %s: %s", query, exc)
        await _emit_tool_event(
            ctx.deps,
            tool="web_search",
            label=query,
            summary=f"[error] {exc}",
        )
        return []

    hits: list[dict] = []
    for h in raw:
        if "error" in h or not h.get("url"):
            continue
        hits.append(
            {
                "title": h.get("title", "") or "",
                "url": h["url"],
                "description": h.get("description", "") or "",
            }
        )
        ctx.deps.seen_urls.add(h["url"])

    await _emit_tool_event(
        ctx.deps,
        tool="web_search",
        label=query,
        summary=f"{len(hits)} hit" + ("s" if len(hits) != 1 else ""),
    )
    logger.debug(
        "web_search %r → %d hits in %.2fs", query, len(hits), time.monotonic() - t0
    )
    return hits


@gap_filler_agent.tool
async def read_url(ctx: RunContext[RunDeps], url: str) -> str:
    """Read the full body of a URL.

    The URL must have been returned by `web_search` in this run;
    URLs you didn't get from `web_search` will error. Returns the
    plain-text body (truncated to ~10k chars) or an ``"[error] …"``
    string on failure.
    """
    if not url:
        return "[error] no URL provided"

    if url not in ctx.deps.seen_urls:
        await _emit_tool_event(
            ctx.deps,
            tool="read_url",
            label=url,
            summary="[error] URL not in web_search results",
        )
        return (
            "[error] URL not in web_search results from this run. "
            "Run `web_search` first and use a URL from the results."
        )

    # The same tiered cache the researcher uses: web URLs aren't curated
    # rows, so they land in the in-process layer.
    read = await read_corpus(url)
    if read.error is not None:
        await _emit_tool_event(
            ctx.deps,
            tool="read_url",
            label=url,
            summary=f"[error] {read.error}",
        )
        return f"[error] {read.error}"
    body = read.body

    label = body.split("\n", 1)[0].strip() if body else url
    if len(label) > 80:
        label = label[:79].rstrip() + "…"
    await _emit_tool_event(
        ctx.deps,
        tool="read_url",
        label=label or url,
        summary=url,
    )
    return body


# ---------------------------------------------------------------------------
# Pipeline orchestrator
# ---------------------------------------------------------------------------


def _build_reframer_user_turn(question: str) -> str:
    """Format the user turn for the reframer.

    Prepends the current UTC date so the reframer can reason about
    whether the user is asking about something the curated corpus would
    already cover or about something genuinely newer than the catalog.
    """
    today = datetime.now(UTC).date().isoformat()
    return f"Current UTC date: {today}\n\nUser question:\n\n{question}"


def _build_researcher_user_turn(
    question: str,
    reframing_instructions: str,
    corpus_topics: list[str],
) -> str:
    """Format the user turn the researcher sees — original question +
    reframer's framing + the explicit corpus topic list."""
    if corpus_topics:
        topic_lines = "\n".join(f"- {t}" for t in corpus_topics)
    else:
        topic_lines = "- (none — search using the question itself)"
    return (
        "Original user question:\n\n"
        f"{question}\n\n"
        "Framing from the planning step (use this to interpret intent, "
        "not as the literal search query):\n\n"
        f"{reframing_instructions}\n\n"
        "Search the curated catalog for these topics — these are the "
        "queries to actually run through `search_resources` (you can "
        "rephrase, but the topic list is the focus):\n\n"
        f"{topic_lines}"
    )


def _format_gap_payload(
    question: str,
    gaps: list[Gap],
    extra_web_topics: list[str],
) -> str:
    """Format the researcher's gaps + the reframer's extra web topics
    into a user turn for the gap-filler."""
    parts: list[str] = [
        "Original user question:\n\n",
        f"{question}\n\n",
    ]
    if gaps:
        parts.append("Curated-corpus gaps to fill (one citation each):\n\n```json\n")
        parts.append(
            json.dumps([g.model_dump() for g in gaps], indent=2, ensure_ascii=False)
        )
        parts.append("\n```\n\n")
    if extra_web_topics:
        parts.append(
            "Additional topics the planning step asked you to web-search "
            "(one citation each — use the topic string as `gap_concept`):\n\n"
        )
        for t in extra_web_topics:
            parts.append(f"- {t}\n")
    return "".join(parts)


def _build_author_payload(
    research: ResearchOutput,
    web_citations: list[GapCitation],
    reframing_instructions: str,
) -> dict:
    """Merge curated excerpts + web citations into the author payload.

    The author sees a unified shape: every excerpt has a `purpose`,
    `excerpt`, `source_url`. Web citations become additional excerpts
    keyed by their gap concept. Further-reading flows through unchanged.
    The reframer's instructions ride alongside so the author can adopt
    the planned angle.
    """
    excerpts: list[dict] = []
    for ex in research.excerpts:
        excerpts.append(
            {
                "purpose": ex.purpose,
                "excerpt": ex.excerpt,
                "source_url": ex.source_url,
            }
        )
    for c in web_citations:
        excerpts.append(
            {
                "purpose": c.gap_concept,
                "excerpt": c.excerpt,
                "source_url": c.source_url,
                "source_title": c.source_title,
            }
        )
    further_reading: list[dict] = [
        {"source_url": fr.source_url, "blurb": fr.blurb}
        for fr in research.further_reading
    ]
    return {
        "reframing_instructions": reframing_instructions,
        "excerpts": excerpts,
        "further_reading": further_reading,
    }


def _build_author_user_turn(question: str, payload: dict) -> str:
    return (
        "Pre-fetched research and a planning note (cite using these URLs "
        "only — every URL is from the verified catalog or a vetted web "
        "source; do not invent any):\n\n"
        "- `reframing_instructions`: how the planning step wants you to "
        "frame the response. Treat this as load-bearing — adopt the "
        "angle and emphasis it specifies.\n"
        "- `excerpts`: passages with citations that directly answer "
        "the question. `purpose` is why the citation matters; "
        "`excerpt` is the verbatim supporting text.\n"
        "- `further_reading`: related sources for the reader to dig "
        "into after the answer.\n\n"
        "```json\n"
        f"{json.dumps(payload, indent=2, ensure_ascii=False)}\n"
        "```\n\n"
        "User question:\n\n"
        f"{question}"
    )


async def _await_text_result(session) -> str:
    """Extract the author's markdown answer from a Skrift session."""
    result = await session.result()
    if isinstance(result, str):
        return result
    return getattr(result, "output", None) or str(result)


async def _await_typed_result(session, model_cls):
    """Extract a typed Pydantic result, defensively handling the
    deferred-tool-request union Skrift wraps `output_type` with."""
    result = await session.result()
    if isinstance(result, model_cls):
        return result
    # Fallback paths if Skrift returns the raw pydantic-ai run-result
    inner = getattr(result, "output", None)
    if isinstance(inner, model_cls):
        return inner
    raise RuntimeError(
        f"Expected {model_cls.__name__} from agent, got {type(result).__name__}"
    )


async def _capture_stage_usage(
    session, stage: str, sink: list[dict] | None, callback=None
) -> None:
    if sink is None:
        return
    state = await session.state()
    totals = state.usage_totals
    records = list(state.turn_usage.values())
    latest = records[-1] if records else None
    # Priced here, from the session's own responses, while it still has them:
    # a long-context tier is decided per request, not on the stage's total.
    # A failed run's responses, billed but outside Skrift's totals, count too.
    billed = session_billing(state)
    if billed is not None:
        totals = billed.tokens
    record = {
        "stage": stage,
        "model_name": (
            latest.configured_model
            if latest
            else (billed.model_name if billed is not None else None)
        ),
        "provider_name": latest.provider_name if latest else None,
        "input_tokens": totals.input_tokens,
        "output_tokens": totals.output_tokens,
        "cache_read_tokens": totals.cache_read_tokens,
        "cache_write_tokens": totals.cache_write_tokens,
    }
    if billed is not None:
        record["cost_usd"] = str(billed.cost)
    sink.append(record)
    if callback is not None:
        await callback(record, len(sink) - 1)


async def run_resources_pipeline(
    question: str,
    *,
    message_history: list | None,
    actor: str,
    conversation_id: str,
    owner_user_id: str,
    usage_records: list[dict] | None = None,
    usage_callback=None,
) -> str:
    """Run the four-stage pipeline (reframer → researcher → gap-filler
    → author) as a single user-visible agent run. Tool events from
    every stage are emitted into the same `agent_tool_event` stream so
    the user sees one continuous chip timeline. The reframer also
    pushes its user-visible `restated_question` over a one-shot
    `agent_reframe_ready` notification — the browser renders that as a
    transient preface above the chip stream. Returns the author's
    markdown.

    Behavior on failure:
    - Reframer failure: re-raised so the caller fires `agent_run_error`.
      The rest of the pipeline depends on its structured output, so
      there's no useful degradation path.
    - Researcher failure: re-raised so the caller fires `agent_run_error`.
    - Gap-filler failure: logged + a degraded `tool="gap_filler"` chip
      is emitted; pipeline continues to the author with curated-only
      excerpts.
    - Author failure: re-raised so the caller fires `agent_run_error`.
    """
    session_ids: list[str] = []
    try:
        return await _run_stages(
            question,
            message_history=message_history,
            actor=actor,
            conversation_id=conversation_id,
            owner_user_id=owner_user_id,
            usage_records=usage_records,
            usage_callback=usage_callback,
            session_ids=session_ids,
        )
    finally:
        # The stages' sessions hold the question, the earlier messages, the
        # research and the answer. Everything the caller needs from them is
        # in hand, and a retry starts new ones.
        await forget_agent_sessions(session_ids)


def _new_session_id(session_ids: list[str]) -> str:
    """A session id for the next stage, recorded so the pipeline can delete it."""
    session_ids.append(uuid4().hex)
    return session_ids[-1]


async def _run_stages(
    question: str,
    *,
    message_history: list | None,
    actor: str,
    conversation_id: str,
    owner_user_id: str,
    usage_records: list[dict] | None,
    usage_callback,
    session_ids: list[str],
) -> str:
    deps_ref = {
        "conversation_id": conversation_id,
        "owner_user_id": owner_user_id,
    }
    # Minimal deps copy the orchestrator uses to emit its own degraded
    # status chips when a stage misbehaves.
    orchestrator_deps = RunDeps(
        conversation_id=conversation_id,
        owner_user_id=owner_user_id,
    )

    # ── Stage 0: Reframer ────────────────────────────────────────────────
    reframer_session = await asyncio.wait_for(
        reframer_agent.run(
            _build_reframer_user_turn(question),
            message_history=message_history,
            actor=actor,
            session_id=_new_session_id(session_ids),
            deps_ref=deps_ref,
        ),
        timeout=_REFRAMER_TIMEOUT_S,
    )
    try:
        reframe = await _await_typed_result(reframer_session, ReframerOutput)
    finally:
        await _capture_stage_usage(
            reframer_session, "reframer", usage_records, usage_callback
        )

    # Push the user-visible restated_question to the open browser tab.
    # The frontend renders this as a transient preface above the tool
    # stream; `agent_run_complete` collapses both preface + stream
    # together. Notification failures are non-fatal — the pipeline
    # still finishes and the user gets the answer, just without the
    # preface.
    try:
        await notify_user(
            owner_user_id,
            "agent_reframe_ready",
            conversation_id=conversation_id,
            message=reframe.restated_question,
        )
    except Exception:  # noqa: BLE001
        logger.exception("agent_reframe_ready notify_user failed; pipeline continues")

    # ── Stage 1: Researcher ──────────────────────────────────────────────
    researcher_input = _build_researcher_user_turn(
        question,
        reframe.reframing_instructions,
        reframe.corpus_topics,
    )
    researcher_session = await asyncio.wait_for(
        researcher_agent.run(
            researcher_input,
            message_history=message_history,
            actor=actor,
            session_id=_new_session_id(session_ids),
            deps_ref=deps_ref,
        ),
        timeout=_RESEARCHER_TIMEOUT_S,
    )
    try:
        research = await _await_typed_result(researcher_session, ResearchOutput)
    finally:
        await _capture_stage_usage(
            researcher_session, "researcher", usage_records, usage_callback
        )

    # ── Stage 2: Gap-filler (gaps OR reframer-requested web topics) ─────
    web_citations: list[GapCitation] = []
    if research.gaps or reframe.web_search_topics:
        try:
            gap_session = await asyncio.wait_for(
                gap_filler_agent.run(
                    _format_gap_payload(
                        question, research.gaps, reframe.web_search_topics
                    ),
                    actor=actor,
                    session_id=_new_session_id(session_ids),
                    deps_ref=deps_ref,
                ),
                timeout=_GAP_FILLER_TIMEOUT_S,
            )
            try:
                gap_output = await _await_typed_result(gap_session, GapFillerOutput)
                web_citations = list(gap_output.citations)
            finally:
                await _capture_stage_usage(
                    gap_session, "gap_filler", usage_records, usage_callback
                )
        except Exception:  # noqa: BLE001
            logger.exception("gap_filler stage failed; continuing without web cites")
            total = len(research.gaps) + len(reframe.web_search_topics)
            await _emit_tool_event(
                orchestrator_deps,
                tool="gap_filler",
                label=(f"{total} item" + ("s" if total != 1 else "")),
                summary="skipped (error)",
            )

    # ── Stage 3: Author ──────────────────────────────────────────────────
    author_payload = _build_author_payload(
        research, web_citations, reframe.reframing_instructions
    )
    author_session = await asyncio.wait_for(
        author_agent.run(
            _build_author_user_turn(question, author_payload),
            message_history=message_history,
            actor=actor,
            session_id=_new_session_id(session_ids),
            deps_ref=deps_ref,
        ),
        timeout=_AUTHOR_TIMEOUT_S,
    )
    try:
        answer = await _await_text_result(author_session)
    finally:
        await _capture_stage_usage(
            author_session, "author", usage_records, usage_callback
        )
    return answer
