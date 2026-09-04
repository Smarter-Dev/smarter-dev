"""Unit tests for provider routing — no network, providers are mocked."""

from __future__ import annotations

from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

# The bot-side module is a re-export shim; patch the real implementation or
# the rebinding lands on the alias and the router keeps its own globals.
from smarter_dev.shared import model_router
from smarter_dev.shared.model_router import build_model_for
from smarter_dev.shared.model_router import model_settings_for
from smarter_dev.shared.model_catalog import CatalogModel
from smarter_dev.shared.model_catalog import ModelProvider
from smarter_dev.shared.model_catalog import ReasoningLevel
from smarter_dev.shared.model_catalog import get_model

# Qwen3.5 397B is the ONLY model Digital Ocean still serves — Gemma, GLM and
# DeepSeek moved to author-precision OpenRouter endpoints on 2026-08-13 — so it
# stands in for both the plain-routing and the reasoning-knob cases here.
_DO_MODEL = get_model("qwen3-5-397b")
_DO_REASONING_MODEL = get_model("qwen3-5-397b")
_GOOGLE_MODEL = get_model("gemini-3-5-flash-lite")
_OPENAI_MODEL = get_model("gpt-5-4")
# Claude left the catalog on 2026-09-03, but ``build_model_for`` and
# ``model_settings_for`` still carry an Anthropic branch — the provider stays
# part of the routing vocabulary, and a future Claude entry must not have to
# rediscover it. Built here rather than looked up so the branch keeps its
# coverage without a catalog entry to back it.
_ANTHROPIC_MODEL = CatalogModel(
    key="claude-sonnet-5",
    label="Claude Sonnet 5",
    family="Claude",
    provider=ModelProvider.ANTHROPIC,
    model_id="claude-sonnet-5",
    supports_vision=True,
    reasoning_levels=(
        ReasoningLevel.LOW,
        ReasoningLevel.MEDIUM,
        ReasoningLevel.HIGH,
        ReasoningLevel.XHIGH,
        ReasoningLevel.MAX,
    ),
    default_reasoning=ReasoningLevel.HIGH,
)
_ANTHROPIC_NO_REASONING_MODEL = CatalogModel(
    key="claude-haiku-4-5",
    label="Claude Haiku 4.5",
    family="Claude",
    provider=ModelProvider.ANTHROPIC,
    model_id="claude-haiku-4-5",
    supports_vision=True,
)
_OPENCODE_ZEN_MODEL = get_model("kimi-k3")
_OPENROUTER_MODEL = get_model("qwen3-8-2-4t")
_OPENROUTER_REASONING_MODEL = get_model("grok-4-6")


def test_digitalocean_threads_base_url_and_key(monkeypatch):
    monkeypatch.setenv("DIGITALOCEAN_INFERENCE_API_KEY", "do-secret")
    base_url = model_router.get_settings().digitalocean_inference_base_url
    with (
        patch.object(model_router, "OpenAIChatModel") as chat_model,
        patch.object(model_router, "OpenAIProvider") as provider,
    ):
        build_model_for(_DO_MODEL)

    provider.assert_called_once_with(base_url=base_url, api_key="do-secret")
    chat_model.assert_called_once()
    args, kwargs = chat_model.call_args
    assert args == (_DO_MODEL.model_id,)
    assert kwargs["provider"] is provider.return_value
    # DO's endpoint quirks: forced tool choice 500s/stalls on several hosted
    # models, and Qwen requires system messages only at position 0.
    profile = kwargs["profile"]
    assert profile.openai_supports_tool_choice_required is False
    assert profile.openai_chat_supports_multiple_system_messages is False


def test_digitalocean_missing_key_falls_back_to_empty(monkeypatch):
    monkeypatch.delenv("DIGITALOCEAN_INFERENCE_API_KEY", raising=False)
    with (
        patch.object(model_router, "OpenAIChatModel"),
        patch.object(model_router, "OpenAIProvider") as provider,
    ):
        build_model_for(_DO_MODEL)
    _, kwargs = provider.call_args
    assert kwargs["api_key"] == ""


def test_digitalocean_uses_configured_base_url():
    custom = "https://inference.example.test/v1"
    fake_settings = MagicMock(digitalocean_inference_base_url=custom)
    with (
        patch.object(model_router, "get_settings", return_value=fake_settings),
        patch.object(model_router, "OpenAIChatModel"),
        patch.object(model_router, "OpenAIProvider") as provider,
    ):
        build_model_for(_DO_MODEL)
    _, kwargs = provider.call_args
    assert kwargs["base_url"] == custom


def test_google_model_reads_gemini_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gem-secret")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with (
        patch.object(model_router, "GoogleModel") as google_model,
        patch.object(model_router, "GoogleProvider") as provider,
    ):
        build_model_for(_GOOGLE_MODEL)
    provider.assert_called_once_with(api_key="gem-secret")
    google_model.assert_called_once_with(
        _GOOGLE_MODEL.model_id, provider=provider.return_value
    )


