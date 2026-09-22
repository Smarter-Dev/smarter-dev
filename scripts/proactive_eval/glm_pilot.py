#!/usr/bin/env python
"""Run GLM once on the exact frozen 24-case Jev pilot selection.

The supplied key is treated as GLM-5.3-Flash-only.  This command refuses a
different host or model, disables client/application retries and provider
fallbacks, caps output per request, records errors without retrying them, and
stops before a request whose conservative bound could cross the hard budget.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import statistics
import time
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from openai import AsyncOpenAI
from pydantic_ai import Agent
from pydantic_ai import PromptedOutput
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.openai import OpenAIProvider

from scripts.proactive_eval.benchmark_manifest import DATA_DIR
from scripts.proactive_eval.benchmark_manifest import verify
from scripts.proactive_eval.benchmark_watchers import _cases
from scripts.proactive_eval.benchmark_watchers import sample_cases
from scripts.proactive_eval.jev_pilot import DEFAULT_MANIFEST
from scripts.proactive_eval.jev_pilot import DEFAULT_SELECTION
from scripts.proactive_eval.jev_pilot import MAX_ATTEMPTS
from scripts.proactive_eval.jev_pilot import build_selection
from smarter_dev.bot.proactive.adapter import WATCHER_CONTEXT_SIZE
from smarter_dev.bot.proactive.agent import OPERATING_POLICY_BRIEF
from smarter_dev.bot.proactive.environment import ChannelEnvironment
from smarter_dev.bot.proactive.watcher import WATCHER_SYSTEM_PROMPT
from smarter_dev.bot.proactive.watcher import WatcherDecision
from smarter_dev.bot.proactive.watcher import build_watcher_prompt
from smarter_dev.bot.proactive.watcher import usage_dict

REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")

DEFAULT_JEV_RESULT = DATA_DIR / "runs" / "jev-pilot.json"
DEFAULT_OUTPUT = DATA_DIR / "runs" / "glm-pilot.json"
EXPECTED_HOST = "llmproxy.zech.sh"
REQUESTED_MODEL = "glm-5.3-flash"
UPSTREAM_MODEL = "openrouter/z-ai/glm-5.3-flash"
CHAT_PATH = "/v1/chat/completions"
INPUT_PRICE_PER_MILLION = 0.075
OUTPUT_PRICE_PER_MILLION = 0.25
CACHE_READ_PRICE_PER_MILLION = 0.015
MAX_OUTPUT_TOKENS = 512
TIMEOUT_SECONDS = 30.0
HARD_SPEND_CAP_USD = 5.0
PROMPT_OVERHEAD_TOKENS = 4_096


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def api_base_from_env() -> str:
    endpoint = os.getenv("LITELLM_ENDPOINT", "").rstrip("/")
    key = os.getenv("LITELLM_API_KEY", "")
    parsed = urlparse(endpoint)
    if not key:
        raise SystemExit("LITELLM_API_KEY is not set")
    if (
        parsed.scheme != "https"
        or parsed.hostname != EXPECTED_HOST
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise SystemExit(
            f"LITELLM_ENDPOINT must be https://{EXPECTED_HOST} with no credentials"
        )
    path = parsed.path.rstrip("/")
    if path not in ("", "/v1"):
        raise SystemExit("unexpected LITELLM_ENDPOINT path")
    return endpoint if path == "/v1" else endpoint + "/v1"


def _price(usage: dict) -> float:
    cache_tokens = usage.get("cache_read_tokens", 0)
    uncached_input_tokens = max(0, usage.get("input_tokens", 0) - cache_tokens)
    return (
        uncached_input_tokens * INPUT_PRICE_PER_MILLION
        + usage.get("output_tokens", 0) * OUTPUT_PRICE_PER_MILLION
        + cache_tokens * CACHE_READ_PRICE_PER_MILLION
    ) / 1_000_000


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * quantile)]


def _prompt(case: dict) -> str:
    env = ChannelEnvironment(
        visible=[*case["history"], *case["new_messages"]],
        bot_user_id=str(case["meta"]["bot_user_id"]),
    )
    return build_watcher_prompt(
        instructions=OPERATING_POLICY_BRIEF,
        context_transcript=env.render(case["history"][-WATCHER_CONTEXT_SIZE:]),
        new_transcript=env.render(case["new_messages"]),
        bot_user_id=str(case["meta"]["bot_user_id"]),
    )


def conservative_case_cost(case: dict) -> float:
    # One UTF-8 character per token plus 4K tokens for system/schema overhead,
    # then the full bounded completion. This is deliberately pessimistic.
    input_tokens = len(_prompt(case)) + len(WATCHER_SYSTEM_PROMPT)
    input_tokens += PROMPT_OVERHEAD_TOKENS
    return (
        input_tokens * INPUT_PRICE_PER_MILLION
        + MAX_OUTPUT_TOKENS * OUTPUT_PRICE_PER_MILLION
    ) / 1_000_000


def _quality(records: list[dict], expected: bool) -> tuple[int, int, int]:
    selected = [record for record in records if record["expected_wake"] is expected]
    evaluated = [record for record in selected if not record["failure"]]
    wakes = sum(record["wake"] for record in evaluated)
    return len(selected), len(evaluated), wakes


def summarize(records: list[dict], cumulative_cost: float) -> dict:
    required_total, required_evaluated, required_detected = _quality(records, True)
    negative_total, negative_evaluated, false_wakes = _quality(records, False)
    ambiguous = [record for record in records if record["expected_wake"] is None]
    evaluated_ambiguous = [record for record in ambiguous if not record["failure"]]
    latencies = [record["latency_ms"] for record in records]
    usage = {
        key: sum(record["usage"].get(key, 0) for record in records)
        for key in ("input_tokens", "output_tokens", "cache_read_tokens")
    }
    return {
        "attempted": len(records),
        "completed_all_cases": len(records) == MAX_ATTEMPTS
        and not any(record["failure"] for record in records),
        "required_wakes": {
            "total": required_total,
            "evaluated": required_evaluated,
            "detected": required_detected,
            "missed": required_evaluated - required_detected,
            "errors": required_total - required_evaluated,
        },
        "deterministic_negatives": {
            "total": negative_total,
            "evaluated": negative_evaluated,
            "false_wakes": false_wakes,
            "correct_no_wake": negative_evaluated - false_wakes,
            "errors": negative_total - negative_evaluated,
        },
        "ambiguous": {
            "total": len(ambiguous),
            "evaluated": len(evaluated_ambiguous),
            "woke": sum(record["wake"] for record in evaluated_ambiguous),
            "did_not_wake": sum(
                not record["wake"] for record in evaluated_ambiguous
            ),
            "errors": len(ambiguous) - len(evaluated_ambiguous),
        },
        "errors": sum(bool(record["failure"]) for record in records),
        "redacted_failure_types": sorted(
            {record["failure"] for record in records if record["failure"]}
        ),
        "latency_ms": {
            "mean": statistics.fmean(latencies) if latencies else None,
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            "minimum": min(latencies) if latencies else None,
            "maximum": max(latencies) if latencies else None,
        },
        "usage": usage,
        "priced_cost_usd": cumulative_cost,
        "conservative_cost_upper_bound_usd": cumulative_cost
        + sum(
            record.get("conservative_cost_reservation_usd", 0)
            for record in records
        ),
        "resolved_model_names": sorted(
            {
                record["resolved_model_name"]
                for record in records
                if record["resolved_model_name"]
            }
        ),
    }


def compare_with_jev(glm_records: list[dict], jev_result: dict) -> dict:
    jev_by_id = {record["case_id"]: record for record in jev_result["records"]}
    if set(jev_by_id) != {record["case_id"] for record in glm_records}:
        raise ValueError("GLM and Jev result case IDs differ")
    disagreements = []
    comparable = [record for record in glm_records if not record["failure"]]
    for glm_record in comparable:
        jev_record = jev_by_id[glm_record["case_id"]]
        if glm_record["wake"] != jev_record["wake"]:
            disagreements.append(
                {
                    "case_id": glm_record["case_id"],
                    "expected_wake": glm_record["expected_wake"],
                    "categories": glm_record["categories"],
                    "glm_wake": glm_record["wake"],
                    "jev_wake": jev_record["wake"],
                }
            )
    return {
        "same_case_count": len(glm_records),
        "comparable_case_count": len(comparable),
        "glm_error_count": len(glm_records) - len(comparable),
        "prediction_agreements": len(comparable) - len(disagreements),
        "prediction_disagreements": len(disagreements),
        "disagreements": disagreements,
        "latency_p50_delta_ms_glm_minus_jev": (
            summarize(glm_records, 0)["latency_ms"]["p50"]
            - jev_result["summary"]["latency_ms"]["p50"]
        ),
        "known_priced_cost_delta_usd_glm_minus_jev": (
            sum(record["cost_usd"] for record in glm_records)
            - jev_result["summary"]["actual_cost_usd"]
        ),
    }


async def run(
    manifest_path: Path,
    selection_path: Path,
    jev_result_path: Path,
    output_path: Path,
) -> dict:
    # A result file means this diagnostic has already begun.  Never resume it:
    # stop-on-first-error must remain a real stop, not merely a pause before an
    # unattended suffix run.  The historical two-stage baseline is preserved
    # at the default path and is intentionally not reproducible by this code.
    if output_path.exists():
        raise SystemExit("GLM pilot result already exists; refusing overwrite/resume")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    verify(manifest, full=True)
    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    if selection != build_selection(manifest_path):
        raise ValueError("pilot selection or source manifest changed after freeze")
    jev_result = json.loads(jev_result_path.read_text(encoding="utf-8"))
    if jev_result["selection_sha256"] != _sha256(selection_path):
        raise ValueError("existing Jev result does not match the frozen selection")

    source = sample_cases(
        _cases(manifest, "tuning", 60),
        limit=manifest["design"]["classifier_samples_per_split"],
        seed=manifest["design"]["case_selection_seed"],
    )
    by_id = {case["case_id"]: case for case in source}
    cases = [by_id[case_id] for case_id in selection["case_ids"]]
    conservative_costs = [conservative_case_cost(case) for case in cases]
    conservative_total = sum(conservative_costs)
    if conservative_total > HARD_SPEND_CAP_USD:
        raise SystemExit("conservative GLM pilot estimate exceeds the $5 ceiling")

    api_base = api_base_from_env()
    client = AsyncOpenAI(
        api_key=os.environ["LITELLM_API_KEY"],
        base_url=api_base,
        max_retries=0,
        timeout=TIMEOUT_SECONDS,
    )
    model = OpenAIChatModel(
        REQUESTED_MODEL,
        provider=OpenAIProvider(openai_client=client),
        profile=OpenAIModelProfile(
            openai_supports_tool_choice_required=False,
            openai_chat_supports_multiple_system_messages=False,
        ),
    )
    agent = Agent(
        model,
        output_type=PromptedOutput(WatcherDecision),
        system_prompt=WATCHER_SYSTEM_PROMPT,
        retries=0,
    )

    records = []
    cumulative_cost = 0.0
    budget_accounted_cost = 0.0
    for attempt, (case, maximum_case_cost) in enumerate(
        zip(cases, conservative_costs, strict=True), start=1
    ):
        if attempt > MAX_ATTEMPTS:
            raise AssertionError("GLM pilot exceeded its attempt ceiling")
        if budget_accounted_cost + maximum_case_cost > HARD_SPEND_CAP_USD:
            raise SystemExit("next GLM request could cross the $5 ceiling; stopped")
        started = time.perf_counter()
        failure = None
        resolved_model_name = None
        usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0}
        decision = WatcherDecision(wake=False)
        try:
            result = await agent.run(
                _prompt(case),
                model_settings={
                    "max_tokens": MAX_OUTPUT_TOKENS,
                    "timeout": TIMEOUT_SECONDS,
                    "extra_body": {
                        "num_retries": 0,
                        "fallbacks": [],
                        "provider": {"allow_fallbacks": False},
                    },
                },
            )
            decision = result.output
            usage = usage_dict(result.usage)
            resolved_model_name = result.response.model_name
        except Exception as error:  # noqa: BLE001 - persist redacted failure type
            failure = type(error).__name__
        latency_ms = (time.perf_counter() - started) * 1_000
        cost = _price(usage)
        cumulative_cost += cost
        if failure:
            budget_accounted_cost += maximum_case_cost
        else:
            budget_accounted_cost += cost
        records.append(
            {
                "case_id": case["case_id"],
                "expected_wake": case["expected_wake"],
                "categories": case["categories"],
                "wake": decision.wake,
                "failure": failure,
                "latency_ms": latency_ms,
                "usage": usage,
                "cost_usd": cost,
                "conservative_cost_reservation_usd": (
                    maximum_case_cost if failure else 0.0
                ),
                "resolved_model_name": resolved_model_name,
            }
        )
        if failure:
            break

    summary = summarize(records, cumulative_cost)
    paired = compare_with_jev(records, jev_result) if len(records) == MAX_ATTEMPTS else None
    result = {
        "diagnostic_only": True,
        "representative": False,
        "manifest_sha256": _sha256(manifest_path),
        "selection_sha256": _sha256(selection_path),
        "jev_result_sha256": _sha256(jev_result_path),
        "endpoint": {
            "api_base": api_base,
            "chat_completions_path": CHAT_PATH,
            "requested_model": REQUESTED_MODEL,
            "configured_upstream": UPSTREAM_MODEL,
        },
        "pricing_per_million_tokens_usd": {
            "input": INPUT_PRICE_PER_MILLION,
            "output": OUTPUT_PRICE_PER_MILLION,
            "cache_read": CACHE_READ_PRICE_PER_MILLION,
        },
        "controls": {
            "client_retries": 0,
            "agent_validation_retries": 0,
            "provider_fallbacks": False,
            "timeout_seconds": TIMEOUT_SECONDS,
            "max_output_tokens_per_call": MAX_OUTPUT_TOKENS,
            "maximum_inference_attempts": MAX_ATTEMPTS,
            "hard_total_glm_cap_usd": HARD_SPEND_CAP_USD,
            "first_case_is_smoke": True,
            "stop_on_first_error": True,
        },
        "preflight": {
            "method": (
                "one UTF-8 character per input token plus 4,096 input tokens "
                "of system/schema overhead and 512 output tokens per case"
            ),
            "maximum_cost_usd": conservative_total,
        },
        "summary": summary,
        "paired_comparison": paired,
        "records": records,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--selection", type=Path, default=DEFAULT_SELECTION)
    parser.add_argument("--jev-result", type=Path, default=DEFAULT_JEV_RESULT)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--confirm-paid", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if not args.confirm_paid:
        raise SystemExit("GLM pilot requires --confirm-paid")
    result = asyncio.run(
        run(args.manifest, args.selection, args.jev_result, args.out)
    )
    print(f"Wrote {args.out}")
    print(json.dumps(result["summary"], indent=2))
    print(json.dumps(result["paired_comparison"], indent=2))


if __name__ == "__main__":
    main()
