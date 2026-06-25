"""Two thin Gemini Flash Lite agents used inside the pipeline's read tools.

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

logger = logging.getLogger(__name__)

_FLASH_LITE_MODEL = "gemini-3.1-flash-lite"

_MODEL_SETTINGS = {"google_thinking_config": {"thinking_level": "LOW"}}

_PROMPTS_DIR = Path(__file__).parent / "prompts"


# ── News summariser (Scout) ──────────────────────────────────────────


_news_summariser_agent: "Agent[None, str] | None" = None


def _news_summariser() -> "Agent[None, str]":
    global _news_summariser_agent
    if _news_summariser_agent is None:
        from pydantic_ai import Agent

        _news_summariser_agent = Agent(
            f"google-gla:{_FLASH_LITE_MODEL}",
            system_prompt=(_PROMPTS_DIR / "news_summariser.md").read_text(
                encoding="utf-8"
            ),
            output_type=str,
            model_settings=_MODEL_SETTINGS,
        )
    return _news_summariser_agent


async def summarise_news_page(*, text: str, url: str, title: str | None) -> str:
    """Return a 3-6 sentence full-document summary of a news page."""
    agent = _news_summariser()
    prompt = f"# Title\n{title or '(unknown)'}\n\n# URL\n{url}\n\n# Body\n{text[:60_000]}"
    try:
        result = await agent.run(user_prompt=prompt)
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
        from pydantic_ai import Agent

        _excerpt_extractor_agent = Agent(
            f"google-gla:{_FLASH_LITE_MODEL}",
            system_prompt=(_PROMPTS_DIR / "excerpt_extractor.md").read_text(
                encoding="utf-8"
            ),
            output_type=ExcerptOutput,
            model_settings=_MODEL_SETTINGS,
        )
    return _excerpt_extractor_agent


async def extract_excerpts(
    *, text: str, url: str, title: str | None, questions: list[str]
) -> list[str]:
    """Pull verbatim excerpts answering ``questions`` from a page."""
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
        result = await agent.run(user_prompt=prompt)
        out = result.output if hasattr(result, "output") else None
        if isinstance(out, ExcerptOutput):
            return [e.strip() for e in out.excerpts if e and e.strip()]
        return []
    except Exception:  # noqa: BLE001
        logger.exception("excerpt extractor failed for %s", url)
        return []
