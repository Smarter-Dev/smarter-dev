"""Unit tests for the model catalog — integrity + lookup helpers."""

from __future__ import annotations

from smarter_dev.shared.model_catalog import ALL_REASONING_LEVELS
from smarter_dev.shared.model_catalog import MODEL_CATALOG
from smarter_dev.shared.model_catalog import MODEL_FAMILIES
from smarter_dev.shared.model_catalog import CatalogModel
from smarter_dev.shared.model_catalog import ModelProvider
from smarter_dev.shared.model_catalog import ReasoningLevel
from smarter_dev.shared.model_catalog import catalog_by_key
from smarter_dev.shared.model_catalog import get_model
from smarter_dev.shared.model_catalog import is_valid_model_key
from smarter_dev.shared.model_catalog import models_by_family
from smarter_dev.shared.model_catalog import parse_reasoning_level
from smarter_dev.shared.model_catalog import resolve_reasoning_level

# Open-weights families. These no longer map to a single provider: the same
# family can be served by Digital Ocean and OpenCode Zen at once (Kimi K2.6 on
# DO, Kimi K3 on Zen; Qwen3.5 on DO, Qwen3.6 Plus on Zen), so routing is
# asserted per model rather than per family.
_OPEN_WEIGHTS_FAMILIES = {"Kimi", "GLM", "DeepSeek", "Gemma", "Qwen", "MiniMax"}
_OPEN_WEIGHTS_PROVIDERS = {
    ModelProvider.DIGITALOCEAN,
    ModelProvider.OPENCODE_ZEN,
}


def test_catalog_entries_are_well_formed():
    for model in MODEL_CATALOG:
        assert isinstance(model, CatalogModel)
        assert model.key, f"empty key on {model!r}"
        assert model.label, f"empty label on {model!r}"
        assert model.model_id, f"empty model_id on {model!r}"
        assert model.family in MODEL_FAMILIES


def test_reasoning_defaults_are_supported():
    for model in MODEL_CATALOG:
        if model.default_reasoning is not None:
            assert model.default_reasoning in model.reasoning_levels
        assert model.supports_reasoning == bool(model.reasoning_levels)


def test_reasoning_levels_are_ordered_subsets_of_the_ladder():
    ladder = list(ReasoningLevel)
    for model in MODEL_CATALOG:
        ranks = [ladder.index(level) for level in model.reasoning_levels]
        assert ranks == sorted(ranks), f"{model.key} reasoning levels out of order"


def test_all_reasoning_levels_is_the_full_ladder():
    assert ALL_REASONING_LEVELS == tuple(ReasoningLevel)


def test_parse_reasoning_level_round_trips_and_degrades():
    assert parse_reasoning_level("high") is ReasoningLevel.HIGH
    assert parse_reasoning_level(None) is None
    assert parse_reasoning_level("") is None
    assert parse_reasoning_level("bogus") is None


def test_resolve_reasoning_level_falls_back_to_default():
    glm = get_model("glm-5-2")
    assert resolve_reasoning_level(glm, None) is glm.default_reasoning


def test_resolve_reasoning_level_keeps_supported_choice():
    gpt = get_model("gpt-5-4")
    assert resolve_reasoning_level(gpt, ReasoningLevel.XHIGH) is ReasoningLevel.XHIGH


def test_resolve_reasoning_level_clamps_unsupported_to_nearest():
    gemini = get_model("gemini-3-flash")  # caps at HIGH
    assert resolve_reasoning_level(gemini, ReasoningLevel.MAX) is ReasoningLevel.HIGH
    glm = get_model("glm-5-2")  # LOW/MEDIUM/HIGH only
    assert resolve_reasoning_level(glm, ReasoningLevel.NONE) is ReasoningLevel.LOW


def test_resolve_reasoning_level_none_for_models_without_reasoning():
    gemma = get_model("gemma-4-31b")
    assert gemma.supports_reasoning is False
    assert resolve_reasoning_level(gemma, ReasoningLevel.HIGH) is None
    assert resolve_reasoning_level(gemma, None) is None


def test_gemini_lineup_reflects_current_releases():
    # 3.6 Flash replaced 3.5 Flash (2026-07-21); 3.5 Flash Lite joined the
    # catalog; 3.1 Flash Lite remains selectable.
    assert get_model("gemini-3-5-flash") is None
    flash_3_6 = get_model("gemini-3-6-flash")
    assert flash_3_6 is not None
    assert flash_3_6.model_id == "gemini-3.6-flash"
    assert flash_3_6.provider is ModelProvider.GOOGLE
    lite_3_5 = get_model("gemini-3-5-flash-lite")
    assert lite_3_5 is not None
    assert lite_3_5.model_id == "gemini-3.5-flash-lite"
    assert lite_3_5.provider is ModelProvider.GOOGLE
    assert get_model("gemini-3-1-flash-lite").model_id == "gemini-3.1-flash-lite"


