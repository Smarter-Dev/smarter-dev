#!/usr/bin/env python
"""Freeze and run the deliberately stratified 24-window Jev diagnostic.

This is not the paired benchmark and is not a representative prevalence
sample.  It uses only the already-frozen tuning sample: every required-wake
and ambiguous case plus 14 deterministic negatives balanced across channels
and label categories.  Raw records and per-case results remain in the ignored
``data/`` tree.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import statistics
import time
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

from scripts.proactive_eval.benchmark_manifest import DATA_DIR
from scripts.proactive_eval.benchmark_manifest import verify
from scripts.proactive_eval.benchmark_watchers import _cases
from scripts.proactive_eval.benchmark_watchers import _cost
from scripts.proactive_eval.benchmark_watchers import sample_cases
from smarter_dev.bot.proactive.adapter import WATCHER_CONTEXT_SIZE
from smarter_dev.bot.proactive.agent import OPERATING_POLICY_BRIEF
from smarter_dev.bot.proactive.environment import ChannelEnvironment
from smarter_dev.bot.proactive.models import build_watcher_runner
from smarter_dev.bot.proactive.models import typesafe_key_present

REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")
DEFAULT_MANIFEST = DATA_DIR / "benchmark-manifest.json"
DEFAULT_SELECTION = DATA_DIR / "jev-pilot-selection.json"
DEFAULT_OUTPUT = DATA_DIR / "runs" / "jev-pilot.json"
SELECTION_VERSION = "jev-pilot-stratified-v1"
SELECTION_SEED = 20260921
JEV_MODEL_ID = "typesafe:jev-1.13.0"
NEGATIVE_COUNT = 14
EXPECTED_COUNTS = {"required": 8, "negative": 110, "ambiguous": 2}
MAX_ATTEMPTS = 24
SPEND_CAP_USD = 1.0
BOOLEAN_THRESHOLD = 0.5
MINIMUM_CONFIDENCE = 0.0
TIMEOUT_SECONDS = 30.0
CONSERVATIVE_OVERHEAD_TOKENS_PER_ATTEMPT = 2_048


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stable_rank(case_id: str) -> str:
    return hashlib.sha256(f"{SELECTION_SEED}:{case_id}".encode()).hexdigest()


def select_negative_cases(cases: list[dict], count: int = NEGATIVE_COUNT) -> list[dict]:
    """Deterministically balance negatives across channels and categories."""
    candidates = [case for case in cases if case["expected_wake"] is False]
    channels = sorted({str(case["meta"]["channel_id"]) for case in candidates})
    if count < len(channels):
        raise ValueError("negative count cannot cover every channel")
    base, remainder = divmod(count, len(channels))
    ranked_channels = sorted(
        channels,
        key=lambda channel: hashlib.sha256(
            f"{SELECTION_SEED}:channel:{channel}".encode()
        ).hexdigest(),
    )
    quotas = {
        channel: base + (index < remainder)
        for index, channel in enumerate(ranked_channels)
    }
    selected: list[dict] = []
    category_counts: Counter[str] = Counter()
    for channel in ranked_channels:
        pool = [
            case
            for case in candidates
            if str(case["meta"]["channel_id"]) == channel
        ]
        for _ in range(quotas[channel]):
            if not pool:
                raise ValueError(f"not enough negative cases in channel {channel}")
            # Favor cases carrying currently underrepresented categories.  The
            # hash supplies a stable tie-break independent of file ordering.
            picked = min(
                pool,
                key=lambda case: (
                    sum(category_counts[item] for item in case["categories"])
                    / max(1, len(case["categories"])),
                    _stable_rank(case["case_id"]),
                ),
            )
            selected.append(picked)
            pool.remove(picked)
            category_counts.update(picked["categories"])
    return sorted(selected, key=lambda case: case["case_id"])


def build_selection(manifest_path: Path) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    verify(manifest, full=True)
    available = _cases(manifest, "tuning", 60)
    frozen_sample = sample_cases(
        available,
        limit=manifest["design"]["classifier_samples_per_split"],
        seed=manifest["design"]["case_selection_seed"],
    )
    buckets = {
        "required": [case for case in frozen_sample if case["expected_wake"] is True],
        "negative": [case for case in frozen_sample if case["expected_wake"] is False],
        "ambiguous": [case for case in frozen_sample if case["expected_wake"] is None],
    }
    actual_counts = {name: len(items) for name, items in buckets.items()}
    if actual_counts != EXPECTED_COUNTS:
        raise ValueError(
            f"frozen tuning sample changed: expected {EXPECTED_COUNTS}, got {actual_counts}"
        )
    negatives = select_negative_cases(frozen_sample)
    selected = [*buckets["required"], *negatives, *buckets["ambiguous"]]
    selected = sorted(selected, key=lambda case: case["case_id"])
    case_ids = [case["case_id"] for case in selected]
    if len(case_ids) != MAX_ATTEMPTS or len(set(case_ids)) != MAX_ATTEMPTS:
        raise ValueError("pilot selection must contain 24 distinct cases")
    return {
        "selection_version": SELECTION_VERSION,
        "manifest_sha256": _sha256(manifest_path),
        "source_split": "tuning",
        "source_sample_seed": manifest["design"]["case_selection_seed"],
        "source_sample_size": len(frozen_sample),
        "selection_seed": SELECTION_SEED,
        "selection_method": (
            "all required and ambiguous source-sample cases plus deterministic "
            "negative balancing across channels and label categories"
        ),
        "representative": False,
        "model_id": JEV_MODEL_ID,
        "settings": {
            "boolean_threshold": BOOLEAN_THRESHOLD,
            "minimum_confidence": MINIMUM_CONFIDENCE,
            "timeout_seconds": TIMEOUT_SECONDS,
            "fallback_model_id": None,
            "retries": 0,
        },
        "limits": {
            "maximum_inference_attempts": MAX_ATTEMPTS,
            "spend_cap_usd": SPEND_CAP_USD,
        },
        "strata": {"required": 8, "negative": 14, "ambiguous": 2},
        "case_ids": case_ids,
        "case_ids_sha256": hashlib.sha256("\n".join(case_ids).encode()).hexdigest(),
        "negative_channel_counts": dict(
            Counter(str(case["meta"]["channel_id"]) for case in negatives)
        ),
        "negative_category_occurrences": dict(
            sorted(Counter(cat for case in negatives for cat in case["categories"]).items())
        ),
    }


def freeze_selection(manifest_path: Path, selection_path: Path) -> dict:
    selection = build_selection(manifest_path)
    if selection_path.exists():
        existing = json.loads(selection_path.read_text(encoding="utf-8"))
        if existing != selection:
            raise ValueError("existing pilot selection differs; refusing to overwrite it")
        return existing
    selection_path.parent.mkdir(parents=True, exist_ok=True)
    selection_path.write_text(
        json.dumps(selection, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return selection


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * quantile)]


def _preflight_cost(cases: list[dict], input_rate: float) -> dict:
    # One character per token plus fixed request/schema overhead is deliberately
    # pessimistic for English Discord text and the four-field schema.
    estimated_tokens = 0
    for case in cases:
        env = ChannelEnvironment(
            visible=[*case["history"], *case["new_messages"]],
            bot_user_id=str(case["meta"]["bot_user_id"]),
        )
        rendered_chars = len(env.render(case["history"][-WATCHER_CONTEXT_SIZE:]))
        rendered_chars += len(env.render(case["new_messages"]))
        rendered_chars += len(OPERATING_POLICY_BRIEF)
        estimated_tokens += rendered_chars + CONSERVATIVE_OVERHEAD_TOKENS_PER_ATTEMPT
    return {
        "method": "one UTF-8 character per token plus 2,048 tokens per attempt",
        "maximum_input_tokens": estimated_tokens,
        "maximum_cost_usd": estimated_tokens * input_rate / 1_000_000,
    }


async def run_pilot(
    manifest_path: Path, selection_path: Path, output_path: Path
) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    verify(manifest, full=True)
    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    if selection != build_selection(manifest_path):
        raise ValueError("pilot selection or source manifest changed after freeze")
    all_cases = _cases(manifest, "tuning", 60)
    source_sample = sample_cases(
        all_cases,
        limit=manifest["design"]["classifier_samples_per_split"],
        seed=manifest["design"]["case_selection_seed"],
    )
    by_id = {case["case_id"]: case for case in source_sample}
    cases = [by_id[case_id] for case_id in selection["case_ids"]]
    input_rate = manifest["price_assumptions_per_million_tokens_usd"][
        "typesafe_input"
    ]
    preflight = _preflight_cost(cases, input_rate)
    if preflight["maximum_cost_usd"] > SPEND_CAP_USD:
        raise SystemExit("conservative pilot estimate exceeds the $1 spend ceiling")
    if not typesafe_key_present():
        raise SystemExit("TYPESAFE_API_KEY or JEV_API_KEY is not set")

    runner = build_watcher_runner(
        JEV_MODEL_ID,
        boolean_threshold=BOOLEAN_THRESHOLD,
        minimum_confidence=MINIMUM_CONFIDENCE,
        timeout_seconds=TIMEOUT_SECONDS,
    )
    records = []
    cumulative_cost = 0.0
    for attempt, case in enumerate(cases, start=1):
        if attempt > MAX_ATTEMPTS:
            raise AssertionError("pilot exceeded its inference-attempt ceiling")
        env = ChannelEnvironment(
            visible=[*case["history"], *case["new_messages"]],
            bot_user_id=str(case["meta"]["bot_user_id"]),
        )
        started = time.perf_counter()
        decision, usage = await runner.decide(
            instructions=OPERATING_POLICY_BRIEF,
            context_transcript=env.render(case["history"][-WATCHER_CONTEXT_SIZE:]),
            new_transcript=env.render(case["new_messages"]),
            bot_user_id=str(case["meta"]["bot_user_id"]),
            new_message_ids=[message.id for message in case["new_messages"]],
        )
        latency_ms = (time.perf_counter() - started) * 1_000
        classifier = decision.details().get("classifier", {})
        failure = classifier.get("failure")
        cost = _cost(
            JEV_MODEL_ID,
            usage,
            manifest["price_assumptions_per_million_tokens_usd"],
        )
        cumulative_cost += cost
        records.append(
            {
                "case_id": case["case_id"],
                "expected_wake": case["expected_wake"],
                "categories": case["categories"],
                "wake": decision.wake,
                "abstained": bool(classifier.get("abstained")),
                "failure": failure,
                "latency_ms": latency_ms,
                "usage": usage,
                "cost_usd": cost,
                "classifier": classifier,
            }
        )
        if cumulative_cost > SPEND_CAP_USD:
            raise SystemExit("pilot exceeded the $1 spend ceiling; stopped")
        if failure:
            break

    eligible_required = [r for r in records if r["expected_wake"] is True]
    eligible_negative = [r for r in records if r["expected_wake"] is False]
    ambiguous = [r for r in records if r["expected_wake"] is None]
    latencies = [r["latency_ms"] for r in records]
    failures = [r for r in records if r["failure"]]
    summary = {
        "attempted": len(records),
        "completed_all_cases": len(records) == MAX_ATTEMPTS and not failures,
        "required_wakes": {
            "total": len(eligible_required),
            "detected": sum(r["wake"] for r in eligible_required),
            "missed": sum(not r["wake"] for r in eligible_required),
        },
        "deterministic_negatives": {
            "total": len(eligible_negative),
            "false_wakes": sum(r["wake"] for r in eligible_negative),
            "correct_no_wake": sum(not r["wake"] for r in eligible_negative),
        },
        "ambiguous": {
            "total": len(ambiguous),
            "woke": sum(r["wake"] for r in ambiguous),
            "did_not_wake": sum(not r["wake"] for r in ambiguous),
        },
        "abstentions": sum(r["abstained"] for r in records),
        "errors": len(failures),
        "redacted_failure_types": sorted({r["failure"] for r in failures}),
        "latency_ms": {
            "mean": statistics.fmean(latencies) if latencies else None,
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
        },
        "actual_cost_usd": cumulative_cost,
        "input_tokens": sum(r["usage"].get("input_tokens", 0) for r in records),
        "resolved_model_names": sorted(
            {
                r["classifier"].get("resolved_model_name")
                for r in records
                if r["classifier"].get("resolved_model_name")
            }
        ),
    }
    result = {
        "diagnostic_only": True,
        "representative": False,
        "manifest_sha256": _sha256(manifest_path),
        "selection_sha256": _sha256(selection_path),
        "model_id": JEV_MODEL_ID,
        "preflight": preflight,
        "limits": selection["limits"],
        "summary": summary,
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
    parser.add_argument("command", choices=("prepare", "run"))
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--selection", type=Path, default=DEFAULT_SELECTION)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--confirm-paid", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "prepare":
        selection = freeze_selection(args.manifest, args.selection)
        print(
            json.dumps(
                {
                    "selection_path": str(args.selection),
                    "selection_sha256": _sha256(args.selection),
                    "strata": selection["strata"],
                    "negative_channel_counts": selection[
                        "negative_channel_counts"
                    ],
                    "negative_category_occurrences": selection[
                        "negative_category_occurrences"
                    ],
                    "representative": False,
                    "maximum_inference_attempts": MAX_ATTEMPTS,
                    "spend_cap_usd": SPEND_CAP_USD,
                },
                indent=2,
            )
        )
        return
    if not args.confirm_paid:
        raise SystemExit("run requires --confirm-paid")
    if not args.selection.exists():
        raise SystemExit("freeze the selection with `prepare` before `run`")
    result = asyncio.run(run_pilot(args.manifest, args.selection, args.out))
    print(f"Wrote {args.out}")
    print(json.dumps(result["summary"], indent=2))


if __name__ == "__main__":
    main()
