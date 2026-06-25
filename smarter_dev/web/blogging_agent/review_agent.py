"""Stage 1 of the blogging pipeline — Review.

Reads every `new` + `kept` candidate topic, returns the subset to keep.
Anything not in the returned list is marked `discarded`. The DB-side
status mutation happens in the orchestrator AFTER the agent returns, so
this module is purely the agent definition.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import skrift
from pydantic import BaseModel, Field
from skrift.agents.models import ResumeContext

REVIEW_MODEL = os.getenv(
    "BLOGGING_REVIEW_MODEL", "gemini-3.1-flash-lite"
)
REVIEW_AGENT_NAME = "blogging.review"
_PROMPT = (Path(__file__).parent / "prompts" / "review.md").read_text(
    encoding="utf-8"
)


class CandidateTopicView(BaseModel):
    """A candidate topic as seen by the Review agent."""

    id: UUID
    headline: str
    observation: str
    scope: str = ""
    evidence: list[str] = Field(default_factory=list)
    category: str | None = None
    status: str  # "new" | "kept"
    surfaced_at_iso: str
    surfaced_by: str


class ReviewInput(BaseModel):
    """User-prompt payload for the Review stage."""

    candidates: list[CandidateTopicView]


class ReviewOutput(BaseModel):
    """What the Review stage returns to the orchestrator."""

    kept_topic_ids: list[UUID] = Field(default_factory=list)
    reasoning: str = Field(
        description="2-5 sentences explaining the cuts. Operator-facing."
    )


@dataclass
class ReviewDeps:
    """Per-run deps for Review. Just carries the run id for telemetry."""

    run_id: str


def _build_deps(ctx: ResumeContext) -> ReviewDeps:
    return ReviewDeps(run_id=str(ctx.deps_ref.get("run_id", "")))


review_agent = skrift.Agent(
    f"google-gla:{REVIEW_MODEL}",
    name=REVIEW_AGENT_NAME,
    system_prompt=_PROMPT,
    output_type=ReviewOutput,
    model_settings={"google_thinking_config": {"thinking_level": "LOW"}},
    deps_type=ReviewDeps,
    deps_factory=_build_deps,
)


def build_review_user_turn(payload: ReviewInput) -> str:
    """Render the candidates payload as the user prompt."""
    lines = [
        f"You are reviewing {len(payload.candidates)} candidate blog topics. "
        "Decide which to keep. Reply with `kept_topic_ids` (exact UUIDs) and "
        "a short `reasoning`."
    ]
    for c in payload.candidates:
        lines.append("")
        lines.append(f"## id: {c.id}")
        lines.append(f"status: {c.status}")
        lines.append(f"category: {c.category or '—'}")
        lines.append(f"surfaced: {c.surfaced_at_iso} by {c.surfaced_by}")
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
