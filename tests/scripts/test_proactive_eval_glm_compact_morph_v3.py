from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from openai import RateLimitError

from scripts.proactive_eval.glm_compact_morph_v3 import DEFAULT_PLAN
from scripts.proactive_eval.glm_compact_morph_v3 import MAX_ATTEMPTS
from scripts.proactive_eval.glm_compact_morph_v3 import PLAN_VERSION
from scripts.proactive_eval.glm_compact_morph_v3 import PROVIDER_NAME
from scripts.proactive_eval.glm_compact_morph_v3 import PROVIDER_SLUG
from scripts.proactive_eval.glm_compact_morph_v3 import RETRY_V4
from scripts.proactive_eval.glm_compact_morph_v3 import SCREEN_PROVIDERS
from scripts.proactive_eval.glm_compact_morph_v3 import SCREEN_V5
from scripts.proactive_eval.glm_compact_morph_v3 import STOP_V3
from scripts.proactive_eval.glm_compact_morph_v3 import _screen_retry_delay
from scripts.proactive_eval.glm_compact_morph_v3 import build_plan
from scripts.proactive_eval.glm_compact_morph_v3 import build_screening_plan
from scripts.proactive_eval.glm_compact_morph_v3 import build_wafer_tuning24_plan
from scripts.proactive_eval.glm_compact_morph_v3 import confirmed_provider
from scripts.proactive_eval.glm_compact_morph_v3 import enriched_error_diagnostics
from scripts.proactive_eval.glm_compact_morph_v3 import is_temporary_overload
from scripts.proactive_eval.glm_compact_morph_v3 import morph_price
from scripts.proactive_eval.glm_compact_morph_v3 import provider_price
from scripts.proactive_eval.glm_compact_morph_v3 import retry_delay_seconds
from scripts.proactive_eval.glm_compact_morph_v3 import run
from scripts.proactive_eval.glm_compact_morph_v3 import safe_nested_upstream_metadata


def test_plan_freezes_original_cases_and_morph_controls() -> None:
    plan = build_plan()

    assert plan["plan_version"] == PLAN_VERSION
    assert plan["strata"] == {"required": 2, "negative": 2, "ambiguous": 0}
    assert len(plan["case_ids"]) == MAX_ATTEMPTS
    assert plan["controls"]["provider_only"] == [PROVIDER_SLUG]
    assert plan["controls"]["require_parameters"] is True
    assert plan["controls"]["provider_fallbacks"] is False
    assert plan["controls"]["client_retries"] == 0
    assert plan["controls"]["concurrency"] == 1
    assert plan["controls"]["minimum_seconds_between_requests"] == 60
    assert plan["controls"]["stop_on_first_error"] is True
    assert plan["controls"]["overwrite_or_resume"] is False
    assert plan["workload"]["conservative_maximum_cost_usd"] < 0.01
    assert plan["workload"]["cumulative_conservative_maximum_usd"] < 5


def test_existing_v3_plan_remains_reproducible() -> None:
    assert build_plan(STOP_V3) == json.loads(DEFAULT_PLAN.read_text(encoding="utf-8"))


def test_retry_plan_is_bounded_and_references_v3() -> None:
    plan = build_plan(RETRY_V4)

    assert plan["plan_version"] == "glm-compact-morph-retry-v4"
    assert plan["controls"]["maximum_attempts"] == 8
    assert plan["controls"]["maximum_retries_per_case"] == 2
    assert plan["controls"]["retry_backoff_seconds"] == [60.0, 120.0]
    assert plan["controls"]["maximum_retry_delay_seconds"] == 300.0
    assert plan["controls"]["hard_spend_cap_usd"] == 0.02
    assert plan["controls"]["prior_conservative_glm_usd"] == 0.01661485
    assert plan["workload"]["conservative_maximum_cost_usd"] < 0.02
    assert plan["workload"]["cumulative_conservative_maximum_usd"] < 5


def test_provider_screen_plan_freezes_order_capabilities_prices_and_caps() -> None:
    plan = build_screening_plan()

    assert build_plan(SCREEN_V5) == plan
    assert plan["controls"]["provider_order"] == [
        "wafer",
        "fireworks",
        "together",
    ]
    assert plan["controls"]["maximum_total_attempts"] == 24
    assert plan["controls"]["maximum_retries_per_case"] == 1
    assert plan["controls"]["provider_fallbacks"] is False
    assert plan["provider_catalog"]["verified_date"] == "2026-09-22"
    assert plan["provider_catalog"]["providers"][0]["prices_per_million_usd"] == {
        "input": 0.10,
        "output": 0.35,
        "cache_read": 0.02,
    }
    assert plan["workload"]["conservative_maximum_cost_usd"] < 0.10
    assert plan["workload"]["cumulative_conservative_maximum_usd"] < 5


def test_wafer_tuning_plan_reuses_screen_and_fits_remaining_attempts() -> None:
    plan = build_wafer_tuning24_plan()

    assert plan["workload"]["distinct_cases"] == 24
    assert plan["workload"]["reused_valid_decisions"] == 4
    assert plan["workload"]["new_provider_calls"] == 20
    assert len(plan["reused_screen_case_ids"]) == 4
    assert len(plan["new_case_ids"]) == 20
    assert plan["controls"]["provider_only"] == ["wafer"]
    assert plan["controls"]["provider_fallbacks"] is False
    assert plan["workload"]["phase_conservative_accounting_usd"] < 0.10
    assert plan["workload"]["cumulative_conservative_glm_usd"] < 5


