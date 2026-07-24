"""Tests for the two-stage WORKER agent factory.

The worker mirrors ``get_chat_agent`` — same tools, same history processors,
same model resolution and DigitalOcean PromptedOutput handling — but emits a
``BriefingDecision`` (a context brief) instead of writing the reply itself.
"""

from __future__ import annotations

import pytest
from pydantic_ai import PromptedOutput

import smarter_dev.bot.agents.chat_agent as chat_agent
from smarter_dev.bot.agents.chat_agent import get_chat_agent
from smarter_dev.bot.agents.chat_agent import get_worker_agent
from smarter_dev.bot.agents.chat_models import BriefingDecision
from smarter_dev.bot.agents.chat_models import TurnDecision
from smarter_dev.bot.agents.chat_tools import chat_tool_functions
from smarter_dev.bot.agents.handler_tools import handler_tool_functions


@pytest.fixture(autouse=True)
def _provider_keys(monkeypatch):
    """Providers raise at construction without a key — feed dummies so building
    an ``Agent`` for any catalog model succeeds under test."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-google-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("DIGITALOCEAN_INFERENCE_API_KEY", "test-do-key")


def _reset_caches():
    chat_agent._chat_agents.clear()
    chat_agent._worker_agents.clear()


def _expected_tool_names() -> set[str]:
    return {
        fn.__name__ for fn in chat_tool_functions() + handler_tool_functions()
    }


def test_worker_agent_outputs_briefing_decision():
    _reset_caches()
    agent = get_worker_agent("gpt-5.4")
    assert agent._output_type is BriefingDecision


def test_worker_agent_do_model_uses_prompted_briefing_decision():
    """DigitalOcean-hosted models get PromptedOutput wrapping BriefingDecision,
    exactly like the chat agent does for its own output type."""
    _reset_caches()
    agent = get_worker_agent("kimi-k2.6")
    assert isinstance(agent._output_type, PromptedOutput)
    assert agent._output_type.outputs is BriefingDecision


def test_worker_agent_has_same_tools_as_chat_agent():
    _reset_caches()
    agent = get_worker_agent("gpt-5.4")
    assert set(agent._function_toolset.tools.keys()) == _expected_tool_names()


def test_worker_agent_uses_worker_prompt_not_chat_prompt():
    _reset_caches()
    worker = get_worker_agent("gpt-5.4")
    chat = get_chat_agent("gpt-5.4")
    assert worker._system_prompts[0] == chat_agent.WORKER_SYSTEM_PROMPT
    assert worker._system_prompts[0] != chat._system_prompts[0]


def test_worker_prompt_file_loads_and_is_non_empty():
    assert isinstance(chat_agent.WORKER_SYSTEM_PROMPT, str)
    assert chat_agent.WORKER_SYSTEM_PROMPT.strip()


def test_worker_prompt_requires_comprehensive_self_contained_brief():
    """The writer never sees history, so the prompt must tell the worker to
    make the brief self-contained and to pull in whatever the questions
    reference (the gap the two-stage eval surfaced on 'say that back')."""
    prompt = chat_agent.WORKER_SYSTEM_PROMPT.lower()
    assert "self-contained" in prompt
    assert "resolve every reference" in prompt
    # It must call for carrying referenced content in, not just naming it.
    assert "quote the exact text" in prompt or "quote exact text" in prompt


def test_same_model_and_reasoning_returns_cached_worker():
    _reset_caches()
    first = get_worker_agent("gpt-5.4", "high")
    second = get_worker_agent("gpt-5.4", "high")
    assert first is second


def test_reasoning_level_partitions_worker_cache():
    _reset_caches()
    high = get_worker_agent("gpt-5.4", "high")
    low = get_worker_agent("gpt-5.4", "low")
    default = get_worker_agent("gpt-5.4")
    assert high is not low
    assert high is not default


def test_distinct_model_ids_yield_distinct_workers():
    _reset_caches()
    gemini = get_worker_agent("gemini-3.1-flash-lite")
    gpt = get_worker_agent("gpt-5.4")
    assert gemini is not gpt


def test_worker_and_chat_caches_are_independent():
    """Building a worker for a model must not hand back its chat agent, and the
    chat agent's output type stays TurnDecision (no regression)."""
    _reset_caches()
    worker = get_worker_agent("gpt-5.4")
    chat = get_chat_agent("gpt-5.4")
    assert worker is not chat
    assert chat._output_type is TurnDecision


def test_none_model_resolves_to_env_default(monkeypatch):
    _reset_caches()
    monkeypatch.delenv(chat_agent.MODEL_ENV_VAR, raising=False)
    default_first = get_worker_agent()
    default_second = get_worker_agent(None)
    explicit_default = get_worker_agent(chat_agent.DEFAULT_MODEL)
    assert default_first is default_second
    assert default_first is explicit_default
