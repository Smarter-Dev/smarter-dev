"""Build a Pydantic AI ``Model`` for any catalog model.

Routing by :class:`~smarter_dev.bot.agents.model_catalog.ModelProvider`:

- ``GOOGLE``       -> ``GoogleModel`` + ``GoogleProvider`` (Gemini keys).
- ``OPENAI``       -> ``OpenAIResponsesModel`` + ``OpenAIProvider`` (OpenAI key).
- ``DIGITALOCEAN`` -> ``OpenAIChatModel`` pointed at DO's OpenAI-compatible
  serverless-inference Chat Completions endpoint. DO exposes
  ``/v1/chat/completions`` (not OpenAI's Responses API), so the chat model —
  not the responses model — is used, with a ``base_url`` override.

API keys are read from the environment (matching the existing convention in
``chat_agent.py`` and ``llm_config.py``), not from the settings class. The DO
base URL *is* configurable via ``Settings.digitalocean_inference_base_url`` so
the endpoint can be overridden without a code change.
"""

from __future__ import annotations

import os

from pydantic_ai.models import Model
from pydantic_ai.models.google import GoogleModel, GoogleModelSettings
from pydantic_ai.models.openai import (
    OpenAIChatModel,
    OpenAIResponsesModel,
    OpenAIResponsesModelSettings,
)
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

from smarter_dev.bot.agents.model_catalog import CatalogModel, ModelProvider
from smarter_dev.shared.config import get_settings

DIGITALOCEAN_API_KEY_ENV_VAR = "DIGITALOCEAN_INFERENCE_API_KEY"


def build_model_for(model: CatalogModel) -> Model:
    """Return a configured Pydantic AI ``Model`` for ``model``."""
    if model.provider is ModelProvider.GOOGLE:
        api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
        return GoogleModel(model.model_id, provider=GoogleProvider(api_key=api_key))
    if model.provider is ModelProvider.OPENAI:
        return OpenAIResponsesModel(
            model.model_id,
            provider=OpenAIProvider(api_key=os.getenv("OPENAI_API_KEY") or ""),
        )
    if model.provider is ModelProvider.DIGITALOCEAN:
        settings = get_settings()
        return OpenAIChatModel(
            model.model_id,
            provider=OpenAIProvider(
                base_url=settings.digitalocean_inference_base_url,
                api_key=os.getenv(DIGITALOCEAN_API_KEY_ENV_VAR) or "",
            ),
        )
    raise ValueError(f"Unhandled provider: {model.provider!r}")


def model_settings_for(model: CatalogModel) -> ModelSettings | None:
    """Return per-provider model settings, mirroring ``chat_agent``.

    Digital Ocean models get no reasoning/thinking config (``None``).
    """
    if model.provider is ModelProvider.GOOGLE:
        return GoogleModelSettings(
            google_thinking_config={"thinking_level": "MEDIUM"},
        )
    if model.provider is ModelProvider.OPENAI:
        return OpenAIResponsesModelSettings(openai_reasoning_effort="low")
    return None
