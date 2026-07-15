"""Unit tests for provider routing — no network, providers are mocked."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from smarter_dev.bot.agents import model_router
from smarter_dev.bot.agents.model_catalog import (
    CatalogModel,
    ModelProvider,
    get_model,
)
from smarter_dev.bot.agents.model_router import (
    build_model_for,
    model_settings_for,
)

_DO_MODEL = get_model("kimi-k2")
_GOOGLE_MODEL = get_model("gemini-3-1-flash-lite")
_OPENAI_MODEL = get_model("gpt-5-4")


def test_digitalocean_threads_base_url_and_key(monkeypatch):
    monkeypatch.setenv("DIGITALOCEAN_INFERENCE_API_KEY", "do-secret")
    base_url = model_router.get_settings().digitalocean_inference_base_url
    with (
        patch.object(model_router, "OpenAIChatModel") as chat_model,
        patch.object(model_router, "OpenAIProvider") as provider,
    ):
        build_model_for(_DO_MODEL)

    provider.assert_called_once_with(base_url=base_url, api_key="do-secret")
    chat_model.assert_called_once_with(
        _DO_MODEL.model_id, provider=provider.return_value
    )


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


def test_model_settings_per_provider():
    assert model_settings_for(_DO_MODEL) is None
    google_settings = model_settings_for(_GOOGLE_MODEL)
    assert google_settings["google_thinking_config"] == {"thinking_level": "MEDIUM"}
    openai_settings = model_settings_for(_OPENAI_MODEL)
    assert openai_settings["openai_reasoning_effort"] == "low"
