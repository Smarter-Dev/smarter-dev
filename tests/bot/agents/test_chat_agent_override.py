"""Tests for the per-model chat-agent cache used by channel overrides."""

from __future__ import annotations

import pytest

import smarter_dev.bot.agents.chat_agent as chat_agent
from smarter_dev.bot.agents.chat_agent import get_chat_agent


@pytest.fixture(autouse=True)
def _provider_keys(monkeypatch):
    """Providers raise at construction without a key — feed dummies so building
    an ``Agent`` for any catalog model succeeds under test."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-google-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("DIGITALOCEAN_INFERENCE_API_KEY", "test-do-key")


def _reset_cache():
    chat_agent._chat_agents.clear()


def test_default_and_override_ids_return_agents(monkeypatch):
    _reset_cache()
    default_agent = get_chat_agent(None)
    override_agent = get_chat_agent("gemini-3.1-flash-lite")
    assert default_agent is not None
    assert override_agent is not None


def test_distinct_model_ids_yield_distinct_agents():
    _reset_cache()
    gemini = get_chat_agent("gemini-3.1-flash-lite")
    gpt = get_chat_agent("gpt-5.4")
    assert gemini is not gpt


def test_same_model_id_returns_cached_instance():
    _reset_cache()
    first = get_chat_agent("gpt-5.4")
    second = get_chat_agent("gpt-5.4")
    assert first is second


def test_none_resolves_to_env_default_and_is_singleton_equivalent(monkeypatch):
    """A no-override channel keeps reusing one default agent, and the default
    resolves to the same cached instance as passing its wire id explicitly."""
    _reset_cache()
    monkeypatch.delenv(chat_agent.MODEL_ENV_VAR, raising=False)
    default_first = get_chat_agent()
    default_second = get_chat_agent(None)
    explicit_default = get_chat_agent(chat_agent.DEFAULT_MODEL)
    assert default_first is default_second
    assert default_first is explicit_default
