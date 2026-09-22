#!/usr/bin/env python
"""Prepare or run the immutable paced follow-up to the stopped GLM trial.

This is a new diagnostic, not a resume. It references the stopped compact
result, selects its three unfinished cases, preserves the compact contract and
settings, waits at least 60 seconds after each successful request, and stops on
the first error or a longer Retry-After instruction.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import time
from datetime import UTC
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import APIStatusError
from openai import APITimeoutError
from openai import AsyncOpenAI
from pydantic import ValidationError

from scripts.proactive_eval.benchmark_manifest import DATA_DIR
from scripts.proactive_eval.glm_compact_pilot import BASELINE_RESULT
from scripts.proactive_eval.glm_compact_pilot import DEFAULT_OUTPUT as STOPPED_RESULT
from scripts.proactive_eval.glm_compact_pilot import DEFAULT_PLAN as STOPPED_PLAN
from scripts.proactive_eval.glm_compact_pilot import MAX_OUTPUT_TOKENS
from scripts.proactive_eval.glm_compact_pilot import REASONING_EFFORT
from scripts.proactive_eval.glm_compact_pilot import _load_source_cases
from scripts.proactive_eval.glm_compact_pilot import compact_messages
from scripts.proactive_eval.glm_compact_pilot import compact_response_format
from scripts.proactive_eval.glm_compact_pilot import conservative_case_cost
from scripts.proactive_eval.glm_compact_pilot import parse_completion
from scripts.proactive_eval.glm_pilot import REQUESTED_MODEL
from scripts.proactive_eval.glm_pilot import TIMEOUT_SECONDS
from scripts.proactive_eval.glm_pilot import UPSTREAM_MODEL
from scripts.proactive_eval.glm_pilot import _price
from scripts.proactive_eval.glm_pilot import api_base_from_env
from scripts.proactive_eval.jev_pilot import DEFAULT_MANIFEST
from scripts.proactive_eval.jev_pilot import DEFAULT_SELECTION
from smarter_dev.bot.proactive.watcher import decision_from_jev

REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")

PLAN_VERSION = "glm-compact-paced-v2"
EXPECTED_STOPPED_PLAN_SHA256 = (
    "682afad09da69269abb2df3f632034951c56cb86829a13db5dea266ee50e67a1"
)
EXPECTED_STOPPED_RESULT_SHA256 = (
    "30ece38eb1682c1793ecf2db9cdf3878ead4d92ddc558177b29f24d4625ba177"
)
DEFAULT_PLAN = DATA_DIR / f"{PLAN_VERSION}-plan.json"
DEFAULT_OUTPUT = DATA_DIR / "runs" / f"{PLAN_VERSION}.json"
MAX_ATTEMPTS = 3
MINIMUM_SECONDS_BETWEEN_REQUESTS = 60.0
HARD_SPEND_CAP_USD = 0.01
PRIOR_CONSERVATIVE_GLM_USD = 0.01432133
TOTAL_GLM_CAP_USD = 5.0

_SAFE_HEADER_NAMES = {
    "retry-after": "retry_after",
    "x-ratelimit-reset": "rate_limit_reset",
    "x-ratelimit-reset-requests": "rate_limit_reset_requests",
    "x-ratelimit-reset-tokens": "rate_limit_reset_tokens",
    "x-ratelimit-remaining": "rate_limit_remaining",
    "x-ratelimit-remaining-requests": "rate_limit_remaining_requests",
    "x-ratelimit-remaining-tokens": "rate_limit_remaining_tokens",
}
_REQUEST_ID_HEADERS = (
    "x-request-id",
    "request-id",
    "x-litellm-request-id",
    "x-openrouter-request-id",
    "x-stainless-request-id",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bounded_header_value(value: Any) -> str | None:
    text = str(value).strip()
    if not text or len(text) > 256:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        return None
    return text


def safe_response_metadata(
    headers: Any,
    *,
    status_code: int | None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Extract an exact allowlist; never retain arbitrary response headers."""
    normalized = {str(key).lower(): value for key, value in (headers or {}).items()}
    result: dict[str, Any] = {}
    if status_code is not None:
        result["status_code"] = int(status_code)
    for source, destination in _SAFE_HEADER_NAMES.items():
        value = _bounded_header_value(normalized.get(source, ""))
        if value is not None:
            result[destination] = value
    candidates = [request_id, *(normalized.get(name) for name in _REQUEST_ID_HEADERS)]
    for candidate in candidates:
        value = _bounded_header_value(candidate or "")
        if value is not None:
            result["request_id"] = value
            break
    return result


