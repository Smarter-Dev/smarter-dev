"""The model behind the bot's small one-shot agents.

The streak celebration, ``/tldr``, ``/help``, the timeout announcement, the
forum auto-responder and moderation triage each produce one structured answer.
They ran on DSPy until 2026-10, on the models named by ``LLM_FAST_MODEL`` and
(forum, moderation) ``LLM_MEDIUM_MODEL``, both ``gpt-6-luna`` everywhere.
They now run on pydantic-ai through the shared model router, so the catalog
entry carries the provider, key and reasoning default like every other agent.
"""

from __future__ import annotations

from typing import TypeVar

from pydantic_ai import Agent
from pydantic_ai.agent import AgentRunResult

from smarter_dev.shared.model_catalog import CatalogModel
from smarter_dev.shared.model_catalog import get_model
from smarter_dev.shared.model_router import build_model_for
from smarter_dev.shared.model_router import model_settings_for

# Catalog key that replaced LLM_FAST_MODEL and LLM_MEDIUM_MODEL. Served by
# OpenAI directly at the catalog's default reasoning (medium), which is what
# DSPy got by sending none.
UTILITY_MODEL_KEY = "gpt-6-luna"

OutputT = TypeVar("OutputT")


def utility_catalog_model() -> CatalogModel:
    model = get_model(UTILITY_MODEL_KEY)
    if model is None:
        raise RuntimeError(f"Utility model is not in the catalog: {UTILITY_MODEL_KEY}")
    return model


def build_utility_agent(
    output_type: type[OutputT], instructions: str
) -> Agent[None, OutputT]:
    """Return an agent on the utility model with typed ``output_type``."""
    model = utility_catalog_model()
    return Agent(
        build_model_for(model),
        output_type=output_type,
        instructions=instructions,
        model_settings=model_settings_for(model),
    )


def render_inputs(**fields: object) -> str:
    """Lay named inputs out as labelled sections for the user prompt."""
    return "\n\n".join(f"## {name}\n{value}" for name, value in fields.items())


def tokens_used(result: AgentRunResult) -> int:
    """Input plus output tokens of a finished run, as the callers record them."""
    usage = result.usage
    return int(usage.input_tokens or 0) + int(usage.output_tokens or 0)
