"""Tests for LLM cost calculation, especially DigitalOcean-served models.

genai-prices has no DigitalOcean provider, so DO models are priced from a
local rate table (DO serverless-inference rates, July 2026). These tests pin
the exact Decimal math so a silent pricing regression cannot reappear.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from smarter_dev.web.llm_pricing import calc_cost, calc_session_cost


class TestDigitalOceanPricing:
    def test_kimi_k26_input_and_output_rates(self):
        # 1M input @ $0.76 + 1M output @ $3.20
        assert calc_cost(1_000_000, 1_000_000, "kimi-k2.6") == Decimal("3.96")

    def test_glm_52_rates(self):
        assert calc_cost(1_000_000, 0, "glm-5.2") == Decimal("1.05")
        assert calc_cost(0, 1_000_000, "glm-5.2") == Decimal("4.40")

    def test_deepseek_4_flash_rates(self):
        assert calc_cost(1_000_000, 1_000_000, "deepseek-4-flash") == Decimal(
            "0.336"
        )

    def test_gemma_4_rates(self):
        assert calc_cost(1_000_000, 1_000_000, "gemma-4-31B-it") == Decimal("0.68")

    def test_qwen_35_rates(self):
        assert calc_cost(1_000_000, 1_000_000, "qwen3.5-397b-a17b") == Decimal(
            "2.835"
        )

    def test_small_token_counts_stay_exact_decimal(self):
        # 110 input + 45 output on kimi-k2.6:
        # (110 * 0.76 + 45 * 3.20) / 1_000_000
        cost = calc_cost(110, 45, "kimi-k2.6")
        assert cost == Decimal("110") * Decimal("0.76") / Decimal(
            "1000000"
        ) + Decimal("45") * Decimal("3.20") / Decimal("1000000")
        assert isinstance(cost, Decimal)

    def test_digitalocean_prefixed_name_also_resolves(self):
        assert calc_cost(1_000_000, 0, "digitalocean:kimi-k2.6") == Decimal("0.76")

    def test_cached_reads_bill_at_caching_rate_not_input_rate(self):
        # Providers report cache tokens as a SUBSET of input_tokens. 1M input
        # of which 600k were cached reads on kimi-k2.6:
        # 400k @ $0.76 + 600k @ $0.19 per Mtok.
        cost = calc_session_cost(
            input_tokens=1_000_000,
            output_tokens=0,
            cache_read_tokens=600_000,
            cache_write_tokens=0,
            model_name="kimi-k2.6",
        )
        assert cost == Decimal("0.418")

    def test_cache_tokens_without_caching_rate_bill_as_input(self):
        # gemma-4 has no prompt-caching tier on DO; the cached portion bills
        # at the plain input rate, so the split changes nothing.
        cost = calc_session_cost(
            input_tokens=2_000_000,
            output_tokens=0,
            cache_read_tokens=1_000_000,
            cache_write_tokens=1_000_000,
            model_name="gemma-4-31B-it",
        )
        assert cost == Decimal("0.36")

    def test_cache_tokens_exceeding_input_clamp_uncached_to_zero(self):
        # Defensive: a provider reporting cache reads without folding them
        # into input_tokens must not produce a negative uncached share.
        cost = calc_session_cost(
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=1_000_000,
            cache_write_tokens=0,
            model_name="kimi-k2.6",
        )
        assert cost == Decimal("0.19")

    def test_calc_cost_accepts_cache_token_split(self):
        cost = calc_cost(
            1_000_000,
            0,
            "kimi-k2.6",
            cache_read_tokens=600_000,
        )
        assert cost == Decimal("0.418")


class TestUnknownModelFallback:
    def test_unknown_model_returns_zero_and_logs_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="smarter_dev.web.llm_pricing"):
            cost = calc_cost(1000, 500, "totally-unknown-model-xyz")

        assert cost == Decimal("0")
        assert any(
            "totally-unknown-model-xyz" in record.message
            for record in caplog.records
        )

    def test_known_genai_prices_model_still_priced(self):
        # The patched google model must keep flowing through genai-prices.
        cost = calc_cost(1_000_000, 0, "google-gla:gemini-3.1-flash-lite")
        assert cost == Decimal("0.25")