def retry_after_seconds(metadata: dict, *, now: datetime | None = None) -> float | None:
    raw = metadata.get("retry_after")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        try:
            target = parsedate_to_datetime(str(raw))
        except (TypeError, ValueError, OverflowError):
            return None
        if target.tzinfo is None:
            target = target.replace(tzinfo=UTC)
        current = now or datetime.now(UTC)
        return max(0.0, (target - current).total_seconds())


def _provider_error_category(error: Exception, status_code: int | None) -> str:
    if status_code == 429:
        return "rate_limit"
    if status_code in (401, 403):
        return "authorization"
    if status_code is not None and 400 <= status_code < 500:
        return "invalid_request"
    if status_code is not None and status_code >= 500:
        return "provider_server_error"
    if isinstance(error, APITimeoutError):
        return "timeout"
    if isinstance(error, ValidationError):
        return "schema_validation"
    return "client_or_provider_error"


def safe_error_diagnostics(error: Exception) -> dict[str, Any]:
    """Return only an error category plus allowlisted transport metadata."""
    response = error.response if isinstance(error, APIStatusError) else None
    status_code = getattr(response, "status_code", None)
    metadata = safe_response_metadata(
        getattr(response, "headers", None),
        status_code=status_code,
        request_id=getattr(error, "request_id", None),
    )
    return {
        "provider_error_category": _provider_error_category(error, status_code),
        "status_code": status_code,
        "response_metadata": metadata,
        "retry_after_seconds": retry_after_seconds(metadata),
    }


def _source_files() -> tuple[dict, dict, dict, dict]:
    manifest = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    selection = json.loads(DEFAULT_SELECTION.read_text(encoding="utf-8"))
    stopped_plan = json.loads(STOPPED_PLAN.read_text(encoding="utf-8"))
    stopped_result = json.loads(STOPPED_RESULT.read_text(encoding="utf-8"))
    return manifest, selection, stopped_plan, stopped_result


def build_plan() -> dict:
    if _sha256(STOPPED_PLAN) != EXPECTED_STOPPED_PLAN_SHA256:
        raise ValueError("stopped compact plan hash changed")
    if _sha256(STOPPED_RESULT) != EXPECTED_STOPPED_RESULT_SHA256:
        raise ValueError("stopped compact result hash changed")
    manifest, selection, stopped_plan, stopped_result = _source_files()
    attempted_ids = [record["case_id"] for record in stopped_result["records"]]
    if attempted_ids != stopped_plan["case_ids"][:2]:
        raise ValueError("stopped result is not the expected two-case prefix")
    if not stopped_result["records"][-1]["failure"]:
        raise ValueError("stopped result did not end on an error")
    case_ids = stopped_plan["case_ids"][1:]
    if len(case_ids) != MAX_ATTEMPTS:
        raise ValueError("paced trial requires exactly three remaining cases")
    by_id = {
        case["case_id"]: case
        for case in _load_source_cases(manifest, selection)
    }
    cases = [by_id[case_id] for case_id in case_ids]
    conservative_total = sum(conservative_case_cost(case) for case in cases)
    if conservative_total > HARD_SPEND_CAP_USD:
        raise ValueError("paced plan exceeds its $0.01 sub-cap")
    if PRIOR_CONSERVATIVE_GLM_USD + conservative_total > TOTAL_GLM_CAP_USD:
        raise ValueError("paced plan could cross the cumulative $5 GLM cap")
    return {
        "plan_version": PLAN_VERSION,
        "trial_kind": "new paced follow-up after cooldown; not a resume",
        "manifest_sha256": _sha256(DEFAULT_MANIFEST),
        "selection_sha256": _sha256(DEFAULT_SELECTION),
        "baseline_result_sha256": _sha256(BASELINE_RESULT),
        "stopped_plan_sha256": _sha256(STOPPED_PLAN),
        "stopped_result_sha256": _sha256(STOPPED_RESULT),
        "case_selection": "stopped 429 case followed by the two unattempted negatives",
        "case_ids": case_ids,
        "strata": {"required": 1, "negative": 2, "ambiguous": 0},
        "controls": {
            "requested_model": REQUESTED_MODEL,
            "configured_upstream": UPSTREAM_MODEL,
            "schema": "unchanged JevWatcherJudgments native strict JSON schema",
            "reasoning_effort": REASONING_EFFORT,
            "max_completion_tokens": MAX_OUTPUT_TOKENS,
            "timeout_seconds": TIMEOUT_SECONDS,
            "client_retries": 0,
            "provider_fallbacks": False,
            "stop_on_first_error": True,
            "overwrite_or_resume": False,
            "minimum_seconds_between_requests": MINIMUM_SECONDS_BETWEEN_REQUESTS,
            "maximum_attempts": MAX_ATTEMPTS,
            "hard_spend_cap_usd": HARD_SPEND_CAP_USD,
            "prior_conservative_glm_usd": PRIOR_CONSERVATIVE_GLM_USD,
            "cumulative_glm_cap_usd": TOTAL_GLM_CAP_USD,
        },
        "workload": {
            "conservative_maximum_cost_usd": conservative_total,
            "cumulative_conservative_maximum_usd": (
                PRIOR_CONSERVATIVE_GLM_USD + conservative_total
            ),
        },
    }


