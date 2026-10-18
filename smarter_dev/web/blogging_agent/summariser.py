"""Two thin Claude Haiku 5.5 agents used inside the pipeline's read tools.

- ``summarise_news_page(text, url, title)`` — Scout's news read helper. Returns
  a full-document summary in 3-6 sentences. Cached per-URL by Scout's read
  tool so the same article isn't re-summarised within a run.

- ``extract_excerpts(text, url, questions)`` — Researcher sub-agent's read
  helper. Returns verbatim excerpts (NOT a summary) that answer the supplied
  questions. Excerpts are not cached because they're question-specific.

Both reuse the singleton-build pattern from
``smarter_dev/bot/agents/chat_compaction.py:125`` — Pydantic AI agents built
once at first use.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from pydantic_ai import Agent

    from smarter_dev.shared.usage_meter import UsageMeter

logger = logging.getLogger(__name__)

# Claude Haiku 5.5 since 2026-10-17 (#199), replacing GPT-6 Luna (which had
# replaced Gemini 3.1 Flash Lite on 2026-09-24). Built by the shared model
# router, imported lazily like pydantic-ai: this module loads on the web tier.
_MODEL = "claude-haiku-5-5"


def _agent(system_prompt: str, output_type):
    """A one-request agent on ``_MODEL`` at low effort."""
    from pydantic_ai import Agent

    from smarter_dev.shared.model_catalog import ReasoningLevel
    from smarter_dev.shared.model_catalog import catalog_model_for_id
    from smarter_dev.shared.model_router import agent_retries_for
    from smarter_dev.shared.model_router import build_model_for
    from smarter_dev.shared.model_router import model_settings_for

    model = catalog_model_for_id(_MODEL)
    return Agent(
        build_model_for(model),
        system_prompt=system_prompt,
        output_type=output_type,
        # Each page is read once, so only the system prompt is worth caching.
        model_settings=model_settings_for(
            model, ReasoningLevel.LOW, cache_messages=False
        ),
        retries=agent_retries_for(model),
    )

_PROMPTS_DIR = Path(__file__).parent / "prompts"


# ── News summariser (Scout) ──────────────────────────────────────────


_news_summariser_agent: "Agent[None, str] | None" = None


def _news_summariser() -> "Agent[None, str]":
    global _news_summariser_agent
    if _news_summariser_agent is None:
        _news_summariser_agent = _agent(
            (_PROMPTS_DIR / "news_summariser.md").read_text(encoding="utf-8"), str
        )
    return _news_summariser_agent


def _run_options(agent, meter: UsageMeter | None) -> dict:
    """``model=`` keeping every response in ``meter``, when there is one."""
    return {"model": meter.wrap(agent.model)} if meter is not None else {}


async def summarise_news_page(
    *, text: str, url: str, title: str | None, meter: UsageMeter | None = None
) -> str:
    """Return a 3-6 sentence full-document summary of a news page.

    ``meter`` keeps the run's usage (the pipeline's), failed runs included.
    """
    agent = _news_summariser()
    prompt = f"# Title\n{title or '(unknown)'}\n\n# URL\n{url}\n\n# Body\n{text[:60_000]}"
    try:
        result = await agent.run(user_prompt=prompt, **_run_options(agent, meter))
        out = result.output if hasattr(result, "output") else str(result)
        return (out or "").strip() or "(empty summary)"
    except Exception:  # noqa: BLE001
        logger.exception("news summariser failed for %s", url)
        # Degrade gracefully — Scout can still see SOMETHING.
        return f"[summary unavailable] {title or url}"


# ── Excerpt extractor (Researcher sub-agent) ─────────────────────────


class ExcerptOutput(BaseModel):
    """List of verbatim quotes the page provides that answer the questions."""

    excerpts: list[str] = Field(
        default_factory=list,
        description=(
            "Verbatim quotes from the page that directly answer one or "
            "more of the questions. 1-4 sentences each. Copy character-"
            "for-character from the source — do not paraphrase. Return "
            "[] if nothing on the page actually answers any question."
        ),
    )


_excerpt_extractor_agent: "Agent[None, ExcerptOutput] | None" = None


def _excerpt_extractor() -> "Agent[None, ExcerptOutput]":
    global _excerpt_extractor_agent
    if _excerpt_extractor_agent is None:
        _excerpt_extractor_agent = _agent(
            (_PROMPTS_DIR / "excerpt_extractor.md").read_text(encoding="utf-8"),
            ExcerptOutput,
        )
    return _excerpt_extractor_agent


async def extract_excerpts(
    *,
    text: str,
    url: str,
    title: str | None,
    questions: list[str],
    meter: UsageMeter | None = None,
) -> list[str]:
    """Pull verbatim excerpts answering ``questions`` from a page; ``meter`` as
    in :func:`summarise_news_page`."""
    if not questions:
        return []
    agent = _excerpt_extractor()
    questions_block = "\n".join(f"- {q}" for q in questions)
    prompt = (
        f"# Source URL\n{url}\n\n"
        f"# Title\n{title or '(unknown)'}\n\n"
        f"# Questions\n{questions_block}\n\n"
        f"# Page body\n{text[:80_000]}"
    )
    try:
        result = await agent.run(user_prompt=prompt, **_run_options(agent, meter))
        out = result.output if hasattr(result, "output") else None
        if isinstance(out, ExcerptOutput):
            return [e.strip() for e in out.excerpts if e and e.strip()]
        return []
    except Exception:  # noqa: BLE001
        logger.exception("excerpt extractor failed for %s", url)
        return []