def test_morph_price_does_not_double_charge_cached_tokens() -> None:
    assert morph_price(
        {"input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 40}
    ) == pytest.approx((60 * 0.08 + 40 * 0.016 + 20 * 0.28) / 1_000_000)


def test_provider_price_does_not_double_charge_cached_tokens() -> None:
    provider = SCREEN_PROVIDERS[0]
    assert provider_price(
        provider,
        {"input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 40},
    ) == pytest.approx((60 * 0.10 + 40 * 0.02 + 20 * 0.35) / 1_000_000)


def test_provider_confirmation_accepts_only_exact_morph_metadata() -> None:
    response = SimpleNamespace(provider="Morph", model_extra={})
    assert confirmed_provider(response, {}) == PROVIDER_NAME
    assert (
        confirmed_provider(SimpleNamespace(provider="DeepInfra", model_extra={}), {})
        is None
    )
    assert (
        confirmed_provider(
            SimpleNamespace(provider=None, model_extra={}),
            {"X-OpenRouter-Provider": "morph"},
        )
        == PROVIDER_NAME
    )
    assert (
        confirmed_provider(
            SimpleNamespace(provider=None, model_extra={}),
            {"X-OpenRouter-Provider": "morph-secret"},
        )
        is None
    )


def test_provider_confirmation_supports_explicit_screen_provider() -> None:
    assert (
        confirmed_provider(
            SimpleNamespace(provider="Wafer", model_extra={}),
            {},
            provider_name="Wafer",
            provider_slug="wafer",
        )
        == "Wafer"
    )


def _rate_limit_error(body: dict) -> RateLimitError:
    request = httpx.Request("POST", "https://llmproxy.zech.sh/v1/chat/completions")
    response = httpx.Response(429, request=request, json={"error": body})
    return RateLimitError("private raw message", response=response, body=body)


def test_nested_upstream_metadata_is_allowlisted_and_drives_retry() -> None:
    error = _rate_limit_error(
        {
            "provider_name": "Morph",
            "provider_error_code": "service_overloaded",
            "limit_source": "upstream_provider_shared_pool",
            "error_rate_limit_category": "vendor_rate_limit",
            "attempted_retries": 0,
            "retry_after_seconds": 1,
            "metadata": {
                "headers": {
                    "Retry-After": "90",
                    "Authorization": "Bearer secret",
                    "Set-Cookie": "secret-cookie",
                },
                "raw_message": "private body",
            },
        }
    )

    nested = safe_nested_upstream_metadata(error)
    diagnostics = enriched_error_diagnostics(error)

    assert nested == {
        "provider_name": "Morph",
        "provider_error_code": "service_overloaded",
        "limit_source": "upstream_provider_shared_pool",
        "error_rate_limit_category": "vendor_rate_limit",
        "attempted_retries": 0,
        "retry_after_seconds": 90.0,
    }
    assert is_temporary_overload(diagnostics) is True
    assert retry_delay_seconds(RETRY_V4, diagnostics, 1) == 90
    assert retry_delay_seconds(RETRY_V4, diagnostics, 2) == 120
    assert "secret" not in str(diagnostics).lower()
    assert "private" not in str(diagnostics).lower()


def test_retry_requires_clear_overload_and_honors_five_minute_stop() -> None:
    unknown_429 = enriched_error_diagnostics(_rate_limit_error({"message": "busy"}))
    long_delay = enriched_error_diagnostics(
        _rate_limit_error(
            {
                "provider_name": "Morph",
                "provider_error_code": "engine_overloaded",
                "limit_source": "upstream_provider_shared_pool",
                "retry_after_seconds": 301,
            }
        )
    )

    assert retry_delay_seconds(RETRY_V4, unknown_429, 1) is None
    assert retry_delay_seconds(RETRY_V4, long_delay, 1) is None
    assert retry_delay_seconds(RETRY_V4, long_delay, 3) is None


def test_screen_retries_one_429_or_timeout_with_bounded_delay() -> None:
    rate_limit = {
        "status_code": 429,
        "provider_error_category": "rate_limit",
        "retry_after_seconds": 90,
    }
    timeout = {
        "status_code": None,
        "provider_error_category": "timeout",
        "retry_after_seconds": None,
    }

    assert _screen_retry_delay(rate_limit, 1) == 90
    assert _screen_retry_delay(timeout, 1) == 60
    assert _screen_retry_delay(rate_limit, 2) is None
    assert _screen_retry_delay({**rate_limit, "retry_after_seconds": 301}, 1) is None


@pytest.mark.asyncio
async def test_morph_run_refuses_to_resume_or_overwrite(tmp_path: Path) -> None:
    output = tmp_path / "existing.json"
    output.write_text("{}", encoding="utf-8")

    with pytest.raises(SystemExit, match="refusing overwrite/resume"):
        await run(tmp_path / "plan.json", output)
