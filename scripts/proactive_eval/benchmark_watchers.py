#!/usr/bin/env python
"""Paired classifier-only replay for Jev and GLM on a frozen manifest.

The command is dry-run by default. Passing ``--confirm-paid`` requires a
verified manifest, an explicit call ceiling, and stops at the manifest's
phase dollar cap. Raw records stay under the gitignored data directory.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import statistics
import time
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

from scripts.proactive_eval.benchmark_manifest import DATA_DIR
from scripts.proactive_eval.benchmark_manifest import verify
from scripts.proactive_eval.wake_labels import window_ground_truth
from smarter_dev.bot.proactive.adapter import WatcherProducer
from smarter_dev.bot.proactive.adapter import engagement_notifications
from smarter_dev.bot.proactive.agent import OPERATING_POLICY_BRIEF
from smarter_dev.bot.proactive.environment import ChannelEnvironment
from smarter_dev.bot.proactive.environment import InstructionStore
from smarter_dev.bot.proactive.models import build_watcher_runner
from smarter_dev.bot.proactive.models import typesafe_key_present
from smarter_dev.bot.proactive.notifications import NotificationQueue
from smarter_dev.bot.proactive.types import ActivationContext
from smarter_dev.bot.proactive.types import ChannelMessage
from smarter_dev.bot.proactive.windows import two_pass_windows

REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")

PINNED_JEV_MODEL_ID = "typesafe:jev-1.13.0"
JEV_HELDOUT_PLAN_VERSION = "jev-heldout-v1"
DEFAULT_JEV_HELDOUT_PLAN = DATA_DIR / "jev-heldout-v1-plan.json"
DEFAULT_JEV_HELDOUT_OUTPUT = DATA_DIR / "runs" / "jev-heldout-v1.json"
JEV_HELDOUT_SPEND_CAP_USD = 1.0
JEV_HELDOUT_MAX_DECISIONS = 360
JEV_INPUT_PRICE_PER_MILLION = 0.042
JEV_OUTPUT_PRICE_PER_MILLION = 0.0
CONSERVATIVE_PROMPT_OVERHEAD_TOKENS = 2_048


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = round((len(ordered) - 1) * percentile)
    return ordered[index]


def _quality(records: list[dict]) -> dict[str, float | int | None]:
    labeled = [record for record in records if record["expected_wake"] is not None]
    eligible = [
        record
        for record in labeled
        if not record.get("abstained") and record.get("failure") is None
    ]
    tp = sum(r["expected_wake"] is True and r["wake"] for r in eligible)
    fp = sum(r["expected_wake"] is False and r["wake"] for r in eligible)
    tn = sum(r["expected_wake"] is False and not r["wake"] for r in eligible)
    fn = sum(r["expected_wake"] is True and not r["wake"] for r in eligible)
    return {
        "labeled_samples": len(labeled),
        "eligible_samples": len(eligible),
        "unevaluable_labeled_samples": len(labeled) - len(eligible),
        "true_positive": tp,
        "false_positive": fp,
        "true_negative": tn,
        "false_negative": fn,
        "wake_precision": tp / (tp + fp) if tp + fp else None,
        "wake_recall": tp / (tp + fn) if tp + fn else None,
        "false_wake_rate": fp / (fp + tn) if fp + tn else None,
    }


def score(records: list[dict]) -> dict:
    by_model: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        by_model[record["model_id"]].append(record)
    scored = {}
    for model_id, model_records in by_model.items():
        quality = _quality(model_records)
        latencies = [r["latency_ms"] for r in model_records]
        requests = sum(r["provider_requests"] for r in model_records)
        usage_totals: dict[str, dict[str, int]] = {}
        for record in model_records:
            for usage_model, usage in record["usage_by_model"].items():
                totals = usage_totals.setdefault(
                    usage_model,
                    {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cache_read_tokens": 0,
                    },
                )
                for key in totals:
                    totals[key] += usage.get(key, 0)
        categories = sorted(
            {category for record in model_records for category in record["categories"]}
        )
        labeled_samples = int(quality["labeled_samples"] or 0)
        scored[model_id] = {
            "samples": len(model_records),
            **quality,
            "ambiguous_samples": len(model_records) - labeled_samples,
            "abstentions": sum(r["abstained"] for r in model_records),
            "failures": sum(r["failure"] is not None for r in model_records),
            "provider_requests": requests,
            "latency_ms": {
                "mean": statistics.fmean(latencies) if latencies else None,
                "p50": _percentile(latencies, 0.50),
                "p95": _percentile(latencies, 0.95),
            },
            "resolved_model_names": sorted(
                {
                    r["resolved_model_name"]
                    for r in model_records
                    if r["resolved_model_name"]
                }
            ),
            "usage_by_model": usage_totals,
            "per_category": {
                category: _quality(
                    [r for r in model_records if category in r["categories"]]
                )
                for category in categories
            },
        }
    return scored


def distinct_case_summary(records: list[dict]) -> dict:
    """Score one predeclared observation per distinct frozen case."""
    return score([record for record in records if record["repeat"] == 0])


def repeat_stability(records: list[dict], *, expected_repeats: int) -> dict:
    """Describe repeat agreement without treating repeats as new labels."""
    by_case: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for record in records:
        by_case[(record["model_id"], record["case_id"])].append(record)
    complete = 0
    stable_wake = 0
    stable_no_wake = 0
    variable = 0
    incomplete = 0
    for case_records in by_case.values():
        evaluable = [
            record
            for record in case_records
            if record.get("failure") is None and not record.get("abstained")
        ]
        if len(evaluable) != expected_repeats:
            incomplete += 1
            continue
        complete += 1
        wakes = {bool(record["wake"]) for record in evaluable}
        if len(wakes) > 1:
            variable += 1
        elif True in wakes:
            stable_wake += 1
        else:
            stable_no_wake += 1
    return {
        "distinct_cases": len(by_case),
        "expected_repeats_per_case": expected_repeats,
        "complete_cases": complete,
        "incomplete_cases": incomplete,
        "stable_wake_cases": stable_wake,
        "stable_no_wake_cases": stable_no_wake,
        "variable_wake_cases": variable,
        "per_repeat_quality": {
            str(repeat): score(
                [record for record in records if record["repeat"] == repeat]
            )
            for repeat in range(expected_repeats)
        },
    }


def grouped_bootstrap(
    records: list[dict], *, iterations: int = 2_000, seed: int = 20260920
) -> dict:
    """Paired 95% intervals, resampling whole fixture/channel-days."""
    model_ids = sorted({record["model_id"] for record in records})
    fixtures = sorted({record["fixture"] for record in records})
    if len(model_ids) != 2 or not fixtures:
        return {}
    grouped = {
        (model_id, fixture): [
            record
            for record in records
            if record["model_id"] == model_id and record["fixture"] == fixture
        ]
        for model_id in model_ids
        for fixture in fixtures
    }
    randomizer = random.Random(seed)
    samples: dict[str, list[float]] = defaultdict(list)
    for _ in range(iterations):
        picked = randomizer.choices(fixtures, k=len(fixtures))
        qualities = {}
        for model_id in model_ids:
            sample_records = [
                record for fixture in picked for record in grouped[(model_id, fixture)]
            ]
            qualities[model_id] = _quality(sample_records)
        for metric in ("wake_precision", "wake_recall", "false_wake_rate"):
            left = qualities[model_ids[0]][metric]
            right = qualities[model_ids[1]][metric]
            if left is not None and right is not None:
                samples[metric].append(float(left) - float(right))
    return {
        "model_order": model_ids,
        "delta_definition": f"{model_ids[0]} minus {model_ids[1]}",
        "iterations": iterations,
        "intervals": {
            metric: {
                "lower": _percentile(values, 0.025),
                "upper": _percentile(values, 0.975),
            }
            for metric, values in samples.items()
        },
    }


def _load_bundle(entry: dict) -> tuple[list[ChannelMessage], dict, dict]:
    fixture = DATA_DIR / entry["fixture"]
    records = [
        json.loads(line)
        for line in fixture.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    meta = json.loads(
        fixture.with_name(f"{fixture.stem}.meta.json").read_text(encoding="utf-8")
    )
    wake_labels = json.loads(
        fixture.with_name(f"{fixture.stem}.wake-labels.json").read_text(
            encoding="utf-8"
        )
    )["labels"]
    return [ChannelMessage.from_record(record) for record in records], meta, wake_labels


def _cases(manifest: dict, split: str, history_size: int) -> list[dict]:
    cases = []
    for entry in manifest["fixtures"]:
        if entry["split"] != split:
            continue
        messages, meta, labels = _load_bundle(entry)
        cursor = 0
        timeline: list[ChannelMessage] = []
        for window_index, (window_start, window_end) in enumerate(
            two_pass_windows([message.timestamp for message in messages])
        ):
            new_messages = []
            while cursor < len(messages) and messages[cursor].timestamp < window_end:
                new_messages.append(messages[cursor])
                cursor += 1
            if not new_messages:
                continue
            expected, categories = window_ground_truth(
                [message.to_record() for message in new_messages], labels
            )
            cases.append(
                {
                    "case_id": f"{entry['fixture']}::{window_index}",
                    "fixture": entry["fixture"],
                    "window_start": window_start,
                    "window_end": window_end,
                    "history": list(timeline[-history_size:]),
                    "new_messages": new_messages,
                    "meta": meta,
                    "expected_wake": expected,
                    "categories": categories,
                }
            )
            timeline.extend(new_messages)
    return cases


def sample_cases(cases: list[dict], *, limit: int, seed: int) -> list[dict]:
    """Select an outcome-blind, proportional sample across channel-days."""
    if len(cases) <= limit:
        return cases
    grouped: dict[str, list[dict]] = defaultdict(list)
    for case in cases:
        grouped[case["fixture"]].append(case)
    if limit < len(grouped):
        raise ValueError("sample limit must include at least one case per fixture")

    total = len(cases)
    raw = {
        fixture: limit * len(fixture_cases) / total
        for fixture, fixture_cases in grouped.items()
    }
    quotas = {
        fixture: max(1, min(len(grouped[fixture]), int(value)))
        for fixture, value in raw.items()
    }
    while sum(quotas.values()) < limit:
        candidates = [
            fixture for fixture in grouped if quotas[fixture] < len(grouped[fixture])
        ]
        fixture = max(
            candidates,
            key=lambda item: (raw[item] - quotas[item], item),
        )
        quotas[fixture] += 1
    while sum(quotas.values()) > limit:
        candidates = [fixture for fixture in grouped if quotas[fixture] > 1]
        fixture = min(
            candidates,
            key=lambda item: (raw[item] - quotas[item], item),
        )
        quotas[fixture] -= 1

    selected = []
    for fixture in sorted(grouped):
        fixture_cases = list(grouped[fixture])
        fixture_seed = int(
            hashlib.sha256(f"{seed}:{fixture}".encode()).hexdigest()[:16], 16
        )
        random.Random(fixture_seed).shuffle(fixture_cases)
        selected.extend(fixture_cases[: quotas[fixture]])
    return sorted(selected, key=lambda case: case["case_id"])


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _direct_engagement(case: dict) -> bool:
    env = ChannelEnvironment(
        visible=[*case["history"], *case["new_messages"]],
        bot_user_id=str(case["meta"]["bot_user_id"]),
    )
    return bool(
        engagement_notifications(
            case["new_messages"],
            env,
            str(case["meta"]["bot_user_id"]),
            channel_id=str(case["meta"]["channel_id"]),
            channel_name=case["meta"]["channel_name"],
        )
    )


def _conservative_jev_case_cost(case: dict) -> float:
    env = ChannelEnvironment(
        visible=[*case["history"], *case["new_messages"]],
        bot_user_id=str(case["meta"]["bot_user_id"]),
    )
    rendered = env.render([*case["history"], *case["new_messages"]])
    # One character per token plus fixed instruction/schema overhead is an
    # intentionally pessimistic reservation for Jev's input-only pricing.
    input_tokens = (
        len(rendered)
        + len(OPERATING_POLICY_BRIEF)
        + CONSERVATIVE_PROMPT_OVERHEAD_TOKENS
    )
    return input_tokens * JEV_INPUT_PRICE_PER_MILLION / 1_000_000


def build_jev_heldout_plan(manifest_path: Path) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    verify(manifest, full=True)
    history_size = 60
    available = _cases(manifest, "heldout", history_size)
    cases = sample_cases(
        available,
        limit=manifest["design"]["classifier_samples_per_split"],
        seed=manifest["design"]["case_selection_seed"],
    )
    repeats = 3
    provider_cases = [case for case in cases if not _direct_engagement(case)]
    exact_provider_calls = len(provider_cases) * repeats
    decision_windows = len(cases) * repeats
    conservative_cost = (
        sum(_conservative_jev_case_cost(case) for case in provider_cases) * repeats
    )
    if decision_windows > JEV_HELDOUT_MAX_DECISIONS:
        raise ValueError("Jev held-out plan exceeds 360 decision windows")
    if conservative_cost > JEV_HELDOUT_SPEND_CAP_USD:
        raise ValueError("Jev held-out plan exceeds its $1 spend cap")
    return {
        "plan_version": JEV_HELDOUT_PLAN_VERSION,
        "manifest_sha256": _sha256(manifest_path),
        "split": "heldout",
        "available_cases": len(available),
        "case_ids": [case["case_id"] for case in cases],
        "selected_case_ids_sha256": hashlib.sha256(
            "\n".join(case["case_id"] for case in cases).encode()
        ).hexdigest(),
        "strata": {
            "required": sum(case["expected_wake"] is True for case in cases),
            "negative": sum(case["expected_wake"] is False for case in cases),
            "null_target": sum(case["expected_wake"] is None for case in cases),
        },
        "settings": {
            "model": PINNED_JEV_MODEL_ID,
            "boolean_threshold": 0.5,
            "minimum_confidence": 0.0,
            "timeout_seconds": 30.0,
            "retries": 0,
            "fallback": None,
            "history_size": history_size,
            "concurrency": 1,
            "repeats": repeats,
        },
        "workload": {
            "distinct_cases": len(cases),
            "decision_windows": decision_windows,
            "deterministic_engagement_windows_per_repeat": len(cases)
            - len(provider_cases),
            "exact_provider_calls": exact_provider_calls,
            "maximum_attempts": JEV_HELDOUT_MAX_DECISIONS,
            "conservative_cost_usd": conservative_cost,
            "incremental_spend_cap_usd": JEV_HELDOUT_SPEND_CAP_USD,
            "prices_per_million_usd": {
                "input": JEV_INPUT_PRICE_PER_MILLION,
                "output": JEV_OUTPUT_PRICE_PER_MILLION,
            },
        },
        "absolute_quality_gates": {
            "maximum_required_wake_misses": 0,
            "maximum_false_wakes": 5,
            "maximum_deterministic_direct_engagement_misses": 0,
            "maximum_failures_plus_abstentions": 1,
            "null_targets_scored_separately": True,
            "repeats_are_not_independent_labels": True,
        },
    }


def freeze_jev_heldout_plan(manifest_path: Path, plan_path: Path) -> dict:
    plan = build_jev_heldout_plan(manifest_path)
    if plan_path.exists():
        existing = json.loads(plan_path.read_text(encoding="utf-8"))
        if existing != plan:
            raise ValueError("existing Jev held-out plan differs; refusing overwrite")
        return existing
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    return plan


def _cost(model_id: str, usage: dict, prices: dict) -> float:
    if model_id.startswith("typesafe:"):
        input_rate = prices["typesafe_input"]
        output_rate = prices["typesafe_output"]
        cache_rate = 0.0
    elif model_id == "z-ai/glm-5.3-flash":
        input_rate = prices["glm_input"]
        output_rate = prices["glm_output"]
        cache_rate = prices["glm_cache_read"]
    else:
        raise ValueError(f"no benchmark price assumption for {model_id}")
    if input_rate is None or output_rate is None:
        raise ValueError(f"price assumptions are incomplete for {model_id}")
    cache_tokens = usage.get("cache_read_tokens", 0)
    # Provider usage reports cached tokens as a subset of total input tokens.
    # Charge those once at the cache rate rather than at both rates.
    uncached_input_tokens = max(0, usage.get("input_tokens", 0) - cache_tokens)
    return (
        uncached_input_tokens * input_rate
        + usage.get("output_tokens", 0) * output_rate
        + cache_tokens * cache_rate
    ) / 1_000_000


async def run(args: argparse.Namespace) -> dict:
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    jev_only = bool(args.jev_heldout)
    if jev_only and args.split != "heldout":
        raise SystemExit("--jev-heldout requires --split heldout")
    verify(manifest, full=(args.confirm_paid or jev_only) and not args.allow_incomplete)
    available_cases = _cases(manifest, args.split, args.history_size)
    sample_limit = manifest["design"]["classifier_samples_per_split"]
    selection_seed = manifest["design"]["case_selection_seed"]
    cases = sample_cases(available_cases, limit=sample_limit, seed=selection_seed)
    models = (
        [PINNED_JEV_MODEL_ID]
        if jev_only
        else [manifest["models"]["jev"], manifest["models"]["glm"]]
    )
    repeats = args.repeats or manifest["design"]["classifier_repeats"]
    decision_windows = len(cases) * len(models) * repeats
    plan = build_jev_heldout_plan(args.manifest) if jev_only else None
    conservative_calls = (
        int(plan["workload"]["exact_provider_calls"])
        if plan is not None
        else decision_windows
    )
    manifest_limit = manifest["design"]["max_classifier_calls"]
    if decision_windows > manifest_limit:
        raise SystemExit(
            f"planned {decision_windows} decisions exceeds manifest limit {manifest_limit}"
        )
    if args.freeze_plan:
        if not jev_only:
            raise SystemExit("--freeze-plan is only supported with --jev-heldout")
        frozen = freeze_jev_heldout_plan(args.manifest, args.plan)
        return {
            "dry_run": True,
            "prepared_plan": frozen,
            "plan_sha256": _sha256(args.plan),
        }
    if not args.confirm_paid:
        return {
            "dry_run": True,
            "split": args.split,
            "available_cases": len(available_cases),
            "cases": len(cases),
            "repeats": repeats,
            "models": models,
            "maximum_paid_calls": conservative_calls,
            "decision_windows": decision_windows,
            "conservative_cost_usd": (
                plan["workload"]["conservative_cost_usd"] if plan else None
            ),
        }
    if jev_only:
        if not args.plan.exists():
            raise SystemExit("freeze the Jev held-out plan before paid execution")
        frozen = json.loads(args.plan.read_text(encoding="utf-8"))
        if frozen != plan:
            raise SystemExit("Jev held-out plan changed after freeze")
        if args.out.exists():
            raise SystemExit("Jev held-out result exists; refusing overwrite/resume")
        if repeats != 3 or args.history_size != 60:
            raise SystemExit("Jev held-out settings differ from the frozen plan")
        if args.jev_min_confidence != 0.0 or args.timeout_seconds != 30.0:
            raise SystemExit("Jev held-out settings differ from the frozen plan")
    if args.max_paid_calls is None or conservative_calls > args.max_paid_calls:
        raise SystemExit(
            f"pass --max-paid-calls at least {conservative_calls} to confirm the ceiling"
        )
    if models[0].startswith("typesafe:") and not typesafe_key_present():
        raise SystemExit("TYPESAFE_API_KEY or JEV_API_KEY is not set")

    runners = {
        model_id: build_watcher_runner(
            model_id,
            boolean_threshold=args.jev_boolean_threshold,
            minimum_confidence=args.jev_min_confidence,
            timeout_seconds=args.timeout_seconds,
        )
        for model_id in models
    }
    records = []
    cumulative_cost = 0.0
    phase_cap = (
        JEV_HELDOUT_SPEND_CAP_USD
        if jev_only
        else manifest["design"]["phase_spend_caps_usd"][
            "heldout_classifier" if args.split == "heldout" else "tuning"
        ]
    )
    for repeat in range(repeats):
        for case_index, case in enumerate(cases):
            order = models if (repeat + case_index) % 2 == 0 else list(reversed(models))
            for model_id in order:
                producer = WatcherProducer(
                    watcher=runners[model_id],
                    instruction_store=InstructionStore(seed=OPERATING_POLICY_BRIEF),
                    watcher_model_id=model_id,
                    notification_queue=NotificationQueue(),
                )
                context = ActivationContext(
                    channel_name=case["meta"]["channel_name"],
                    channel_id=str(case["meta"]["channel_id"]),
                    guild_name=case["meta"]["guild_name"],
                    bot_user_id=str(case["meta"]["bot_user_id"]),
                    activated_at=case["window_end"],
                    history=case["history"],
                    new_messages=case["new_messages"],
                )
                started = time.perf_counter()
                failure = None
                try:
                    usage_by_model = await producer.produce(context)
                except Exception as error:  # noqa: BLE001 - benchmark records failures
                    usage_by_model = {}
                    failure = type(error).__name__
                latency_ms = (time.perf_counter() - started) * 1000
                details = producer.details.get("watcher", {})
                classifier = details.get("classifier", {})
                provider_requests = classifier.get("requests", 0) + classifier.get(
                    "jev", {}
                ).get("requests", 0)
                for usage_model_id, usage in usage_by_model.items():
                    cumulative_cost += _cost(
                        usage_model_id,
                        usage,
                        manifest["price_assumptions_per_million_tokens_usd"],
                    )
                if cumulative_cost > phase_cap:
                    raise SystemExit(
                        f"phase spend cap ${phase_cap:.2f} exceeded; stopped"
                    )
                records.append(
                    {
                        "case_id": case["case_id"],
                        "fixture": case["fixture"],
                        "repeat": repeat,
                        "model_id": model_id,
                        "expected_wake": case["expected_wake"],
                        "categories": case["categories"],
                        "wake": bool(details.get("wake", False)),
                        "abstained": bool(classifier.get("abstained", False)),
                        "failure": failure or classifier.get("failure"),
                        "provider_requests": provider_requests,
                        "deterministic_engagement": _direct_engagement(case),
                        "latency_ms": latency_ms,
                        "usage_by_model": usage_by_model,
                        "resolved_model_name": classifier.get("resolved_model_name"),
                        "classifier": classifier,
                    }
                )
    actual_provider_requests = sum(record["provider_requests"] for record in records)
    if plan is not None and actual_provider_requests != conservative_calls:
        raise SystemExit(
            "actual Jev provider request count differs from the frozen preflight"
        )
    distinct = distinct_case_summary(records)
    gate_evaluation = None
    if jev_only:
        metrics = distinct[PINNED_JEV_MODEL_ID]
        direct_records = [
            record
            for record in records
            if record["repeat"] == 0 and record["deterministic_engagement"]
        ]
        direct_misses = sum(
            record["expected_wake"] is True and not record["wake"]
            for record in direct_records
        )
        gates = plan["absolute_quality_gates"]
        gate_evaluation = {
            "required_wake_misses": metrics["false_negative"],
            "false_wakes": metrics["false_positive"],
            "deterministic_direct_engagement_misses": direct_misses,
            "failures_plus_abstentions": metrics["failures"] + metrics["abstentions"],
            "passes": (
                metrics["false_negative"] <= gates["maximum_required_wake_misses"]
                and metrics["false_positive"] <= gates["maximum_false_wakes"]
                and direct_misses
                <= gates["maximum_deterministic_direct_engagement_misses"]
                and metrics["failures"] + metrics["abstentions"]
                <= gates["maximum_failures_plus_abstentions"]
            ),
        }
    return {
        "dry_run": False,
        "manifest_sha256": hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
        "split": args.split,
        "available_cases": len(available_cases),
        "selected_case_ids_sha256": hashlib.sha256(
            "\n".join(case["case_id"] for case in cases).encode()
        ).hexdigest(),
        "repeats": repeats,
        "plan_version": plan["plan_version"] if plan else None,
        "plan_sha256": _sha256(args.plan) if plan else None,
        "jev_min_confidence": args.jev_min_confidence,
        "jev_boolean_threshold": args.jev_boolean_threshold,
        "started_with_concurrency": 1,
        "cumulative_cost_usd": cumulative_cost,
        "actual_provider_requests": actual_provider_requests,
        "records": records,
        "summary": score(records),
        "distinct_case_summary": distinct,
        "repeat_stability": repeat_stability(records, expected_repeats=repeats),
        "absolute_gate_evaluation": gate_evaluation,
        "paired_grouped_bootstrap": grouped_bootstrap(records),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--split", choices=("tuning", "heldout"), default="tuning")
    parser.add_argument("--repeats", type=int)
    parser.add_argument("--history-size", type=int, default=60)
    parser.add_argument("--jev-heldout", action="store_true")
    parser.add_argument("--jev-boolean-threshold", type=float, default=0.5)
    parser.add_argument("--jev-min-confidence", type=float, default=0.0)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--max-paid-calls", type=int)
    parser.add_argument("--confirm-paid", action="store_true")
    parser.add_argument("--freeze-plan", action="store_true")
    parser.add_argument("--plan", type=Path, default=DEFAULT_JEV_HELDOUT_PLAN)
    parser.add_argument("--allow-incomplete", action="store_true")
    parser.add_argument("--out", type=Path)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.out is None:
        args.out = (
            DEFAULT_JEV_HELDOUT_OUTPUT
            if args.jev_heldout
            else DATA_DIR / "runs" / "watcher-benchmark.json"
        )
    result = asyncio.run(run(args))
    if result["dry_run"]:
        print(json.dumps(result, indent=2))
        return
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {args.out}")
    print(json.dumps(result["summary"], indent=2))


if __name__ == "__main__":
    main()
