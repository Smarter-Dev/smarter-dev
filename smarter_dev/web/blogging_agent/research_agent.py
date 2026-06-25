"""Stage 4 of the blogging pipeline — Research.

The outer Research stage is a Gemini 3 Flash agent whose only tool is
``dig_into``. Each call dispatches a GPT-5.4-Nano sub-agent that runs its
own multi-turn search/read loop and returns 4-8 verbatim citations.

Sub-agent dispatches are auto-tracked by Skrift's event log
(``SubAgentDispatched`` / ``SubAgentCompleted``), so the admin audit view
sees the full lineage for free.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import skrift
from pydantic import BaseModel, Field
from pydantic_ai import RunContext
from pydantic_ai.models.google import GoogleModel, GoogleModelSettings
from pydantic_ai.models.openai import (
    OpenAIResponsesModel,
    OpenAIResponsesModelSettings,
)
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider
from skrift.agents.models import ResumeContext

from smarter_dev.web.blogging_agent.cache import get_cache
from smarter_dev.web.blogging_agent.summariser import extract_excerpts
from smarter_dev.web.research_tools import brave_search, jina_read

RESEARCH_MODEL = os.getenv(
    "BLOGGING_RESEARCH_MODEL", "gemini-3-flash-preview"
)
RESEARCH_AGENT_NAME = "blogging.research"
RESEARCHER_SUBAGENT_MODEL = os.getenv(
    "BLOGGING_RESEARCHER_SUBAGENT_MODEL", "gpt-5.4-nano"
)
RESEARCHER_SUBAGENT_NAME = "blogging.researcher_subagent"

_PROMPT = (Path(__file__).parent / "prompts" / "research.md").read_text(
    encoding="utf-8"
)
_SUBAGENT_PROMPT = (
    Path(__file__).parent / "prompts" / "researcher_subagent.md"
).read_text(encoding="utf-8")


# ── Shared schemas ───────────────────────────────────────────────────


class Citation(BaseModel):
    url: str
    excerpt: str = Field(description="Verbatim 1-4 sentence quote from the page.")
    why_relevant: str = Field(
        description="One line: which question this answers and why this source matters."
    )


HypothesisStatus = Literal["supported", "partially", "contradicted", "mixed"]


class ResearchOutput(BaseModel):
    """The hypothesis as it stands after evidence, plus citations.

    ``hypothesis_status`` forces the model to take a position rather
    than punt. ``surprises`` and ``limits`` are required slots so the
    model has to actually look for them; without those slots, today's
    failure mode is dumping citations and letting Synthesis figure it
    out.
    """

    citations: list[Citation] = Field(
        default_factory=list,
        description=(
            "Verbatim citations gathered by the dig_into sub-agent. 8-15 "
            "across the whole Research stage is a healthy budget."
        ),
    )
    hypothesis_status: HypothesisStatus = Field(
        description=(
            "Where the evidence landed against the brainstorm's "
            "hypothesis. 'supported' = the evidence broadly confirms it. "
            "'contradicted' = the evidence broadly refutes it. "
            "'partially' = supported under specific conditions only. "
            "'mixed' = strong evidence on both sides. Punting is not an "
            "option."
        ),
    )
    revised_hypothesis: str = Field(
        description=(
            "The hypothesis as it now stands after research. May equal "
            "the original. If the evidence forced a change, rewrite the "
            "claim to match what the citations actually support. One to "
            "three sentences."
        ),
    )
    surprises: list[str] = Field(
        description=(
            "1-3 things the research found that the brainstorm didn't "
            "anticipate. If nothing surprised you, surface that as one "
            "entry ('no real surprises — every angle held') rather than "
            "leaving the list empty."
        ),
    )
    limits: list[str] = Field(
        description=(
            "1-3 things the citations don't cover that a careful post "
            "should acknowledge. Synthesis must reference these in the "
            "limits paragraph. If genuinely no limits, say so as one "
            "entry."
        ),
    )


class ResearchInput(BaseModel):
    hypothesis: str
    counter_hypothesis: str
    open_questions: list[str]


# ── Sub-agent (GPT-5.4-Nano) ─────────────────────────────────────────


@dataclass
class ResearcherSubagentDeps:
    run_id: str


def _build_openai_model() -> OpenAIResponsesModel:
    api_key = os.getenv("OPENAI_API_KEY") or ""
    return OpenAIResponsesModel(
        RESEARCHER_SUBAGENT_MODEL,
        provider=OpenAIProvider(api_key=api_key),
    )


def _build_subagent_deps(ctx: ResumeContext) -> ResearcherSubagentDeps:
    return ResearcherSubagentDeps(run_id=str(ctx.deps_ref.get("run_id", "")))


researcher_subagent = skrift.Agent(
    _build_openai_model(),
    name=RESEARCHER_SUBAGENT_NAME,
    system_prompt=_SUBAGENT_PROMPT,
    output_type=ResearchOutput,
    model_settings=OpenAIResponsesModelSettings(
        openai_reasoning_effort="medium"
    ),
    deps_type=ResearcherSubagentDeps,
    deps_factory=_build_subagent_deps,
)


@researcher_subagent.tool
async def search_web(
    ctx: RunContext[ResearcherSubagentDeps], query: str
) -> list[dict]:
    """Search the web via Brave Search. Returns up to 5 result snippets.

    Args:
        query: Search query.

    Returns:
        List of ``{title, url, description}`` dicts.
    """
    cache = get_cache(ctx.deps.run_id)
    await cache.search_rate_limiter.wait()
    return await brave_search(cache.http_client, query, num_results=5)


@researcher_subagent.tool
async def read_page_for_excerpts(
    ctx: RunContext[ResearcherSubagentDeps],
    url: str,
    questions: list[str],
) -> dict:
    """Fetch a page and extract verbatim excerpts answering ``questions``.

    The raw page is cached per-URL for the duration of this pipeline run,
    so re-reads on the same URL skip the network. Excerpts are not cached
    (they're question-specific).

    Args:
        url: A URL from a prior ``search_web`` result.
        questions: 1-5 specific questions the excerpts should answer.

    Returns:
        Dict ``{url, title, excerpts}`` — ``excerpts`` is a list of
        verbatim quotes. Empty list means nothing on the page actually
        addressed the questions.
    """
    cache = get_cache(ctx.deps.run_id)
    raw = cache.raw_reads.get(url)
    title: str | None = None
    if raw is None:
        await cache.url_rate_limiter.wait_if_needed(url)
        result = await jina_read(cache.http_client, url)
        if "error" in result:
            return {
                "url": url,
                "title": None,
                "excerpts": [],
                "error": result["error"],
            }
        raw = result.get("content") or ""
        title = result.get("title")
        cache.raw_reads[url] = raw
    excerpts = await extract_excerpts(
        text=raw, url=url, title=title, questions=questions
    )
    return {"url": url, "title": title, "excerpts": excerpts}


# ── Outer Research stage (Gemini 3 Flash) ────────────────────────────


@dataclass
class ResearchDeps:
    run_id: str


def _build_google_model() -> GoogleModel:
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
    return GoogleModel(RESEARCH_MODEL, provider=GoogleProvider(api_key=api_key))


def _build_research_deps(ctx: ResumeContext) -> ResearchDeps:
    return ResearchDeps(run_id=str(ctx.deps_ref.get("run_id", "")))


research_agent = skrift.Agent(
    _build_google_model(),
    name=RESEARCH_AGENT_NAME,
    system_prompt=_PROMPT,
    output_type=ResearchOutput,
    model_settings=GoogleModelSettings(
        google_thinking_config={"thinking_level": "MEDIUM"},
    ),
    deps_type=ResearchDeps,
    deps_factory=_build_research_deps,
)


@research_agent.tool
async def dig_into(
    ctx: RunContext[ResearchDeps], focus: str, questions: list[str]
) -> list[dict]:
    """Dispatch the researcher sub-agent to gather citation excerpts.

    The sub-agent runs its own multi-turn search → read loop and returns
    4-8 verbatim citations from primary / authoritative sources. You can
    call this multiple times per Research stage, once per facet of the
    plan.

    Args:
        focus: One line describing what this dig is for.
        questions: 2-5 concrete questions the sub-agent should answer.

    Returns:
        List of ``{url, excerpt, why_relevant}`` dicts.
    """
    user_turn = (
        f"# Focus\n{focus}\n\n# Questions\n"
        + "\n".join(f"- {q}" for q in questions)
    )
    session = await researcher_subagent.run(
        user_turn,
        deps_ref={"run_id": ctx.deps.run_id},
    )
    result = await session.result()
    out: ResearchOutput | None
    if isinstance(result, ResearchOutput):
        out = result
    else:
        inner = getattr(result, "output", None)
        out = inner if isinstance(inner, ResearchOutput) else None
    if out is None:
        return []
    return [c.model_dump() for c in out.citations]


def build_research_user_turn(payload: ResearchInput) -> str:
    questions = "\n".join(f"- {q}" for q in payload.open_questions)
    return (
        f"# Hypothesis\n{payload.hypothesis}\n\n"
        f"# Counter-hypothesis (what would make the hypothesis wrong)\n"
        f"{payload.counter_hypothesis}\n\n"
        f"# Open questions research must answer\n{questions}"
    )
