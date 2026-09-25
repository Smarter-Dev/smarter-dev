"""Cost calculation for research sessions using genai-prices.

Patches missing models into the genai-prices snapshot at import time,
then exposes calc_session_cost() for computing per-session costs.

Chat and compaction costs are calculated once when their usage rows are created
and persisted with those rows. Updating a rate here therefore affects newly
ingested usage only; it does not retroactively reprice existing invoice rows.
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


# Every patch applied, so tests can check each one still prices something.
_PATCHES: list[tuple[str, types.ModelInfo]] = []


def _patch_provider(provider_id: str, model: types.ModelInfo) -> None:
    provider = find_provider_by_id(_snapshot.providers, provider_id)
    if provider is not None:
        provider.models.append(model)
        _PATCHES.append((provider_id, model))


# Patched ids that genai-prices also ships. A patch is APPENDED after the
# library's own entries, so the library's entry is found first and prices the
# id itself (and the dated snapshots its clause lists), long-context tier and
# all. These patches still price the other ids under their prefix, which the
# library's exact-id clauses miss, so they stay. All are retired or priced the
# same by the library, and usage costs are persisted at ingest, so no settled
# row changes. A guard test fails when this set drifts from the snapshot or a
# patch no longer prices anything (#5).
_LIBRARY_PRICES_OWN_ID = frozenset(
    {
        "gemini-3-flash-preview",
        "gemini-3.1-flash-lite",
        "gemini-3.5-flash",
        "gemini-2.5-flash-preview-tts",
        "gpt-5.4-nano",
        "gpt-5.4-mini",
        "gpt-5.4",
        "gpt-5.5",
        "gpt-5.6-luna",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
    }
)


# Gemini 3.1 Flash Lite (GA + preview prefix). Its
# direct pins moved to GPT-6 Luna on 2026-09-24; kept for the usage rows it
# wrote.
# genai-prices prices the id itself; this patch prices only other ids under
# its prefix (see _LIBRARY_PRICES_OWN_ID).
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

# Gemini 3 Flash Preview. Its direct pins moved to
# Gemini 3.8 Flash on 2026-09-24; kept for the usage rows it wrote.
# genai-prices prices the id itself; this patch prices only other ids under
# its prefix (see _LIBRARY_PRICES_OWN_ID).
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

# Gemini 3.5 Flash. Retired as a selectable model (replaced by 3.6 Flash).
# genai-prices prices the id itself; this patch prices only other ids under
# its prefix (see _LIBRARY_PRICES_OWN_ID).
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

# Gemini 3.5 Flash Lite and 3.6, 3.7 and 3.8 Flash are priced by genai-prices
# itself, at the rates this module used to patch in: 3.6-3.8 Flash at the
# $0.75/$3.75/$0.075 promotional rate through 2026-12-31, and the library
# already carries 3.8 Flash's 2027-01-01 reversion to $1.50/$7.50/$0.15. Their
# patches never applied (the library's identical prefixes match first) and were
# removed in #5. Only 3.8 Flash is live.

# Gemini 3.1 Pro — base tier (up to 200K input tokens). Google applies
# $4/$18 long-context pricing above 200K; the current usage ledger does not
# expose the threshold split, so admission uses the documented base rate.
# https://ai.google.dev/gemini-api/docs/pricing
_patch_provider(
    "google",
    types.ModelInfo(
        id="gemini-3.1-pro",
        match=types.ClauseStartsWith(starts_with="gemini-3.1-pro"),
        prices=types.ModelPrice(
            input_mtok=Decimal("2.00"),
            output_mtok=Decimal("12.00"),
            cache_read_mtok=Decimal("0.20"),
        ),
    ),
)

# GPT-5.4 Nano
# genai-prices prices the id itself; this patch prices only other ids under
# its prefix (see _LIBRARY_PRICES_OWN_ID).
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

# Gemini 2.5 Flash Preview TTS.
# Pricing per Google's docs (Gemini 2.5 Flash TTS preview tier):
#   text input  ~$0.50/M tokens
#   audio output ~$10.00/M tokens (1M tokens ≈ ~700-1k seconds @ 24kHz)
# These are approximations — the operator should true up against real bills
# if needed.
# genai-prices prices the id itself; this patch prices only other ids under
# its prefix (see _LIBRARY_PRICES_OWN_ID).
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

# GPT-5.5 — official legacy rate. Retired for GPT-6 Sol on 2026-09-24; kept
# for the usage rows it wrote. Long-context requests above 272K input
# tokens receive a 2x input / 1.5x output multiplier that cannot be separated
# from aggregate provider usage, so this is the documented base rate.
# https://developers.openai.com/api/docs/models/gpt-5.5
# genai-prices prices the id itself; this patch prices only other ids under
# its prefix (see _LIBRARY_PRICES_OWN_ID).
_patch_provider(
    "openai",
    types.ModelInfo(
        id="gpt-5.5",
        match=types.ClauseStartsWith(starts_with="gpt-5.5"),
        prices=types.ModelPrice(
            input_mtok=Decimal("5.00"),
            output_mtok=Decimal("30.00"),
            cache_read_mtok=Decimal("0.50"),
        ),
    ),
)

# GPT-5.6 Luna DIRECT-OpenAI rates, effective 2026-07-30 after OpenAI's 80%
# price cut. Luna moved to the OpenRouter route on 2026-08-06 (see
# _OPENROUTER_PRICES) and was retired for GPT-6 Luna on 2026-09-24; this patch
# stays so rows written before the move keep pricing at OpenAI's own rates.
# genai-prices prices the id itself; this patch prices only other ids under
# its prefix (see _LIBRARY_PRICES_OWN_ID).
_patch_provider(
    "openai",
    types.ModelInfo(
        id="gpt-5.6-luna",
        match=types.ClauseStartsWith(starts_with="gpt-5.6-luna"),
        prices=types.ModelPrice(
            input_mtok=Decimal("0.20"),
            output_mtok=Decimal("1.20"),
            cache_read_mtok=Decimal("0.02"),
            cache_write_mtok=Decimal("0.25"),
        ),
    ),
)

# GPT-5.6 Sol — retired for GPT-6 Sol on 2026-09-24; kept for the usage rows
# it wrote. Explicit cache writes are 1.25x
# uncached input and cache reads are 10% of uncached input.
# https://developers.openai.com/api/docs/models/gpt-5.6-sol
# genai-prices prices the id itself; this patch prices only other ids under
# its prefix (see _LIBRARY_PRICES_OWN_ID).
_patch_provider(
    "openai",
    types.ModelInfo(
        id="gpt-5.6-sol",
        match=types.ClauseStartsWith(starts_with="gpt-5.6-sol"),
        prices=types.ModelPrice(
            input_mtok=Decimal("5.00"),
            output_mtok=Decimal("30.00"),
            cache_read_mtok=Decimal("0.50"),
            cache_write_mtok=Decimal("6.25"),
        ),
    ),
)

# GPT-5.6 Terra. Rates effective 2026-07-30
# after OpenAI's 20% price cut. Retired for GPT-6 Sol on 2026-09-24; kept for
# the usage rows that carry its wire id.
# genai-prices prices the id itself; this patch prices only other ids under
# its prefix (see _LIBRARY_PRICES_OWN_ID).
_patch_provider(
    "openai",
    types.ModelInfo(
        id="gpt-5.6-terra",
        match=types.ClauseStartsWith(starts_with="gpt-5.6-terra"),
        prices=types.ModelPrice(
            input_mtok=Decimal("2.00"),
            output_mtok=Decimal("12.00"),
            cache_read_mtok=Decimal("0.20"),
            cache_write_mtok=Decimal("2.50"),
        ),
    ),
)

# GPT-6 Sol — not yet in genai-prices. Cache writes are 1.25x uncached input
# and cache reads are 10% of uncached input. Prompts above 272K input tokens
# are billed at 2x input/cache and 1.5x output for the whole request; this is
# the base rate, and _LONG_CONTEXT_TIERS carries the tier.
# https://developers.openai.com/api/docs/models/gpt-6-sol
_patch_provider(
    "openai",
    types.ModelInfo(
        id="gpt-6-sol",
        match=types.ClauseStartsWith(starts_with="gpt-6-sol"),
        prices=types.ModelPrice(
            input_mtok=Decimal("2.00"),
            output_mtok=Decimal("10.00"),
            cache_read_mtok=Decimal("0.20"),
            cache_write_mtok=Decimal("2.50"),
        ),
    ),
)

# GPT-6 Luna — not yet in genai-prices. Served by OpenAI directly, the
# server-default chat model since 2026-09-24. Same cache ratios and same
# >272K-input long-context multiplier as GPT-6 Sol, carried by
# _LONG_CONTEXT_TIERS.
# https://developers.openai.com/api/docs/models/gpt-6-luna
_patch_provider(
    "openai",
    types.ModelInfo(
        id="gpt-6-luna",
        match=types.ClauseStartsWith(starts_with="gpt-6-luna"),
        prices=types.ModelPrice(
            input_mtok=Decimal("0.10"),
            output_mtok=Decimal("0.50"),
            cache_read_mtok=Decimal("0.01"),
            cache_write_mtok=Decimal("0.125"),
        ),
    ),
)

# GPT-5.4 Mini — keep this more-specific prefix ahead of standard GPT-5.4.
# Retired for GPT-6 Luna on 2026-09-24; kept for the usage rows it wrote.
# genai-prices prices the id itself; this patch prices only other ids under
# its prefix (see _LIBRARY_PRICES_OWN_ID).
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

# GPT-5.4 (standard). Retired for GPT-6 Sol on
# 2026-09-24; kept for the usage rows it wrote.
# NOTE: must come after mini/nano so the more specific matches win.
# genai-prices prices the id itself; this patch prices only other ids under
# its prefix (see _LIBRARY_PRICES_OWN_ID).
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


# Claude Opus 5 and Sonnet 5 are priced by genai-prices itself, at the rates
# this module used to patch in ($5/$25 and Sonnet's $2/$10 introductory rate,
# 1.25x cache writes, 10% cache reads). Their patches never applied and were
# removed in #5; settled rows keep the cost persisted at ingest.


# ---------------------------------------------------------------------------
# DigitalOcean serverless inference pricing
# ---------------------------------------------------------------------------
# genai-prices has no DigitalOcean provider, and DO resells these open-weight
# models at its own rates (not the origin vendors'), so they are priced
# directly from this table. USD per million tokens, from
# https://docs.digitalocean.com/products/inference/details/pricing/ (2026-07).

_DIGITALOCEAN_PRICES: dict[str, types.ModelPrice] = {
    # Retired from the catalog on 2026-08-02 (superseded by Kimi K3 on Zen).
    # Kept because rows written while it was selectable still carry this id.
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
    # Re-read from DO's live pricing page on 2026-08-13. DO cut this rate from
    # $0.385/$2.45 after the table was first written on 2026-07-21 (the
    # 2026-07-24 Wayback snapshot still shows the old figure, so the original
    # entry was right when recorded). Rows written before this edit keep the
    # rate they were costed at; only new usage prices at the cut rate.
    "qwen3.5-397b-a17b": types.ModelPrice(
        input_mtok=Decimal("0.302"),
        output_mtok=Decimal("1.925"),
        cache_read_mtok=Decimal("0.111"),
    ),
}

# Models served through OpenRouter. USD per million tokens;
# cache is cached-input/read pricing. Cache writes bill at the input rate
# unless a cache_write_mtok tier is supplied.
# Rates read from GET https://openrouter.ai/api/v1/models (2026-08).
_OPENROUTER_PRICES: dict[str, types.ModelPrice] = {
    # Luna moved here from direct OpenAI on 2026-08-06: the same OpenAI
    # upstream at half the rate. Retired for GPT-6 Luna (direct) on 2026-09-24;
    # kept for the rows written while it served. OpenRouter's long-context override (>272K
    # prompt tokens: $0.20/$0.90) has no tier here; chat compacts far below
    # 272K, so the base rate is the honest one for our traffic.
    "openai/gpt-5.6-luna": types.ModelPrice(
        input_mtok=Decimal("0.10"),
        output_mtok=Decimal("0.60"),
        cache_read_mtok=Decimal("0.01"),
        cache_write_mtok=Decimal("0.125"),
    ),
    "poolside/laguna-xs-2.1": types.ModelPrice(
        input_mtok=Decimal("0.06"),
        output_mtok=Decimal("0.12"),
        cache_read_mtok=Decimal("0.03"),
    ),
    # Laguna S left the catalog on 2026-08-13; kept so rows written while it
    # was selectable keep pricing at the rate they ran under.
    "poolside/laguna-s-2.1": types.ModelPrice(
        input_mtok=Decimal("0.10"),
        output_mtok=Decimal("0.20"),
        cache_read_mtok=Decimal("0.01"),
    ),
    # Grok 4.5 doubles every rate once a single prompt exceeds 200K tokens
    # ($4/$12/$0.60). This table has no length tier, so long-context turns
    # under-bill; chat compacts well below 200K, so the base rate is the
    # honest one for our traffic. Retired 2026-08-13 in favour of 4.6; kept
    # for historical rows.
    "x-ai/grok-4.5": types.ModelPrice(
        input_mtok=Decimal("2.00"),
        output_mtok=Decimal("6.00"),
        cache_read_mtok=Decimal("0.30"),
    ),
    # Grok 4.6 carries 4.5's headline rate and the same >200K doubling
    # ($4/$12/$1.00), but charges more for cached reads: $0.50 against $0.30.
    # Retired 2026-09-24 in favour of 4.7; kept for historical rows.
    "x-ai/grok-4.6": types.ModelPrice(
        input_mtok=Decimal("2.00"),
        output_mtok=Decimal("6.00"),
        cache_read_mtok=Decimal("0.50"),
    ),
    # Grok 4.7 undercuts 4.6 by 20% on every axis — $1.60/$4.80/$0.40 — and
    # keeps the same >200K doubling ($3.20/$9.60/$0.80), priced from
    # _LONG_CONTEXT_TIERS for single requests only. Read from GET
    # https://openrouter.ai/api/v1/models on 2026-09-24.
    "x-ai/grok-4.7": types.ModelPrice(
        input_mtok=Decimal("1.60"),
        output_mtok=Decimal("4.80"),
        cache_read_mtok=Decimal("0.40"),
    ),
    # Qwen3.8 2.4T A95B. Every OpenRouter endpoint quotes the same rate, so
    # there is no route-dependent price to model here.
    "qwen/qwen3.8-2.4t-a95b": types.ModelPrice(
        input_mtok=Decimal("2.00"),
        output_mtok=Decimal("6.00"),
        cache_read_mtok=Decimal("0.20"),
    ),
    # --- Moved off Zen / Digital Ocean onto author-precision routes 2026-08-13.
    #
    # These rates are MEASURED, not quoted. OpenRouter load-balances across every
    # endpoint a model's OpenRouterRouting admits rather than always taking the
    # cheapest, so the headline price of the endpoint we would prefer is not what
    # traffic actually bills at. Each figure below is the endpoint that served
    # the majority of a 6-request sample on 2026-08-13, and each is bounded above
    # by that entry's max_price ceiling.
    #
    # This table holds ONE rate per wire id, so it cannot represent a pool
    # honestly — a turn served by a different endpoint in the pool bills at this
    # rate regardless. The durable fix is to read the exact per-call figure that
    # OpenRouter already returns on ``usage.cost`` instead of inferring it here.
    #
    # Gemma: bf16 pool, sampled across open-inference/coreweave/venice/novita
    # ($0.08-$0.14 in, $0.34-$0.40 out). Priced mid-pool. Still under DO's
    # $0.18/$0.50, which was undeclared precision.
    "google/gemma-4-31b-it": types.ModelPrice(
        input_mtok=Decimal("0.12"),
        output_mtok=Decimal("0.36"),
        cache_read_mtok=Decimal("0.09"),
    ),
    # Qwen3.6 Plus: Alibaba's own and only endpoint, so this rate is exact — no
    # pool, nothing to average. No cache tier published, so cached reads fall
    # through to the input rate.
    "qwen/qwen3.6-plus": types.ModelPrice(
        input_mtok=Decimal("0.325"),
        output_mtok=Decimal("1.95"),
    ),
    # GLM-5.2: fp8 pool. Sampling never reached sail-research's $0.50/$3.15 —
    # gmicloud and novita split every request at ~$0.742/$2.332, so that is what
    # this bills at. Against Z.AI's own $1.40/$4.40 for the same fp8 build.
    # Retired from the catalog 2026-08-27 in favour of GLM-5.3-Flash; kept
    # because rows written while it was selectable still carry this id.
    "z-ai/glm-5.2": types.ModelPrice(
        input_mtok=Decimal("0.742"),
        output_mtok=Decimal("2.332"),
        cache_read_mtok=Decimal("0.1378"),
    ),
    # GLM-5.3-Flash: unlike 5.2's pool, every endpoint the catalog's routing
    # admits (parasail, reka, deepinfra, baseten, gmicloud, modal, io-net,
    # cloudflare) quotes the identical flat rate — no sampling needed, this is
    # the declared price on every one of them (2026-08-27).
    "z-ai/glm-5.3-flash": types.ModelPrice(
        input_mtok=Decimal("0.15"),
        output_mtok=Decimal("0.50"),
        cache_read_mtok=Decimal("0.03"),
    ),
    # DeepSeek V4 Pro: baidu/fp8 served 5 of 6, streamlake/fp8 the other one,
    # never the authors' endpoint. Priced at baidu's rate. Against Zen's
    # $1.74/$3.48 for the same model this is a quarter of the cost.
    "deepseek/deepseek-v4-pro": types.ModelPrice(
        input_mtok=Decimal("0.4225"),
        output_mtok=Decimal("0.845"),
        cache_read_mtok=Decimal("0.035"),
    ),
    # DeepSeek V4 Flash: streamlake/fp8 served 6 of 6, never the authors' own
    # endpoint — consistent with DeepSeek rate-limiting heavy callers. So the
    # authors' $0.0028 cache read is NOT what we get; streamlake's $0.0173 is,
    # which still beats the $0.028 Zen charged, at 38% less input and output.
    "deepseek/deepseek-v4-flash": types.ModelPrice(
        input_mtok=Decimal("0.0867"),
        output_mtok=Decimal("0.1733"),
        cache_read_mtok=Decimal("0.0173"),
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
    writes at the model's write tier when it has one (otherwise plain
    input). The uncached share is clamped at zero in case a provider
    reports cache tokens without folding them into ``input_tokens``.
    """
    input_rate = price.input_mtok
    cache_read_rate = (
        price.cache_read_mtok if price.cache_read_mtok is not None else input_rate
    )
    cache_write_rate = (
        price.cache_write_mtok if price.cache_write_mtok is not None else input_rate
    )
    uncached_input_tokens = max(
        input_tokens - cache_read_tokens - cache_write_tokens, 0
    )
    return (
        Decimal(uncached_input_tokens) * input_rate
        + Decimal(output_tokens) * price.output_mtok
        + Decimal(cache_read_tokens) * cache_read_rate
        + Decimal(cache_write_tokens) * cache_write_rate
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


@dataclass(frozen=True, slots=True)
class LongContextTier:
    """A whole request's price multipliers once its prompt reaches a size."""

    min_input_tokens: int
    input_multiplier: Decimal  # also applies to cache reads and writes
    output_multiplier: Decimal


# Models whose price rises for the whole request once a single prompt reaches a
# token count. The patches and tables above carry the base rate; the tier is
# applied only when the tokens priced are ONE provider request's
# (``per_request=True``: web chat settles each request on its own). Everywhere
# else the counts are a turn's or session's total across several requests, and
# a tier keyed on that total would surcharge turns whose requests were each
# well under the threshold. Those totals stay at the base rate, which
# undercharges a request that really did reach the tier, so a total over the
# threshold is logged rather than priced silently.
_LONG_CONTEXT_TIERS: dict[str, LongContextTier] = {
    # OpenAI: prompts above 272K bill 2x input/cache and 1.5x output.
    "gpt-6-sol": LongContextTier(272_001, Decimal("2"), Decimal("1.5")),
    "gpt-6-luna": LongContextTier(272_001, Decimal("2"), Decimal("1.5")),
    # OpenRouter's xAI endpoints: an override from min_prompt_tokens 200000
    # doubles prompt, cache read and completion ($3.20/$0.80/$9.60). Read from
    # GET /api/v1/models/x-ai/grok-4.7/endpoints on 2026-09-24.
    "x-ai/grok-4.7": LongContextTier(200_000, Decimal("2"), Decimal("2")),
}


def long_context_tier(model_ref: str, input_tokens: int) -> LongContextTier | None:
    """The tier one request of *input_tokens* bills at, or None for base rate.

    *model_ref* is a wire id with or without its ``provider:`` prefix.
    """
    model_ref = model_ref.split(":", 1)[-1]
    for prefix, tier in _LONG_CONTEXT_TIERS.items():
        if model_ref.startswith(prefix):
            return tier if input_tokens >= tier.min_input_tokens else None
    return None


def calc_session_cost(
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    model_name: str,
    *,
    per_request: bool = False,
) -> Decimal:
    """Calculate the USD cost for a research session.

    *model_name* is the pydantic-ai model string, e.g.
    ``"google-gla:gemini-3.1-flash-lite-preview"``.  We split on ``":"``
    to extract the provider_id and model_ref expected by genai-prices.
    Pass ``per_request=True`` only when the counts are one provider
    request's, so a long-context tier can be priced (see
    ``_LONG_CONTEXT_TIERS``).
    """
    tier = long_context_tier(model_name, input_tokens)
    if tier is not None and not per_request:
        logger.warning(
            "%s usage of %d input tokens priced at the base rate; if one "
            "request reached %d tokens, it bills at %sx input and cache and "
            "%sx output",
            model_name,
            input_tokens,
            tier.min_input_tokens,
            tier.input_multiplier,
            tier.output_multiplier,
        )
    if tier is None or not per_request:
        return _base_cost(
            input_tokens,
            output_tokens,
            cache_read_tokens,
            cache_write_tokens,
            model_name,
        )
    # Every rate is linear in its tokens, so the input side and the output side
    # can be priced apart and scaled by their own multipliers.
    input_cost = _base_cost(
        input_tokens, 0, cache_read_tokens, cache_write_tokens, model_name
    )
    output_cost = _base_cost(0, output_tokens, 0, 0, model_name)
    return input_cost * tier.input_multiplier + output_cost * tier.output_multiplier


def _base_cost(
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    model_name: str,
) -> Decimal:
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
        # Base rates: a synthetic 1M-token prompt is not a long-context request.
        uncached = _base_cost(1_000_000, 0, 0, 0, model_name)
        output = _base_cost(0, 1_000_000, 0, 0, model_name)
        cached = _base_cost(1_000_000, 0, 1_000_000, 0, model_name)
        cache_write = _base_cost(1_000_000, 0, 0, 1_000_000, model_name)
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
    *,
    per_request: bool = False,
) -> Decimal:
    """Cheaper-to-call cost helper for per-turn token totals.

    Same provider/model resolution as ``calc_session_cost``. Cache tokens
    follow the provider-reporting convention: a subset of ``input_tokens``,
    billed at the cache rates instead of the full input rate. Used by
    per-turn cost computations on the chat agent (chat, compaction, voice
    buckets each call this once); ``per_request`` is as in
    ``calc_session_cost``. An unknown model returns Decimal("0") so
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
            per_request=per_request,
        )
    except LookupError:
        logger.warning(
            "No pricing found for model %r — recording cost as $0. "
            "Add rates to llm_pricing (or genai-prices) to bill this model.",
            model_name,
        )
        return Decimal("0")
