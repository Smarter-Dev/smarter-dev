"""Instruction-guided image/audio reader (Gemini 3.1 Flash Lite, multimodal).

Sibling to ``web_summarizer`` (which handles text): when ``web_read`` is given
an image or audio URL, it downloads the bytes and hands them here for an
instruction-shaped description. The chat agent never receives the raw bytes —
only the description — and the same refuse-if-not-meaningful contract applies.
"""

from __future__ import annotations

import logging
import os

from pydantic_ai import Agent, BinaryContent
from pydantic_ai.models.google import GoogleModel, GoogleModelSettings
from pydantic_ai.providers.google import GoogleProvider

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-3.1-flash-lite"
MODEL_ENV_VAR = "MEDIA_READER_MODEL"

SYSTEM_PROMPT = """\
You examine a single attached media file — an image or an audio clip — to \
satisfy a specific INSTRUCTION from another assistant. You are given the \
source URL, the kind of media, the INSTRUCTION, and the file itself.

Follow these rules:
- Obey the INSTRUCTION precisely: report exactly what it asks for, at the \
requested level of detail, and include verbatim text/quotations only when it \
asks (otherwise paraphrase).
- For an IMAGE: describe only what is actually visible — objects, people, UI, \
diagrams, charts, and any readable text. Transcribe on-screen text accurately \
when relevant. Read details in the context of the whole image, not in \
isolation.
- For AUDIO: transcribe or summarize what is actually said or heard per the \
instruction, noting speakers or notable non-speech sounds when relevant.
- Stay grounded in what is present. Do not guess at, infer, or invent details \
that are not actually in the media.
- Be concise: at most about 5 paragraphs, fewer when the instruction asks for \
less. Do not pad.
- If the media cannot be meaningfully read — it is blank, corrupt, silent, \
unintelligible, or it simply does not contain what the INSTRUCTION asks for — \
say plainly that you cannot provide a meaningful summary and briefly state \
why. Never fabricate a description to fill the gap."""


_media_reader_agent: Agent[None, str] | None = None


def _build_model() -> GoogleModel:
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
    model_id = os.getenv(MODEL_ENV_VAR, DEFAULT_MODEL)
    return GoogleModel(model_id, provider=GoogleProvider(api_key=api_key))


def get_media_reader_agent() -> Agent[None, str]:
    """Return the singleton image/audio reader agent, building it on first use."""
    global _media_reader_agent
    if _media_reader_agent is None:
        _media_reader_agent = Agent(
            _build_model(),
            output_type=str,
            system_prompt=SYSTEM_PROMPT,
            model_settings=GoogleModelSettings(
                google_thinking_config={"thinking_level": "LOW"}
            ),
        )
    return _media_reader_agent


async def describe_media(
    *, instruction: str, data: bytes, media_type: str, url: str, kind: str
) -> str:
    """Describe an image or audio clip to satisfy ``instruction`` via Gemini."""
    agent = get_media_reader_agent()
    prompt = (
        f"URL: {url}\n"
        f"KIND: {kind}\n\n"
        f"INSTRUCTION:\n{instruction}"
    )
    result = await agent.run([prompt, BinaryContent(data=data, media_type=media_type)])
    return result.output
