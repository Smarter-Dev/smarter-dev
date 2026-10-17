"""Claude Haiku 5.5 title generator for `agent_conversations`.

Cheap, one-shot Skrift agent (Claude Haiku 5.5 by default) that turns a
user's first question into a short, topic-style title. Used by the
``/v2/api/resources/ask`` endpoint via a fire-and-forget background task — it
patches ``agent_conversations.title`` and notifies the owner via Skrift's
notification system so the open browser tab can swap the placeholder in place.

Skrift wraps the underlying pydantic-ai call so the run is durable and
queueable; it runs on the agent-worker tier. Its session (the question and the
title) is deleted as soon as the title comes back.
"""

from __future__ import annotations

import logging
import os
import re
from uuid import UUID
from uuid import uuid4

import skrift

from smarter_dev.shared.model_catalog import ReasoningLevel
from smarter_dev.shared.model_catalog import catalog_model_for_id
from smarter_dev.shared.skrift_models import agent_model_name
from smarter_dev.shared.skrift_models import agent_model_settings
from smarter_dev.web.agent_session_cleanup import UsageOwner
from smarter_dev.web.agent_session_cleanup import forget_agent_sessions

logger = logging.getLogger(__name__)

# Claude Haiku 5.5 since 2026-10-17 (#199), replacing GPT-6 Luna (which had
# replaced Gemini 3.1 Flash Lite on 2026-09-24). A catalog wire id.
TITLE_MODEL = os.getenv("TITLE_AGENT_MODEL", "claude-haiku-5-5")
AGENT_NAME = "smarter.dev.title.generator"

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


# Model is a plain pydantic-ai model name ("anthropic:claude-haiku-5-5"), see
# shared.skrift_models. Passing a string (not a Model object) keeps this module
# import free of pydantic-ai; Skrift materializes the real model lazily in the
# worker that runs the agent. A title needs no deliberation, so effort runs
# low, and it is one request, so the message cache breakpoint is skipped.
_title_model = catalog_model_for_id(TITLE_MODEL)
title_agent = skrift.Agent(
    agent_model_name(_title_model),
    name=AGENT_NAME,
    system_prompt=_SYSTEM_PROMPT,
    model_settings=agent_model_settings(
        _title_model, ReasoningLevel.LOW, cache_messages=False
    ),
)


def _uuid(value: str | None) -> UUID | None:
    try:
        return UUID(str(value)) if value else None
    except ValueError:
        return None


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


async def generate_title(
    question: str, *, actor: str | None = None, conversation_id: UUID | None = None
) -> str | None:
    """Generate a title for ``question`` via Claude Haiku 5.5. ``None`` on failure.

    ``actor`` is the user id this run should be attributed to. Pass the
    asker's UUID as a string: the run's tokens are recorded in
    ``usage_cost_rows`` against that user and ``conversation_id`` as the
    session is deleted.
    """
    if not (question or "").strip():
        return None
    # Local debug short-circuit: skip the model call and return a synthetic
    # title derived from the question's first 6 words so we can iterate on
    # the live UI without burning tokens.
    if os.getenv("TITLE_AGENT_STUB", "").strip().lower() in {"1", "true", "yes"}:
        words = question.strip().split()[:6]
        return _sanitize(" ".join(w.capitalize() for w in words)) or "Synthetic Title"
    session_id = uuid4().hex
    try:
        # dispatch="queued" forces this run onto the agent-worker tier instead
        # of materializing pydantic-ai inline in the web process (the default
        # 'inline' subagent dispatch would otherwise pull the inference stack
        # into the web pods). Web just awaits the result via session.result().
        session = await title_agent.run(
            question.strip(), actor=actor, dispatch="queued", session_id=session_id
        )
        # Skrift's Agent.run returns a Session; poll until completion to
        # collect the final text.
        raw = await session.result()
    except Exception:  # noqa: BLE001
        logger.exception("Title generation failed")
        return None
    finally:
        # The session holds the question and the title; the caller stores the
        # title and never reads the session again. Its usage is recorded first:
        # the session was the only record of it.
        await forget_agent_sessions(
            [session_id],
            usage=UsageOwner(
                product_mode="resources",
                operation_type="resource_title",
                user_id=_uuid(actor),
                conversation_id=conversation_id,
            ),
        )
    if not isinstance(raw, str):
        raw = getattr(raw, "output", None) or getattr(raw, "data", None) or str(raw)
    title = _sanitize(str(raw))
    return title or None
