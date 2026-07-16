"""Unit tests for the model catalog — integrity + lookup helpers."""

from __future__ import annotations

from smarter_dev.shared.model_catalog import (
    ALL_REASONING_LEVELS,
    MODEL_CATALOG,
    MODEL_FAMILIES,
    CatalogModel,
    ModelProvider,
    ReasoningLevel,
    catalog_by_key,
    get_model,
    is_valid_model_key,
    models_by_family,
    parse_reasoning_level,
    resolve_reasoning_level,
)

_DIGITALOCEAN_FAMILIES = {"Kimi", "GLM", "DeepSeek", "Gemma", "Qwen"}


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
        elif model.family in _DIGITALOCEAN_FAMILIES:
            assert model.provider is ModelProvider.DIGITALOCEAN
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
