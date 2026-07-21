"""Cost calculation for research sessions using genai-prices.

Patches missing models into the genai-prices snapshot at import time,
then exposes calc_session_cost() for computing per-session costs.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from genai_prices import calc_price, types
from genai_prices.data_snapshot import find_provider_by_id, get_snapshot

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

# GPT-5.4 (standard) — not yet in genai-prices
# NOTE: must come after gpt-5.4-nano so the more specific match wins
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

_TOKENS_PER_MTOK = Decimal("1000000")


def _digitalocean_cost(
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    price: types.ModelPrice,
) -> Decimal:
    """Direct cost computation for a DO-served model.

    Cache tokens follow the provider-reporting convention (same one
    genai-prices assumes): they are a SUBSET of ``input_tokens``. The
    uncached share bills at the input rate, cached reads at the model's
    prompt-caching rate when it has one (otherwise plain input), and cache
    writes at plain input (DO has no separate write tier). The uncached
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

    # DO-served models are unknown to genai-prices and billed at DO's own
    # rates; price them directly. Chat/voice/summarizer names arrive flat
    # (provider_id None), a "digitalocean:" prefix also resolves here.
    if provider_id in (None, "digitalocean"):
        digitalocean_price = _DIGITALOCEAN_PRICES.get(model_ref)
        if digitalocean_price is not None:
            return _digitalocean_cost(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
                price=digitalocean_price,
            )

    usage = types.Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
    )

    result = calc_price(usage, model_ref, provider_id=provider_id)
    return result.total_price


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
