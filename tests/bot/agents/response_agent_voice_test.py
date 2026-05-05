"""Tests for response-agent voice mode behavior."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

from smarter_dev.bot.agents import response_agent as response_agent_module
from smarter_dev.bot.agents.response_agent import ResponseAgent


@contextmanager
def fake_dspy_context(*args, **kwargs):
    yield


class FakeReAct:
    captured_kwargs: dict | None = None

    def __init__(self, *args, **kwargs):
        pass

    async def acall(self, **kwargs):
        FakeReAct.captured_kwargs = kwargs
        return SimpleNamespace(
            response="text model response",
            continue_watching=False,
            watching_for="",
            wait_duration=60,
            update_frequency="1m",
        )


async def test_voice_mode_adds_word_budget_and_captures_response(monkeypatch):
    agent = ResponseAgent()
    captured_parts: list[str] | None = None

    def fake_create_response_tools(*, voice_response_parts=None, **kwargs):
        nonlocal captured_parts
        captured_parts = voice_response_parts
        voice_response_parts.append("captured voice response")
        return [], []

    monkeypatch.setattr(response_agent_module, "create_response_tools", fake_create_response_tools)
    monkeypatch.setattr(response_agent_module.dspy, "context", fake_dspy_context)
    monkeypatch.setattr(response_agent_module.dspy, "ReAct", FakeReAct)
    monkeypatch.setattr(
        "smarter_dev.shared.config.get_settings",
        Mock(
            return_value=SimpleNamespace(
                voice_words_per_minute=120,
                voice_max_duration_seconds=15,
            )
        ),
    )

    success, output = await agent.generate_response(
        bot=Mock(),
        channel_id=123,
        guild_id=456,
        relevant_messages="User asked for a voice reply",
        intent="Answer briefly",
        context_summary="Test context",
        channel_info={},
        users=[],
        me_info={},
        request_id="test",
        voice_mode=True,
    )

    assert success is True
    assert captured_parts == ["captured voice response"]
    assert output.response_text == "captured voice response"
    assert "under 30 words" in FakeReAct.captured_kwargs["personality_hint"]


async def test_text_mode_does_not_add_voice_budget(monkeypatch):
    agent = ResponseAgent()

    def fake_create_response_tools(*, voice_response_parts=None, **kwargs):
        assert voice_response_parts is None
        return [], []

    monkeypatch.setattr(response_agent_module, "create_response_tools", fake_create_response_tools)
    monkeypatch.setattr(response_agent_module.dspy, "context", fake_dspy_context)
    monkeypatch.setattr(response_agent_module.dspy, "ReAct", FakeReAct)

    success, output = await agent.generate_response(
        bot=Mock(),
        channel_id=123,
        guild_id=456,
        relevant_messages="User asked for a text reply",
        intent="Answer normally",
        context_summary="Test context",
        channel_info={},
        users=[],
        me_info={},
        request_id="test",
        personality_hint="dry wit",
        voice_mode=False,
    )

    assert success is True
    assert output.response_text == "text model response"
    assert FakeReAct.captured_kwargs["personality_hint"] == "dry wit"
