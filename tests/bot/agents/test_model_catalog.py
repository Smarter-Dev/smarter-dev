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
    gemini = get_model("gemini-3-7-flash")  # caps at HIGH
    assert resolve_reasoning_level(gemini, ReasoningLevel.MAX) is ReasoningLevel.HIGH
    glm = get_model("glm-5-2")  # LOW/MEDIUM/HIGH only
    assert resolve_reasoning_level(glm, ReasoningLevel.NONE) is ReasoningLevel.LOW


def test_resolve_reasoning_level_none_for_models_without_reasoning():
    gemma = get_model("gemma-4-31b")
    assert gemma.supports_reasoning is False
    assert resolve_reasoning_level(gemma, ReasoningLevel.HIGH) is None
    assert resolve_reasoning_level(gemma, None) is None


def test_gemini_lineup_reflects_current_releases():
    # 3.6 Flash replaced 3.5 Flash (2026-07-21). 3.1 Flash Lite left on
    # 2026-08-13, superseded within its own class by 3.5 Flash Lite — Flash and
    # Flash Lite are separate classes, so 3.6 Flash never replaced it.
    assert get_model("gemini-3-5-flash") is None
    flash_3_6 = get_model("gemini-3-6-flash")
    assert flash_3_6 is not None
    assert flash_3_6.model_id == "gemini-3.6-flash"
    assert flash_3_6.provider is ModelProvider.GOOGLE
    lite_3_5 = get_model("gemini-3-5-flash-lite")
    assert lite_3_5 is not None
    assert lite_3_5.model_id == "gemini-3.5-flash-lite"
    assert lite_3_5.provider is ModelProvider.GOOGLE
    assert get_model("gemini-3-1-flash-lite") is None
    # 3.7 Flash shipped 2026-08-13 and took the slot from Gemini 3 Flash — the
    # oldest Flash we carried, and the last entry on a preview wire id.
    assert get_model("gemini-3-flash") is None
    flash_3_7 = get_model("gemini-3-7-flash")
    assert flash_3_7 is not None
    assert flash_3_7.model_id == "gemini-3.7-flash"
    assert flash_3_7.provider is ModelProvider.GOOGLE
    assert flash_3_7.supports_vision is True
    # Verified against the Gemini models API rather than assumed.
    assert flash_3_7.context_window == 1_048_576
    assert flash_3_7.max_output_tokens == 65_536


