"""Cost calculation for research sessions using genai-prices.

Patches missing models into the genai-prices snapshot at import time,
then exposes calc_session_cost() for computing per-session costs.
"""

from __future__ import annotations

from decimal import Decimal

from genai_prices import calc_price, types
from genai_prices.data_snapshot import find_provider_by_id, get_snapshot

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
) -> Decimal:
    """Cheaper-to-call cost helper for ad-hoc input/output token totals.

    Same provider/model resolution as ``calc_session_cost`` but without
    cache-token bookkeeping. Used by per-turn cost computations on the
    chat agent (chat, compaction, voice buckets each call this once).
    Returns Decimal("0") on any unknown-model failure so callers don't
    have to wrap.
    """
    try:
        return calc_session_cost(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=0,
            cache_write_tokens=0,
            model_name=model_name,
        )
    except Exception:
        return Decimal("0")
