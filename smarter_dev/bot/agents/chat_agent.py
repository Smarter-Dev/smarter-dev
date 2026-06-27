"""Discord chat agent — single Pydantic AI agent driving every conversation turn.

Replaces the old classification/evaluation/response trio. One agent, one model
(Gemini 3.1 Flash Lite on medium thinking), one structured return type.

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
from pydantic_ai.models.google import GoogleModel, GoogleModelSettings
from pydantic_ai.providers.google import GoogleProvider

from smarter_dev.bot.agents.chat_compaction import compact_history
from smarter_dev.bot.agents.chat_models import AgentReturn
from smarter_dev.bot.agents.chat_tools import ChatDeps, chat_tool_functions
from smarter_dev.bot.agents.handler_tools import handler_tool_functions

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-3.1-flash-lite"
MODEL_ENV_VAR = "CHAT_AGENT_MODEL"

SYSTEM_PROMPT = (
    Path(__file__).parent / "prompts" / "chat_agent.md"
).read_text(encoding="utf-8")


def _build_model() -> GoogleModel:
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
    model_id = os.getenv(MODEL_ENV_VAR, DEFAULT_MODEL)
    return GoogleModel(model_id, provider=GoogleProvider(api_key=api_key))


def _model_settings() -> GoogleModelSettings:
    return GoogleModelSettings(
        google_thinking_config={"thinking_level": "MEDIUM"},
    )


_chat_agent: Agent[ChatDeps, AgentReturn] | None = None


def get_chat_agent() -> Agent[ChatDeps, AgentReturn]:
    """Return the singleton chat agent, building it on first use."""
    global _chat_agent
    if _chat_agent is None:
        _chat_agent = Agent(
            _build_model(),
            output_type=AgentReturn,
            deps_type=ChatDeps,
            system_prompt=SYSTEM_PROMPT,
            tools=chat_tool_functions() + handler_tool_functions(),
            model_settings=_model_settings(),
            history_processors=[compact_history],
        )
    return _chat_agent
