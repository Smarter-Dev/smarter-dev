"""Fit an overlong chat reply into Discord's 2000-character message cap.

Three tiers, cheapest first:

1. ``len <= 2000`` — send as-is (no work; :func:`split_for_discord` is a
   pass-through).
2. ``2000 < len <= 3000`` — :func:`split_for_discord` splits it into two
   messages at the last newline before the 1500-character mark (falling back
   to the last space, then a hard cut), keeping both parts under the cap.
3. ``len > 3000`` — :func:`fit_overlong_response` first asks the chat agent
   itself to rewrite the reply shorter (it has the full conversation context);
   if the rewrite is still over 3000 characters (or the run fails), a cheap
   GPT-5.6 Luna summarizer condenses the original; if even that overruns, the
   text is hard-truncated as a last resort. The result then flows through tier
   1/2 for sending.

The shorten re-run uses the turn's own agent + history, so its tokens are the
chat model's and are folded into the turn's metering/pricing by the engine.
The Luna summarizer runs its own model; like chat compaction, its spend is
logged but not metered against the channel budget.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage

from smarter_dev.bot.agents.chat_agent import build_agent_model
from smarter_dev.bot.agents.model_router import model_settings_for
from smarter_dev.shared.model_catalog import ReasoningLevel
from smarter_dev.shared.model_catalog import get_model

logger = logging.getLogger(__name__)

DISCORD_MESSAGE_LIMIT = 2000
# Where the two-message split aims to break: the last newline at or before
# this index.
SPLIT_TARGET = 1500
# Above this, splitting in two can't stay under the cap — rewrite instead.
SUMMARIZE_THRESHOLD = 3000

# Catalog key of the model that summarizes as the second resort.
LENGTH_SUMMARIZER_MODEL_KEY = "gpt-5-6-luna"

_SHORTEN_PROMPT_TEMPLATE = (
    "<length-notice>Your drafted reply could not be sent: it is {length} "
    "characters and Discord caps messages at 2000. Rewrite it to under 1500 "
    "characters — keep the essential answer and any code or links that are "
    "load-bearing, cut the rest. Respond again with the rewritten message, "
    "targeting the same message as before.</length-notice>"
)

_SUMMARIZER_SYSTEM_PROMPT = (
    "You condense overlong Discord messages written by a chat bot. Rewrite "
    "the given message to under 1500 characters while keeping its essential "
    "answer, any load-bearing code or links, and its tone. Output ONLY the "
    "rewritten message — no preamble, no commentary."
)


@dataclass(frozen=True)
class FitResult:
    """Outcome of fitting an overlong reply.

    ``text`` is guaranteed to be at most :data:`SUMMARIZE_THRESHOLD` characters
    (so :func:`split_for_discord` can always send it in at most two messages).
    ``extra_input_tokens`` / ``extra_output_tokens`` are the shorten re-run's
    chat-model spend for the engine to meter; ``method`` records which tier
    produced the text ("shortened" / "summarized" / "truncated").
    """

    text: str
    extra_input_tokens: int
    extra_output_tokens: int
    method: str


def split_for_discord(text: str) -> list[str]:
    """Split ``text`` into Discord-sendable chunks (at most two).

    At or under the 2000-character cap the text passes through unchanged. Up
    to 3000 characters it splits at the last newline before the
    1500-character mark — falling back to the last space, then a hard cut —
    constrained so the second part also fits the cap. Anything longer should
    have gone through :func:`fit_overlong_response` first; as a defensive
    last resort the second part is truncated with an ellipsis.
    """
    stripped = text.strip()
    if len(stripped) <= DISCORD_MESSAGE_LIMIT:
        return [stripped] if stripped else []

    # The split index must leave the tail under the cap too (a newline very
    # early in the text would otherwise push part two over 2000).
    earliest = max(0, len(stripped) - DISCORD_MESSAGE_LIMIT)
    split_at = stripped.rfind("\n", earliest, SPLIT_TARGET + 1)
    if split_at <= 0:
        split_at = stripped.rfind(" ", earliest, SPLIT_TARGET + 1)
    if split_at <= 0:
        split_at = SPLIT_TARGET
    head = stripped[:split_at].rstrip()
    tail = stripped[split_at:].strip()
    if len(tail) > DISCORD_MESSAGE_LIMIT:
        tail = tail[: DISCORD_MESSAGE_LIMIT - 1] + "…"
    return [part for part in (head, tail) if part]


_length_summarizer: Agent | None = None


def get_length_summarizer() -> Agent:
    """The cached Luna summarizer agent (plain-text output)."""
    global _length_summarizer
    if _length_summarizer is None:
        catalog_model = get_model(LENGTH_SUMMARIZER_MODEL_KEY)
        _length_summarizer = Agent(
            build_agent_model(catalog_model.model_id),
            output_type=str,
            system_prompt=_SUMMARIZER_SYSTEM_PROMPT,
            model_settings=model_settings_for(catalog_model, ReasoningLevel.LOW),
        )
    return _length_summarizer


async def _shorten_with_agent(
    message: str,
    agent: Agent,
    deps: Any,
    message_history: list[ModelMessage],
) -> tuple[str | None, int, int]:
    """Ask the turn's own agent to rewrite its overlong reply.

    Returns ``(rewritten_text, input_tokens, output_tokens)``; the text is
    ``None`` when the run fails or the agent declines to respond, so the
    caller can fall through to the summarizer. Failures must never break the
    turn — the original reply is still deliverable via later tiers.
    """
    prompt = _SHORTEN_PROMPT_TEMPLATE.format(length=len(message))
    try:
        result = await agent.run(
            user_prompt=prompt, message_history=message_history, deps=deps
        )
    except Exception:
        logger.exception("Shorten re-run failed — falling back to summarizer")
        return None, 0, 0
    usage = result.usage()
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    response = result.output.response
    text = response.message.strip() if response and response.message else None
    return text or None, input_tokens, output_tokens


async def _summarize_with_luna(message: str) -> str | None:
    """Condense ``message`` with the Luna summarizer (fail-soft)."""
    try:
        result = await get_length_summarizer().run(user_prompt=message)
    except Exception:
        logger.exception("Length summarizer failed — falling back to truncation")
        return None
    usage = result.usage()
    logger.info(
        "Length summarizer condensed %d chars -> %d (tokens in=%s out=%s)",
        len(message),
        len(result.output or ""),
        getattr(usage, "input_tokens", 0),
        getattr(usage, "output_tokens", 0),
    )
    text = (result.output or "").strip()
    return text or None


async def fit_overlong_response(
    message: str,
    *,
    agent: Agent,
    deps: Any,
    message_history: list[ModelMessage],
) -> FitResult:
    """Bring a > :data:`SUMMARIZE_THRESHOLD` reply down to a sendable size.

    Tries the chat agent's own rewrite first (context-aware), then the Flash
    Lite summarizer on the original text, then a hard truncation so a reply
    is always delivered.
    """
    shortened, input_tokens, output_tokens = await _shorten_with_agent(
        message, agent, deps, message_history
    )
    if shortened is not None and len(shortened) <= SUMMARIZE_THRESHOLD:
        return FitResult(shortened, input_tokens, output_tokens, "shortened")

    summary = await _summarize_with_luna(message)
    if summary is not None and len(summary) <= SUMMARIZE_THRESHOLD:
        return FitResult(summary, input_tokens, output_tokens, "summarized")

    return FitResult(
        message[: DISCORD_MESSAGE_LIMIT - 1].rstrip() + "…",
        input_tokens,
        output_tokens,
        "truncated",
    )
