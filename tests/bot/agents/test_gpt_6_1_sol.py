"""GPT-6.1 Sol: catalog entry, reasoning ladder and direct-OpenAI routing (#37)."""

from __future__ import annotations

from pydantic_ai.models.openai import OpenAIResponsesModel

from smarter_dev.shared.model_catalog import CatalogModel
from smarter_dev.shared.model_catalog import ModelProvider
from smarter_dev.shared.model_catalog import ReasoningLevel
from smarter_dev.shared.model_catalog import get_model
from smarter_dev.shared.model_catalog import resolve_reasoning_level
from smarter_dev.shared.model_catalog import successor_key
from smarter_dev.shared.model_router import build_model_for
from smarter_dev.shared.model_router import model_settings_for


def test_gpt_6_1_sol_is_served_by_openai_directly():
    model = get_model("gpt-6-1-sol")
    assert model is not None
    assert model.model_id == "gpt-6.1-sol"
    assert model.family == "GPT"
    assert model.provider is ModelProvider.OPENAI
    assert model.openrouter_routing is None
    assert model.supports_vision
    assert model.supports_tools
    assert model.default_reasoning is ReasoningLevel.MEDIUM


def test_gpt_6_1_sol_keeps_6_sol_budgets():
    # The move must not shift compaction points, output caps or long-context
    # billing exposure for the selections it takes over. 6 Sol ran on the
    # catalog defaults.
    model = get_model("gpt-6-1-sol")
    assert model.context_window == CatalogModel.context_window
    assert model.max_output_tokens == CatalogModel.max_output_tokens


def test_gpt_6_sol_is_retired_onto_gpt_6_1_sol():
    assert get_model("gpt-6-sol") is None
    assert successor_key("gpt-6-sol") == "gpt-6-1-sol"


def test_gpt_6_1_sol_never_sends_none_or_minimal():
    # OpenAI answers unsupported_value for "none" and "minimal" on this model.
    model = get_model("gpt-6-1-sol")
    assert model.reasoning_levels == (
        ReasoningLevel.LOW,
        ReasoningLevel.MEDIUM,
        ReasoningLevel.HIGH,
        ReasoningLevel.XHIGH,
        ReasoningLevel.MAX,
    )
    for requested in (ReasoningLevel.NONE, ReasoningLevel.MINIMAL):
        assert resolve_reasoning_level(model, requested) is ReasoningLevel.LOW
        settings = model_settings_for(model, requested)
        assert settings["openai_reasoning_effort"] == "low"
    for level in model.reasoning_levels:
        assert (
            model_settings_for(model, level)["openai_reasoning_effort"] == level.value
        )
    assert model_settings_for(model)["openai_reasoning_effort"] == "medium"


def test_gpt_6_1_sol_routes_through_the_responses_api(monkeypatch):
    # Chat Completions serves this model without tool calling; every tool the
    # chat hands it has to go through Responses.
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    built = build_model_for(get_model("gpt-6-1-sol"))
    assert isinstance(built, OpenAIResponsesModel)
    assert built.model_name == "gpt-6.1-sol"
