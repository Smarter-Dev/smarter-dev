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
from smarter_dev.bot.agents.model_catalog import MODEL_CATALOG
from smarter_dev.bot.agents.model_catalog import CatalogModel
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


def _model_settings_for(model_id: str) -> ModelSettings | None:
    """Per-provider model settings for ``model_id``.

    Catalog models route through the shared :mod:`model_router` so a per-channel
    override gets the same reasoning/thinking config the router assigns; anything
    else falls back to the historical prefix logic used for ad-hoc
    ``CHAT_AGENT_MODEL`` ids.
    """
    catalog_model = _catalog_model_for_id(model_id)
    if catalog_model is not None:
        return model_settings_for(catalog_model)
    if model_id.startswith(("gpt-", "openai/")):
        from pydantic_ai.models.openai import OpenAIResponsesModelSettings

        return OpenAIResponsesModelSettings(openai_reasoning_effort="low")
    return GoogleModelSettings(
        google_thinking_config={"thinking_level": "MEDIUM"},
    )


# One agent per resolved model id. A per-channel override means we can no longer
# share a single global agent, so agents are cached by the wire model id they
# were built for. ``get_chat_agent(None)`` resolves to the env/default id, so a
# channel with no override keeps returning the same (default) instance.
_chat_agents: dict[str, Agent[ChatDeps, AgentReturn]] = {}


def get_chat_agent(model_id: str | None = None) -> Agent[ChatDeps, AgentReturn]:
    """Return (building on first use) the chat agent for ``model_id``.

    ``model_id`` is the provider wire id (e.g. a catalog ``CatalogModel.model_id``);
    ``None`` uses the env/default model. Agents are cached per resolved id, so a
    given model always reuses one ``Agent`` and the default path is
    singleton-equivalent to the previous behaviour.
    """
    resolved_id = model_id or _model_id()
    agent = _chat_agents.get(resolved_id)
    if agent is None:
        agent = Agent(
            build_agent_model(resolved_id),
            output_type=AgentReturn,
            deps_type=ChatDeps,
            system_prompt=SYSTEM_PROMPT,
            tools=chat_tool_functions() + handler_tool_functions(),
            model_settings=_model_settings_for(resolved_id),
            history_processors=[compact_history],
        )
        _chat_agents[resolved_id] = agent
    return agent
