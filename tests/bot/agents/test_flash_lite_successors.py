"""Gemini 3.1 Flash Lite's direct pins moved to GPT-6 Luna on 2026-09-24.

Each of these call sites built its model by wire id rather than through the
catalog, so nothing else guards which provider they reach. Media reading splits:
the OpenAI Responses API takes no audio, so audio goes to Gemini 3.8 Flash.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIResponsesModel

from smarter_dev.bot.agents import image_prompt_reviewer
from smarter_dev.bot.agents import media_reader
from smarter_dev.web import media_read


@pytest.fixture(autouse=True)
def _keys_and_fresh_singletons(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    for var in (
        "MEDIA_READER_MODEL",
        "MEDIA_READER_AUDIO_MODEL",
        "IMAGE_REVIEWER_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(media_reader, "_media_reader_agent", None)
    monkeypatch.setattr(media_reader, "_audio_reader_agent", None)
    monkeypatch.setattr(image_prompt_reviewer, "_reviewer_agent", None)
    monkeypatch.setattr(media_read, "_media_agent", None)
    monkeypatch.setattr(media_read, "_audio_agent", None)


def test_media_reader_reads_images_with_gpt_6_luna():
    agent = media_reader.get_media_reader_agent()
    assert isinstance(agent.model, OpenAIResponsesModel)
    assert agent.model.model_name == "gpt-6-luna"


def test_media_reader_reads_audio_with_gemini_3_8_flash():
    agent = media_reader.get_audio_reader_agent()
    assert isinstance(agent.model, GoogleModel)
    assert agent.model.model_name == "gemini-3.8-flash"


@pytest.mark.parametrize(
    ("kind", "media_type", "expected"),
    [
        ("image", "image/png", "image"),
        ("audio", "audio/ogg", "audio"),
    ],
)
async def test_media_reader_dispatches_by_kind(monkeypatch, kind, media_type, expected):
    image_agent = MagicMock()
    image_agent.run = AsyncMock(return_value=MagicMock(output="image"))
    audio_agent = MagicMock()
    audio_agent.run = AsyncMock(return_value=MagicMock(output="audio"))
    monkeypatch.setattr(media_reader, "get_media_reader_agent", lambda: image_agent)
    monkeypatch.setattr(media_reader, "get_audio_reader_agent", lambda: audio_agent)

    result = await media_reader.describe_media(
        instruction="describe", data=b"x", media_type=media_type, url="u", kind=kind
    )

    assert result == expected


def test_worker_media_reader_matches_the_bot_one():
    image = media_read._get_media_agent()
    audio = media_read._get_audio_agent()
    assert isinstance(image.model, OpenAIResponsesModel)
    assert image.model.model_name == "gpt-6-luna"
    assert isinstance(audio.model, GoogleModel)
    assert audio.model.model_name == "gemini-3.8-flash"


async def test_worker_media_reader_dispatches_audio_to_gemini(monkeypatch):
    image_agent = MagicMock()
    image_agent.run = AsyncMock(return_value=MagicMock(output="image"))
    audio_agent = MagicMock()
    audio_agent.run = AsyncMock(return_value=MagicMock(output="audio"))
    monkeypatch.setattr(media_read, "_get_media_agent", lambda: image_agent)
    monkeypatch.setattr(media_read, "_get_audio_agent", lambda: audio_agent)

    audio = await media_read._describe_media(
        instruction="i", data=b"x", media_type="audio/mpeg", url="u", kind="audio"
    )
    image = await media_read._describe_media(
        instruction="i", data=b"x", media_type="image/png", url="u", kind="image"
    )

    assert (audio, image) == ("audio", "image")


def test_image_prompt_reviewer_uses_gpt_6_luna():
    agent = image_prompt_reviewer.get_image_prompt_reviewer()
    assert isinstance(agent.model, OpenAIResponsesModel)
    assert agent.model.model_name == "gpt-6-luna"


def test_title_and_blogging_agents_default_to_gpt_6_luna():
    from smarter_dev.web import title_agent
    from smarter_dev.web.blogging_agent import review_agent
    from smarter_dev.web.blogging_agent import summariser

    assert title_agent.TITLE_MODEL == "gpt-6-luna"
    assert review_agent.REVIEW_MODEL == "gpt-6-luna"
    assert summariser._LUNA_MODEL == "gpt-6-luna"
    assert summariser._MODEL_SETTINGS == {"openai_reasoning_effort": "low"}
