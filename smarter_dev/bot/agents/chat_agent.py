"""Discord chat agent — single Pydantic AI agent driving every conversation turn.

Replaces the old classification/evaluation/response trio. One agent, one model
(Gemini 3.1 Flash Lite on medium thinking by default; override with the
CHAT_AGENT_MODEL env var — "gpt-"/"openai/" ids route to OpenAI), one
structured return type.

Usage:
    from smarter_dev.bot.agents.chat_input_format import render_input_xml
    agent = get_chat_agent()
    result = await agent.run(
        user_prompt=render_input_xml(agent_input),
        deps=ChatDeps(bot=bot, channel_id=ch, guild_id=g),
    )
    output = result.output  # TurnDecision (response: ResponseBody | None)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai import PromptedOutput
from pydantic_ai.models import Model
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.google import GoogleModelSettings
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.settings import ModelSettings

from smarter_dev.bot.agents.chat_compaction import compact_history
from smarter_dev.bot.agents.chat_models import AgentReturn
from smarter_dev.bot.agents.chat_tools import ChatDeps
from smarter_dev.bot.agents.chat_tools import chat_tool_functions
from smarter_dev.bot.agents.handler_tools import handler_tool_functions
from smarter_dev.shared.model_catalog import MODEL_CATALOG
from smarter_dev.shared.model_catalog import CatalogModel
from smarter_dev.shared.model_catalog import ModelProvider
from smarter_dev.shared.model_catalog import parse_reasoning_level
from smarter_dev.bot.agents.model_router import build_model_for
from smarter_dev.bot.agents.model_router import model_settings_for

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-3.1-flash-lite"
MODEL_ENV_VAR = "CHAT_AGENT_MODEL"

SYSTEM_PROMPT = (
    Path(__file__).parent / "prompts" / "chat_agent.md"
).read_text(encoding="utf-8")


def _model_id() -> str:
    return os.getenv(MODEL_ENV_VAR, DEFAULT_MODEL)


def _catalog_model_for_id(model_id: str) -> CatalogModel | None:
    """Return the catalog model whose wire ``model_id`` matches, if any."""
    for model in MODEL_CATALOG:
        if model.model_id == model_id:
            return model
    return None


def build_agent_model(model_id: str) -> Model:
    """Build a Pydantic AI model for ``model_id``.

    Catalog models (the admin-selectable set) route through the shared
    :mod:`model_router`; anything else falls back to the historical prefix
    logic so ad-hoc ``CHAT_AGENT_MODEL`` ids keep working. Later stages call
    this to realize a per-channel override.
    """
    catalog_model = _catalog_model_for_id(model_id)
    if catalog_model is not None:
        return build_model_for(catalog_model)
    if model_id.startswith(("gpt-", "openai/")):
        from pydantic_ai.models.openai import OpenAIResponsesModel
        from pydantic_ai.providers.openai import OpenAIProvider

        return OpenAIResponsesModel(
            model_id.removeprefix("openai/"),
            provider=OpenAIProvider(api_key=os.getenv("OPENAI_API_KEY") or ""),
        )
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
    return GoogleModel(model_id, provider=GoogleProvider(api_key=api_key))


def _output_type_for(model_id: str) -> type[AgentReturn] | PromptedOutput:
    """Structured-output mode for ``model_id``.

    Gemini/OpenAI models return ``AgentReturn`` via pydantic_ai's default
    tool-call output. DigitalOcean-hosted models get ``PromptedOutput`` (schema
    in the prompt, JSON text back): DO's OpenAI-compatible endpoint is uneven —
    tool_choice="required" 500s on Kimi/GLM and stalls Qwen, and with "auto"
    the reasoning models answer in plain text instead of calling the output
    tool; ``response_format`` json_schema is likewise only partially supported.
    Prompted JSON is the one mode every hosted model handles.
    """
    catalog_model = _catalog_model_for_id(model_id)
    if catalog_model is not None and catalog_model.provider is ModelProvider.DIGITALOCEAN:
        return PromptedOutput(AgentReturn)
    return AgentReturn


def _model_settings_for(
    model_id: str, reasoning_level: str | None = None
) -> ModelSettings | None:
    """Per-provider model settings for ``model_id``.

    Catalog models route through the shared :mod:`model_router` so a per-channel
    override gets the reasoning/thinking config for ``reasoning_level`` (clamped
    to what the model supports, or the model default when unset); anything else
    falls back to the historical prefix logic used for ad-hoc
    ``CHAT_AGENT_MODEL`` ids.
    """
    catalog_model = _catalog_model_for_id(model_id)
    if catalog_model is not None:
        return model_settings_for(catalog_model, parse_reasoning_level(reasoning_level))
    if model_id.startswith(("gpt-", "openai/")):
        from pydantic_ai.models.openai import OpenAIResponsesModelSettings

        return OpenAIResponsesModelSettings(openai_reasoning_effort="low")
    return GoogleModelSettings(
        google_thinking_config={"thinking_level": "MEDIUM"},
    )


# One agent per resolved (model id, reasoning level). A per-channel override
# means we can no longer share a single global agent, so agents are cached by the
# wire model id *and* reasoning level they were built for — two channels on the
# same model but different reasoning levels need distinct model_settings.
# ``get_chat_agent(None)`` resolves to the env/default id with no reasoning
# override, so a channel with no override keeps returning the same instance.
_chat_agents: dict[tuple[str, str | None], Agent[ChatDeps, AgentReturn]] = {}


def get_chat_agent(
    model_id: str | None = None, reasoning_level: str | None = None
) -> Agent[ChatDeps, AgentReturn]:
    """Return (building on first use) the chat agent for ``model_id``.

    ``model_id`` is the provider wire id (e.g. a catalog ``CatalogModel.model_id``);
    ``None`` uses the env/default model. ``reasoning_level`` is the channel
    override's chosen level (or ``None`` for the model default). Agents are cached
    per resolved (id, reasoning) pair, so a given configuration always reuses one
    ``Agent`` and the default path is singleton-equivalent to the previous
    behaviour.
    """
    resolved_id = model_id or _model_id()
    cache_key = (resolved_id, reasoning_level)
    agent = _chat_agents.get(cache_key)
    if agent is None:
        agent = Agent(
            build_agent_model(resolved_id),
            output_type=_output_type_for(resolved_id),
            deps_type=ChatDeps,
            system_prompt=SYSTEM_PROMPT,
            tools=chat_tool_functions() + handler_tool_functions(),
            model_settings=_model_settings_for(resolved_id, reasoning_level),
            history_processors=[compact_history],
        )
        _chat_agents[cache_key] = agent
    return agent
