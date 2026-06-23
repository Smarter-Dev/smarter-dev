"""Instruction-guided web-content summarizer (Gemini 3.1 Flash Lite).

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
import os

from pydantic_ai import Agent
from pydantic_ai.models.google import GoogleModel, GoogleModelSettings
from pydantic_ai.providers.google import GoogleProvider

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-3.1-flash-lite"
MODEL_ENV_VAR = "WEB_SUMMARIZER_MODEL"

SYSTEM_PROMPT = """\
You summarize fetched web content to satisfy a specific INSTRUCTION from \
another assistant. You are given the page's URL and title, the INSTRUCTION \
describing what the requester wants, and the page CONTENT as readable text \
(markdown), which may be long.

Follow these rules:
- Obey the INSTRUCTION precisely: focus on exactly what it asks for, leave out \
what it says to ignore, match the requested level of detail, and include exact \
quotations only when it asks you to (otherwise paraphrase).
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


def _build_model() -> GoogleModel:
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
    model_id = os.getenv(MODEL_ENV_VAR, DEFAULT_MODEL)
    return GoogleModel(model_id, provider=GoogleProvider(api_key=api_key))


def get_web_summarizer_agent() -> Agent[None, str]:
    """Return the singleton web-summarizer agent, building it on first use."""
    global _summarizer_agent
    if _summarizer_agent is None:
        _summarizer_agent = Agent(
            _build_model(),
            output_type=str,
            system_prompt=SYSTEM_PROMPT,
            model_settings=GoogleModelSettings(
                google_thinking_config={"thinking_level": "LOW"}
            ),
        )
    return _summarizer_agent


async def summarize_web_content(
    *, instruction: str, content: str, title: str, url: str
) -> str:
    """Summarize ``content`` to satisfy ``instruction`` via Gemini 3.1 Flash Lite."""
    agent = get_web_summarizer_agent()
    prompt = (
        f"URL: {url}\n"
        f"TITLE: {title}\n\n"
        f"INSTRUCTION:\n{instruction}\n\n"
        f"CONTENT:\n{content}"
    )
    result = await agent.run(prompt)
    return result.output
