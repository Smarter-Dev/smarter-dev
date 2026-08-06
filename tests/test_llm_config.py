"""Unit tests for llm_config provider/key resolution (dspy path)."""

from __future__ import annotations

from smarter_dev import llm_config


def test_default_model_routes_via_openrouter():
    # Luna moved to OpenRouter 2026-08-06 for the 50% rate; litellm wants the
    # openrouter/<upstream>/<model> form.
    assert llm_config.DEFAULT_LLM_MODEL == "openrouter/openai/gpt-5.6-luna"


def test_provider_detected_from_openrouter_prefix():
    assert (
        llm_config._get_provider_from_model("openrouter/openai/gpt-5.6-luna")
        == "openrouter"
    )
    # Direct ids keep their existing detection.
    assert llm_config._get_provider_from_model("gpt-5.4") == "openai"
    assert llm_config._get_provider_from_model("claude-sonnet-5") == "anthropic"


def test_openrouter_api_key_resolution(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("OPEN_ROUTER", "legacy-key")
    assert (
        llm_config._get_api_key_for_model("openrouter/openai/gpt-5.6-luna")
        == "or-key"
    )


def test_openrouter_legacy_env_key_fallback(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPEN_ROUTER", "legacy-key")
    assert (
        llm_config._get_api_key_for_model("openrouter/openai/gpt-5.6-luna")
        == "legacy-key"
    )


def test_reasoning_model_detection_survives_openrouter_prefix():
    assert llm_config._is_reasoning_model("openrouter/openai/gpt-5.6-luna")
    assert llm_config._is_reasoning_model("openai/gpt-5.6-luna")
    assert not llm_config._is_reasoning_model("openrouter/x-ai/grok-4.5")
