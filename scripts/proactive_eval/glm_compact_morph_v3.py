#!/usr/bin/env python
"""Prepare or run the immutable Morph-routed four-case GLM diagnostic.

This is a new diagnostic over the original compact plan's two positives and
two negatives. It preserves the compact contract, pins OpenRouter to Morph,
paces requests, refuses resume/overwrite, and stops on the first error.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import statistics
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import AsyncOpenAI

from scripts.proactive_eval.benchmark_manifest import DATA_DIR
from scripts.proactive_eval.glm_compact_paced_v2 import retry_after_seconds
from scripts.proactive_eval.glm_compact_paced_v2 import safe_error_diagnostics
from scripts.proactive_eval.glm_compact_paced_v2 import safe_response_metadata
from scripts.proactive_eval.glm_compact_pilot import BASELINE_RESULT
from scripts.proactive_eval.glm_compact_pilot import DEFAULT_PLAN as ORIGINAL_PLAN
from scripts.proactive_eval.glm_compact_pilot import JUDGMENT_FIELDS
from scripts.proactive_eval.glm_compact_pilot import MAX_OUTPUT_TOKENS
from scripts.proactive_eval.glm_compact_pilot import PROMPT_OVERHEAD_TOKENS
from scripts.proactive_eval.glm_compact_pilot import REASONING_EFFORT
from scripts.proactive_eval.glm_compact_pilot import _load_source_cases
from scripts.proactive_eval.glm_compact_pilot import compact_messages
from scripts.proactive_eval.glm_compact_pilot import compact_response_format
from scripts.proactive_eval.glm_compact_pilot import parse_completion
from scripts.proactive_eval.glm_pilot import REQUESTED_MODEL
from scripts.proactive_eval.glm_pilot import TIMEOUT_SECONDS
from scripts.proactive_eval.glm_pilot import UPSTREAM_MODEL
from scripts.proactive_eval.glm_pilot import api_base_from_env
from scripts.proactive_eval.jev_pilot import DEFAULT_MANIFEST
from scripts.proactive_eval.jev_pilot import DEFAULT_SELECTION
from smarter_dev.bot.proactive.watcher import decision_from_jev

REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")

EXPECTED_ORIGINAL_PLAN_SHA256 = (
    "682afad09da69269abb2df3f632034951c56cb86829a13db5dea266ee50e67a1"
)
JEV_RESULT = DATA_DIR / "runs" / "jev-pilot.json"
TOTAL_GLM_CAP_USD = 5.0

PROVIDER_NAME = "Morph"
PROVIDER_SLUG = "morph"
PROVIDER_CATALOG_TAG = "morph"
PROVIDER_CATALOG_VERIFIED_DATE = "2026-09-21"
MORPH_INPUT_PRICE_PER_MILLION = 0.08
MORPH_OUTPUT_PRICE_PER_MILLION = 0.28
MORPH_CACHE_READ_PRICE_PER_MILLION = 0.016
REQUIRED_PROVIDER_PARAMETERS = (
    "response_format",
    "structured_outputs",
    "reasoning_effort",
)


@dataclass(frozen=True)
class RunConfig:
    name: str
    plan_version: str
    plan_path: Path
    output_path: Path
    prior_conservative_glm_usd: float
    incremental_spend_cap_usd: float
    maximum_total_attempts: int
    maximum_retries_per_case: int
    retry_backoff_seconds: tuple[float, ...]
    maximum_retry_delay_seconds: float
    minimum_seconds_between_requests: float
    retry_temporary_overload: bool


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    slug: str
    endpoint_name: str
    input_price_per_million: float
    output_price_per_million: float
    cache_read_price_per_million: float


STOP_V3 = RunConfig(
    name="morph-stop-v3",
    plan_version="glm-compact-morph-v3",
    plan_path=DATA_DIR / "glm-compact-morph-v3-plan.json",
    output_path=DATA_DIR / "runs" / "glm-compact-morph-v3.json",
    prior_conservative_glm_usd=0.01558013,
    incremental_spend_cap_usd=0.01,
    maximum_total_attempts=4,
    maximum_retries_per_case=0,
    retry_backoff_seconds=(),
    maximum_retry_delay_seconds=0.0,
    minimum_seconds_between_requests=60.0,
    retry_temporary_overload=False,
)
RETRY_V4 = RunConfig(
    name="morph-retry-v4",
    plan_version="glm-compact-morph-retry-v4",
    plan_path=DATA_DIR / "glm-compact-morph-retry-v4-plan.json",
    output_path=DATA_DIR / "runs" / "glm-compact-morph-retry-v4.json",
    prior_conservative_glm_usd=0.01661485,
    incremental_spend_cap_usd=0.02,
    maximum_total_attempts=8,
    maximum_retries_per_case=2,
    retry_backoff_seconds=(60.0, 120.0),
    maximum_retry_delay_seconds=300.0,
    minimum_seconds_between_requests=60.0,
    retry_temporary_overload=True,
)
SCREEN_V5 = RunConfig(
    name="provider-screen-v5",
    plan_version="glm-provider-screen-v5",
    plan_path=DATA_DIR / "glm-provider-screen-v5-plan.json",
    output_path=DATA_DIR / "runs" / "glm-provider-screen-v5.json",
    prior_conservative_glm_usd=0.01791813,
    incremental_spend_cap_usd=0.10,
    maximum_total_attempts=24,
    maximum_retries_per_case=1,
    retry_backoff_seconds=(60.0,),
    maximum_retry_delay_seconds=300.0,
    minimum_seconds_between_requests=60.0,
    retry_temporary_overload=True,
)
RUN_CONFIGS = {config.name: config for config in (STOP_V3, RETRY_V4, SCREEN_V5)}

SCREEN_PROVIDERS = (
    ProviderSpec(
        "Wafer", "wafer", "Wafer | z-ai/glm-5.3-flash-20260826", 0.10, 0.35, 0.02
    ),
    ProviderSpec(
        "Fireworks",
        "fireworks",
        "Fireworks | z-ai/glm-5.3-flash-20260826",
        0.15,
        0.50,
        0.03,
    ),
    ProviderSpec(
        "Together",
        "together",
        "Together | z-ai/glm-5.3-flash-20260826",
        0.15,
        0.50,
        0.03,
    ),
)
SCREEN_CATALOG_VERIFIED_DATE = "2026-09-22"
WAFER_TUNING24_PLAN_VERSION = "glm-wafer-tuning24-v1"
WAFER_TUNING24_PLAN = DATA_DIR / f"{WAFER_TUNING24_PLAN_VERSION}-plan.json"
WAFER_TUNING24_OUTPUT = DATA_DIR / "runs" / f"{WAFER_TUNING24_PLAN_VERSION}.json"

# Backwards-compatible aliases keep the frozen v3 plan reproducible for tests.
PLAN_VERSION = STOP_V3.plan_version
DEFAULT_PLAN = STOP_V3.plan_path
DEFAULT_OUTPUT = STOP_V3.output_path
MAX_ATTEMPTS = 4
MINIMUM_SECONDS_BETWEEN_REQUESTS = STOP_V3.minimum_seconds_between_requests
HARD_SPEND_CAP_USD = STOP_V3.incremental_spend_cap_usd
PRIOR_CONSERVATIVE_GLM_USD = STOP_V3.prior_conservative_glm_usd

EXPECTED_STOP_V3_RESULT_SHA256 = (
    "70da85881e61d68655c49755cf8e46a57dbb0748b5fa19a60cc2cd1e16a2de21"
)
_SAFE_TOKEN = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_TEMPORARY_OVERLOAD_CODES = {"engine_overloaded", "service_overloaded"}

_PROVIDER_HEADER_NAMES = (
    "x-openrouter-provider",
    "x-litellm-provider",
    "x-provider",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def morph_price(usage: dict[str, int]) -> float:
    cached = int(usage.get("cache_read_tokens", 0) or 0)
    uncached = max(0, int(usage.get("input_tokens", 0) or 0) - cached)
    output = int(usage.get("output_tokens", 0) or 0)
    return (
        uncached * MORPH_INPUT_PRICE_PER_MILLION
        + cached * MORPH_CACHE_READ_PRICE_PER_MILLION
        + output * MORPH_OUTPUT_PRICE_PER_MILLION
    ) / 1_000_000


def provider_price(provider: ProviderSpec, usage: dict[str, int]) -> float:
    cached = int(usage.get("cache_read_tokens", 0) or 0)
    uncached = max(0, int(usage.get("input_tokens", 0) or 0) - cached)
    output = int(usage.get("output_tokens", 0) or 0)
    return (
        uncached * provider.input_price_per_million
        + cached * provider.cache_read_price_per_million
        + output * provider.output_price_per_million
    ) / 1_000_000


def conservative_case_cost(case: dict) -> float:
    message_characters = sum(
        len(message["content"]) for message in compact_messages(case)
    )
    schema_characters = len(
        json.dumps(compact_response_format(), separators=(",", ":"))
    )
    input_tokens = message_characters + schema_characters + PROMPT_OVERHEAD_TOKENS
    return (
        input_tokens * MORPH_INPUT_PRICE_PER_MILLION
        + MAX_OUTPUT_TOKENS * MORPH_OUTPUT_PRICE_PER_MILLION
    ) / 1_000_000


def conservative_provider_case_cost(case: dict, provider: ProviderSpec) -> float:
    message_characters = sum(
        len(message["content"]) for message in compact_messages(case)
    )
    schema_characters = len(
        json.dumps(compact_response_format(), separators=(",", ":"))
    )
    input_tokens = message_characters + schema_characters + PROMPT_OVERHEAD_TOKENS
    return (
        input_tokens * provider.input_price_per_million
        + MAX_OUTPUT_TOKENS * provider.output_price_per_million
    ) / 1_000_000


def confirmed_provider(
    response: Any,
    headers: Any,
    *,
    provider_name: str = PROVIDER_NAME,
    provider_slug: str = PROVIDER_SLUG,
) -> str | None:
    """Return a provider only when response metadata independently names it."""
    candidates = [getattr(response, "provider", None)]
    extra = getattr(response, "model_extra", None)
    if isinstance(extra, dict):
        candidates.append(extra.get("provider"))
    normalized_headers = {
        str(key).lower(): value for key, value in (headers or {}).items()
    }
    candidates.extend(normalized_headers.get(name) for name in _PROVIDER_HEADER_NAMES)
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip().lower() == provider_slug:
            return provider_name
    return None


def _walk_mappings(value: Any) -> list[Mapping[str, Any]]:
    pending = [value]
    mappings: list[Mapping[str, Any]] = []
    seen: set[int] = set()
    while pending and len(mappings) < 64:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, Mapping):
            mappings.append(current)
            pending.extend(current.values())
        elif isinstance(current, list | tuple):
            pending.extend(current)
    return mappings


def _safe_token(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized if _SAFE_TOKEN.fullmatch(normalized) else None


def _safe_retry_seconds(value: Any) -> float | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds < 0 or seconds > 86_400:
        return None
    return seconds


def safe_nested_upstream_metadata(
    error: Exception,
    *,
    provider_name: str = PROVIDER_NAME,
    provider_slug: str = PROVIDER_SLUG,
) -> dict[str, Any]:
    """Extract an exact, validated allowlist from an API error body.

    The raw body, messages, unknown keys, headers, and credential-like values
    are never returned or persisted.
    """
    body = getattr(error, "body", None)
    result: dict[str, Any] = {}
    token_fields = {
        "provider_error_code",
        "limit_source",
        "error_rate_limit_category",
    }
    retry_hints: list[float] = []
    for mapping in _walk_mappings(body):
        lowered = {str(key).lower(): value for key, value in mapping.items()}
        provider = lowered.get("provider_name")
        if isinstance(provider, str) and provider.strip().lower() == provider_slug:
            result["provider_name"] = provider_name
        for field in token_fields:
            token = _safe_token(lowered.get(field))
            if token is not None:
                result[field] = token
        attempted_retries = lowered.get("attempted_retries")
        if isinstance(attempted_retries, int) and 0 <= attempted_retries <= 100:
            result["attempted_retries"] = attempted_retries
        direct_retry = _safe_retry_seconds(lowered.get("retry_after_seconds"))
        if direct_retry is not None:
            retry_hints.append(direct_retry)
        nested_retry = _safe_retry_seconds(lowered.get("retry-after"))
        if nested_retry is not None:
            retry_hints.append(nested_retry)
    if retry_hints:
        result["retry_after_seconds"] = max(retry_hints)
    return result


def enriched_error_diagnostics(error: Exception) -> dict[str, Any]:
    diagnostics = safe_error_diagnostics(error)
    upstream = safe_nested_upstream_metadata(error)
    if upstream:
        diagnostics["upstream_metadata"] = upstream
    hints = [
        diagnostics.get("retry_after_seconds"),
        upstream.get("retry_after_seconds"),
    ]
    valid_hints = [float(value) for value in hints if value is not None]
    diagnostics["retry_after_seconds"] = max(valid_hints) if valid_hints else None
    return diagnostics


def provider_error_diagnostics(
    error: Exception, provider: ProviderSpec
) -> dict[str, Any]:
    diagnostics = safe_error_diagnostics(error)
    upstream = safe_nested_upstream_metadata(
        error,
        provider_name=provider.name,
        provider_slug=provider.slug,
    )
    if upstream:
        diagnostics["upstream_metadata"] = upstream
    hints = [
        diagnostics.get("retry_after_seconds"),
        upstream.get("retry_after_seconds"),
    ]
    valid_hints = [float(value) for value in hints if value is not None]
    diagnostics["retry_after_seconds"] = max(valid_hints) if valid_hints else None
    return diagnostics


def is_temporary_overload(failure: dict[str, Any]) -> bool:
    upstream = failure.get("upstream_metadata") or {}
    return (
        failure.get("status_code") == 429
        and upstream.get("provider_name") == PROVIDER_NAME
        and upstream.get("provider_error_code") in _TEMPORARY_OVERLOAD_CODES
        and upstream.get("limit_source") == "upstream_provider_shared_pool"
    )


def retry_delay_seconds(
    config: RunConfig, failure: dict, retry_index: int
) -> float | None:
    """Return a bounded retry delay, or None when this failure must stop."""
    if not config.retry_temporary_overload or not is_temporary_overload(failure):
        return None
    if retry_index < 1 or retry_index > config.maximum_retries_per_case:
        return None
    scheduled = config.retry_backoff_seconds[retry_index - 1]
    hinted = float(failure.get("retry_after_seconds") or 0.0)
    delay = max(scheduled, hinted)
    return delay if delay <= config.maximum_retry_delay_seconds else None


def _source_cases() -> tuple[dict, dict, list[dict]]:
    manifest = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    selection = json.loads(DEFAULT_SELECTION.read_text(encoding="utf-8"))
    original = json.loads(ORIGINAL_PLAN.read_text(encoding="utf-8"))
    by_id = {case["case_id"]: case for case in _load_source_cases(manifest, selection)}
    return manifest, selection, [by_id[case_id] for case_id in original["case_ids"]]


def build_screening_plan() -> dict:
    if _sha256(ORIGINAL_PLAN) != EXPECTED_ORIGINAL_PLAN_SHA256:
        raise ValueError("original compact plan hash changed")
    _, _, cases = _source_cases()
    if [case["expected_wake"] for case in cases] != [True, True, False, False]:
        raise ValueError("original compact strata changed")
    maximum_case_cost = max(
        conservative_provider_case_cost(case, provider)
        for case in cases
        for provider in SCREEN_PROVIDERS
    )
    conservative_total = maximum_case_cost * SCREEN_V5.maximum_total_attempts
    if conservative_total > SCREEN_V5.incremental_spend_cap_usd:
        raise ValueError("provider screen exceeds its incremental spend cap")
    if SCREEN_V5.prior_conservative_glm_usd + conservative_total > TOTAL_GLM_CAP_USD:
        raise ValueError("provider screen could cross the cumulative $5 cap")
    return {
        "plan_version": SCREEN_V5.plan_version,
        "trial_kind": "bounded tuning-only operational provider screen",
        "manifest_sha256": _sha256(DEFAULT_MANIFEST),
        "selection_sha256": _sha256(DEFAULT_SELECTION),
        "original_compact_plan_sha256": _sha256(ORIGINAL_PLAN),
        "case_ids": [case["case_id"] for case in cases],
        "strata": {"required": 2, "negative": 2, "ambiguous": 0},
        "provider_catalog": {
            "source": "https://openrouter.ai/api/v1/models/z-ai/glm-5.3-flash/endpoints",
            "verified_date": SCREEN_CATALOG_VERIFIED_DATE,
            "required_parameters": list(REQUIRED_PROVIDER_PARAMETERS),
            "providers": [
                {
                    "name": provider.name,
                    "slug": provider.slug,
                    "endpoint_name": provider.endpoint_name,
                    "status": "active",
                    "prices_per_million_usd": {
                        "input": provider.input_price_per_million,
                        "output": provider.output_price_per_million,
                        "cache_read": provider.cache_read_price_per_million,
                    },
                }
                for provider in SCREEN_PROVIDERS
            ],
        },
        "controls": {
            "requested_model": REQUESTED_MODEL,
            "configured_upstream": UPSTREAM_MODEL,
            "provider_order": [provider.slug for provider in SCREEN_PROVIDERS],
            "provider_only_one_route_per_request": True,
            "require_parameters": True,
            "provider_fallbacks": False,
            "schema": "unchanged JevWatcherJudgments native strict JSON schema",
            "reasoning_effort": REASONING_EFFORT,
            "max_completion_tokens": MAX_OUTPUT_TOKENS,
            "timeout_seconds": TIMEOUT_SECONDS,
            "client_retries": 0,
            "concurrency": 1,
            "minimum_seconds_between_requests": SCREEN_V5.minimum_seconds_between_requests,
            "maximum_total_attempts": SCREEN_V5.maximum_total_attempts,
            "maximum_retries_per_case": SCREEN_V5.maximum_retries_per_case,
            "retry_backoff_seconds": list(SCREEN_V5.retry_backoff_seconds),
            "honor_longer_retry_after": True,
            "maximum_retry_delay_seconds": SCREEN_V5.maximum_retry_delay_seconds,
            "move_to_next_provider_after_repeated_failure": True,
            "overwrite_or_resume": False,
            "hard_spend_cap_usd": SCREEN_V5.incremental_spend_cap_usd,
            "prior_conservative_glm_usd": SCREEN_V5.prior_conservative_glm_usd,
            "cumulative_glm_cap_usd": TOTAL_GLM_CAP_USD,
        },
        "workload": {
            "distinct_cases_per_provider": len(cases),
            "maximum_total_attempts": SCREEN_V5.maximum_total_attempts,
            "conservative_maximum_cost_usd": conservative_total,
            "cumulative_conservative_maximum_usd": (
                SCREEN_V5.prior_conservative_glm_usd + conservative_total
            ),
        },
    }


def _tuning_24_cases() -> list[dict]:
    manifest = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    selection = json.loads(DEFAULT_SELECTION.read_text(encoding="utf-8"))
    return _load_source_cases(manifest, selection)


def build_wafer_tuning24_plan() -> dict:
    screen = json.loads(SCREEN_V5.output_path.read_text(encoding="utf-8"))
    if screen.get("usable_provider_slug") != "wafer":
        raise ValueError("provider screen did not establish Wafer as usable")
    if screen.get("attempted") != 4:
        raise ValueError("provider screen is not the expected four-attempt result")
    screen_records = screen["records"]
    if any(record["failure"] for record in screen_records):
        raise ValueError("provider screen does not contain four valid decisions")
    cases = _tuning_24_cases()
    screen_ids = {record["case_id"] for record in screen_records}
    remaining = [case for case in cases if case["case_id"] not in screen_ids]
    if len(cases) != 24 or len(remaining) != 20:
        raise ValueError("the frozen tuning selection no longer matches 4 + 20")
    wafer = SCREEN_PROVIDERS[0]
    conservative_cost = sum(
        conservative_provider_case_cost(case, wafer) for case in remaining
    )
    phase_accounting = float(screen["budget_accounted_cost_usd"]) + conservative_cost
    if phase_accounting > SCREEN_V5.incremental_spend_cap_usd:
        raise ValueError("Wafer tuning comparison could exceed the $0.10 screen cap")
    cumulative = SCREEN_V5.prior_conservative_glm_usd + phase_accounting
    if cumulative > TOTAL_GLM_CAP_USD:
        raise ValueError("Wafer tuning comparison could exceed the cumulative $5 cap")
    return {
        "plan_version": WAFER_TUNING24_PLAN_VERSION,
        "trial_kind": "same 24-case tuning comparison reusing four provider-screen decisions",
        "manifest_sha256": _sha256(DEFAULT_MANIFEST),
        "selection_sha256": _sha256(DEFAULT_SELECTION),
        "provider_screen_plan_sha256": _sha256(SCREEN_V5.plan_path),
        "provider_screen_result_sha256": _sha256(SCREEN_V5.output_path),
        "all_case_ids": [case["case_id"] for case in cases],
        "reused_screen_case_ids": [record["case_id"] for record in screen_records],
        "new_case_ids": [case["case_id"] for case in remaining],
        "controls": {
            "requested_model": REQUESTED_MODEL,
            "provider_only": [wafer.slug],
            "provider_fallbacks": False,
            "require_parameters": True,
            "schema": "unchanged JevWatcherJudgments native strict JSON schema",
            "reasoning_effort": REASONING_EFFORT,
            "max_completion_tokens": MAX_OUTPUT_TOKENS,
            "timeout_seconds": TIMEOUT_SECONDS,
            "client_retries": 0,
            "case_retries": 0,
            "concurrency": 1,
            "minimum_seconds_between_requests": 60.0,
            "overwrite_or_resume": False,
            "new_attempts": len(remaining),
            "total_screen_attempt_ceiling": SCREEN_V5.maximum_total_attempts,
        },
        "workload": {
            "distinct_cases": 24,
            "reused_valid_decisions": 4,
            "new_provider_calls": 20,
            "new_conservative_cost_usd": conservative_cost,
            "provider_screen_accounted_cost_usd": screen["budget_accounted_cost_usd"],
            "phase_conservative_accounting_usd": phase_accounting,
            "phase_spend_cap_usd": SCREEN_V5.incremental_spend_cap_usd,
            "cumulative_conservative_glm_usd": cumulative,
            "cumulative_glm_cap_usd": TOTAL_GLM_CAP_USD,
        },
    }


def freeze_wafer_tuning24_plan() -> dict:
    plan = build_wafer_tuning24_plan()
    if WAFER_TUNING24_PLAN.exists():
        existing = json.loads(WAFER_TUNING24_PLAN.read_text(encoding="utf-8"))
        if existing != plan:
            raise ValueError("existing Wafer tuning plan differs; refusing overwrite")
        return existing
    WAFER_TUNING24_PLAN.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    return plan


def build_plan(config: RunConfig = STOP_V3) -> dict:
    if config is SCREEN_V5:
        return build_screening_plan()
    if _sha256(ORIGINAL_PLAN) != EXPECTED_ORIGINAL_PLAN_SHA256:
        raise ValueError("original compact plan hash changed")
    if (
        config is RETRY_V4
        and _sha256(STOP_V3.output_path) != EXPECTED_STOP_V3_RESULT_SHA256
    ):
        raise ValueError("stopped Morph v3 result hash changed")
    _, _, cases = _source_cases()
    if [case["expected_wake"] for case in cases] != [True, True, False, False]:
        raise ValueError("original compact strata changed")
    one_pass_total = sum(conservative_case_cost(case) for case in cases)
    conservative_total = (
        max(conservative_case_cost(case) for case in cases)
        * config.maximum_total_attempts
        if config.retry_temporary_overload
        else one_pass_total
    )
    if conservative_total > config.incremental_spend_cap_usd:
        raise ValueError("Morph plan exceeds its incremental spend cap")
    if config.prior_conservative_glm_usd + conservative_total > TOTAL_GLM_CAP_USD:
        raise ValueError("Morph plan could cross the cumulative $5 cap")
    plan = {
        "plan_version": config.plan_version,
        "trial_kind": "new immutable same-model provider-routed diagnostic",
        "manifest_sha256": _sha256(DEFAULT_MANIFEST),
        "selection_sha256": _sha256(DEFAULT_SELECTION),
        "original_compact_plan_sha256": _sha256(ORIGINAL_PLAN),
        "baseline_result_sha256": _sha256(BASELINE_RESULT),
        "jev_result_sha256": _sha256(JEV_RESULT),
        "case_selection": "unchanged original compact two positives then two negatives",
        "case_ids": [case["case_id"] for case in cases],
        "strata": {"required": 2, "negative": 2, "ambiguous": 0},
        "provider_catalog": {
            "source": "https://openrouter.ai/api/v1/models/z-ai/glm-5.3-flash/endpoints",
            "verified_date": PROVIDER_CATALOG_VERIFIED_DATE,
            "provider_name": PROVIDER_NAME,
            "provider_slug": PROVIDER_SLUG,
            "endpoint_tag": PROVIDER_CATALOG_TAG,
            "required_parameters": list(REQUIRED_PROVIDER_PARAMETERS),
            "prices_per_million_usd": {
                "input": MORPH_INPUT_PRICE_PER_MILLION,
                "output": MORPH_OUTPUT_PRICE_PER_MILLION,
                "cache_read": MORPH_CACHE_READ_PRICE_PER_MILLION,
            },
        },
        "controls": {
            "requested_model": REQUESTED_MODEL,
            "configured_upstream": UPSTREAM_MODEL,
            "provider_only": [PROVIDER_SLUG],
            "require_parameters": True,
            "provider_fallbacks": False,
            "schema": "unchanged JevWatcherJudgments native strict JSON schema",
            "reasoning_effort": REASONING_EFFORT,
            "max_completion_tokens": MAX_OUTPUT_TOKENS,
            "timeout_seconds": TIMEOUT_SECONDS,
            "client_retries": 0,
            "concurrency": 1,
            "minimum_seconds_between_requests": config.minimum_seconds_between_requests,
            "stop_on_first_error": True,
            "overwrite_or_resume": False,
            "maximum_attempts": config.maximum_total_attempts,
            "hard_spend_cap_usd": config.incremental_spend_cap_usd,
            "prior_conservative_glm_usd": config.prior_conservative_glm_usd,
            "cumulative_glm_cap_usd": TOTAL_GLM_CAP_USD,
        },
        "workload": {
            "one_pass_conservative_cost_usd": one_pass_total,
            "conservative_maximum_cost_usd": conservative_total,
            "cumulative_conservative_maximum_usd": (
                config.prior_conservative_glm_usd + conservative_total
            ),
        },
    }
    if config is STOP_V3:
        # Preserve the already-frozen v3 plan byte-for-byte.
        plan["workload"].pop("one_pass_conservative_cost_usd")
        return plan
    plan["trial_kind"] = (
        "new immutable bounded-retry diagnostic referencing stopped Morph v3"
    )
    plan["stopped_morph_v3_result_sha256"] = _sha256(STOP_V3.output_path)
    plan["controls"].update(
        {
            "stop_on_first_error": False,
            "retry_policy": "temporary Morph shared-pool overload HTTP 429 only",
            "maximum_retries_per_case": config.maximum_retries_per_case,
            "retry_backoff_seconds": list(config.retry_backoff_seconds),
            "honor_longer_retry_after": True,
            "maximum_retry_delay_seconds": config.maximum_retry_delay_seconds,
            "stop_on_non_429_or_non_temporary_error": True,
            "stop_on_auth_budget_or_permanent_quota": True,
        }
    )
    return plan


def freeze_plan(plan_path: Path, config: RunConfig = STOP_V3) -> dict:
    plan = build_plan(config)
    if plan_path.exists():
        existing = json.loads(plan_path.read_text(encoding="utf-8"))
        if existing != plan:
            raise ValueError("existing Morph plan differs; refusing to overwrite")
        return existing
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return plan


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * quantile)]


def summarize(records: list[dict], jev_result: dict) -> dict:
    jev_by_id = {record["case_id"]: record for record in jev_result["records"]}
    evaluable = [record for record in records if not record["failure"]]
    required = [record for record in records if record["expected_wake"] is True]
    negatives = [record for record in records if record["expected_wake"] is False]
    atomic_differences: list[dict] = []
    wake_agreements = 0
    exact_agreements = 0
    for record in evaluable:
        jev = jev_by_id[record["case_id"]]
        wake_agreements += record["wake"] == jev["wake"]
        jev_judgments = jev["classifier"]["judgments"]
        different_fields = sorted(
            field
            for field in JUDGMENT_FIELDS
            if record["judgments"][field] != jev_judgments[field]
        )
        exact_agreements += not different_fields
        if different_fields:
            atomic_differences.append(
                {"case_id": record["case_id"], "fields": different_fields}
            )
    latencies = [float(record["latency_ms"]) for record in evaluable]
    return {
        "target_cases": MAX_ATTEMPTS,
        "attempted": len(records),
        "evaluable": len(evaluable),
        "errors": len(records) - len(evaluable),
        "required": {
            "total": len(required),
            "detected": sum(
                bool(record["wake"]) for record in required if not record["failure"]
            ),
            "missed": sum(
                not record["wake"] for record in required if not record["failure"]
            ),
            "errors": sum(bool(record["failure"]) for record in required),
        },
        "negatives": {
            "total": len(negatives),
            "correct_no_wake": sum(
                not record["wake"] for record in negatives if not record["failure"]
            ),
            "false_wakes": sum(
                bool(record["wake"]) for record in negatives if not record["failure"]
            ),
            "errors": sum(bool(record["failure"]) for record in negatives),
        },
        "same_case_jev_wake_agreements": wake_agreements,
        "same_case_jev_wake_disagreements": len(evaluable) - wake_agreements,
        "same_case_jev_exact_atomic_agreements": exact_agreements,
        "atomic_differences": atomic_differences,
        "provider_confirmed_records": sum(
            record["provider_confirmed"] for record in evaluable
        ),
        "latency_ms": {
            "mean": statistics.fmean(latencies) if latencies else None,
            "p50": _percentile(latencies, 0.5),
            "p95": _percentile(latencies, 0.95),
            "minimum": min(latencies) if latencies else None,
            "maximum": max(latencies) if latencies else None,
        },
    }


def summarize_retry(records: list[dict], jev_result: dict, cases: list[dict]) -> dict:
    successful_by_id = {
        record["case_id"]: record for record in records if not record["failure"]
    }
    successful = [
        successful_by_id[case["case_id"]]
        for case in cases
        if case["case_id"] in successful_by_id
    ]
    jev_by_id = {record["case_id"]: record for record in jev_result["records"]}
    atomic_differences: list[dict] = []
    wake_agreements = 0
    exact_agreements = 0
    for record in successful:
        jev = jev_by_id[record["case_id"]]
        wake_agreements += record["wake"] == jev["wake"]
        jev_judgments = jev["classifier"]["judgments"]
        different_fields = sorted(
            field
            for field in JUDGMENT_FIELDS
            if record["judgments"][field] != jev_judgments[field]
        )
        exact_agreements += not different_fields
        if different_fields:
            atomic_differences.append(
                {"case_id": record["case_id"], "fields": different_fields}
            )
    required = [case for case in cases if case["expected_wake"] is True]
    negatives = [case for case in cases if case["expected_wake"] is False]
    latencies = [float(record["latency_ms"]) for record in successful]
    return {
        "target_cases": len(cases),
        "attempts": len(records),
        "temporary_overload_attempts": sum(
            is_temporary_overload(record["failure"])
            for record in records
            if record["failure"]
        ),
        "evaluable_cases": len(successful),
        "unresolved_cases": len(cases) - len(successful),
        "required": {
            "total": len(required),
            "detected": sum(
                bool(successful_by_id[case["case_id"]]["wake"])
                for case in required
                if case["case_id"] in successful_by_id
            ),
            "missed": sum(
                not successful_by_id[case["case_id"]]["wake"]
                for case in required
                if case["case_id"] in successful_by_id
            ),
            "unresolved": sum(
                case["case_id"] not in successful_by_id for case in required
            ),
        },
        "negatives": {
            "total": len(negatives),
            "correct_no_wake": sum(
                not successful_by_id[case["case_id"]]["wake"]
                for case in negatives
                if case["case_id"] in successful_by_id
            ),
            "false_wakes": sum(
                bool(successful_by_id[case["case_id"]]["wake"])
                for case in negatives
                if case["case_id"] in successful_by_id
            ),
            "unresolved": sum(
                case["case_id"] not in successful_by_id for case in negatives
            ),
        },
        "same_case_jev_wake_agreements": wake_agreements,
        "same_case_jev_wake_disagreements": len(successful) - wake_agreements,
        "same_case_jev_exact_atomic_agreements": exact_agreements,
        "atomic_differences": atomic_differences,
        "provider_confirmed_cases": sum(
            record["provider_confirmed"] for record in successful
        ),
        "latency_ms": {
            "mean": statistics.fmean(latencies) if latencies else None,
            "p50": _percentile(latencies, 0.5),
            "p95": _percentile(latencies, 0.95),
            "minimum": min(latencies) if latencies else None,
            "maximum": max(latencies) if latencies else None,
        },
    }


def _write_result(
    output_path: Path,
    *,
    config: RunConfig,
    plan_path: Path,
    cases: list[dict],
    records: list[dict],
    budget_accounted_cost: float,
    stop_reason: str | None,
    jev_result: dict,
) -> dict:
    known_cost = sum(float(record["cost_usd"]) for record in records)
    usage = {
        key: sum(int(record["usage"][key]) for record in records)
        for key in ("input_tokens", "output_tokens", "cache_read_tokens")
    }
    result = {
        "plan_version": config.plan_version,
        "plan_sha256": _sha256(plan_path),
        "original_compact_plan_sha256": _sha256(ORIGINAL_PLAN),
        "attempted": len(records),
        "completed_all_cases": (
            len({record["case_id"] for record in records if not record["failure"]})
            == MAX_ATTEMPTS
            and stop_reason is None
        ),
        "stop_reason": stop_reason,
        "known_returned_usage_cost_usd": known_cost,
        "budget_accounted_cost_usd": budget_accounted_cost,
        "prior_conservative_glm_usd": config.prior_conservative_glm_usd,
        "cumulative_conservative_glm_usd": (
            config.prior_conservative_glm_usd + budget_accounted_cost
        ),
        "usage": usage,
        "summary": (
            summarize_retry(records, jev_result, cases)
            if config.retry_temporary_overload
            else summarize(records, jev_result)
        ),
        "records": records,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return result


def _screen_provider_summary(
    records: list[dict], cases: list[dict], jev_result: dict
) -> dict:
    jev_by_id = {record["case_id"]: record for record in jev_result["records"]}
    summaries: dict[str, dict[str, Any]] = {}
    for provider in SCREEN_PROVIDERS:
        provider_records = [
            record
            for record in records
            if record["requested_provider_slug"] == provider.slug
        ]
        successes_by_id = {
            record["case_id"]: record
            for record in provider_records
            if record["failure"] is None
        }
        atomic_differences = []
        wake_agreements = 0
        for case_id, record in successes_by_id.items():
            jev = jev_by_id[case_id]
            wake_agreements += record["wake"] == jev["wake"]
            different_fields = sorted(
                field
                for field in JUDGMENT_FIELDS
                if record["judgments"][field] != jev["classifier"]["judgments"][field]
            )
            if different_fields:
                atomic_differences.append(
                    {"case_id": case_id, "fields": different_fields}
                )
        required = [case for case in cases if case["expected_wake"] is True]
        negatives = [case for case in cases if case["expected_wake"] is False]
        latencies = [float(record["latency_ms"]) for record in successes_by_id.values()]
        summaries[provider.slug] = {
            "attempts": len(provider_records),
            "evaluable_cases": len(successes_by_id),
            "completed_four_cases": len(successes_by_id) == len(cases),
            "required_detected": sum(
                bool(successes_by_id[case["case_id"]]["wake"])
                for case in required
                if case["case_id"] in successes_by_id
            ),
            "required_missed": sum(
                not successes_by_id[case["case_id"]]["wake"]
                for case in required
                if case["case_id"] in successes_by_id
            ),
            "negative_correct_no_wake": sum(
                not successes_by_id[case["case_id"]]["wake"]
                for case in negatives
                if case["case_id"] in successes_by_id
            ),
            "false_wakes": sum(
                bool(successes_by_id[case["case_id"]]["wake"])
                for case in negatives
                if case["case_id"] in successes_by_id
            ),
            "same_case_jev_wake_agreements": wake_agreements,
            "same_case_jev_wake_disagreements": len(successes_by_id) - wake_agreements,
            "atomic_differences": atomic_differences,
            "provider_confirmed_cases": sum(
                record["provider_confirmed"] for record in successes_by_id.values()
            ),
            "latency_ms": {
                "mean": statistics.fmean(latencies) if latencies else None,
                "p50": _percentile(latencies, 0.5),
                "p95": _percentile(latencies, 0.95),
            },
        }
    return summaries


def _screen_retry_delay(failure: dict[str, Any], case_attempt: int) -> float | None:
    if case_attempt > SCREEN_V5.maximum_retries_per_case:
        return None
    retryable = (
        failure.get("status_code") == 429
        or failure.get("provider_error_category") == "timeout"
    )
    if not retryable:
        return None
    hinted = float(failure.get("retry_after_seconds") or 0.0)
    delay = max(SCREEN_V5.retry_backoff_seconds[case_attempt - 1], hinted)
    return delay if delay <= SCREEN_V5.maximum_retry_delay_seconds else None


async def run_provider_screen(plan_path: Path, output_path: Path) -> dict:
    if output_path.exists():
        raise SystemExit("provider-screen result exists; refusing overwrite/resume")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan != build_screening_plan():
        raise ValueError("provider-screen plan changed after freeze")
    _, _, cases = _source_cases()
    jev_result = json.loads(JEV_RESULT.read_text(encoding="utf-8"))
    client = AsyncOpenAI(
        api_key=os.environ["LITELLM_API_KEY"],
        base_url=api_base_from_env(),
        max_retries=0,
        timeout=TIMEOUT_SECONDS,
    )
    records: list[dict] = []
    budget_accounted_cost = 0.0
    usable_provider: str | None = None
    stop_reason: str | None = None
    last_request_completed_at: float | None = None

    async def write_result() -> dict:
        summaries = _screen_provider_summary(records, cases, jev_result)
        remaining = SCREEN_V5.maximum_total_attempts - len(records)
        result = {
            "plan_version": SCREEN_V5.plan_version,
            "plan_sha256": _sha256(plan_path),
            "attempted": len(records),
            "usable_provider_slug": usable_provider,
            "stop_reason": stop_reason,
            "known_returned_usage_cost_usd": sum(
                float(record["cost_usd"]) for record in records
            ),
            "budget_accounted_cost_usd": budget_accounted_cost,
            "prior_conservative_glm_usd": SCREEN_V5.prior_conservative_glm_usd,
            "cumulative_conservative_glm_usd": SCREEN_V5.prior_conservative_glm_usd
            + budget_accounted_cost,
            "remaining_screen_attempt_cap": remaining,
            "same_24_case_followup": {
                "required_calls": 24,
                "authorized_remaining_calls": remaining,
                "additional_call_ceiling_needed": max(0, 24 - remaining),
                "executed": False,
            },
            "provider_summaries": summaries,
            "records": records,
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        return result

    result: dict[str, Any] = {}
    try:
        for provider in SCREEN_PROVIDERS:
            provider_failed = False
            successful_ids: set[str] = set()
            for case in cases:
                case_attempt = 0
                while case["case_id"] not in successful_ids:
                    if len(records) >= SCREEN_V5.maximum_total_attempts:
                        stop_reason = "maximum_total_attempts_reached"
                        provider_failed = True
                        break
                    maximum_case_cost = conservative_provider_case_cost(case, provider)
                    if (
                        budget_accounted_cost + maximum_case_cost
                        > SCREEN_V5.incremental_spend_cap_usd
                    ):
                        stop_reason = "incremental_spend_cap"
                        provider_failed = True
                        break
                    if (
                        SCREEN_V5.prior_conservative_glm_usd
                        + budget_accounted_cost
                        + maximum_case_cost
                        > TOTAL_GLM_CAP_USD
                    ):
                        stop_reason = "cumulative_spend_cap"
                        provider_failed = True
                        break
                    if last_request_completed_at is not None:
                        elapsed = time.monotonic() - last_request_completed_at
                        if elapsed < SCREEN_V5.minimum_seconds_between_requests:
                            await asyncio.sleep(
                                SCREEN_V5.minimum_seconds_between_requests - elapsed
                            )
                    case_attempt += 1
                    started = time.perf_counter()
                    executed_provider = None
                    response_metadata: dict[str, Any] = {}
                    try:
                        raw_response = (
                            await client.chat.completions.with_raw_response.create(
                                model=REQUESTED_MODEL,
                                messages=compact_messages(case),  # type: ignore[arg-type]
                                response_format=compact_response_format(),  # type: ignore[arg-type]
                                max_completion_tokens=MAX_OUTPUT_TOKENS,
                                reasoning_effort=REASONING_EFFORT,  # type: ignore[arg-type]
                                timeout=TIMEOUT_SECONDS,
                                extra_body={
                                    "num_retries": 0,
                                    "fallbacks": [],
                                    "provider": {
                                        "only": [provider.slug],
                                        "require_parameters": True,
                                        "allow_fallbacks": False,
                                    },
                                },
                            )
                        )
                        response_metadata = safe_response_metadata(
                            raw_response.headers,
                            status_code=raw_response.status_code,
                            request_id=raw_response.request_id,
                        )
                        response = raw_response.parse()
                        executed_provider = confirmed_provider(
                            response,
                            raw_response.headers,
                            provider_name=provider.name,
                            provider_slug=provider.slug,
                        )
                        judgments, usage, diagnostics, failure = parse_completion(
                            response
                        )
                        if failure is not None:
                            failure = {
                                "provider_error_category": failure["category"],
                                "status_code": raw_response.status_code,
                                "response_metadata": response_metadata,
                                "retry_after_seconds": retry_after_seconds(
                                    response_metadata
                                ),
                            }
                    except Exception as error:  # noqa: BLE001 - strict safe diagnostics
                        judgments = None
                        usage = {
                            "input_tokens": 0,
                            "output_tokens": 0,
                            "cache_read_tokens": 0,
                        }
                        diagnostics = {}
                        failure = provider_error_diagnostics(error, provider)
                        response_metadata = failure["response_metadata"]
                        executed_provider = (
                            failure.get("upstream_metadata") or {}
                        ).get("provider_name")
                    latency_ms = (time.perf_counter() - started) * 1_000
                    last_request_completed_at = time.monotonic()
                    cost = provider_price(provider, usage)
                    budget_accounted_cost += maximum_case_cost if failure else cost
                    decision = None
                    if judgments is not None:
                        decision = decision_from_jev(
                            judgments,
                            new_message_ids=[
                                message.id for message in case["new_messages"]
                            ],
                            provider_details={},
                            resolved_model_name=diagnostics.get("resolved_model_name"),
                            minimum_confidence=0.0,
                        )
                    retry_delay = (
                        _screen_retry_delay(failure, case_attempt) if failure else None
                    )
                    records.append(
                        {
                            "global_attempt": len(records) + 1,
                            "case_attempt": case_attempt,
                            "case_id": case["case_id"],
                            "expected_wake": case["expected_wake"],
                            "categories": case["categories"],
                            "wake": decision.wake if decision else None,
                            "judgments": judgments.model_dump() if judgments else None,
                            "failure": failure,
                            "safe_diagnostics": diagnostics,
                            "safe_response_metadata": response_metadata,
                            "requested_provider": provider.name,
                            "requested_provider_slug": provider.slug,
                            "executed_provider": executed_provider,
                            "provider_confirmed": executed_provider == provider.name,
                            "retry_scheduled_seconds": retry_delay,
                            "latency_ms": latency_ms,
                            "usage": usage,
                            "cost_usd": cost,
                        }
                    )
                    if failure is None:
                        successful_ids.add(case["case_id"])
                    elif retry_delay is not None:
                        await asyncio.sleep(retry_delay)
                        last_request_completed_at = None
                    else:
                        provider_failed = True
                    result = await write_result()
                    print(
                        json.dumps(
                            {
                                "attempt": len(records),
                                "provider": provider.slug,
                                "case_attempt": case_attempt,
                                "success": failure is None,
                                "retry_scheduled_seconds": retry_delay,
                                "provider_confirmed": executed_provider
                                == provider.name,
                            }
                        ),
                        flush=True,
                    )
                    if provider_failed:
                        break
                if provider_failed:
                    break
            if stop_reason in {
                "maximum_total_attempts_reached",
                "incremental_spend_cap",
                "cumulative_spend_cap",
            }:
                break
            if not provider_failed and len(successful_ids) == len(cases):
                usable_provider = provider.slug
                stop_reason = "four_valid_decisions_obtained"
                result = await write_result()
                break
        if usable_provider is None and stop_reason is None:
            stop_reason = "all_providers_failed_operational_screen"
            result = await write_result()
    finally:
        await client.close()
    return result


async def run_wafer_tuning24() -> dict:
    if WAFER_TUNING24_OUTPUT.exists():
        raise SystemExit("Wafer tuning result exists; refusing overwrite/resume")
    plan = json.loads(WAFER_TUNING24_PLAN.read_text(encoding="utf-8"))
    if plan != build_wafer_tuning24_plan():
        raise ValueError("Wafer tuning plan changed after freeze")
    screen = json.loads(SCREEN_V5.output_path.read_text(encoding="utf-8"))
    cases = _tuning_24_cases()
    by_id = {case["case_id"]: case for case in cases}
    remaining = [by_id[case_id] for case_id in plan["new_case_ids"]]
    jev_result = json.loads(JEV_RESULT.read_text(encoding="utf-8"))
    provider = SCREEN_PROVIDERS[0]
    client = AsyncOpenAI(
        api_key=os.environ["LITELLM_API_KEY"],
        base_url=api_base_from_env(),
        max_retries=0,
        timeout=TIMEOUT_SECONDS,
    )
    new_records: list[dict] = []
    budget_accounted_cost = float(screen["budget_accounted_cost_usd"])
    last_request_completed_at: float | None = None
    stop_reason: str | None = None

    def write_result() -> dict:
        reused = [
            {**record, "record_source": "provider_screen"}
            for record in screen["records"]
        ]
        fresh = [
            {**record, "record_source": "tuning_followup"} for record in new_records
        ]
        combined = [*reused, *fresh]
        result = {
            "plan_version": WAFER_TUNING24_PLAN_VERSION,
            "plan_sha256": _sha256(WAFER_TUNING24_PLAN),
            "provider_screen_result_sha256": _sha256(SCREEN_V5.output_path),
            "new_attempts": len(new_records),
            "total_attempts_in_screen_authorization": len(combined),
            "completed_24_cases": (
                len(combined) == 24
                and not any(record["failure"] for record in combined)
                and stop_reason is None
            ),
            "stop_reason": stop_reason,
            "known_returned_usage_cost_usd": sum(
                float(record["cost_usd"]) for record in combined
            ),
            "budget_accounted_cost_usd": budget_accounted_cost,
            "cumulative_conservative_glm_usd": (
                SCREEN_V5.prior_conservative_glm_usd + budget_accounted_cost
            ),
            "summary": _screen_provider_summary(combined, cases, jev_result)["wafer"],
            "records": combined,
        }
        WAFER_TUNING24_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        WAFER_TUNING24_OUTPUT.write_text(
            json.dumps(result, indent=2) + "\n", encoding="utf-8"
        )
        return result

    result: dict[str, Any] = {}
    try:
        for case in remaining:
            maximum_case_cost = conservative_provider_case_cost(case, provider)
            if (
                budget_accounted_cost + maximum_case_cost
                > SCREEN_V5.incremental_spend_cap_usd
            ):
                stop_reason = "incremental_spend_cap"
                break
            if (
                SCREEN_V5.prior_conservative_glm_usd
                + budget_accounted_cost
                + maximum_case_cost
                > TOTAL_GLM_CAP_USD
            ):
                stop_reason = "cumulative_spend_cap"
                break
            if last_request_completed_at is not None:
                elapsed = time.monotonic() - last_request_completed_at
                if elapsed < 60.0:
                    await asyncio.sleep(60.0 - elapsed)
            started = time.perf_counter()
            executed_provider = None
            response_metadata: dict[str, Any] = {}
            try:
                raw_response = await client.chat.completions.with_raw_response.create(
                    model=REQUESTED_MODEL,
                    messages=compact_messages(case),  # type: ignore[arg-type]
                    response_format=compact_response_format(),  # type: ignore[arg-type]
                    max_completion_tokens=MAX_OUTPUT_TOKENS,
                    reasoning_effort=REASONING_EFFORT,  # type: ignore[arg-type]
                    timeout=TIMEOUT_SECONDS,
                    extra_body={
                        "num_retries": 0,
                        "fallbacks": [],
                        "provider": {
                            "only": [provider.slug],
                            "require_parameters": True,
                            "allow_fallbacks": False,
                        },
                    },
                )
                response_metadata = safe_response_metadata(
                    raw_response.headers,
                    status_code=raw_response.status_code,
                    request_id=raw_response.request_id,
                )
                response = raw_response.parse()
                executed_provider = confirmed_provider(
                    response,
                    raw_response.headers,
                    provider_name=provider.name,
                    provider_slug=provider.slug,
                )
                judgments, usage, diagnostics, failure = parse_completion(response)
                if failure is not None:
                    failure = {
                        "provider_error_category": failure["category"],
                        "status_code": raw_response.status_code,
                        "response_metadata": response_metadata,
                        "retry_after_seconds": retry_after_seconds(response_metadata),
                    }
            except Exception as error:  # noqa: BLE001 - strict safe diagnostics
                judgments = None
                usage = {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                }
                diagnostics = {}
                failure = provider_error_diagnostics(error, provider)
                response_metadata = failure["response_metadata"]
                executed_provider = (failure.get("upstream_metadata") or {}).get(
                    "provider_name"
                )
            latency_ms = (time.perf_counter() - started) * 1_000
            last_request_completed_at = time.monotonic()
            cost = provider_price(provider, usage)
            budget_accounted_cost += maximum_case_cost if failure else cost
            decision = None
            if judgments is not None:
                decision = decision_from_jev(
                    judgments,
                    new_message_ids=[message.id for message in case["new_messages"]],
                    provider_details={},
                    resolved_model_name=diagnostics.get("resolved_model_name"),
                    minimum_confidence=0.0,
                )
            new_records.append(
                {
                    "global_attempt": len(screen["records"]) + len(new_records) + 1,
                    "case_attempt": 1,
                    "case_id": case["case_id"],
                    "expected_wake": case["expected_wake"],
                    "categories": case["categories"],
                    "wake": decision.wake if decision else None,
                    "judgments": judgments.model_dump() if judgments else None,
                    "failure": failure,
                    "safe_diagnostics": diagnostics,
                    "safe_response_metadata": response_metadata,
                    "requested_provider": provider.name,
                    "requested_provider_slug": provider.slug,
                    "executed_provider": executed_provider,
                    "provider_confirmed": executed_provider == provider.name,
                    "retry_scheduled_seconds": None,
                    "latency_ms": latency_ms,
                    "usage": usage,
                    "cost_usd": cost,
                }
            )
            result = write_result()
            print(
                json.dumps(
                    {
                        "attempt": len(screen["records"]) + len(new_records),
                        "success": failure is None,
                        "provider_confirmed": executed_provider == provider.name,
                        "wake": decision.wake if decision else None,
                    }
                ),
                flush=True,
            )
        if not result or stop_reason is not None:
            result = write_result()
    finally:
        await client.close()
    return result


async def run(
    plan_path: Path,
    output_path: Path,
    config: RunConfig = STOP_V3,
) -> dict:
    if output_path.exists():
        raise SystemExit("Morph result already exists; refusing overwrite/resume")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan != build_plan(config):
        raise ValueError("Morph plan changed after freeze")
    _, _, cases = _source_cases()
    jev_result = json.loads(JEV_RESULT.read_text(encoding="utf-8"))
    client = AsyncOpenAI(
        api_key=os.environ["LITELLM_API_KEY"],
        base_url=api_base_from_env(),
        max_retries=0,
        timeout=TIMEOUT_SECONDS,
    )
    records: list[dict] = []
    budget_accounted_cost = 0.0
    stop_reason: str | None = None
    result: dict[str, Any] = {}
    try:
        for case_index, case in enumerate(cases):
            case_attempt = 0
            case_succeeded = False
            while not case_succeeded:
                if len(records) >= config.maximum_total_attempts:
                    stop_reason = "maximum_total_attempts_reached"
                    break
                maximum_case_cost = conservative_case_cost(case)
                if (
                    budget_accounted_cost + maximum_case_cost
                    > config.incremental_spend_cap_usd
                ):
                    stop_reason = "incremental_spend_cap"
                    break
                if (
                    config.prior_conservative_glm_usd
                    + budget_accounted_cost
                    + maximum_case_cost
                    > TOTAL_GLM_CAP_USD
                ):
                    stop_reason = "cumulative_spend_cap"
                    break
                case_attempt += 1
                started = time.perf_counter()
                response_metadata: dict[str, Any] = {}
                executed_provider = None
                try:
                    raw_response = (
                        await client.chat.completions.with_raw_response.create(
                            model=REQUESTED_MODEL,
                            messages=compact_messages(case),  # type: ignore[arg-type]
                            response_format=compact_response_format(),  # type: ignore[arg-type]
                            max_completion_tokens=MAX_OUTPUT_TOKENS,
                            reasoning_effort=REASONING_EFFORT,  # type: ignore[arg-type]
                            timeout=TIMEOUT_SECONDS,
                            extra_body={
                                "num_retries": 0,
                                "fallbacks": [],
                                "provider": {
                                    "only": [PROVIDER_SLUG],
                                    "require_parameters": True,
                                    "allow_fallbacks": False,
                                },
                            },
                        )
                    )
                    response_metadata = safe_response_metadata(
                        raw_response.headers,
                        status_code=raw_response.status_code,
                        request_id=raw_response.request_id,
                    )
                    response = raw_response.parse()
                    executed_provider = confirmed_provider(
                        response, raw_response.headers
                    )
                    judgments, usage, diagnostics, failure = parse_completion(response)
                    if failure is not None:
                        failure = {
                            "provider_error_category": failure["category"],
                            "status_code": raw_response.status_code,
                            "response_metadata": response_metadata,
                            "retry_after_seconds": retry_after_seconds(
                                response_metadata
                            ),
                        }
                except Exception as error:  # noqa: BLE001 - strict safe diagnostics
                    judgments = None
                    usage = {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cache_read_tokens": 0,
                    }
                    diagnostics = {}
                    failure = enriched_error_diagnostics(error)
                    response_metadata = failure["response_metadata"]
                    executed_provider = (failure.get("upstream_metadata") or {}).get(
                        "provider_name"
                    )
                latency_ms = (time.perf_counter() - started) * 1_000
                cost = morph_price(usage)
                budget_accounted_cost += maximum_case_cost if failure else cost
                decision = None
                if judgments is not None:
                    decision = decision_from_jev(
                        judgments,
                        new_message_ids=[
                            message.id for message in case["new_messages"]
                        ],
                        provider_details={},
                        resolved_model_name=diagnostics.get("resolved_model_name"),
                        minimum_confidence=0.0,
                    )
                retry_delay = None
                if failure:
                    retry_delay = retry_delay_seconds(config, failure, case_attempt)
                    if retry_delay is None:
                        hint = failure.get("retry_after_seconds")
                        if (
                            is_temporary_overload(failure)
                            and hint is not None
                            and float(hint) > config.maximum_retry_delay_seconds
                        ):
                            stop_reason = "retry_delay_exceeds_five_minutes"
                        elif (
                            is_temporary_overload(failure)
                            and case_attempt > config.maximum_retries_per_case
                        ):
                            stop_reason = "temporary_overload_retries_exhausted"
                        else:
                            stop_reason = (
                                "first_error"
                                if not config.retry_temporary_overload
                                else "non_retryable_error"
                            )
                    elif len(records) + 1 >= config.maximum_total_attempts:
                        stop_reason = "maximum_total_attempts_reached"
                        retry_delay = None
                    elif (
                        budget_accounted_cost + maximum_case_cost
                        > config.incremental_spend_cap_usd
                    ):
                        stop_reason = "incremental_spend_cap"
                        retry_delay = None
                    elif (
                        config.prior_conservative_glm_usd
                        + budget_accounted_cost
                        + maximum_case_cost
                        > TOTAL_GLM_CAP_USD
                    ):
                        stop_reason = "cumulative_spend_cap"
                        retry_delay = None
                else:
                    case_succeeded = True
                records.append(
                    {
                        "global_attempt": len(records) + 1,
                        "case_attempt": case_attempt,
                        "case_id": case["case_id"],
                        "expected_wake": case["expected_wake"],
                        "categories": case["categories"],
                        "wake": decision.wake if decision else None,
                        "judgments": judgments.model_dump() if judgments else None,
                        "failure": failure,
                        "safe_diagnostics": diagnostics,
                        "safe_response_metadata": response_metadata,
                        "requested_provider": PROVIDER_NAME,
                        "executed_provider": executed_provider,
                        "provider_confirmed": executed_provider == PROVIDER_NAME,
                        "retry_scheduled_seconds": retry_delay,
                        "latency_ms": latency_ms,
                        "usage": usage,
                        "cost_usd": cost,
                    }
                )
                result = _write_result(
                    output_path,
                    config=config,
                    plan_path=plan_path,
                    cases=cases,
                    records=records,
                    budget_accounted_cost=budget_accounted_cost,
                    stop_reason=stop_reason,
                    jev_result=jev_result,
                )
                print(
                    json.dumps(
                        {
                            "attempt": len(records),
                            "case_attempt": case_attempt,
                            "success": failure is None,
                            "temporary_overload": (
                                is_temporary_overload(failure) if failure else False
                            ),
                            "retry_scheduled_seconds": retry_delay,
                            "wake": decision.wake if decision else None,
                            "latency_ms": latency_ms,
                            "provider_confirmed": executed_provider == PROVIDER_NAME,
                            "stop_reason": stop_reason,
                        }
                    ),
                    flush=True,
                )
                if stop_reason is not None:
                    break
                if retry_delay is not None:
                    await asyncio.sleep(retry_delay)
            if stop_reason is not None:
                break
            if case_index < len(cases) - 1:
                await asyncio.sleep(config.minimum_seconds_between_requests)
    finally:
        await client.close()
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("prepare", "run", "prepare-wafer24", "run-wafer24")
    )
    parser.add_argument("--run-config", choices=tuple(RUN_CONFIGS))
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--confirm-paid", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "prepare-wafer24":
        plan = freeze_wafer_tuning24_plan()
        print(
            json.dumps(
                {
                    "plan_path": str(WAFER_TUNING24_PLAN),
                    "plan_sha256": _sha256(WAFER_TUNING24_PLAN),
                    "plan": plan,
                },
                indent=2,
            )
        )
        return
    if args.command == "run-wafer24":
        if not args.confirm_paid:
            raise SystemExit("Wafer tuning run requires --confirm-paid")
        result = asyncio.run(run_wafer_tuning24())
        print(f"Wrote {WAFER_TUNING24_OUTPUT}")
        print(
            json.dumps(
                {key: value for key, value in result.items() if key != "records"},
                indent=2,
            )
        )
        return
    if args.run_config is None:
        raise SystemExit("--run-config is required for prepare/run")
    config = RUN_CONFIGS[args.run_config]
    plan_path = args.plan or config.plan_path
    output_path = args.out or config.output_path
    if args.command == "prepare":
        plan = freeze_plan(plan_path, config)
        print(
            json.dumps(
                {
                    "plan_path": str(plan_path),
                    "plan_sha256": _sha256(plan_path),
                    "plan_version": plan["plan_version"],
                    "strata": plan["strata"],
                    "provider_catalog": plan["provider_catalog"],
                    "controls": plan["controls"],
                    "workload": plan["workload"],
                },
                indent=2,
            )
        )
        return
    if not args.confirm_paid:
        raise SystemExit("Morph GLM run requires explicit --confirm-paid authorization")
    result = asyncio.run(
        run_provider_screen(plan_path, output_path)
        if config is SCREEN_V5
        else run(plan_path, output_path, config)
    )
    print(f"Wrote {output_path}")
    print(
        json.dumps(
            {key: value for key, value in result.items() if key != "records"}, indent=2
        )
    )


if __name__ == "__main__":
    main()
