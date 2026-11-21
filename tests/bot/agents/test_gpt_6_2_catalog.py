"""GPT-6.2 Sol, GPT-6.2 Astra and GPT-6.1 Terra: catalog entries and routing (#494)."""

from __future__ import annotations

import importlib.util
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
    # OpenAI's reasoning guide: 6.2 Sol and Astra reject "none".
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


def test_admission_migration_covers_exactly_the_new_keys_disabled():
    mod = _load()
    assert mod.down_revision == "b4e7a2c9d1f6"
    assert {k for k, _ in mod._ADDED} == set(_WIRE)
    for key, _ in mod._ADDED:
        assert get_model(key) is not None
    assert all(t in ("low", "medium", "high", "ultra") for _, t in mod._ADDED)
