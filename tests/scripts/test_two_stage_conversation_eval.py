"""Cost-math tests for scripts/two_stage_conversation_eval.py.

The token aggregation and list-price cost are the correctness-critical parts of
the conversation cost report, so they get unit coverage even though the script
itself is a local harness.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import eval_prices  # noqa: E402
import two_stage_conversation_eval as harness  # noqa: E402

from smarter_dev.shared.model_catalog import get_model  # noqa: E402


@pytest.fixture(autouse=True, scope="module")
def _prices():
    eval_prices.install()


def test_token_use_add_and_total():
    a = harness.TokenUse(
        input_tokens=10, output_tokens=5, cache_read_tokens=2, cache_write_tokens=1
    )
    b = harness.TokenUse(
        input_tokens=3, output_tokens=4, cache_read_tokens=1, cache_write_tokens=0
    )
    combined = harness.total_tokens([a, b])
    assert combined.input_tokens == 13
    assert combined.output_tokens == 9
    assert combined.cache_read_tokens == 3
    assert combined.cache_write_tokens == 1


def test_total_tokens_empty_is_zero():
    empty = harness.total_tokens([])
    assert (empty.input_tokens, empty.output_tokens) == (0, 0)


def test_stage_cost_matches_list_price_terra():
    # Terra: $2 in / $12 out per 1M. 1M in + 1M out -> $14.
    terra = get_model("gpt-5-6-terra")
    cost = harness.stage_cost(
        harness.TokenUse(input_tokens=1_000_000, output_tokens=1_000_000), terra
    )
    assert cost == Decimal("14.00")


def test_stage_cost_prices_cache_read_and_write_separately():
    # 1M input of which 800k cache-read, 100k cache-write, on Terra:
    #   uncached 100k @ $2.00/M = 0.20
    #   cache-read 800k @ $0.20/M = 0.16
    #   cache-write 100k @ $2.50/M = 0.25  -> total 0.61
    terra = get_model("gpt-5-6-terra")
    cost = harness.stage_cost(
        harness.TokenUse(
            input_tokens=1_000_000,
            output_tokens=0,
            cache_read_tokens=800_000,
            cache_write_tokens=100_000,
        ),
        terra,
    )
    assert cost == Decimal("0.61")


def test_stage_cost_worker_cheaper_than_large_for_same_tokens():
    tokens = harness.TokenUse(input_tokens=500_000, output_tokens=200_000)
    worker_cost = harness.stage_cost(tokens, get_model("gemini-3-5-flash-lite"))
    large_cost = harness.stage_cost(tokens, get_model("gpt-5-6-terra"))
    assert worker_cost < large_cost
