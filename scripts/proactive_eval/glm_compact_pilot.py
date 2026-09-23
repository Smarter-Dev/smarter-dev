#!/usr/bin/env python
"""Prepare or run the compact atomic GLM repair diagnostic.

The prospective GLM path uses the exact four Boolean judgments Jev receives,
native JSON-schema output, and the same deterministic judgment-to-wake/brief
conversion.  ``prepare`` is offline.  ``run`` is paid-gated, refuses to
overwrite an existing result, stops on the first error, and is not authorized
merely by preparing the plan.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import APIStatusError
from openai import APITimeoutError
from openai import AsyncOpenAI
from pydantic import ValidationError

from scripts.proactive_eval.benchmark_manifest import DATA_DIR
from scripts.proactive_eval.benchmark_manifest import verify
from scripts.proactive_eval.benchmark_watchers import _cases
from scripts.proactive_eval.benchmark_watchers import sample_cases
from scripts.proactive_eval.glm_pilot import INPUT_PRICE_PER_MILLION
from scripts.proactive_eval.glm_pilot import OUTPUT_PRICE_PER_MILLION
from scripts.proactive_eval.glm_pilot import REQUESTED_MODEL
from scripts.proactive_eval.glm_pilot import TIMEOUT_SECONDS
from scripts.proactive_eval.glm_pilot import UPSTREAM_MODEL
from scripts.proactive_eval.glm_pilot import _price
from scripts.proactive_eval.glm_pilot import _prompt as baseline_prompt
from scripts.proactive_eval.glm_pilot import api_base_from_env
from scripts.proactive_eval.jev_pilot import DEFAULT_MANIFEST
from scripts.proactive_eval.jev_pilot import DEFAULT_SELECTION
from scripts.proactive_eval.jev_pilot import build_selection
from smarter_dev.bot.proactive.agent import OPERATING_POLICY_BRIEF
from smarter_dev.bot.proactive.environment import ChannelEnvironment
from smarter_dev.bot.proactive.watcher import JevWatcherJudgments
from smarter_dev.bot.proactive.watcher import build_jev_watcher_instructions
from smarter_dev.bot.proactive.watcher import build_jev_watcher_material
from smarter_dev.bot.proactive.watcher import decision_from_jev

REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")

BASELINE_RESULT = DATA_DIR / "runs" / "glm-pilot.json"
DEFAULT_PLAN = DATA_DIR / "glm-compact-pilot-plan.json"
DEFAULT_OUTPUT = DATA_DIR / "runs" / "glm-compact-pilot.json"
PLAN_VERSION = "glm-compact-atomic-v1"
REPAIR_CASE_COUNT = 4
MAX_OUTPUT_TOKENS = 512
REASONING_EFFORT = "low"
REPAIR_SPEND_CAP_USD = 0.01
PROMPT_OVERHEAD_TOKENS = 4_096
JUDGMENT_FIELDS = frozenset(JevWatcherJudgments.model_fields)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compact_response_format() -> dict:
    schema = JevWatcherJudgments.model_json_schema()
    schema["additionalProperties"] = False
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "proactive_watcher_judgments",
            "strict": True,
            "schema": schema,
        },
    }


def compact_messages(case: dict) -> list[dict[str, str]]:
    env = ChannelEnvironment(
        visible=[*case["history"], *case["new_messages"]],
        bot_user_id=str(case["meta"]["bot_user_id"]),
    )
    return [
        {
            "role": "system",
            "content": build_jev_watcher_instructions(
                instructions=OPERATING_POLICY_BRIEF,
                bot_user_id=str(case["meta"]["bot_user_id"]),
                bot_display_name="the bot",
            ),
        },
        {
            "role": "user",
            "content": build_jev_watcher_material(
                context_transcript=env.render(case["history"][-30:]),
                new_transcript=env.render(case["new_messages"]),
            ),
        },
    ]


def _usage(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    return {
        "input_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
        "cache_read_tokens": int(getattr(prompt_details, "cached_tokens", 0) or 0),
    }


def safe_exception_diagnostics(error: Exception) -> dict:
    """Return bounded type/status metadata without exception text or bodies."""
    chain = []
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(chain) < 6:
        seen.add(id(current))
        chain.append(type(current).__name__)
        current = current.__cause__ or current.__context__
    if isinstance(error, APITimeoutError) or "Timeout" in chain:
        category = "timeout"
    elif isinstance(error, APIStatusError):
        category = "http_error"
    elif isinstance(error, ValidationError):
        category = "schema_validation"
    else:
        category = "client_or_provider_error"
    diagnostics = {"category": category, "exception_types": chain}
    if isinstance(error, APIStatusError):
        diagnostics["http_status"] = error.status_code
    if isinstance(error, ValidationError):
        diagnostics["validation_issue_types"] = sorted(
            {item["type"] for item in error.errors(include_input=False)}
        )
        diagnostics["validation_fields"] = sorted(
            {
                str(item["loc"][0])
                for item in error.errors(include_input=False)
                if item.get("loc") and str(item["loc"][0]) in JUDGMENT_FIELDS
            }
        )
    return diagnostics


def parse_completion(
    response: Any,
) -> tuple[JevWatcherJudgments | None, dict, dict, dict | None]:
    """Parse native JSON while retaining only safe structural diagnostics."""
    usage = _usage(response)
    choices = list(getattr(response, "choices", []) or [])
    if not choices:
        return None, usage, {"choice_count": 0}, {"category": "empty_response"}
    choice = choices[0]
    message = choice.message
    content = message.content if isinstance(message.content, str) else ""
    completion_details = getattr(getattr(response, "usage", None), "completion_tokens_details", None)
    diagnostics = {
        "finish_reason": getattr(choice, "finish_reason", None),
        "truncated": getattr(choice, "finish_reason", None) == "length",
        "refused": bool(getattr(message, "refusal", None)),
        "content_present": bool(content),
        "content_characters": len(content),
        "reasoning_tokens": int(
            getattr(completion_details, "reasoning_tokens", 0) or 0
        ),
        "resolved_model_name": getattr(response, "model", None),
    }
    if diagnostics["truncated"]:
        return None, usage, diagnostics, {"category": "truncated_completion"}
    if diagnostics["refused"]:
        return None, usage, diagnostics, {"category": "refusal"}
    try:
        parsed = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        diagnostics["json_decodable"] = False
        return None, usage, diagnostics, {"category": "invalid_json"}
    diagnostics["json_decodable"] = True
    parsed_keys = set(parsed) if isinstance(parsed, dict) else set()
    # Report only fixed contract field names and counts.  An arbitrary key can
    # echo private input, so never retain it even though response text itself is
    # already excluded.
    diagnostics["recognized_fields"] = sorted(parsed_keys & JUDGMENT_FIELDS)
    diagnostics["missing_field_count"] = len(JUDGMENT_FIELDS - parsed_keys)
    diagnostics["unexpected_field_count"] = len(parsed_keys - JUDGMENT_FIELDS)
    try:
        judgments = JevWatcherJudgments.model_validate(parsed)
    except ValidationError as error:
        return None, usage, diagnostics, safe_exception_diagnostics(error)
    diagnostics["schema_valid"] = True
    return judgments, usage, diagnostics, None


def _compact_character_count(case: dict) -> int:
    return sum(len(message["content"]) for message in compact_messages(case)) + len(
        json.dumps(compact_response_format(), separators=(",", ":"))
    )


def conservative_case_cost(case: dict) -> float:
    input_tokens = _compact_character_count(case) + PROMPT_OVERHEAD_TOKENS
    return (
        input_tokens * INPUT_PRICE_PER_MILLION
        + MAX_OUTPUT_TOKENS * OUTPUT_PRICE_PER_MILLION
    ) / 1_000_000


def _load_source_cases(manifest: dict, selection: dict) -> list[dict]:
    source = sample_cases(
        _cases(manifest, "tuning", 60),
        limit=manifest["design"]["classifier_samples_per_split"],
        seed=manifest["design"]["case_selection_seed"],
    )
    by_id = {case["case_id"]: case for case in source}
    return [by_id[case_id] for case_id in selection["case_ids"]]


def repair_cases(cases: list[dict]) -> list[dict]:
    required = [case for case in cases if case["expected_wake"] is True]
    negative = [case for case in cases if case["expected_wake"] is False]
    picked = [*required[:2], *negative[:2]]
    if len(picked) != REPAIR_CASE_COUNT:
        raise ValueError("repair diagnostic needs two required and two negative cases")
    return picked


def build_plan(manifest_path: Path, selection_path: Path) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    verify(manifest, full=True)
    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    if selection != build_selection(manifest_path):
        raise ValueError("pilot selection or source manifest changed after freeze")
    all_cases = _load_source_cases(manifest, selection)
    cases = repair_cases(all_cases)
    baseline_material_characters = sum(len(baseline_prompt(case)) for case in cases)
    compact_message_characters = sum(
        sum(len(message["content"]) for message in compact_messages(case))
        for case in cases
    )
    compact_schema_characters = len(
        json.dumps(compact_response_format(), separators=(",", ":"))
    )
    conservative_total = sum(conservative_case_cost(case) for case in cases)
    if conservative_total > REPAIR_SPEND_CAP_USD:
        raise ValueError("compact repair plan exceeds its proposed $0.01 cap")
    return {
        "plan_version": PLAN_VERSION,
        "manifest_sha256": _sha256(manifest_path),
        "selection_sha256": _sha256(selection_path),
        "baseline_result_sha256": (
            _sha256(BASELINE_RESULT) if BASELINE_RESULT.exists() else None
        ),
        "case_selection": "first two required then first two negatives from frozen pilot",
        "case_ids": [case["case_id"] for case in cases],
        "strata": {"required": 2, "negative": 2, "ambiguous": 0},
        "contract": {
            "schema": "JevWatcherJudgments",
            "fields": sorted(JUDGMENT_FIELDS),
            "output_mode": "native_json_schema_strict",
            "brief_construction": "all new message IDs when deterministic policy wakes",
        },
        "controls": {
            "requested_model": REQUESTED_MODEL,
            "configured_upstream": UPSTREAM_MODEL,
            "reasoning_effort": REASONING_EFFORT,
            "max_completion_tokens": MAX_OUTPUT_TOKENS,
            "timeout_seconds": TIMEOUT_SECONDS,
            "client_retries": 0,
            "provider_fallbacks": False,
            "stop_on_first_error": True,
            "overwrite_or_resume": False,
            "maximum_attempts": REPAIR_CASE_COUNT,
            "hard_spend_cap_usd": REPAIR_SPEND_CAP_USD,
        },
        "workload": {
            "baseline_user_prompt_characters": baseline_material_characters,
            "compact_system_and_user_characters": compact_message_characters,
            "compact_response_schema_characters_per_call": compact_schema_characters,
            "compact_contract": "four required booleans; no generated brief or message-id list",
            "comparison_note": (
                "baseline excludes Pydantic's generated PromptedOutput instructions; "
                "the character totals are therefore workload descriptors, not a "
                "claimed input-token reduction"
            ),
            "conservative_maximum_cost_usd": conservative_total,
        },
    }


def freeze_plan(manifest_path: Path, selection_path: Path, plan_path: Path) -> dict:
    plan = build_plan(manifest_path, selection_path)
    if plan_path.exists():
        existing = json.loads(plan_path.read_text(encoding="utf-8"))
        if existing != plan:
            raise ValueError("existing compact plan differs; refusing to overwrite")
        return existing
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return plan


async def run(
    manifest_path: Path,
    selection_path: Path,
    plan_path: Path,
    output_path: Path,
) -> dict:
    if output_path.exists():
        raise SystemExit("compact result already exists; refusing overwrite/resume")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan != build_plan(manifest_path, selection_path):
        raise ValueError("compact plan changed after freeze")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    by_id = {
        case["case_id"]: case
        for case in _load_source_cases(manifest, selection)
    }
    cases = [by_id[case_id] for case_id in plan["case_ids"]]
    client = AsyncOpenAI(
        api_key=os.environ["LITELLM_API_KEY"],
        base_url=api_base_from_env(),
        max_retries=0,
        timeout=TIMEOUT_SECONDS,
    )
    records = []
    accounted_cost = 0.0
    for case in cases:
        bound = conservative_case_cost(case)
        if accounted_cost + bound > REPAIR_SPEND_CAP_USD:
            raise SystemExit("next request could cross the compact $0.01 cap")
        started = time.perf_counter()
        response = None
        try:
            response = await client.chat.completions.create(
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
            judgments, usage, diagnostics, failure = parse_completion(response)
        except Exception as error:  # noqa: BLE001 - safe type-only diagnostics
            judgments = None
            usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0}
            diagnostics = {}
            failure = safe_exception_diagnostics(error)
        latency_ms = (time.perf_counter() - started) * 1_000
        cost = _price(usage)
        accounted_cost += bound if failure and response is None else cost
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
                "latency_ms": latency_ms,
                "usage": usage,
                "cost_usd": cost,
                "deterministic_relevant_message_count": (
                    len(decision.relevant_message_ids) if decision else 0
                ),
            }
        )
        if failure:
            break
    result = {
        "plan_sha256": _sha256(plan_path),
        "baseline_result_sha256": plan["baseline_result_sha256"],
        "attempted": len(records),
        "stopped_on_first_error": bool(records and records[-1]["failure"]),
        "budget_accounted_cost_usd": accounted_cost,
        "records": records,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run"))
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--selection", type=Path, default=DEFAULT_SELECTION)
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--confirm-paid", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "prepare":
        plan = freeze_plan(args.manifest, args.selection, args.plan)
        print(
            json.dumps(
                {
                    "plan_path": str(args.plan),
                    "plan_sha256": _sha256(args.plan),
                    "strata": plan["strata"],
                    "contract": plan["contract"],
                    "controls": plan["controls"],
                    "workload": plan["workload"],
                },
                indent=2,
            )
        )
        return
    if not args.confirm_paid:
        raise SystemExit("compact GLM run requires separate --confirm-paid authorization")
    result = asyncio.run(run(args.manifest, args.selection, args.plan, args.out))
    print(f"Wrote {args.out}")
    print(json.dumps({key: value for key, value in result.items() if key != "records"}, indent=2))


if __name__ == "__main__":
    main()