def test_google_model_falls_back_to_google_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "google-secret")
    with (
        patch.object(model_router, "GoogleModel"),
        patch.object(model_router, "GoogleProvider") as provider,
    ):
        build_model_for(_GOOGLE_MODEL)
    provider.assert_called_once_with(api_key="google-secret")


def test_openai_model_reads_openai_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "oai-secret")
    with (
        patch.object(model_router, "OpenAIResponsesModel") as responses_model,
        patch.object(model_router, "OpenAIProvider") as provider,
    ):
        build_model_for(_OPENAI_MODEL)
    provider.assert_called_once_with(api_key="oai-secret")
    responses_model.assert_called_once_with(
        _OPENAI_MODEL.model_id, provider=provider.return_value
    )


def test_anthropic_model_reads_anthropic_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-secret")
    with (
        patch.object(model_router, "AnthropicModel") as anthropic_model,
        patch.object(model_router, "AnthropicProvider") as provider,
    ):
        build_model_for(_ANTHROPIC_MODEL)
    provider.assert_called_once_with(api_key="ant-secret")
    anthropic_model.assert_called_once_with(
        _ANTHROPIC_MODEL.model_id, provider=provider.return_value
    )


def test_opencode_zen_threads_base_url_and_key(monkeypatch):
    monkeypatch.setenv("OPENCODE_ZEN_API_KEY", "zen-secret")
    base_url = model_router.get_settings().opencode_zen_base_url
    with (
        patch.object(model_router, "OpenAIChatModel") as chat_model,
        patch.object(model_router, "OpenAIProvider") as provider,
    ):
        build_model_for(_OPENCODE_ZEN_MODEL)

    provider.assert_called_once_with(base_url=base_url, api_key="zen-secret")
    assert chat_model.call_args.args[0] == _OPENCODE_ZEN_MODEL.model_id


def test_openrouter_model_reads_standard_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-secret")
    monkeypatch.setenv("OPEN_ROUTER", "legacy-secret")
    with (
        patch.object(model_router, "OpenAIChatModel") as chat_model,
        patch.object(model_router, "OpenRouterProvider") as provider,
    ):
        build_model_for(_OPENROUTER_MODEL)

    provider.assert_called_once_with(api_key="or-secret")
    chat_model.assert_called_once_with(
        _OPENROUTER_MODEL.model_id, provider=provider.return_value
    )


def test_openrouter_model_accepts_local_legacy_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPEN_ROUTER", "legacy-secret")
    with (
        patch.object(model_router, "OpenAIChatModel"),
        patch.object(model_router, "OpenRouterProvider") as provider,
    ):
        build_model_for(_OPENROUTER_MODEL)

    provider.assert_called_once_with(api_key="legacy-secret")


def test_openrouter_reasoning_model_builds_a_chat_model(monkeypatch):
    # Grok has a reasoning knob but takes the same OpenAI-compatible route as
    # every other OpenRouter model — no profile overrides, no base_url.
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-secret")
    with (
        patch.object(model_router, "OpenAIChatModel") as chat_model,
        patch.object(model_router, "OpenRouterProvider") as provider,
    ):
        build_model_for(_OPENROUTER_REASONING_MODEL)

    provider.assert_called_once_with(api_key="or-secret")
    chat_model.assert_called_once_with(
        _OPENROUTER_REASONING_MODEL.model_id, provider=provider.return_value
    )


def test_openrouter_routing_constraints_ride_on_every_request():
    """Endpoint constraints go out as the OpenRouter ``provider`` block.

    OpenRouter fronts one model id with endpoints at different precisions and
    picks by price, so a request that loses this block does not fail — it just
    quietly gets the most quantized endpoint. That makes the block a
    correctness concern, not a tuning knob.
    """
    glm = get_model("glm-5-3-flash")
    settings = model_settings_for(glm)
    provider = settings["extra_body"]["provider"]
    # China-linked/unvetted endpoints are excluded by name, not precision.
    assert provider["ignore"] == ["z-ai", "novita", "together", "wafer", "venice"]
    # A ceiling means a fallback can never silently cost more than the rate
    # llm_pricing records for this model.
    assert provider["max_price"] == {"prompt": 0.15, "completion": 0.50}
    # Reasoning still rides along on the same settings object.
    assert settings["openai_reasoning_effort"] == "medium"


def test_openrouter_routing_applies_without_a_reasoning_level():
    """A model with no reasoning knob still carries its endpoint constraints.

    Regression guard: settings used to short-circuit to None whenever no
    reasoning level resolved, which would have dropped the provider block for
    any OpenRouter model that has no effort ladder.
    """
    gemma = get_model("gemma-4-31b")
    assert gemma.supports_reasoning is False
    settings = model_settings_for(gemma)
    assert settings is not None
    assert "openai_reasoning_effort" not in settings
    assert settings["extra_body"]["provider"]["quantizations"] == ["bf16"]


