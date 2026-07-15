"""Unit tests for the model catalog — integrity + lookup helpers."""

from __future__ import annotations

from smarter_dev.bot.agents.model_catalog import (
    MODEL_CATALOG,
    MODEL_FAMILIES,
    CatalogModel,
    ModelProvider,
    catalog_by_key,
    get_model,
    is_valid_model_key,
    models_by_family,
)

_DIGITALOCEAN_FAMILIES = {"Kimi", "GLM", "DeepSeek", "Gemma", "Qwen"}


def test_catalog_entries_are_well_formed():
    for model in MODEL_CATALOG:
        assert isinstance(model, CatalogModel)
        assert model.key, f"empty key on {model!r}"
        assert model.label, f"empty label on {model!r}"
        assert model.model_id, f"empty model_id on {model!r}"
        assert model.family in MODEL_FAMILIES


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
