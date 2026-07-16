"""Build a Pydantic AI ``Model`` for any catalog model.

Routing by :class:`~smarter_dev.shared.model_catalog.ModelProvider`:

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
    OpenAIChatModelSettings,
    OpenAIResponsesModel,
    OpenAIResponsesModelSettings,
)
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

from smarter_dev.shared.model_catalog import (
    CatalogModel,
    ModelProvider,
    ReasoningLevel,
    resolve_reasoning_level,
)
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
            # DO's endpoint is only mostly OpenAI-compatible: several hosted
            # models (Kimi, GLM) return HTTP 500 for tool_choice="required"
            # and Qwen stalls on it, so never force tool choice; Qwen also
            # rejects system messages anywhere but position 0, so pydantic_ai
            # must merge them (PromptedOutput otherwise appends its schema
            # instructions as a second system message).
            profile=OpenAIModelProfile(
                openai_supports_tool_choice_required=False,
                openai_chat_supports_multiple_system_messages=False,
            ),
        )
    raise ValueError(f"Unhandled provider: {model.provider!r}")


def model_settings_for(
    model: CatalogModel, reasoning_level: ReasoningLevel | None = None
) -> ModelSettings | None:
    """Return per-provider model settings for ``model`` at ``reasoning_level``.

    ``reasoning_level`` is the admin's per-channel choice (or ``None`` to use the
    model's default). It is mapped onto the model's supported levels by
    :func:`resolve_reasoning_level`, so an invalid pick is clamped rather than
    passed through verbatim. A model with no reasoning knob (or one that resolves
    to no level) gets ``None`` — the provider's own default applies.
    """
    effective = resolve_reasoning_level(model, reasoning_level)
    if effective is None:
        return None
    if model.provider is ModelProvider.GOOGLE:
        # Gemini's thinking_level enum is upper-cased on the wire.
        return GoogleModelSettings(
            google_thinking_config={"thinking_level": effective.value.upper()},
        )
    if model.provider is ModelProvider.OPENAI:
        return OpenAIResponsesModelSettings(openai_reasoning_effort=effective.value)
    return OpenAIChatModelSettings(openai_reasoning_effort=effective.value)
