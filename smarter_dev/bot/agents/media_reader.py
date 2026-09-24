"""Instruction-guided image/audio reader (GPT-6 Luna for images, Gemini for audio).

Sibling to ``web_summarizer`` (which handles text): when ``web_read`` is given
an image or audio URL, it downloads the bytes and hands them here for an
instruction-shaped description. The chat agent never receives the raw bytes —
only the description — and the same refuse-if-not-meaningful contract applies.

Images moved from Gemini 3.1 Flash Lite to GPT-6 Luna on 2026-09-24. Audio could
not follow: the OpenAI Responses API takes no audio input, so audio goes to
Gemini 3.8 Flash, the one Gemini Flash still in use. Luna refuses BMP and reads
only an animated GIF's first frame, so ``shared.media_images`` converts those.
"""

from __future__ import annotations

import logging
import os

from pydantic_ai import Agent, BinaryContent
from pydantic_ai.models.google import GoogleModel, GoogleModelSettings
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.models.openai import OpenAIResponsesModelSettings
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider

from smarter_dev.shared.media_images import ImageTooLarge
from smarter_dev.shared.media_images import prepare_image_bounded

logger = logging.getLogger(__name__)

# Images: an OpenAI wire id, served by OpenAI directly.
DEFAULT_MODEL = "gpt-6-luna"
MODEL_ENV_VAR = "MEDIA_READER_MODEL"
# Audio: a Gemini wire id.
DEFAULT_AUDIO_MODEL = "gemini-3.8-flash"
AUDIO_MODEL_ENV_VAR = "MEDIA_READER_AUDIO_MODEL"

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
_audio_reader_agent: Agent[None, str] | None = None


def _build_model() -> OpenAIResponsesModel:
    model_id = os.getenv(MODEL_ENV_VAR, DEFAULT_MODEL)
    return OpenAIResponsesModel(
        model_id, provider=OpenAIProvider(api_key=os.getenv("OPENAI_API_KEY") or "")
    )


def _build_audio_model() -> GoogleModel:
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
    model_id = os.getenv(AUDIO_MODEL_ENV_VAR, DEFAULT_AUDIO_MODEL)
    return GoogleModel(model_id, provider=GoogleProvider(api_key=api_key))


def get_media_reader_agent() -> Agent[None, str]:
    """Return the singleton image reader agent, building it on first use."""
    global _media_reader_agent
    if _media_reader_agent is None:
        _media_reader_agent = Agent(
            _build_model(),
            output_type=str,
            system_prompt=SYSTEM_PROMPT,
            model_settings=OpenAIResponsesModelSettings(openai_reasoning_effort="low"),
        )
    return _media_reader_agent


def get_audio_reader_agent() -> Agent[None, str]:
    """Return the singleton audio reader agent, building it on first use."""
    global _audio_reader_agent
    if _audio_reader_agent is None:
        _audio_reader_agent = Agent(
            _build_audio_model(),
            output_type=str,
            system_prompt=SYSTEM_PROMPT,
            model_settings=GoogleModelSettings(
                google_thinking_config={"thinking_level": "LOW"}
            ),
        )
    return _audio_reader_agent


async def describe_media(
    *, instruction: str, data: bytes, media_type: str, url: str, kind: str
) -> str:
    """Describe an image (GPT-6 Luna) or audio clip (Gemini) per ``instruction``."""
    is_audio = kind == "audio" or media_type.startswith("audio/")
    agent = get_audio_reader_agent() if is_audio else get_media_reader_agent()
    prompt = (
        f"URL: {url}\n"
        f"KIND: {kind}\n\n"
        f"INSTRUCTION:\n{instruction}"
    )
    if is_audio:
        parts = [(data, media_type)]
    else:
        # BMP -> PNG, animated GIF -> sampled frames (see media_images).
        try:
            parts, note = await prepare_image_bounded(data, media_type)
        except ImageTooLarge as too_large:
            return str(too_large)
        if note:
            prompt += f"\n\nNOTE: {note}"
    result = await agent.run(
        [prompt, *(BinaryContent(data=part, media_type=mt) for part, mt in parts)]
    )
    return result.output
