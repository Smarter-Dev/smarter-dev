"""GPT-6.2 Sol, GPT-6.2 Astra and GPT-6.1 Terra: catalog entries and routing (#494)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from smarter_dev.shared.model_catalog import MODEL_CATALOG
from smarter_dev.shared.model_catalog import ModelProvider
from smarter_dev.shared.model_catalog import ReasoningLevel
from smarter_dev.shared.model_catalog import get_model
from smarter_dev.shared.model_catalog import resolve_reasoning_level
from smarter_dev.shared.model_router import model_settings_for

_WIRE = {
    "gpt-6-2-sol": "gpt-6.2-sol",
    "gpt-6-2-astra": "gpt-6.2-astra",
    "gpt-6-1-terra": "gpt-6.1-terra",
}


@pytest.mark.parametrize("key,wire", _WIRE.items())
def test_new_models_are_served_by_openai_with_documented_capabilities(key, wire):
    model = get_model(key)
    assert model is not None
    assert model.model_id == wire
    assert model.provider is ModelProvider.OPENAI
    assert model.family == "GPT"
    assert model.supports_vision
    assert model.supports_tools
    assert model.openrouter_routing is None
    assert model.default_reasoning is ReasoningLevel.MEDIUM


@pytest.mark.parametrize("key", ["gpt-6-2-sol", "gpt-6-2-astra"])
def test_6_2_models_never_send_none_or_minimal(key):
    # OpenAI's reasoning guide: 6.2 Sol and Astra reject "none" and "minimal".
    model = get_model(key)
    assert ReasoningLevel.NONE not in model.reasoning_levels
    assert ReasoningLevel.MINIMAL not in model.reasoning_levels
    for requested in (ReasoningLevel.NONE, ReasoningLevel.MINIMAL):
        assert resolve_reasoning_level(model, requested) is ReasoningLevel.LOW
        assert model_settings_for(model, requested)["openai_reasoning_effort"] == "low"
    for level in model.reasoning_levels:
        assert model_settings_for(model, level)["openai_reasoning_effort"] == level.value


def test_terra_accepts_none_through_max():
    model = get_model("gpt-6-1-terra")
    assert model.reasoning_levels == (
        ReasoningLevel.NONE,
        ReasoningLevel.LOW,
        ReasoningLevel.MEDIUM,
        ReasoningLevel.HIGH,
        ReasoningLevel.XHIGH,
        ReasoningLevel.MAX,
    )
    assert model_settings_for(model, ReasoningLevel.NONE)["openai_reasoning_effort"] == "none"


def test_nothing_was_retired_and_catalog_fits_one_select():
    assert get_model("gpt-6-luna") is not None
    assert get_model("gpt-6-1-sol") is not None
    assert len(MODEL_CATALOG) <= 24
    assert len({m.key for m in MODEL_CATALOG}) == len(MODEL_CATALOG)


_MIGRATION = next(
    (Path(__file__).resolve().parents[3] / "alembic/main/versions").glob(
        "*_c8e2a5f7b1d3_*.py"
    )
)


def _load():
    spec = importlib.util.spec_from_file_location("admit_62", _MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_admission_migration_covers_exactly_the_new_keys_enabled():
    mod = _load()
    assert mod.down_revision == "b4e7a2c9d1f6"
    assert {k for k, _ in mod._ADDED} == set(_WIRE)
    for key, _ in mod._ADDED:
        assert get_model(key) is not None
    assert "SELECT :key, true, :tier" in _MIGRATION.read_text()
    assert all(t in ("low", "medium", "high", "ultra") for _, t in mod._ADDED)


def test_terra_clamps_minimal_to_none():
    # Probed: Terra answers "minimal" with 400, accepts "none".
    model = get_model("gpt-6-1-terra")
    assert resolve_reasoning_level(model, ReasoningLevel.MINIMAL) is ReasoningLevel.NONE
    assert (
        model_settings_for(model, ReasoningLevel.MINIMAL)["openai_reasoning_effort"]
        == "none"
    )


_OPENAI_MODELS = [m for m in MODEL_CATALOG if m.provider is ModelProvider.OPENAI]


@pytest.mark.parametrize("model", _OPENAI_MODELS, ids=lambda m: m.key)
def test_every_openai_catalog_model_is_built_with_a_reasoning_profile(
    model, monkeypatch
):
    # pydantic-ai-slim 2.57.0 does not know the 6.2 ids or 6.1 Terra and would
    # give them a no-reasoning profile (#494).
    from smarter_dev.shared import model_router

    monkeypatch.setattr(model_router, "require_credential", lambda name: "sk-test")
    built = model_router.build_model_for(model)
    assert built.profile.get("openai_supports_reasoning") is True


def _scripted_client(requests):
    import httpx
    from openai import AsyncOpenAI

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        base = {
            "id": f"resp_{len(requests)}",
            "object": "response",
            "created_at": 0,
            "model": body["model"],
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": [],
            "status": "completed",
            "usage": {
                "input_tokens": 1,
                "output_tokens": 1,
                "total_tokens": 2,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        }
        if len(requests) == 1:
            output = [
                {
                    "id": "rs_1",
                    "type": "reasoning",
                    "summary": [],
                    "encrypted_content": "enc",
                },
                {
                    "id": "fc_1",
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "lookup",
                    "arguments": "{}",
                    "status": "completed",
                },
            ]
        else:
            output = [
                {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {"type": "output_text", "text": "done", "annotations": []}
                    ],
                }
            ]
        return httpx.Response(200, json={**base, "output": output})

    return AsyncOpenAI(
        api_key="sk-test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )


@pytest.mark.parametrize("key", ["gpt-6-2-sol", "gpt-6-2-astra", "gpt-6-1-terra"])
async def test_tool_loop_sends_reasoning_state_back(key):
    from pydantic_ai import Agent
    from pydantic_ai.models.openai import OpenAIResponsesModel
    from pydantic_ai.providers.openai import OpenAIProvider

    from smarter_dev.shared.model_router import openai_profile_for

    catalog = get_model(key)
    requests: list[dict] = []
    model = OpenAIResponsesModel(
        catalog.model_id,
        provider=OpenAIProvider(openai_client=_scripted_client(requests)),
        profile=openai_profile_for(catalog.model_id),
    )
    agent = Agent(model)

    @agent.tool_plain
    def lookup() -> str:
        return "42"

    result = await agent.run(
        "go", model_settings=model_settings_for(catalog, ReasoningLevel.MEDIUM)
    )
    assert result.output == "done"
    assert len(requests) == 2
    second = requests[1]
    assert "reasoning.encrypted_content" in second.get("include", [])
    assert "<think>" not in json.dumps(second)
    types = [item.get("type") for item in second["input"]]
    assert "reasoning" in types and "function_call" in types
