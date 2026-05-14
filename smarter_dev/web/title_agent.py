"""Gemini-backed title generator for `agent_conversations`.

Cheap, one-shot model call (Gemini 3 Flash Lite by default) that turns a
user's first question into a short, topic-style title. Used by the
``/v2/api/resources/ask`` endpoint via a fire-and-forget background task — it
patches ``agent_conversations.title`` and notifies the owner via Skrift's
notification system so the open browser tab can swap the placeholder in place.
"""

from __future__ import annotations

import logging
import os
import re

from pydantic_ai import Agent
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google import GoogleProvider

logger = logging.getLogger(__name__)

TITLE_MODEL = os.getenv("TITLE_AGENT_MODEL", "gemini-3-flash-lite-preview")

_SYSTEM_PROMPT = """\
You generate concise titles for engineering Q&A sessions.

Given a user's first question, reply with ONE short title — 3 to 7 words,
title-cased, no quotes, no trailing punctuation, no preamble.

The title should capture the TOPIC, not the question. For example:
- "How do I architect a webhook receiver for bursts?" -> "Webhook Receiver Burst Architecture"
- "Postgres SKIP LOCKED vs SQS" -> "Postgres SKIP LOCKED vs SQS"
- "Best book on distributed systems?" -> "Distributed Systems Reading"

Reply with the title alone. No labels, no explanation.
"""

_TITLE_MAX_LEN = 80


def _build_model() -> GoogleModel:
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
    return GoogleModel(TITLE_MODEL, provider=GoogleProvider(api_key=api_key))


title_agent = Agent(_build_model(), system_prompt=_SYSTEM_PROMPT)


def _sanitize(raw: str) -> str:
    """Strip the things models like to add despite being told not to."""
    text = (raw or "").strip()
    # Take the first non-empty line only.
    for line in text.splitlines():
        line = line.strip()
        if line:
            text = line
            break
    # Drop leading/trailing markdown bullets, bold markers, quotes, labels.
    text = re.sub(r'^[#>\-*_•"\'`]+\s*', "", text).strip()
    text = re.sub(r'[*_`"\']+$', "", text).strip()
    text = re.sub(r'^(title|topic)\s*[:\-]\s*', "", text, flags=re.IGNORECASE).strip()
    # Strip wrapping quotes and trailing punctuation we don't want.
    text = text.strip('"').strip("'").rstrip(".!?,:;")
    if len(text) > _TITLE_MAX_LEN:
        text = text[: _TITLE_MAX_LEN - 1].rstrip() + "…"
    return text


async def generate_title(question: str) -> str | None:
    """Generate a title for ``question`` via Gemini. ``None`` on failure."""
    if not (question or "").strip():
        return None
    try:
        result = await title_agent.run(question.strip())
    except Exception:  # noqa: BLE001
        logger.exception("Title generation failed")
        return None
    raw = getattr(result, "output", None) or getattr(result, "data", None) or ""
    title = _sanitize(str(raw))
    return title or None
