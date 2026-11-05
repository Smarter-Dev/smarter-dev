"""Instruction-guided image/audio reader (Claude Haiku 5.5 for images, Gemini for audio).

Sibling to ``web_summarizer`` (which handles text): when ``web_read`` is given
an image or audio URL, it downloads the bytes and hands them here for an
instruction-shaped description. The chat agent never receives the raw bytes —
only the description — and the same refuse-if-not-meaningful contract applies.

Images moved from Gemini 3.1 Flash Lite to GPT-6 Luna on 2026-09-24, and on to
Claude Haiku 5.5 on 2026-10-17 (#199). Audio could follow neither, since
neither API takes audio input, so audio goes to Gemini 3.8 Flash, the one
Gemini Flash still in use. Claude, like Luna, takes PNG, JPEG, WebP and GIF
but not BMP, and reads only an animated GIF's first frame, so
``shared.media_images`` converts those.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai import BinaryContent
from pydantic_ai.models import Model
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.google import GoogleModelSettings
from pydantic_ai.providers.google import GoogleProvider

from smarter_dev.bot.services.usage_reporter import metered
from smarter_dev.shared.media_images import ImageTooLarge
from smarter_dev.shared.media_images import prepare_image_bounded
from smarter_dev.shared.media_reads import MAX_SEND_BYTES
from smarter_dev.shared.media_reads import SpooledImage
from smarter_dev.shared.media_reads import too_large_to_send
from smarter_dev.shared.model_catalog import CatalogModel
from smarter_dev.shared.model_catalog import ReasoningLevel
from smarter_dev.shared.model_catalog import catalog_model_for_id
from smarter_dev.shared.model_router import build_model_for
from smarter_dev.shared.model_router import model_settings_for

logger = logging.getLogger(__name__)

# Images: a catalog wire id (or any Claude id), routed by the model router.
DEFAULT_MODEL = "claude-haiku-5-5"
MODEL_ENV_VAR = "MEDIA_READER_MODEL"
# Audio: a Gemini wire id.
DEFAULT_AUDIO_MODEL = "gemini-3.8-flash"
AUDIO_MODEL_ENV_VAR = "MEDIA_READER_AUDIO_MODEL"

SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "media_reader.md").read_text(
    encoding="utf-8"
)


_media_reader_agent: Agent[None, str] | None = None
_audio_reader_agent: Agent[None, str] | None = None


def _image_model() -> CatalogModel:
    return catalog_model_for_id(os.getenv(MODEL_ENV_VAR, DEFAULT_MODEL))


def _build_model() -> Model:
    return build_model_for(_image_model())


def _build_audio_model() -> GoogleModel:
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
    model_id = os.getenv(AUDIO_MODEL_ENV_VAR, DEFAULT_AUDIO_MODEL)
    return GoogleModel(model_id, provider=GoogleProvider(api_key=api_key))


def get_media_reader_agent() -> Agent[None, str]:
    """Return the singleton image reader agent, building it on first use."""
    global _media_reader_agent
    if _media_reader_agent is None:
        model = _image_model()
        _media_reader_agent = Agent(
            _build_model(),
            output_type=str,
            system_prompt=SYSTEM_PROMPT,
            # One request per image; the image itself is never re-read.
            model_settings=model_settings_for(
                model, ReasoningLevel.LOW, cache_messages=False
            ),
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
    *,
    instruction: str,
    data: bytes | SpooledImage,
    media_type: str,
    url: str,
    kind: str,
    usage_fields: dict | None = None,
) -> str:
    """Describe an image (Claude Haiku 5.5) or audio clip (Gemini) per ``instruction``.

    An image may come as a ``SpooledImage`` (over MAX_DOWNLOAD_BYTES, on disk),
    downsampled in a bounded child and deleted by ``prepare_image_bounded``.
    The read goes to the usage ledger as ``chat_media_reader``, attributed by
    ``usage_fields`` (``metered``'s guild_id / channel_id / engagement_id).
    """
    is_audio = kind == "audio" or media_type.startswith("audio/")
    agent = get_audio_reader_agent() if is_audio else get_media_reader_agent()
    prompt = (
        f"URL: {url}\n"
        f"KIND: {kind}\n\n"
        f"INSTRUCTION:\n{instruction}"
    )
    if is_audio:
        if len(data) > MAX_SEND_BYTES:
            return too_large_to_send(len(data), "audio clip")
        parts = [(data, media_type)]
    else:
        # BMP -> PNG, animated GIF -> sampled frames (see media_images).
        try:
            parts, note = await prepare_image_bounded(data, media_type)
        except ImageTooLarge as too_large:
            return str(too_large)
        if note:
            prompt += f"\n\nNOTE: {note}"
    async with metered("chat_media_reader", **(usage_fields or {})) as meter:
        result = await agent.run(
            [prompt, *(BinaryContent(data=part, media_type=mt) for part, mt in parts)],
            model=meter.wrap(agent.model),
        )
    return result.output
