"""Model names and settings for agents Skrift materializes in the worker.

A ``skrift.Agent`` takes a pydantic-ai model *name* (``anthropic:...``), not a
``Model``: Skrift builds the pydantic-ai agent lazily in the agent-worker, and
records each turn's usage under that name, which the usage ledger matches back
to the catalog (``agent_session_cleanup``). The modules defining those agents
are imported on the web tier too, which cannot afford pydantic-ai's memory, so
this module imports nothing from it: settings are plain dicts (pydantic-ai's
settings types are TypedDicts, so they are identical at runtime).

A name cannot carry what ``model_router.build_model_for`` puts on a direct
Claude model, so :func:`agent_model_settings` carries the cache breakpoints,
and the provider pydantic-ai builds for the name reads ``ANTHROPIC_API_KEY``
only (not the ``CLAUDE_API_KEY`` fallback). The output tool is never forced:
with thinking on, pydantic-ai asks a 5.5-generation Claude for native
structured output instead.
"""

from __future__ import annotations

from smarter_dev.shared.model_catalog import CatalogModel
from smarter_dev.shared.model_catalog import ModelProvider
from smarter_dev.shared.model_catalog import ReasoningLevel
from smarter_dev.shared.model_catalog import resolve_reasoning_level

# Every direct Claude cache breakpoint uses this TTL (see model_router), and
# llm_pricing prices direct Claude cache writes at its rate.
ANTHROPIC_CACHE_TTL = "1h"

_PREFIX = {
    ModelProvider.ANTHROPIC: "anthropic",
    ModelProvider.OPENAI: "openai-responses",
    ModelProvider.GOOGLE: "google-gla",
}


def agent_model_name(model: CatalogModel) -> str:
    """The pydantic-ai model name for ``model``, e.g. ``anthropic:claude-haiku-5-5``."""
    prefix = _PREFIX.get(model.provider)
    if prefix is None:
        raise ValueError(f"No model-name route for {model.provider!r}: {model.key}")
    return f"{prefix}:{model.model_id}"


def agent_model_settings(
    model: CatalogModel,
    reasoning_level: ReasoningLevel | None = None,
    *,
    cache_messages: bool = True,
) -> dict:
    """``model_router.model_settings_for`` as a plain dict, plus Claude caching.

    ``cache_messages=False`` is for an agent that answers in one request: its
    message breakpoint would only bill the prompt at the 1-hour write rate.
    """
    effective = resolve_reasoning_level(model, reasoning_level)
    if model.provider is ModelProvider.ANTHROPIC:
        settings: dict = {
            "anthropic_cache_tool_definitions": ANTHROPIC_CACHE_TTL,
            "anthropic_cache_instructions": ANTHROPIC_CACHE_TTL,
            "anthropic_cache_messages": ANTHROPIC_CACHE_TTL
            if cache_messages
            else False,
        }
        if effective is not None:
            # Adaptive thinking, never "disabled" (the 5.5 generation rejects it).
            settings["anthropic_thinking"] = {"type": "adaptive"}
            settings["anthropic_effort"] = effective.value
        return settings
    if effective is None:
        return {}
    if model.provider is ModelProvider.GOOGLE:
        return {"google_thinking_config": {"thinking_level": effective.value.upper()}}
    if model.provider is ModelProvider.OPENAI:
        return {"openai_reasoning_effort": effective.value}
    raise ValueError(f"No settings route for {model.provider!r}: {model.key}")