def test_gpt_5_6_lineup_is_selectable():
    expected = {
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


def test_luna_routes_through_openrouter():
    # Switched 2026-08-06 for OpenRouter's 50% rate ($0.10/$0.60 against
    # OpenAI direct's $0.20/$1.20). Probed live through OpenRouter: every
    # effort none→max returns 200 from the OpenAI upstream, so the full
    # GPT-5.6 ladder stays.
    luna = get_model("gpt-5-6-luna")
    assert luna is not None
    assert luna.provider is ModelProvider.OPENROUTER
    assert luna.model_id == "openai/gpt-5.6-luna"
    assert luna.family == "GPT"
    assert luna.supports_vision is True
    assert luna.reasoning_levels == (
        ReasoningLevel.NONE,
        ReasoningLevel.LOW,
        ReasoningLevel.MEDIUM,
        ReasoningLevel.HIGH,
        ReasoningLevel.XHIGH,
        ReasoningLevel.MAX,
    )
    assert luna.default_reasoning is ReasoningLevel.MEDIUM


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


def test_poolside_left_the_catalog():
    # Laguna S 2.1 was retired on 2026-08-13, taking the whole Poolside family
    # with it. Its pricing stays in llm_pricing for historical usage rows.
    assert get_model("poolside-laguna-s-2-1") is None
    assert get_model("poolside-laguna-xs-2-1") is None
    assert "Poolside" not in MODEL_FAMILIES


def test_grok_routes_through_openrouter_with_verified_capabilities():
    # We hold no first-party xAI key, so Grok rides OpenRouter. Capabilities
    # verified against OpenRouter's endpoints API (2026-08). Grok 4.5 was
    # retired for 4.6 on 2026-08-13.
    assert get_model("grok-4-5") is None
    model = get_model("grok-4-6")
    assert model is not None
    assert model.model_id == "x-ai/grok-4.6"
    assert model.family == "Grok"
    assert model.provider is ModelProvider.OPENROUTER
    assert model.supports_vision is True
    assert model.supports_tools is True
    assert model.context_window == 500_000
    assert model.reasoning_levels == (
        ReasoningLevel.LOW,
        ReasoningLevel.MEDIUM,
        ReasoningLevel.HIGH,
    )
    assert model.default_reasoning is ReasoningLevel.MEDIUM


def test_qwen3_8_routes_through_openrouter_not_digital_ocean():
    # DO's live account carries qwen3.8-max but not the 2.4T A95B weights, so
    # OpenRouter is the only route that can serve it (its cheapest OpenRouter
    # endpoint is in fact DO-served, at the same $2/$6 as every other route).
    # Text-only: OpenRouter reports input_modalities ["text"].
    model = get_model("qwen3-8-2-4t")
    assert model is not None
    assert model.model_id == "qwen/qwen3.8-2.4t-a95b"
    assert model.family == "Qwen"
    assert model.provider is ModelProvider.OPENROUTER
    assert model.supports_vision is False
    assert model.supports_tools is True
    assert model.context_window == 262_144
    assert model.reasoning_levels == (
        ReasoningLevel.LOW,
        ReasoningLevel.MEDIUM,
        ReasoningLevel.HIGH,
    )
    assert model.default_reasoning is ReasoningLevel.MEDIUM


def test_opencode_zen_models_carry_their_verified_wire_ids():
    # Verified against GET https://opencode.ai/zen/v1/models. DeepSeek is the
    # trap: Zen's id differs from the DO id the same model used to carry.
    # Qwen3.6 Plus, GLM and DeepSeek left Zen on 2026-08-13 for endpoints that
    # match their authors' published precision, so only these two remain.
    expected = {
        "kimi-k3": "kimi-k3",
        "minimax-m3": "minimax-m3",
    }
    for key, model_id in expected.items():
        model = get_model(key)
        assert model is not None, key
        assert model.model_id == model_id
        assert model.provider is ModelProvider.OPENCODE_ZEN


def test_prompted_output_follows_the_model_not_the_endpoint():
    """Open weights need prompted JSON wherever they are served.

    Regression guard for the 2026-08-13 moves: gating on provider alone meant
    Gemma/GLM/DeepSeek silently lost PromptedOutput the moment they moved to
    OpenRouter, while Grok and Luna — which share that provider and DO handle
    native tool output — must not be forced onto it.
    """
    for key in ("gemma-4-31b", "glm-5-2", "deepseek-v4", "qwen3-6-plus"):
        model = get_model(key)
        assert model.provider is ModelProvider.OPENROUTER, key
        assert model.needs_prompted_output is True, key

    for key in ("kimi-k3", "minimax-m3"):
        assert get_model(key).needs_prompted_output is True, key
    assert get_model("qwen3-5-397b").needs_prompted_output is True

    # Proprietary models keep native structured output, including the two that
    # share OpenRouter with the open weights.
    for key in ("grok-4-6", "gpt-5-6-luna", "gemini-3-5-flash-lite", "claude-opus-5"):
        assert get_model(key).needs_prompted_output is False, key


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
            # Luna rides OpenRouter (same OpenAI upstream, half the rate);
            # the rest of the GPT lineup is served direct.
            assert model.provider in (
                ModelProvider.OPENAI,
                ModelProvider.OPENROUTER,
            )
        elif model.family == "Claude":
            assert model.provider is ModelProvider.ANTHROPIC
        elif model.family == "Grok":
            assert model.provider is ModelProvider.OPENROUTER
        elif model.family in _OPEN_WEIGHTS_FAMILIES:
            # Open weights normally ride DO or Zen, but Qwen3.8 2.4T A95B is on
            # neither account, so it rides OpenRouter like Luna does.
            assert model.provider in (
                *_OPEN_WEIGHTS_PROVIDERS,
                ModelProvider.OPENROUTER,
            )
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
