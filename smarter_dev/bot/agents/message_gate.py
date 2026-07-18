"""Pre-turn relevance gate (GPT-5.4 Nano) for admin-restricted channels.

An admin can restrict a channel so the bot only replies to messages matching a
written response filter. Before the chat engine spends a turn on an expensive
model, it asks this cheap Nano classifier which of the pending messages the
filter actually allows — so clearly off-topic chatter never reaches the pricey
model.

The gate is deliberately fail-open: a Nano outage returns every candidate
rather than silencing the bot, since a wasted expensive reply is far cheaper
than a channel that stops answering. It also short-circuits (no model call) when
there is nothing to judge or no filter to apply.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pydantic import BaseModel, Field
from pydantic_ai import Agent

from smarter_dev.bot.agents.model_router import build_model_for, model_settings_for
from smarter_dev.shared.model_catalog import ReasoningLevel, get_model

logger = logging.getLogger(__name__)

GATE_MODEL_KEY = "gpt-5-4-nano"

SYSTEM_PROMPT = """\
You are a fast, cheap relevance gate for a Discord bot. An admin has restricted \
this channel so the bot only replies to messages that match their written \
INSTRUCTIONS. For each CANDIDATE message you decide whether the instructions \
allow the bot to spend an (expensive) reply on it.

You are given:
- INSTRUCTIONS: the admin's filter describing which messages the bot should \
respond to. They are about the TOPIC and CONTENT of a message, never about who \
wrote it — ignore the author when deciding.
- CONTEXT: recent channel messages, oldest first, provided only so you can \
interpret the candidates. These are NOT candidates; never return a context id.
- CANDIDATES: the messages to judge. Return the ids of the candidates the \
instructions allow.

Rules:
- Judge each candidate on whether its topic/content matches the instructions.
- The point of this gate is to protect an expensive model from clearly \
off-topic messages, so lean DROP when a candidate is plainly off-topic.
- When a candidate is genuinely ambiguous or borderline — it could reasonably \
fall under the instructions — lean ALLOW.
- Use the CONTEXT only to interpret a candidate (e.g. a short reply that only \
makes sense given the prior messages); a candidate that is on-topic given the \
conversation should be allowed even if it looks thin in isolation.

Set allowed_message_ids to the ids of the candidates (and only the candidates) \
that the instructions allow, in any order."""


@dataclass(frozen=True)
class GateMessage:
    """One Discord message the gate reasons about."""

    message_id: str
    author_display: str
    content: str


class GateDecision(BaseModel):
    """The gate's verdict: which candidate message ids the filter allows."""

    allowed_message_ids: list[str] = Field(
        description="Ids of the CANDIDATE messages the admin instructions allow.",
    )


_gate_agent: Agent[None, GateDecision] | None = None


def get_message_gate_agent() -> Agent[None, GateDecision]:
    """Return the singleton message-gate agent, building it on first use."""
    global _gate_agent
    if _gate_agent is None:
        catalog_model = get_model(GATE_MODEL_KEY)
        if catalog_model is None:
            raise ValueError(f"Gate model {GATE_MODEL_KEY!r} is not in the catalog")
        _gate_agent = Agent(
            build_model_for(catalog_model),
            output_type=GateDecision,
            system_prompt=SYSTEM_PROMPT,
            model_settings=model_settings_for(catalog_model, ReasoningLevel.NONE),
        )
    return _gate_agent


def _render_message(message: GateMessage) -> str:
    return f"[{message.message_id}] {message.author_display}: {message.content}"


def _render_prompt(
    response_filter: str,
    candidates: list[GateMessage],
    grounding: list[GateMessage],
) -> str:
    sections = [f"INSTRUCTIONS:\n{response_filter.strip()}"]
    if grounding:
        rendered = "\n".join(_render_message(message) for message in grounding)
        sections.append(
            "CONTEXT (oldest first, for reference only — never return these ids):\n"
            + rendered
        )
    rendered_candidates = "\n".join(_render_message(message) for message in candidates)
    sections.append("CANDIDATES (judge these):\n" + rendered_candidates)
    return "\n\n".join(sections)


async def filter_messages(
    response_filter: str,
    candidates: list[GateMessage],
    grounding: list[GateMessage],
) -> list[str]:
    """Return the candidate message ids the ``response_filter`` allows.

    Ids are returned in candidate order. An empty ``candidates`` list returns
    ``[]`` without a model call, and an empty/whitespace ``response_filter``
    allows every candidate without a model call. Any exception from the model is
    logged and fails open — every candidate id is returned — so a Nano outage
    never silences the bot. The model's answer is intersected with the real
    candidate ids, since it may hallucinate ids that were never offered.
    """
    if not candidates:
        return []
    candidate_ids = [message.message_id for message in candidates]
    if not response_filter.strip():
        return candidate_ids
    agent = get_message_gate_agent()
    try:
        result = await agent.run(_render_prompt(response_filter, candidates, grounding))
    except Exception:
        logger.warning(
            "message gate model call failed; allowing all %d candidate(s) (fail-open)",
            len(candidate_ids),
            exc_info=True,
        )
        return candidate_ids
    allowed = set(result.output.allowed_message_ids)
    return [message_id for message_id in candidate_ids if message_id in allowed]
