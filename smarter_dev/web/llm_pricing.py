"""Cost calculation for research sessions using genai-prices.

Patches missing models into the genai-prices snapshot at import time,
then exposes calc_session_cost() for computing per-session costs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from genai_prices import calc_price
from genai_prices import types
from genai_prices.data_snapshot import find_provider_by_id
from genai_prices.data_snapshot import get_snapshot

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Patch missing models into the genai-prices snapshot
# ---------------------------------------------------------------------------

_snapshot = get_snapshot()


def _patch_provider(provider_id: str, model: types.ModelInfo) -> None:
    provider = find_provider_by_id(_snapshot.providers, provider_id)
    if provider is not None:
        provider.models.append(model)


# Gemini 3.1 Flash Lite (GA + preview prefix) — not yet in genai-prices
_patch_provider(
    "google",
    types.ModelInfo(
        id="gemini-3.1-flash-lite",
        match=types.ClauseStartsWith(starts_with="gemini-3.1-flash-lite"),
        prices=types.ModelPrice(
            input_mtok=Decimal("0.25"),
            output_mtok=Decimal("1.50"),
            cache_read_mtok=Decimal("0.025"),
        ),
    ),
)

# Gemini 3 Flash Preview — not yet in genai-prices
_patch_provider(
    "google",
    types.ModelInfo(
        id="gemini-3-flash-preview",
        match=types.ClauseStartsWith(starts_with="gemini-3-flash"),
        prices=types.ModelPrice(
            input_mtok=Decimal("0.15"),
            output_mtok=Decimal("0.60"),
            cache_read_mtok=Decimal("0.0375"),
        ),
    ),
)

# Gemini 3.5 Flash Lite — not yet in genai-prices
# NOTE: must come before gemini-3.5-flash so the more specific match wins
_patch_provider(
    "google",
    types.ModelInfo(
        id="gemini-3.5-flash-lite",
        match=types.ClauseStartsWith(starts_with="gemini-3.5-flash-lite"),
        prices=types.ModelPrice(
            input_mtok=Decimal("0.30"),
            output_mtok=Decimal("2.50"),
            cache_read_mtok=Decimal("0.03"),
        ),
    ),
)

# Gemini 3.5 Flash — not yet in genai-prices. Retired as a selectable model
# (replaced by 3.6 Flash) but historical turns still price against it.
_patch_provider(
    "google",
    types.ModelInfo(
        id="gemini-3.5-flash",
        match=types.ClauseStartsWith(starts_with="gemini-3.5-flash"),
        prices=types.ModelPrice(
            input_mtok=Decimal("1.50"),
            output_mtok=Decimal("9.00"),
            cache_read_mtok=Decimal("0.15"),
        ),
    ),
)

# Gemini 3.6 Flash — not yet in genai-prices
_patch_provider(
    "google",
    types.ModelInfo(
        id="gemini-3.6-flash",
        match=types.ClauseStartsWith(starts_with="gemini-3.6-flash"),
        prices=types.ModelPrice(
            input_mtok=Decimal("1.50"),
            output_mtok=Decimal("7.50"),
            cache_read_mtok=Decimal("0.15"),
        ),
    ),
)

# GPT-5.4 Nano — not yet in genai-prices
_patch_provider(
    "openai",
    types.ModelInfo(
        id="gpt-5.4-nano",
        match=types.ClauseStartsWith(starts_with="gpt-5.4-nano"),
        prices=types.ModelPrice(
            input_mtok=Decimal("0.20"),
            output_mtok=Decimal("1.25"),
            cache_read_mtok=Decimal("0.02"),
        ),
    ),
)

# Gemini 2.5 Flash Preview TTS — not yet in genai-prices.
# Pricing per Google's docs (Gemini 2.5 Flash TTS preview tier):
#   text input  ~$0.50/M tokens
#   audio output ~$10.00/M tokens (1M tokens ≈ ~700-1k seconds @ 24kHz)
# These are approximations — the operator should true up against real bills
# if needed.
_patch_provider(
    "google",
    types.ModelInfo(
        id="gemini-2.5-flash-preview-tts",
        match=types.ClauseStartsWith(starts_with="gemini-2.5-flash-preview-tts"),
        prices=types.ModelPrice(
            input_mtok=Decimal("0.50"),
            output_mtok=Decimal("10.00"),
        ),
    ),
)

# GPT-5.6 Luna — not yet in genai-prices (preview pricing, July 2026)
_patch_provider(
    "openai",
    types.ModelInfo(
        id="gpt-5.6-luna",
        match=types.ClauseStartsWith(starts_with="gpt-5.6-luna"),
        prices=types.ModelPrice(
            input_mtok=Decimal("1.00"),
            output_mtok=Decimal("6.00"),
        ),
    ),
)

# GPT-5.4 Mini — keep this more-specific prefix ahead of standard GPT-5.4.
_patch_provider(
    "openai",
    types.ModelInfo(
        id="gpt-5.4-mini",
        match=types.ClauseStartsWith(starts_with="gpt-5.4-mini"),
        prices=types.ModelPrice(
            input_mtok=Decimal("0.75"),
            output_mtok=Decimal("4.50"),
            cache_read_mtok=Decimal("0.075"),
        ),
    ),
)

# GPT-5.4 (standard) — not yet in genai-prices
# NOTE: must come after mini/nano so the more specific matches win.
_patch_provider(
    "openai",
    types.ModelInfo(
        id="gpt-5.4",
        match=types.ClauseStartsWith(starts_with="gpt-5.4"),
        prices=types.ModelPrice(
            input_mtok=Decimal("2.50"),
            output_mtok=Decimal("15.00"),
            cache_read_mtok=Decimal("0.25"),
        ),
    ),
)


# ---------------------------------------------------------------------------
# DigitalOcean serverless inference pricing
# ---------------------------------------------------------------------------
# genai-prices has no DigitalOcean provider, and DO resells these open-weight
# models at its own rates (not the origin vendors'), so they are priced
# directly from this table. USD per million tokens, from
# https://docs.digitalocean.com/products/inference/details/pricing/ (2026-07).

_DIGITALOCEAN_PRICES: dict[str, types.ModelPrice] = {
    "kimi-k2.6": types.ModelPrice(
        input_mtok=Decimal("0.76"),
        output_mtok=Decimal("3.20"),
        cache_read_mtok=Decimal("0.19"),
    ),
    "glm-5.2": types.ModelPrice(
        input_mtok=Decimal("1.05"),
        output_mtok=Decimal("4.40"),
        cache_read_mtok=Decimal("0.21"),
    ),
    "deepseek-4-flash": types.ModelPrice(
        input_mtok=Decimal("0.112"),
        output_mtok=Decimal("0.224"),
        cache_read_mtok=Decimal("0.028"),
    ),
    "gemma-4-31B-it": types.ModelPrice(
        input_mtok=Decimal("0.18"),
        output_mtok=Decimal("0.50"),
    ),
    "qwen3.5-397b-a17b": types.ModelPrice(
        input_mtok=Decimal("0.385"),
        output_mtok=Decimal("2.45"),
        cache_read_mtok=Decimal("0.111"),
    ),
}

# Poolside Laguna models served through OpenRouter. USD per million tokens;
# cache is cached-input/read pricing. Cache writes bill at the input rate.
_OPENROUTER_PRICES: dict[str, types.ModelPrice] = {
    "poolside/laguna-xs-2.1": types.ModelPrice(
        input_mtok=Decimal("0.06"),
        output_mtok=Decimal("0.12"),
        cache_read_mtok=Decimal("0.03"),
    ),
    "poolside/laguna-s-2.1": types.ModelPrice(
        input_mtok=Decimal("0.10"),
        output_mtok=Decimal("0.20"),
        cache_read_mtok=Decimal("0.01"),
    ),
}

# OpenCode Zen models. USD per million tokens, from
# https://opencode.ai/docs/zen/#pricing (2026-07). "laguna-s-2.1-free" is a
# limited-time free tier and prices at zero on every axis; it is listed
# explicitly rather than omitted so a free turn resolves to Decimal("0")
# instead of falling through to genai-prices, which does not know the id.
_OPENCODE_ZEN_PRICES: dict[str, types.ModelPrice] = {
    "kimi-k3": types.ModelPrice(
        input_mtok=Decimal("3.00"),
        output_mtok=Decimal("15.00"),
        cache_read_mtok=Decimal("0.30"),
    ),
    "minimax-m3": types.ModelPrice(
        input_mtok=Decimal("0.30"),
        output_mtok=Decimal("1.20"),
        cache_read_mtok=Decimal("0.06"),
    ),
    "qwen3.6-plus": types.ModelPrice(
        input_mtok=Decimal("0.50"),
        output_mtok=Decimal("3.00"),
        cache_read_mtok=Decimal("0.05"),
    ),
    "glm-5.2": types.ModelPrice(
        input_mtok=Decimal("1.40"),
        output_mtok=Decimal("4.40"),
        cache_read_mtok=Decimal("0.26"),
    ),
    "deepseek-v4-flash": types.ModelPrice(
        input_mtok=Decimal("0.14"),
        output_mtok=Decimal("0.28"),
        cache_read_mtok=Decimal("0.028"),
    ),
    # No longer in the catalog — Laguna S went back to OpenRouter's paid route
    # after the free pool throttled. Kept because rows written while it WAS
    # selectable still carry this id, and dropping it would fall them through to
    # genai-prices, which does not know it.
    "laguna-s-2.1-free": types.ModelPrice(
        input_mtok=Decimal("0"),
        output_mtok=Decimal("0"),
        cache_read_mtok=Decimal("0"),
    ),
}

_TOKENS_PER_MTOK = Decimal("1000000")


def _direct_model_cost(
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    price: types.ModelPrice,
) -> Decimal:
    """Direct cost computation for a locally priced model.

    Cache tokens follow the provider-reporting convention (same one
    genai-prices assumes): they are a SUBSET of ``input_tokens``. The
    uncached share bills at the input rate, cached reads at the model's
    prompt-caching rate when it has one (otherwise plain input), and cache
    writes at plain input when no separate write tier was supplied. The uncached
    share is clamped at zero in case a provider reports cache tokens
    without folding them into ``input_tokens``.
    """
    input_rate = price.input_mtok
    cache_read_rate = (
        price.cache_read_mtok if price.cache_read_mtok is not None else input_rate
    )
    uncached_input_tokens = max(
        input_tokens - cache_read_tokens - cache_write_tokens, 0
    )
    return (
        Decimal(uncached_input_tokens) * input_rate
        + Decimal(output_tokens) * price.output_mtok
        + Decimal(cache_read_tokens) * cache_read_rate
        + Decimal(cache_write_tokens) * input_rate
    ) / _TOKENS_PER_MTOK


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# pydantic-ai provider prefix → genai-prices provider ID
_PROVIDER_MAP: dict[str, str] = {
    "google-gla": "google",
    "openai": "openai",
    "anthropic": "anthropic",
}


def calc_session_cost(
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    model_name: str,
) -> Decimal:
    """Calculate the USD cost for a research session.

    *model_name* is the pydantic-ai model string, e.g.
    ``"google-gla:gemini-3.1-flash-lite-preview"``.  We split on ``":"``
    to extract the provider_id and model_ref expected by genai-prices.
    """
    parts = model_name.split(":", 1)
    if len(parts) == 2:
        pydantic_provider, model_ref = parts
        provider_id = _PROVIDER_MAP.get(pydantic_provider, pydantic_provider)
    else:
        provider_id, model_ref = None, parts[0]

    # Zen is checked BEFORE Digital Ocean because the two share one wire id:
    # "glm-5.2" means the same model on both, at different rates ($1.40 vs
    # $1.05 input). Names arrive flat (provider_id None), so the id alone cannot
    # say which endpoint served a given turn. GLM now runs only on Zen, so every
    # NEW row is a Zen row and Zen-first prices those correctly; the cost is that
    # pre-move GLM rows re-price at the Zen rate too, overstating historical GLM
    # lines by ~33%. Deliberate: mispricing live traffic forever is worse than a
    # bounded restatement of one model's history. The other moved ids do not
    # collide — Zen's DeepSeek is "deepseek-v4-flash" against DO's
    # "deepseek-4-flash", and the Laguna ids differ from the OpenRouter paths.
    if provider_id in (None, "opencode_zen"):
        zen_price = _OPENCODE_ZEN_PRICES.get(model_ref)
        if zen_price is not None:
            return _direct_model_cost(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
                price=zen_price,
            )

    # DO-served models are unknown to genai-prices and billed at DO's own
    # rates; price them directly. Chat/voice/summarizer names arrive flat
    # (provider_id None), a "digitalocean:" prefix also resolves here.
    if provider_id in (None, "digitalocean"):
        digitalocean_price = _DIGITALOCEAN_PRICES.get(model_ref)
        if digitalocean_price is not None:
            return _direct_model_cost(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
                price=digitalocean_price,
            )

    if provider_id in (None, "openrouter"):
        openrouter_price = _OPENROUTER_PRICES.get(model_ref)
        if openrouter_price is not None:
            return _direct_model_cost(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
                price=openrouter_price,
            )

    usage = types.Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
    )

    result = calc_price(usage, model_ref, provider_id=provider_id)
    return result.total_price


@dataclass(frozen=True, slots=True)
class ModelPriceRates:
    """Actual USD-per-million rates suitable for user-facing comparisons."""

    input_mtok: Decimal
    output_mtok: Decimal
    cache_read_mtok: Decimal
    cache_write_mtok: Decimal


def price_rates_for_model(model) -> ModelPriceRates | None:
    """Resolve provider-explicit rates for a catalog model.

    Synthetic one-million-token calculations deliberately reuse the canonical
    calculator, avoiding a second pricing table. Unknown models return ``None``
    rather than inventing a discount or silently recording a misleading quote.
    """
    provider = getattr(getattr(model, "provider", None), "value", None)
    provider_prefix = {
        "google": "google-gla", "openai": "openai", "anthropic": "anthropic",
        "digitalocean": "digitalocean", "openrouter": "openrouter",
        "opencode_zen": "opencode_zen",
    }.get(provider)
    wire_id = getattr(model, "model_id", str(model))
    model_name = f"{provider_prefix}:{wire_id}" if provider_prefix else wire_id
    try:
        uncached = calc_session_cost(1_000_000, 0, 0, 0, model_name)
        output = calc_session_cost(0, 1_000_000, 0, 0, model_name)
        cached = calc_session_cost(1_000_000, 0, 1_000_000, 0, model_name)
        cache_write = calc_session_cost(1_000_000, 0, 0, 1_000_000, model_name)
    except (LookupError, ValueError):
        return None
    return ModelPriceRates(uncached, output, cached, cache_write)


def model_change_warning(old_model, new_model) -> str:
    """Build an honest confirmation warning from known provider pricing."""
    old_rates = price_rates_for_model(old_model)
    new_rates = price_rates_for_model(new_model)
    if old_rates is None or new_rates is None:
        return (
            f"Change from {old_model.label} to {new_model.label}? Exact provider "
            "pricing is unavailable for at least one model; usage cost may change."
        )
    return (
        f"Change from {old_model.label} to {new_model.label}? Per 1M tokens, "
        f"the new model is ${new_rates.input_mtok} uncached input, "
        f"${new_rates.cache_read_mtok} cached input, and "
        f"${new_rates.output_mtok} output (current: ${old_rates.input_mtok}, "
        f"${old_rates.cache_read_mtok}, ${old_rates.output_mtok})."
    )


def calc_cost(
    input_tokens: int,
    output_tokens: int,
    model_name: str,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> Decimal:
    """Cheaper-to-call cost helper for per-turn token totals.

    Same provider/model resolution as ``calc_session_cost``. Cache tokens
    follow the provider-reporting convention: a subset of ``input_tokens``,
    billed at the cache rates instead of the full input rate. Used by
    per-turn cost computations on the chat agent (chat, compaction, voice
    buckets each call this once). An unknown model returns Decimal("0") so
    the turn write still lands, but logs loudly — a $0 model means this
    module needs a price entry.
    """
    try:
        return calc_session_cost(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            model_name=model_name,
        )
    except LookupError:
        logger.warning(
            "No pricing found for model %r — recording cost as $0. "
            "Add rates to llm_pricing (or genai-prices) to bill this model.",
            model_name,
        )
        return Decimal("0")
