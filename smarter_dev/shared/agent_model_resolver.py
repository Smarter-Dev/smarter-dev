"""Builds pydantic-ai models from ``skrift_models`` names with explicit keys.

Imports pydantic-ai, so only the worker (and the bot) load it; the web tier
reaches it through ``skrift_models.agent_capabilities`` lazily.
"""

from __future__ import annotations

from typing import Any

from smarter_dev.shared.credentials import require_credential


def resolve_model_name(model_id: str) -> Any:
    """The pydantic-ai ``Model`` for an :func:`agent_model_name` name, built
    with its provider key from ``get_credential`` (never the SDK's env read)."""
    prefix, _, name = model_id.partition(":")
    if prefix == "anthropic":
        from pydantic_ai.models.anthropic import AnthropicModel
        from pydantic_ai.providers.anthropic import AnthropicProvider

        return AnthropicModel(
            name, provider=AnthropicProvider(api_key=require_credential("anthropic"))
        )
    if prefix == "openai-responses":
        from pydantic_ai.models.openai import OpenAIResponsesModel
        from pydantic_ai.providers.openai import OpenAIProvider

        return OpenAIResponsesModel(
            name, provider=OpenAIProvider(api_key=require_credential("openai"))
        )
    if prefix == "google-gla":
        from pydantic_ai.models.google import GoogleModel
        from pydantic_ai.providers.google import GoogleProvider

        return GoogleModel(
            name, provider=GoogleProvider(api_key=require_credential("gemini"))
        )
    raise ValueError(f"No model resolver for {model_id!r}")


def model_resolution_capability() -> Any:
    from pydantic_ai.capabilities import ResolveModelId

    return ResolveModelId(lambda _ctx, model_id: resolve_model_name(model_id))
