"""Tests for the tool-less WRITER agent used in two-stage chat mode.

Covers the pure ``build_writer_prompt`` renderer (message summaries, search
findings, verbatim questions, language, voice on/off) and ``get_writer_agent``
construction (no tools, ``WriterOutput`` output, per-model caching, and the
DigitalOcean ``PromptedOutput`` path).
"""

from __future__ import annotations

import pytest
from pydantic_ai import PromptedOutput

import smarter_dev.bot.agents.writer_agent as writer_agent
from smarter_dev.bot.agents.chat_models import WriterBrief
from smarter_dev.bot.agents.chat_models import WriterOutput
from smarter_dev.bot.agents.writer_agent import build_writer_prompt
from smarter_dev.bot.agents.writer_agent import get_writer_agent


@pytest.fixture(autouse=True)
def _provider_keys(monkeypatch):
    """Providers raise at construction without a key — feed dummies so building
    an ``Agent`` for any catalog model succeeds under test."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-google-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setenv("DIGITALOCEAN_INFERENCE_API_KEY", "test-do-key")


def _reset_cache():
    writer_agent._writer_agents.clear()


# --- build_writer_prompt ---------------------------------------------------


def test_prompt_renders_message_summaries_findings_and_questions():
    brief = WriterBrief(
        message_summaries=["Alice asked whether tail calls are optimized"],
        search_findings=["CPython does not do tail-call optimization"],
        questions=["does python optimize tail calls?"],
        response_language="english",
    )
    prompt = build_writer_prompt(brief)
    assert "Alice asked whether tail calls are optimized" in prompt
    assert "CPython does not do tail-call optimization" in prompt
    # The question is quoted verbatim.
    assert "does python optimize tail calls?" in prompt


def test_prompt_quotes_questions_verbatim():
    verbatim = "WHY does my regex `\\d+` not match '007'?!"
    brief = WriterBrief(
        message_summaries=["Bob is confused about a regex"],
        questions=[verbatim],
        response_language="english",
    )
    prompt = build_writer_prompt(brief)
    assert verbatim in prompt


def test_prompt_states_response_language():
    brief = WriterBrief(
        message_summaries=["Carlos preguntó cómo instalar Python"],
        questions=["¿cómo instalo python?"],
        response_language="spanish",
    )
    prompt = build_writer_prompt(brief)
    assert "spanish" in prompt.lower()


def test_prompt_omits_empty_sections():
    brief = WriterBrief(
        questions=["what is a monad?"],
        response_language="english",
    )
    prompt = build_writer_prompt(brief)
    # No findings and no summaries were provided; only the question should drive
    # the prompt. There should be no stray empty bullet lines.
    assert "\n- \n" not in prompt
    assert "what is a monad?" in prompt


def test_prompt_includes_voice_instruction_when_send_voice_true():
    brief = WriterBrief(
        message_summaries=["Dana asked for a spoken summary of async/await"],
        questions=["can you explain async await out loud?"],
        response_language="english",
        send_voice=True,
    )
    prompt = build_writer_prompt(brief)
    assert "voice" in prompt.lower()
    assert "voice_summary" in prompt


def test_prompt_omits_voice_instruction_when_send_voice_false():
    brief = WriterBrief(
        message_summaries=["Eve asked about list comprehensions"],
        questions=["how do list comprehensions work?"],
        response_language="english",
        send_voice=False,
    )
    prompt = build_writer_prompt(brief)
    assert "voice_summary" not in prompt


def test_build_writer_prompt_is_pure():
    """Rendering must not mutate the brief it is handed."""
    brief = WriterBrief(
        message_summaries=["Frank asked X"],
        search_findings=["finding"],
        questions=["what is X?"],
        response_language="english",
        send_voice=False,
    )
    before = brief.model_dump()
    build_writer_prompt(brief)
    assert brief.model_dump() == before


# --- get_writer_agent ------------------------------------------------------


def test_writer_agent_has_no_tools():
    _reset_cache()
    agent = get_writer_agent("gpt-5.4")
    assert list(agent._function_toolset.tools.keys()) == []


def test_writer_agent_output_is_writer_output():
    _reset_cache()
    agent = get_writer_agent("gpt-5.4")
    assert agent.output_type is WriterOutput


def test_writer_agent_caches_per_model_id():
    _reset_cache()
    first = get_writer_agent("gpt-5.4")
    second = get_writer_agent("gpt-5.4")
    assert first is second


def test_distinct_model_ids_yield_distinct_writer_agents():
    _reset_cache()
    gpt = get_writer_agent("gpt-5.4")
    gemini = get_writer_agent("gemini-3.1-pro")
    assert gpt is not gemini


def test_digitalocean_writer_agent_uses_prompted_output_and_no_tools():
    _reset_cache()
    agent = get_writer_agent("glm-5.2")
    assert isinstance(agent.output_type, PromptedOutput)
    assert list(agent._function_toolset.tools.keys()) == []