def freeze_plan(plan_path: Path) -> dict:
    plan = build_plan()
    if plan_path.exists():
        existing = json.loads(plan_path.read_text(encoding="utf-8"))
        if existing != plan:
            raise ValueError("existing paced plan differs; refusing to overwrite")
        return existing
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return plan


def _combined_summary(records: list[dict], stopped_result: dict, jev_result: dict) -> dict:
    prior_success = stopped_result["records"][0]
    combined = [prior_success, *records]
    jev_by_id = {record["case_id"]: record for record in jev_result["records"]}
    evaluable = [record for record in combined if not record["failure"]]
    required = [record for record in combined if record["expected_wake"] is True]
    negatives = [record for record in combined if record["expected_wake"] is False]
    agreements = sum(
        record["wake"] == jev_by_id[record["case_id"]]["wake"]
        for record in evaluable
    )
    return {
        "temporal_design": "one prior success plus a separately paced cooldown trial",
        "target_cases": 4,
        "attempted_unique_cases": len(combined),
        "evaluable": len(evaluable),
        "errors": len(combined) - len(evaluable),
        "required": {
            "total": len(required),
            "evaluable": sum(not record["failure"] for record in required),
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
            "evaluable": sum(not record["failure"] for record in negatives),
            "false_wakes": sum(
                bool(record["wake"]) for record in negatives if not record["failure"]
            ),
            "errors": sum(bool(record["failure"]) for record in negatives),
        },
        "same_case_jev_agreements": agreements,
        "same_case_jev_disagreements": len(evaluable) - agreements,
    }


