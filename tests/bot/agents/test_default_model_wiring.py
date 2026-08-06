"""Guards that flat default-model ids resolve through the catalog.

``chat_agent.build_agent_model`` falls back to a direct-provider client when
an id is not in the catalog, so a default that drifts out of the catalog is a
silent misroute (for Luna: back to direct OpenAI at twice the OpenRouter
rate) rather than an error. These tests pin every flat default to a catalog
entry and pin Luna's defaults to the OpenRouter route.
"""

from __future__ import annotations

from smarter_dev.bot.agents import chat_agent
from smarter_dev.bot.agents import chat_compaction
from smarter_dev.shared.model_catalog import MODEL_CATALOG
from smarter_dev.shared.model_catalog import CatalogModel
from smarter_dev.shared.model_catalog import ModelProvider
from smarter_dev.web import handler_agent


def _catalog_model_for(model_id: str) -> CatalogModel | None:
    return next(
        (model for model in MODEL_CATALOG if model.model_id == model_id), None
    )


def test_chat_agent_default_is_catalog_luna_on_openrouter():
    model = _catalog_model_for(chat_agent.DEFAULT_MODEL)
    assert model is not None
    assert model.key == "gpt-5-6-luna"
    assert model.provider is ModelProvider.OPENROUTER


def test_handler_agent_default_is_catalog_luna_on_openrouter():
    model = _catalog_model_for(handler_agent.DEFAULT_MODEL)
    assert model is not None
    assert model.key == "gpt-5-6-luna"
    assert model.provider is ModelProvider.OPENROUTER


def test_compaction_defaults_resolve_to_catalog_models():
    assert _catalog_model_for(chat_compaction.DEFAULT_CHAT_MODEL) is not None
    assert _catalog_model_for(chat_compaction.DEFAULT_COMPACT_MODEL) is not None