def test_deepseek_prefers_authors_endpoint_and_excludes_digital_ocean():
    """DeepSeek declares no quantization, so precision comes from the source.

    An allow-list would exclude the authors' own endpoint along with everything
    else undeclared, so this one orders rather than filters — and names Digital
    Ocean specifically, which undercuts every declared fp4 endpoint of this
    model and so cannot plausibly be serving the full build.
    """
    provider = model_settings_for(get_model("deepseek-v4"))["extra_body"]["provider"]
    assert provider["order"] == ["deepseek"]
    assert provider["ignore"] == ["digitalocean"]
    assert "quantizations" not in provider
    # Every fallback stays at or under what the authors themselves charge.
    assert provider["max_price"] == {"prompt": 0.14, "completion": 0.28}
    # Fallbacks stay enabled: DeepSeek is actively rate-limiting heavy callers,
    # so pinning a single endpoint trades quantization risk for downtime risk.
    assert "allow_fallbacks" not in provider


def test_unconstrained_openrouter_model_sends_no_provider_block():
    """Qwen3.6 Plus has exactly one endpoint — its author's — so constraining it
    could only ever make it fail."""
    settings = model_settings_for(get_model("qwen3-6-plus"))
    assert "extra_body" not in settings
    assert settings["openai_reasoning_effort"] == "medium"


def test_openrouter_reasoning_effort_is_sent_as_openai_effort():
    default_settings = model_settings_for(_OPENROUTER_REASONING_MODEL)
    assert default_settings["openai_reasoning_effort"] == "medium"
    # xhigh is off Grok's ladder and clamps down to high rather than failing.
    clamped = model_settings_for(_OPENROUTER_REASONING_MODEL, ReasoningLevel.XHIGH)
    assert clamped["openai_reasoning_effort"] == "high"


def test_unhandled_provider_raises():
    bogus = CatalogModel(
        key="x",
        label="X",
        family="Kimi",
        provider=MagicMock(spec=ModelProvider),
        model_id="x",
    )
    with pytest.raises(ValueError, match="Unhandled provider"):
        build_model_for(bogus)


def test_model_settings_per_provider_uses_model_default():
    # Gemini 3.1 Flash Lite defaults to MEDIUM thinking.
    google_settings = model_settings_for(_GOOGLE_MODEL)
    assert google_settings["google_thinking_config"] == {"thinking_level": "MEDIUM"}
    # GPT-5.4 defaults to medium reasoning effort.
    openai_settings = model_settings_for(_OPENAI_MODEL)
    assert openai_settings["openai_reasoning_effort"] == "medium"
    # Open reasoning model routes reasoning through the chat-model settings.
    do_settings = model_settings_for(_DO_REASONING_MODEL)
    assert do_settings["openai_reasoning_effort"] == "medium"
    # Claude Sonnet 5 defaults to high effort with adaptive thinking.
    anthropic_settings = model_settings_for(_ANTHROPIC_MODEL)
    assert anthropic_settings["anthropic_thinking"] == {"type": "adaptive"}
    assert anthropic_settings["anthropic_effort"] == "high"
    # Claude Haiku 4.5 has no effort knob -> no settings at all.
    assert model_settings_for(_ANTHROPIC_NO_REASONING_MODEL) is None
    # Kimi K3 declares no effort contract on Zen -> no settings at all.
    assert model_settings_for(_OPENCODE_ZEN_MODEL) is None


def test_model_settings_applies_selected_reasoning_level():
    openai_settings = model_settings_for(_OPENAI_MODEL, ReasoningLevel.HIGH)
    assert openai_settings["openai_reasoning_effort"] == "high"
    google_settings = model_settings_for(_GOOGLE_MODEL, ReasoningLevel.LOW)
    assert google_settings["google_thinking_config"] == {"thinking_level": "LOW"}
    do_settings = model_settings_for(_DO_REASONING_MODEL, ReasoningLevel.HIGH)
    assert do_settings["openai_reasoning_effort"] == "high"
    anthropic_settings = model_settings_for(_ANTHROPIC_MODEL, ReasoningLevel.MAX)
    assert anthropic_settings["anthropic_effort"] == "max"


def test_model_settings_clamps_unsupported_reasoning_level():
    # Gemini caps at HIGH; requesting MAX clamps down to HIGH.
    google_settings = model_settings_for(_GOOGLE_MODEL, ReasoningLevel.MAX)
    assert google_settings["google_thinking_config"] == {"thinking_level": "HIGH"}
    # A model with no reasoning knob ignores any requested level.
    assert model_settings_for(_OPENCODE_ZEN_MODEL, ReasoningLevel.HIGH) is None
