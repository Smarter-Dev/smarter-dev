"""Tests for the per-model chat-agent cache used by channel overrides."""

from __future__ import annotations

import pytest

import smarter_dev.bot.agents.chat_agent as chat_agent
from smarter_dev.bot.agents.chat_agent import _model_identity_prompt
from smarter_dev.bot.agents.chat_agent import get_chat_agent
from smarter_dev.bot.agents.chat_agent import resolved_reasoning_level


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


def test_reasoning_level_partitions_the_agent_cache():
    """Same model, different reasoning levels -> distinct agents; same level -> one."""
    _reset_cache()
    high = get_chat_agent("gpt-5.4", "high")
    high_again = get_chat_agent("gpt-5.4", "high")
    low = get_chat_agent("gpt-5.4", "low")
    default = get_chat_agent("gpt-5.4")
    assert high is high_again
    assert high is not low
    assert high is not default


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


def test_model_identity_prompt_names_model_and_resolved_reasoning():
    identity = _model_identity_prompt("gpt-5.4", "high")
    assert "GPT-5.4" in identity
    assert "`gpt-5.4`" in identity
    assert "**high**" in identity


def test_model_identity_prompt_reports_the_clamped_level():
    # Gemini's thinking_level tops out at "high" — the identity must reflect
    # what actually runs, not the raw stored choice.
    identity = _model_identity_prompt("gemini-3.1-flash-lite", "max")
    assert "**high**" in identity
    assert "max" not in identity


def test_model_identity_prompt_omits_reasoning_without_knob():
    identity = _model_identity_prompt("kimi-k2.6", "high")
    assert "Kimi K2.6" in identity
    assert "at reasoning level" not in identity


def test_model_identity_prompt_handles_adhoc_model_id():
    identity = _model_identity_prompt("some-unlisted-model", None)
    assert "`some-unlisted-model`" in identity
    assert "at reasoning level" not in identity


def test_model_identity_prompt_is_share_on_request_only():
    identity = _model_identity_prompt("gpt-5.4", None)
    assert "when someone asks" in identity
    assert "never volunteer" in identity


def test_agent_system_prompt_carries_its_model_identity():
    _reset_cache()
    agent = get_chat_agent("gpt-5.4", "high")
    prompt = "".join(agent._system_prompts)
    assert "## Your model" in prompt
    assert "`gpt-5.4`" in prompt
    assert "**high**" in prompt


def test_default_agent_system_prompt_names_the_default_model(monkeypatch):
    _reset_cache()
    monkeypatch.delenv(chat_agent.MODEL_ENV_VAR, raising=False)
    agent = get_chat_agent()
    prompt = "".join(agent._system_prompts)
    assert f"`{chat_agent.DEFAULT_MODEL}`" in prompt


def test_resolved_reasoning_level_returns_supported_choice():
    # gpt-5.4 supports "high" — an explicit supported pick passes through.
    assert resolved_reasoning_level("gpt-5.4", "high") == "high"


def test_resolved_reasoning_level_clamps_unsupported_choice():
    # Gemini's thinking_level tops out at "high"; "max" clamps down to it.
    assert resolved_reasoning_level("gemini-3.1-flash-lite", "max") == "high"


def test_resolved_reasoning_level_falls_back_to_model_default():
    # No explicit choice -> the catalog model's default_reasoning (MEDIUM here).
    assert resolved_reasoning_level("gpt-5.4", None) == "medium"


def test_resolved_reasoning_level_none_for_model_without_knob():
    # Kimi K2.6 has no reasoning ladder -> always None.
    assert resolved_reasoning_level("kimi-k2.6", "high") is None


def test_resolved_reasoning_level_none_for_adhoc_model():
    # A non-catalog ad-hoc model id maps to no ReasoningLevel.
    assert resolved_reasoning_level("some-unlisted-model", "high") is None


def test_resolved_reasoning_level_default_model_when_id_omitted(monkeypatch):
    # None model id resolves to the env/default (a catalog Gemini, default MEDIUM).
    monkeypatch.delenv(chat_agent.MODEL_ENV_VAR, raising=False)
    assert resolved_reasoning_level(None, None) == "medium"
