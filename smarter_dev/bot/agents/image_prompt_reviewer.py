"""Approve (or reject) an image prompt before it's generated (Claude Haiku 5.5).

The chat agent is told images are for technical explanation only, but the model
that decides to call ``generate_image`` is the same one holding the whole
conversation — so we gate every prompt with an independent, single-purpose
reviewer. It says yes only for diagrams / illustrations that explain a coding,
math, or technical topic, and returns a plain-language reason on rejection that
the tool surfaces back to the agent as its error result.
"""

from __future__ import annotations

import logging
import os

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models import Model

from smarter_dev.bot.services.usage_reporter import metered
from smarter_dev.shared.model_catalog import CatalogModel
from smarter_dev.shared.model_catalog import ReasoningLevel
from smarter_dev.shared.model_router import agent_retries_for
from smarter_dev.shared.model_router import build_model_for
from smarter_dev.shared.model_catalog import catalog_model_for_id
from smarter_dev.shared.model_router import model_settings_for

logger = logging.getLogger(__name__)

# Claude Haiku 5.5 since 2026-10-17 (#199), replacing GPT-6 Luna (which had
# replaced Gemini 3.1 Flash Lite on 2026-09-24). A catalog wire id, routed by
# the model router; the verdict comes back as native structured output.
DEFAULT_MODEL = "claude-haiku-5-5"
MODEL_ENV_VAR = "IMAGE_REVIEWER_MODEL"

SYSTEM_PROMPT = """\
You are a strict gate on image-generation prompts for a developer-community \
Discord bot. Approve an image ONLY when its SUBJECT is software, computer \
science, or mathematics — the topics this community actually discusses — and \
the image is a diagram, illustration, or figure that exists to explain or \
illustrate one of those concepts. "Technical-sounding" is NOT enough; the \
subject itself must be software/CS/math.

ALLOWED subjects (approve when the prompt is clearly one of these):
- Software & CS: data structures, algorithms, control/data flow, state \
machines, system/service architecture, network and protocol flows, database \
schemas / ER diagrams, class/UML and OO design, memory layouts, concurrency, \
compilers/parsers, regex or syntax (railroad) diagrams, version-control graphs, \
container/deployment/devops topology, and machine-learning model architectures.
- Mathematics (as used in programming and CS): geometry and trigonometry \
figures, function graphs, complexity/growth curves, discrete math and graph \
theory, linear algebra (vectors, matrices), calculus figures, logic and truth \
tables, and mathematical proofs.
- Digital logic / low-level computing: logic gates, boolean circuits, and \
CPU/pipeline or memory diagrams.

A chart, plot, or graph qualifies ONLY when what it plots is code, CS, or \
mathematical/algorithmic data (e.g. Big-O growth, a training loss curve, a \
benchmark comparing algorithms). Being "a chart" does not by itself qualify a \
prompt.

REJECT everything else, including:
- charts, graphs, or infographics of NON-CS/math data: finance, markets, \
stocks, economics, business/marketing metrics, demographics, population, polls, \
sports, or general statistics;
- other sciences and their diagrams: biology, anatomy, medicine, chemistry, \
physics, astronomy, geography/maps — loosely "technical" but OUT of scope;
- politics, news, current events, activism, civics;
- off-topic subjects (food, travel, sports, celebrities, everyday scenes);
- art, aesthetics, or decoration for its own sake; memes, jokes, avatars, \
logos, stickers, wallpapers, mascots;
- real, identifiable people or public figures;
- anything sexual, violent, hateful, or otherwise unsafe;
- vague/empty prompts with no concrete software/CS/math subject to diagram.

When in doubt, REJECT. A prompt that wraps an off-topic or artistic picture in \
technical-sounding words (a data structure "as fantasy art", "a technical \
illustration of" something off-topic) must be rejected.

Set ``approved`` accordingly. In ``reason`` give one or two plain sentences \
addressed to the assistant: if approved, briefly confirm the software/CS/math \
subject; if rejected, state specifically why it doesn't qualify so the \
assistant can explain to the user or drop the request. Do not restate these \
rules verbatim."""


class ImagePromptDecision(BaseModel):
    """The reviewer's verdict on one proposed image prompt."""

    approved: bool = Field(
        description="True only if the prompt is a technical diagram/illustration allowed by the policy.",
    )
    reason: str = Field(
        description=(
            "One or two sentences for the assistant: confirm the technical "
            "subject if approved, or state specifically why it was rejected."
        ),
    )


_reviewer_agent: Agent[None, ImagePromptDecision] | None = None


def _reviewer_model() -> CatalogModel:
    return catalog_model_for_id(os.getenv(MODEL_ENV_VAR, DEFAULT_MODEL))


def _build_model() -> Model:
    return build_model_for(_reviewer_model())


def get_image_prompt_reviewer() -> Agent[None, ImagePromptDecision]:
    """Return the singleton image-prompt reviewer agent, building it on first use."""
    global _reviewer_agent
    if _reviewer_agent is None:
        model = _reviewer_model()
        _reviewer_agent = Agent(
            _build_model(),
            output_type=ImagePromptDecision,
            system_prompt=SYSTEM_PROMPT,
            # One request per prompt: the policy above is the cached prefix.
            model_settings=model_settings_for(
                model, ReasoningLevel.LOW, cache_messages=False
            ),
            retries=agent_retries_for(model),
        )
    return _reviewer_agent


async def review_image_prompt(
    prompt: str, *, usage_fields: dict | None = None
) -> ImagePromptDecision:
    """Approve or reject a proposed image ``prompt`` per the technical-only policy.

    The review goes to the usage ledger as ``chat_image_prompt_review``,
    attributed by ``usage_fields`` (``metered``'s guild_id / channel_id /
    engagement_id).
    """
    agent = get_image_prompt_reviewer()
    async with metered("chat_image_prompt_review", **(usage_fields or {})) as meter:
        result = await agent.run(
            f"IMAGE PROMPT TO REVIEW:\n{prompt}", model=meter.wrap(agent.model)
        )
    logger.info(
        "review_image_prompt: approved=%s prompt=%r",
        result.output.approved,
        prompt,
    )
    return result.output