def _write_result(
    output_path: Path,
    *,
    plan_path: Path,
    records: list[dict],
    stopped_result: dict,
    jev_result: dict,
    budget_accounted_cost: float,
    stop_reason: str | None,
) -> dict:
    result = {
        "plan_version": PLAN_VERSION,
        "plan_sha256": _sha256(plan_path),
        "stopped_result_sha256": _sha256(STOPPED_RESULT),
        "baseline_result_sha256": _sha256(BASELINE_RESULT),
        "attempted": len(records),
        "completed_all_cases": len(records) == MAX_ATTEMPTS and stop_reason is None,
        "stop_reason": stop_reason,
        "budget_accounted_cost_usd": budget_accounted_cost,
        "prior_conservative_glm_usd": PRIOR_CONSERVATIVE_GLM_USD,
        "cumulative_conservative_glm_usd": (
            PRIOR_CONSERVATIVE_GLM_USD + budget_accounted_cost
        ),
        "combined_four_case_summary": _combined_summary(
            records, stopped_result, jev_result
        ),
        "records": records,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return result


async def run(plan_path: Path, output_path: Path) -> dict:
    if output_path.exists():
        raise SystemExit("paced result already exists; refusing overwrite/resume")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan != build_plan():
        raise ValueError("paced plan changed after freeze")
    manifest, selection, _, stopped_result = _source_files()
    by_id = {
        case["case_id"]: case
        for case in _load_source_cases(manifest, selection)
    }
    cases = [by_id[case_id] for case_id in plan["case_ids"]]
    jev_result = json.loads(
        (DATA_DIR / "runs" / "jev-pilot.json").read_text(encoding="utf-8")
    )
    client = AsyncOpenAI(
        api_key=os.environ["LITELLM_API_KEY"],
        base_url=api_base_from_env(),
        max_retries=0,
        timeout=TIMEOUT_SECONDS,
    )
    records: list[dict] = []
    budget_accounted_cost = 0.0
    stop_reason: str | None = None
    try:
        for index, case in enumerate(cases):
            maximum_case_cost = conservative_case_cost(case)
            if budget_accounted_cost + maximum_case_cost > HARD_SPEND_CAP_USD:
                raise SystemExit("next request could cross the paced $0.01 cap")
            if (
                PRIOR_CONSERVATIVE_GLM_USD
                + budget_accounted_cost
                + maximum_case_cost
                > TOTAL_GLM_CAP_USD
            ):
                raise SystemExit("next request could cross the cumulative $5 cap")
            started = time.perf_counter()
            response = None
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
                        "provider": {"allow_fallbacks": False},
                    },
                )
                response_metadata = safe_response_metadata(
                    raw_response.headers,
                    status_code=raw_response.status_code,
                    request_id=raw_response.request_id,
                )
                response = raw_response.parse()
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
                failure = safe_error_diagnostics(error)
                response_metadata = failure["response_metadata"]
            latency_ms = (time.perf_counter() - started) * 1_000
            cost = _price(usage)
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
            records.append(
                {
                    "case_id": case["case_id"],
                    "expected_wake": case["expected_wake"],
                    "categories": case["categories"],
                    "wake": decision.wake if decision else None,
                    "judgments": judgments.model_dump() if judgments else None,
                    "failure": failure,
                    "safe_diagnostics": diagnostics,
                    "safe_response_metadata": response_metadata,
                    "latency_ms": latency_ms,
                    "usage": usage,
                    "cost_usd": cost,
                }
            )
            if failure:
                stop_reason = "first_error"
            else:
                retry_seconds = retry_after_seconds(response_metadata)
                if (
                    retry_seconds is not None
                    and retry_seconds > MINIMUM_SECONDS_BETWEEN_REQUESTS
                ):
                    stop_reason = "retry_after_exceeds_pacing_interval"
            result = _write_result(
                output_path,
                plan_path=plan_path,
                records=records,
                stopped_result=stopped_result,
                jev_result=jev_result,
                budget_accounted_cost=budget_accounted_cost,
                stop_reason=stop_reason,
            )
            if stop_reason is not None:
                break
            if index < len(cases) - 1:
                await asyncio.sleep(MINIMUM_SECONDS_BETWEEN_REQUESTS)
    finally:
        await client.close()
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run"))
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--confirm-paid", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "prepare":
        plan = freeze_plan(args.plan)
        print(
            json.dumps(
                {
                    "plan_path": str(args.plan),
                    "plan_sha256": _sha256(args.plan),
                    "plan_version": plan["plan_version"],
                    "strata": plan["strata"],
                    "controls": plan["controls"],
                    "workload": plan["workload"],
                },
                indent=2,
            )
        )
        return
    if not args.confirm_paid:
        raise SystemExit("paced GLM run requires explicit --confirm-paid authorization")
    result = asyncio.run(run(args.plan, args.out))
    print(f"Wrote {args.out}")
    print(json.dumps({key: value for key, value in result.items() if key != "records"}, indent=2))


if __name__ == "__main__":
    main()
