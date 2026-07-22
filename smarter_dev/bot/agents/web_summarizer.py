"""Instruction-guided web-content summarizer with model failover.

The chat agent's ``web_read`` tool fetches a page's readable text and then
hands it to this summarizer along with the caller's instruction. The agent
never sees the raw page — only an instruction-shaped summary — which keeps
large pages from flooding the conversation with tokens.

The summarizer is told to:
- follow the instruction (what to look for / ignore, quote-or-paraphrase, length),
- read facts in the context of the whole document, and
- refuse (say it cannot give a meaningful summary) when the content can't be
  summarized or doesn't contain what the instruction asks for, rather than
  fabricating one.
"""

from __future__ import annotations

import logging

from pydantic_ai import Agent

from smarter_dev.bot.agents.model_router import build_model_for
from smarter_dev.bot.agents.model_router import model_settings_for
from smarter_dev.shared.model_catalog import CatalogModel
from smarter_dev.shared.model_catalog import ReasoningLevel
from smarter_dev.shared.model_catalog import get_model
from smarter_dev.shared.observability import record_llm_failover

logger = logging.getLogger(__name__)

PRIMARY_MODEL_KEY = "poolside-laguna-s-2-1"
FALLBACK_MODEL_KEY = "gemini-3-1-flash-lite"

SYSTEM_PROMPT = """\
You summarize fetched web content to satisfy a specific INSTRUCTION from \
another assistant. You are given the page's URL and title, the INSTRUCTION \
describing what the requester wants, and the page CONTENT as readable text \
(markdown), which may be long.

Follow these rules:
- Obey the INSTRUCTION precisely: focus on exactly what it asks for, leave out \
what it says to ignore, and match the requested level of detail.
- When the INSTRUCTION asks you to find, extract, identify, or report specific \
details, default to brief verbatim excerpts enclosed in quotation marks. Do not \
silently paraphrase those details: the answer MUST contain at least one relevant \
verbatim excerpt in quotation marks, and merely restating source wording without \
quotation marks does not satisfy this rule. Paraphrase when the INSTRUCTION \
explicitly asks for a summary, explanation, or paraphrase. If it explicitly \
requests quotations, quote exactly.
- Read every fact in the context of the whole document — its structure, \
purpose, and surrounding text — not as an isolated snippet. Note when a detail \
is qualified, dated, conditional, or contradicted elsewhere on the page, since \
that context can change its meaning.
- Stay grounded in the CONTENT. Do not add outside knowledge, infer beyond what \
the text supports, or speculate.
- Be concise: at most about 5 paragraphs, and fewer when the instruction asks \
for less (e.g. a couple of sentences or a single paragraph). Do not pad.
- If the CONTENT cannot be meaningfully summarized — it is empty, an error or \
login/paywall page, pure boilerplate/navigation, or garbled — OR it does not \
contain what the INSTRUCTION is asking for, then say plainly that you cannot \
provide a meaningful summary and briefly state why. Never invent a summary to \
fill the gap."""


_summarizer_agent: Agent[None, str] | None = None
_fallback_summarizer_agent: Agent[None, str] | None = None


def _catalog_model(model_key: str) -> CatalogModel:
    model = get_model(model_key)
    if model is None:
        raise RuntimeError(f"Web summarizer model is not in the catalog: {model_key}")
    return model


def _build_agent(
    model_key: str, *, reasoning_level: ReasoningLevel | None = None
) -> Agent[None, str]:
    model = _catalog_model(model_key)
    return Agent(
        build_model_for(model),
        output_type=str,
        system_prompt=SYSTEM_PROMPT,
        model_settings=model_settings_for(model, reasoning_level),
    )


def get_web_summarizer_agent() -> Agent[None, str]:
    """Return the singleton web-summarizer agent, building it on first use."""
    global _summarizer_agent
    if _summarizer_agent is None:
        _summarizer_agent = _build_agent(PRIMARY_MODEL_KEY)
    return _summarizer_agent


def get_web_summarizer_fallback_agent() -> Agent[None, str]:
    """Return the singleton Gemini fallback agent at low thinking."""
    global _fallback_summarizer_agent
    if _fallback_summarizer_agent is None:
        _fallback_summarizer_agent = _build_agent(
            FALLBACK_MODEL_KEY, reasoning_level=ReasoningLevel.LOW
        )
    return _fallback_summarizer_agent


async def summarize_web_content(
    *, instruction: str, content: str, title: str, url: str
) -> str:
    """Summarize with Laguna S 2.1, failing over loudly to Gemini Flash Lite."""
    agent = get_web_summarizer_agent()
    prompt = (
        f"URL: {url}\n"
        f"TITLE: {title}\n\n"
        f"INSTRUCTION:\n{instruction}\n\n"
        f"CONTENT:\n{content}"
    )
    try:
        result = await agent.run(prompt)
    except Exception as exc:
        logger.critical(
            "WEB SUMMARIZER FAILOVER: Laguna S 2.1 failed; using Gemini 3.1 "
            "Flash Lite for url=%s title=%r",
            url,
            title,
            exc_info=True,
        )
        record_llm_failover(
            operation="web_summarizer",
            primary_model="poolside/laguna-s-2.1",
            fallback_model="gemini-3.1-flash-lite",
            error=exc,
        )
        result = await get_web_summarizer_fallback_agent().run(prompt)
    return result.output
