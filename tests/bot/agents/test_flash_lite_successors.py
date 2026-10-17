"""Gemini 3.1 Flash Lite's direct pins moved to GPT-6 Luna on 2026-09-24, and
on to Claude Haiku 5.5 on 2026-10-17 (#199).

Each of these call sites names its model by wire id rather than a catalog key
lookup the catalog tests cover, so nothing else guards which provider they
reach. Media reading splits: Claude takes no audio, so audio goes to Gemini 3.8
Flash.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.google import GoogleModel

from smarter_dev.bot.agents import image_prompt_reviewer
from smarter_dev.bot.agents import media_reader
from smarter_dev.web import media_read


@pytest.fixture(autouse=True)
def _keys_and_fresh_singletons(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
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


# A one-request Claude agent at low effort: adaptive thinking, never Luna's
# openai_reasoning_effort, and no message cache breakpoint.
ONE_SHOT_LOW = {
    "anthropic_thinking": {"type": "adaptive"},
    "anthropic_effort": "low",
    "anthropic_cache_messages": False,
}


def test_media_reader_reads_images_with_claude_haiku():
    agent = media_reader.get_media_reader_agent()
    assert isinstance(agent.model, AnthropicModel)
    assert agent.model.model_name == "claude-haiku-5-5"
    assert agent.model_settings == ONE_SHOT_LOW


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
    assert isinstance(image.model, AnthropicModel)
    assert image.model.model_name == "claude-haiku-5-5"
    assert image.model_settings == ONE_SHOT_LOW
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


def test_image_prompt_reviewer_uses_claude_haiku():
    agent = image_prompt_reviewer.get_image_prompt_reviewer()
    assert isinstance(agent.model, AnthropicModel)
    assert agent.model.model_name == "claude-haiku-5-5"
    assert agent.model_settings == ONE_SHOT_LOW
    # Its verdict is structured output, and the output tool is never forced.
    assert agent.model.profile["supports_forced_tool_choice"] is False


def test_an_unknown_override_fails_instead_of_guessing_a_provider(monkeypatch):
    monkeypatch.setenv("IMAGE_REVIEWER_MODEL", "gpt-6-lunar")
    with pytest.raises(ValueError):
        image_prompt_reviewer.get_image_prompt_reviewer()


def test_skrift_agents_run_on_claude_haiku_with_its_own_settings():
    """Title, Resources and blogging agents take a model *name* (Skrift builds
    the pydantic-ai agent in the worker), so their settings carry the cache
    breakpoints; tool loops keep the message breakpoint, single requests do not."""
    from smarter_dev.web import resources_agent
    from smarter_dev.web import title_agent
    from smarter_dev.web.blogging_agent import brainstorm_agent
    from smarter_dev.web.blogging_agent import research_agent
    from smarter_dev.web.blogging_agent import scout_agent
    from smarter_dev.web.blogging_agent import synthesis_agent

    def settings(effort: str, *, cache_messages: bool) -> dict:
        return {
            "anthropic_cache_tool_definitions": "1h",
            "anthropic_cache_instructions": "1h",
            "anthropic_cache_messages": "1h" if cache_messages else False,
            "anthropic_thinking": {"type": "adaptive"},
            "anthropic_effort": effort,
        }

    one_shot = [
        (title_agent.title_agent, "low"),
        (resources_agent.reframer_agent, "medium"),
        (resources_agent.author_agent, "low"),
        (brainstorm_agent.brainstorm_agent, "medium"),
        (synthesis_agent.synthesis_agent, "medium"),
    ]
    tool_loops = [
        (resources_agent.researcher_agent, "medium"),
        (resources_agent.gap_filler_agent, "low"),
        (scout_agent.scout_agent, "medium"),
        (research_agent.research_agent, "medium"),
        (research_agent.researcher_subagent, "medium"),
    ]
    for agents, cache_messages in ((one_shot, False), (tool_loops, True)):
        for agent, effort in agents:
            assert agent.model == "anthropic:claude-haiku-5-5", agent.skrift_name
            assert agent._init_kwargs["model_settings"] == settings(
                effort, cache_messages=cache_messages
            ), agent.skrift_name


def test_blogging_summariser_runs_on_claude_haiku():
    from smarter_dev.web.blogging_agent import summariser

    agent = summariser._agent("prompt", str)
    assert isinstance(agent.model, AnthropicModel)
    assert agent.model.model_name == "claude-haiku-5-5"
    assert agent.model_settings == ONE_SHOT_LOW
