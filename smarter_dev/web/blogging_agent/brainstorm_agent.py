"""Stage 3 of the blogging pipeline — Brainstorm.

Takes up to ~20 candidate topics (kept survivors of Review + Scout's
news topics) and synthesises one post topic, a research plan, and a goal.
No tools; pure reasoning.
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

BRAINSTORM_MODEL = os.getenv(
    "BLOGGING_BRAINSTORM_MODEL", "gemini-3-flash-preview"
)
BRAINSTORM_AGENT_NAME = "blogging.brainstorm"
_PROMPT = (Path(__file__).parent / "prompts" / "brainstorm.md").read_text(
    encoding="utf-8"
)


def _build_model() -> GoogleModel:
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
    return GoogleModel(BRAINSTORM_MODEL, provider=GoogleProvider(api_key=api_key))


def _model_settings() -> GoogleModelSettings:
    return GoogleModelSettings(
        google_thinking_config={"thinking_level": "MEDIUM"},
    )


class BrainstormCandidate(BaseModel):
    """Slim candidate view (works for both kept chat-captures and scout topics)."""

    source: str  # "kept" or "scout"
    headline: str
    observation: str
    scope: str = ""
    evidence: list[str] = Field(default_factory=list)
    category: str | None = None


class BrainstormInput(BaseModel):
    candidates: list[BrainstormCandidate]


class BrainstormOutput(BaseModel):
    """Falsifiable hypothesis the Research stage will test.

    Notably absent: no `topic`, no `title`, no `goal`. Synthesis derives
    the title from the *validated* hypothesis after research. Avoiding
    topic-first framing keeps the pipeline from forcing evidence to
    match a pre-decided angle.
    """

    hypothesis: str = Field(
        description=(
            "A falsifiable claim the post will test against evidence. "
            "One to three sentences. Specific enough that a careful "
            "researcher could find sources that either support or "
            "contradict it. NOT a question. NOT a survey. A claim."
        ),
    )
    counter_hypothesis: str = Field(
        description=(
            "What would have to be true for the hypothesis to be wrong. "
            "The load-bearing field — without it, the model treats "
            "'hypothesis' as a synonym for 'thesis'. Make the "
            "disconfirmation conditions explicit. Example, for a "
            "hypothesis 'A 7-day deploy buffer reduces incident "
            "blast radius': a counter would be 'A 7-day buffer "
            "provides marginal additional protection over 1 day while "
            "creating significant friction with legitimate hotfix "
            "releases.'"
        ),
    )
    open_questions: list[str] = Field(
        description=(
            "3-5 specific questions the research stage must answer. "
            "Each one phrased so it can be answered by a citation, "
            "not by speculation."
        ),
    )

    @property
    def is_abort(self) -> bool:
        """True when the brainstorm explicitly returned a no-op."""
        return self.hypothesis.strip().lower().startswith("(no post worth")


@dataclass
class BrainstormDeps:
    run_id: str


def _build_deps(ctx: ResumeContext) -> BrainstormDeps:
    return BrainstormDeps(run_id=str(ctx.deps_ref.get("run_id", "")))


brainstorm_agent = skrift.Agent(
    _build_model(),
    name=BRAINSTORM_AGENT_NAME,
    system_prompt=_PROMPT,
    output_type=BrainstormOutput,
    model_settings=_model_settings(),
    deps_type=BrainstormDeps,
    deps_factory=_build_deps,
)


def build_brainstorm_user_turn(payload: BrainstormInput) -> str:
    """Render the candidate set as the user prompt."""
    if not payload.candidates:
        return (
            "You received zero candidate claims this run (Review skipped, "
            "Scout returned nothing). Return the abort sentinel: "
            "`hypothesis='(no post worth writing this run)'`, "
            "`counter_hypothesis='skip'`, `open_questions=['skip']`."
        )
    lines = [
        f"You have {len(payload.candidates)} candidate claims. Form a "
        "single falsifiable hypothesis. Return `hypothesis`, "
        "`counter_hypothesis`, and `open_questions`."
    ]
    for i, c in enumerate(payload.candidates, start=1):
        lines.append("")
        lines.append(f"## candidate {i} (source: {c.source})")
        if c.category:
            lines.append(f"category: {c.category}")
        lines.append(f"headline: {c.headline}")
        lines.append("observation:")
        lines.append(c.observation)
        if c.scope:
            lines.append("scope:")
            lines.append(c.scope)
        if c.evidence:
            lines.append("evidence:")
            for u in c.evidence:
                lines.append(f"  - {u}")
    return "\n".join(lines)