def test_gpt_5_6_lineup_is_selectable():
    expected = {
        "gpt-5-6-luna": "gpt-5.6-luna",
        "gpt-5-6-sol": "gpt-5.6-sol",
        "gpt-5-6-terra": "gpt-5.6-terra",
    }
    for key, model_id in expected.items():
        model = get_model(key)
        assert model is not None, f"missing {key}"
        assert model.model_id == model_id
        assert model.family == "GPT"
        assert model.provider is ModelProvider.OPENAI
        assert model.default_reasoning in model.reasoning_levels


def test_claude_opus_5_is_selectable():
    opus = get_model("claude-opus-5")
    assert opus is not None
    assert opus.label == "Claude Opus 5"
    assert opus.family == "Claude"
    assert opus.provider is ModelProvider.ANTHROPIC
    assert opus.model_id == "claude-opus-5"
    # Flagship Claude exposes the full low→max effort ladder, like Sonnet 5.
    assert opus.supports_reasoning is True
    assert opus.default_reasoning in opus.reasoning_levels


def test_poolside_model_stays_on_openrouter():
    # Zen only carries the free Laguna S, whose pool throttles, so Poolside
    # stays on the paid OpenRouter route. Laguna XS left the catalog to keep
    # the Discord select under its cap.
    model = get_model("poolside-laguna-s-2-1")
    assert model is not None
    assert model.model_id == "poolside/laguna-s-2.1"
    assert model.family == "Poolside"
    assert model.provider is ModelProvider.OPENROUTER
    assert model.supports_reasoning is False
    assert get_model("poolside-laguna-xs-2-1") is None


def test_opencode_zen_models_carry_their_verified_wire_ids():
    # Verified against GET https://opencode.ai/zen/v1/models. DeepSeek is the
    # trap: Zen's id differs from the DO id the same model used to carry.
    expected = {
        "kimi-k3": "kimi-k3",
        "minimax-m3": "minimax-m3",
        "qwen3-6-plus": "qwen3.6-plus",
        "glm-5-2": "glm-5.2",
        "deepseek-v4": "deepseek-v4-flash",
    }
    for key, model_id in expected.items():
        model = get_model(key)
        assert model is not None, key
        assert model.model_id == model_id
        assert model.provider is ModelProvider.OPENCODE_ZEN


def test_keys_are_unique():
    keys = [model.key for model in MODEL_CATALOG]
    assert len(keys) == len(set(keys))


def test_catalog_fits_in_one_discord_select():
    # 25-option Discord limit, leaving room for a "server default" sentinel.
    assert len(MODEL_CATALOG) <= 24


def test_every_family_is_represented():
    present = {model.family for model in MODEL_CATALOG}
    assert present == set(MODEL_FAMILIES)


def test_get_model_round_trips():
    for model in MODEL_CATALOG:
        assert get_model(model.key) is model
        assert is_valid_model_key(model.key) is True


def test_unknown_key_returns_none_and_false():
    assert get_model("does-not-exist") is None
    assert is_valid_model_key("does-not-exist") is False


def test_catalog_by_key_covers_all_entries():
    mapping = catalog_by_key()
    assert set(mapping) == {model.key for model in MODEL_CATALOG}
    assert all(mapping[key].key == key for key in mapping)


def test_provider_routing_by_family():
    for model in MODEL_CATALOG:
        if model.family == "Gemini":
            assert model.provider is ModelProvider.GOOGLE
        elif model.family == "GPT":
            assert model.provider is ModelProvider.OPENAI
        elif model.family == "Claude":
            assert model.provider is ModelProvider.ANTHROPIC
        elif model.family == "Poolside":
            assert model.provider is ModelProvider.OPENROUTER
        elif model.family in _OPEN_WEIGHTS_FAMILIES:
            assert model.provider in _OPEN_WEIGHTS_PROVIDERS
        else:  # pragma: no cover - guarded by test_catalog_entries_are_well_formed
            raise AssertionError(f"unexpected family {model.family}")


def test_models_by_family_preserves_catalog_order():
    grouped = models_by_family()
    # Every grouped model keeps its relative catalog order within its family.
    for family, models in grouped.items():
        catalog_order = [m for m in MODEL_CATALOG if m.family == family]
        assert models == catalog_order
    # Family keys follow first appearance in the catalog.
    first_seen: list[str] = []
    for model in MODEL_CATALOG:
        if model.family not in first_seen:
            first_seen.append(model.family)
    assert list(grouped) == first_seen
