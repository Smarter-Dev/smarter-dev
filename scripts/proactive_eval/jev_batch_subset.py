#!/usr/bin/env python
"""Bounded Jev-only replay of two tuning channel-days under live batch rules.

Dry-run by default. The paid path requires --confirm-paid, has no fallback or
retries, and never runs the downstream chat agent. Raw per-batch records stay
in the ignored data/runs directory.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import UTC
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from scripts.proactive_eval.benchmark_manifest import DATA_DIR
from scripts.proactive_eval.benchmark_manifest import verify
from smarter_dev.bot.proactive.adapter import JEV_WATCHER_CONTEXT_SIZE
from smarter_dev.bot.proactive.adapter import bot_directed_message_ids
from smarter_dev.bot.proactive.agent import OPERATING_POLICY_BRIEF
from smarter_dev.bot.proactive.environment import ChannelEnvironment
from smarter_dev.bot.proactive.models import build_watcher_runner
from smarter_dev.bot.proactive.models import typesafe_key_present
from smarter_dev.bot.proactive.types import ChannelMessage
from smarter_dev.bot.proactive.windows import jev_batch_windows

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")
MODEL_ID = "typesafe:jev-1.13.0"
# Captured bot author name in the same guild's frozen fixture collection.
BOT_DISPLAY_NAME = "Smarter Dev"
FIXTURES = (
    "644299523686006834-💾technical-talk-2026-08-08.jsonl",
    "644299523686006834-🤡off-topic-chat-2026-08-17.jsonl",
)
OUTPUT = DATA_DIR / "runs" / "jev-batch-subset-v1.json"
MAX_CALLS = 25
MAX_COST_USD = 0.02
INPUT_PRICE_PER_MILLION = 0.042


def prepare() -> tuple[list[dict], float]:
    manifest = json.loads((DATA_DIR / "benchmark-manifest.json").read_text())
    verify(manifest, full=True)
    entries = {entry["fixture"]: entry for entry in manifest["fixtures"]}
    cases = []
    preflight_cost = 0.0
    for filename in FIXTURES:
        entry = entries[filename]
        if entry["split"] != "tuning":
            raise ValueError("batch pilot must use tuning fixtures only")
        messages = [
            ChannelMessage.from_record(json.loads(line))
            for line in (DATA_DIR / filename).read_text().splitlines()
            if line.strip()
        ]
        if any(message.is_bot for message in messages):
            raise ValueError("pilot fixtures must be human-only for live ingest parity")
        meta = json.loads(
            (DATA_DIR / filename.replace(".jsonl", ".meta.json")).read_text()
        )
        cursor = 0
        for first, fire_at, count in jev_batch_windows(
            [message.timestamp for message in messages]
        ):
            new_messages = messages[cursor : cursor + count]
            history = messages[max(0, cursor - JEV_WATCHER_CONTEXT_SIZE) : cursor]
            cursor += count
            assert new_messages[0].timestamp == first
            env = ChannelEnvironment(
                visible=[*history, *new_messages],
                bot_user_id=str(meta["bot_user_id"]),
            )
            direct = bool(
                bot_directed_message_ids(
                    new_messages, env, str(meta["bot_user_id"])
                )
            )
            if not direct:
                conservative_tokens = (
                    len(env.render(history))
                    + len(env.render(new_messages))
                    + len(OPERATING_POLICY_BRIEF)
                    + 2048
                )
                preflight_cost += (
                    conservative_tokens * INPUT_PRICE_PER_MILLION / 1_000_000
                )
            cases.append(
                {
                    "fixture": filename,
                    "first_at": first.isoformat(),
                    "fire_at": fire_at.isoformat(),
                    "history": history,
                    "new_messages": new_messages,
                    "env": env,
                    "meta": meta,
                    "direct": direct,
                }
            )
        assert cursor == len(messages)
    provider_calls = sum(not case["direct"] for case in cases)
    if provider_calls > MAX_CALLS or preflight_cost > MAX_COST_USD:
        raise ValueError("batch pilot exceeds its frozen call or cost ceiling")
    return cases, preflight_cost


async def run(cases: list[dict], preflight_cost: float) -> dict:
    if not typesafe_key_present():
        raise SystemExit("TYPESAFE_API_KEY or JEV_API_KEY is not set")
    runner = build_watcher_runner(
        MODEL_ID,
        boolean_threshold=0.5,
        minimum_confidence=0.0,
        timeout_seconds=30.0,
    )
    records = []
    cumulative_cost = 0.0
    for case in cases:
        if case["direct"]:
            continue
        env = case["env"]
        started = time.perf_counter()
        decision, usage = await runner.decide(
            instructions=OPERATING_POLICY_BRIEF,
            context_transcript=env.render(case["history"]),
            new_transcript=env.render(case["new_messages"]),
            bot_user_id=str(case["meta"]["bot_user_id"]),
            bot_display_name=BOT_DISPLAY_NAME,
            new_message_ids=[message.id for message in case["new_messages"]],
        )
        classifier = decision.details().get("classifier", {})
        cost = usage.get("input_tokens", 0) * INPUT_PRICE_PER_MILLION / 1_000_000
        cumulative_cost += cost
        records.append(
            {
                "fixture": case["fixture"],
                "first_at": case["first_at"],
                "fire_at": case["fire_at"],
                "new_message_ids": [message.id for message in case["new_messages"]],
                "history_count": len(case["history"]),
                "wake": decision.wake,
                "classifier": classifier,
                "usage": usage,
                "latency_ms": (time.perf_counter() - started) * 1000,
                "cost_usd": cost,
            }
        )
        if cumulative_cost > MAX_COST_USD or classifier.get("failure"):
            break
    result = {
        "model_id": MODEL_ID,
        "fixtures": FIXTURES,
        "generated_at": datetime.now(UTC).isoformat(),
        "total_batches": len(cases),
        "deterministic_batches": sum(case["direct"] for case in cases),
        "planned_provider_calls": sum(not case["direct"] for case in cases),
        "preflight_cost_usd": preflight_cost,
        "actual_provider_calls": sum(
            record["classifier"].get("requests", 0) for record in records
        ),
        "input_tokens": sum(record["usage"].get("input_tokens", 0) for record in records),
        "actual_cost_usd": cumulative_cost,
        "waking_batches": sum(record["wake"] for record in records),
        "failures": sum(bool(record["classifier"].get("failure")) for record in records),
        "records": records,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-paid", action="store_true")
    args = parser.parse_args()
    cases, preflight_cost = prepare()
    if not args.confirm_paid:
        print(
            json.dumps(
                {
                    "fixtures": FIXTURES,
                    "messages": sum(len(case["new_messages"]) for case in cases),
                    "batches": len(cases),
                    "planned_provider_calls": sum(not case["direct"] for case in cases),
                    "preflight_cost_usd": preflight_cost,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    result = asyncio.run(run(cases, preflight_cost))
    print(json.dumps({key: value for key, value in result.items() if key != "records"}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
