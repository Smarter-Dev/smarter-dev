"""Stage 5 of the blogging pipeline — Synthesis.

Takes the topic, goal, and citations and writes the post. Returns a
structured ``SynthesisOutput`` that the orchestrator turns into a
``pages`` row (`type='blog'`, `is_published=true`,
`meta_robots='noindex, nofollow'`).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import skrift
from pydantic import BaseModel, Field
from pydantic_ai.models.google import GoogleModel, GoogleModelSettings
from pydantic_ai.providers.google import GoogleProvider
from skrift.agents.models import ResumeContext

from smarter_dev.web.blogging_agent.research_agent import Citation

SYNTHESIS_MODEL = os.getenv(
    "BLOGGING_SYNTHESIS_MODEL", "gemini-3-flash-preview"
)
SYNTHESIS_AGENT_NAME = "blogging.synthesis"
_PROMPT = (Path(__file__).parent / "prompts" / "synthesis.md").read_text(
    encoding="utf-8"
)


def _build_model() -> GoogleModel:
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
    return GoogleModel(SYNTHESIS_MODEL, provider=GoogleProvider(api_key=api_key))


def _model_settings() -> GoogleModelSettings:
    return GoogleModelSettings(
        google_thinking_config={"thinking_level": "MEDIUM"},
    )


class SynthesisInput(BaseModel):
    """Inherits the validated thesis — not the brainstorm's original."""

    revised_hypothesis: str
    hypothesis_status: str  # "supported" | "partially" | "contradicted" | "mixed"
    citations: list[Citation] = Field(default_factory=list)
    surprises: list[str] = Field(default_factory=list)
    limits: list[str] = Field(default_factory=list)


class SynthesisOutput(BaseModel):
    title: str = Field(
        description=(
            "Final published post title. Generated from the *revised* "
            "hypothesis — what the evidence actually supports, not what "
            "the brainstorm originally guessed. No clickbait, no "
            "marketing fluff."
        ),
    )
    slug: str = Field(
        description="URL slug (lowercase, hyphenated, ASCII, <= 60 chars)."
    )
    content: str = Field(
        description=(
            "Markdown post body. Build the post around the revised "
            "hypothesis; link to citations inline. Do NOT include the "
            "limits paragraph here — it goes in its own field."
        ),
    )
    limits_paragraph: str = Field(
        description=(
            "REQUIRED. A single paragraph addressing the items in "
            "research.limits — what the citations don't cover, where the "
            "argument's edges are. Synthesis must engage with the limits "
            "rather than emitting a token disclaimer. The renderer "
            "appends this to the post body."
        ),
    )


@dataclass
class SynthesisDeps:
    run_id: str


def _build_deps(ctx: ResumeContext) -> SynthesisDeps:
    return SynthesisDeps(run_id=str(ctx.deps_ref.get("run_id", "")))


synthesis_agent = skrift.Agent(
    _build_model(),
    name=SYNTHESIS_AGENT_NAME,
    system_prompt=_PROMPT,
    output_type=SynthesisOutput,
    model_settings=_model_settings(),
    deps_type=SynthesisDeps,
    deps_factory=_build_deps,
)


def build_synthesis_user_turn(payload: SynthesisInput) -> str:
    cites = "\n\n".join(
        f"### {i+1}. {c.url}\n\n> {c.excerpt}\n\n_why relevant_: {c.why_relevant}"
        for i, c in enumerate(payload.citations)
    )
    if not cites:
        cites = "(no citations gathered)"
    surprises = (
        "\n".join(f"- {s}" for s in payload.surprises)
        if payload.surprises
        else "(none recorded)"
    )
    limits = (
        "\n".join(f"- {limit}" for limit in payload.limits)
        if payload.limits
        else "(none recorded)"
    )
    return (
        f"# Revised hypothesis (the post's thesis)\n"
        f"{payload.revised_hypothesis}\n\n"
        f"# Evidence verdict\nhypothesis_status: {payload.hypothesis_status}\n\n"
        f"# Surprises research uncovered\n{surprises}\n\n"
        f"# Limits the post must acknowledge\n{limits}\n\n"
        f"# Citations\n{cites}"
    )
