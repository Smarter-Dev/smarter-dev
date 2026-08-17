#!/usr/bin/env python
"""Replay a fixture day through a bot adapter on a periodic-activation loop.

Simulates the future proactive bot waking every --every seconds over the
pulled day, calls the chosen adapter for each non-empty window, and writes a
run record (activations, responses, token usage, USD cost) that stage 4
scores. Ends with the cost summary the repo owner asked for: cost per day,
30-day projection, and cadence sensitivity.

Usage:
    uv run python -m scripts.proactive_eval.simulate scripts/proactive_eval/data/<fixture>.jsonl \
        [--every 300] [--model gemini-3.5-flash-lite] [--adapter baseline|silent] \
        [--history-size 60] [--out path.json]

The baseline adapter needs the provider API key for --model in .env.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable
from pathlib import Path

from dotenv import load_dotenv
from genai_prices import Usage, calc_price

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(SCRIPTS_DIR))
load_dotenv(REPO_ROOT / ".env")

import eval_prices  # noqa: E402  — prices for models newer than the snapshot

from scripts.proactive_eval.adapters import (  # noqa: E402
    BaselineAdapter,
    SilentAdapter,
)
from scripts.proactive_eval.simulation import (  # noqa: E402
    ActivationResult,
    FixtureMessage,
    format_cost_summary,
    run_simulation,
)
from smarter_dev.shared.model_catalog import MODEL_CATALOG, ModelProvider  # noqa: E402

eval_prices.install()

RUNS_DIR = Path(__file__).resolve().parent / "data" / "runs"

_PROVIDER_IDS = {
    ModelProvider.GOOGLE: "google",
    ModelProvider.OPENAI: "openai",
    ModelProvider.ANTHROPIC: "anthropic",
    ModelProvider.OPENROUTER: "openrouter",
}


def _provider_id_for(model_id: str) -> str:
    for model in MODEL_CATALOG:
        if model.model_id == model_id:
            return _PROVIDER_IDS.get(model.provider, model.provider.name.lower())
    if model_id.startswith(("gpt-", "openai/")):
        return "openai"
    if "/" in model_id:
        return "openrouter"
    return "google"


def _usage_cost(
    model_id: str, input_tokens: int, output_tokens: int, cache_read_tokens: int
) -> float:
    """List-price USD for one model's usage; zero usage skips the lookup."""
    if not (input_tokens or output_tokens or cache_read_tokens):
        return 0.0
    priced = calc_price(
        Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
        ),
        model_ref=model_id,
        provider_id=_provider_id_for(model_id),
    )
    return float(priced.total_price)


def model_cost_calculator(model_id: str) -> Callable[[ActivationResult], float]:
    """USD cost of one activation at list price.

    Multi-model results (``usage_by_model`` set) price each entry at its own
    model's list price; otherwise the whole usage is priced on ``model_id``.
    Zero-usage entries cost nothing and skip the price lookup entirely, so
    unpriceable model ids only fail once real tokens are spent.
    """

    def activation_cost(result: ActivationResult) -> float:
        if result.usage_by_model is not None:
            return sum(
                _usage_cost(
                    usage_model_id,
                    usage.get("input_tokens", 0),
                    usage.get("output_tokens", 0),
                    usage.get("cache_read_tokens", 0),
                )
                for usage_model_id, usage in result.usage_by_model.items()
            )
        return _usage_cost(
            model_id,
            result.input_tokens,
            result.output_tokens,
            result.cache_read_tokens,
        )

    return activation_cost


def _load_fixture(fixture_path: Path) -> tuple[list[FixtureMessage], dict]:
    messages = [
        FixtureMessage.from_record(json.loads(line))
        for line in fixture_path.read_text(encoding="utf-8").splitlines()
    ]
    meta_path = fixture_path.parent / f"{fixture_path.stem}.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    return messages, meta


def _bot_display_name(messages: list[FixtureMessage], bot_user_id: str) -> str:
    for message in messages:
        if message.author_id == bot_user_id:
            return message.author_display
    return "the proactive bot"


def _default_out_path(
    fixture_path: Path, adapter_name: str, model_id: str, cadence_seconds: int
) -> Path:
    model_slug = model_id.replace("/", "-")
    return (
        RUNS_DIR
        / f"{fixture_path.stem}.{adapter_name}.{model_slug}.{cadence_seconds}s.json"
    )


async def run(args: argparse.Namespace) -> None:
    messages, meta = _load_fixture(args.fixture)
    if args.adapter == "silent":
        adapter = SilentAdapter()
        model_id = "none"
    else:
        model_id = args.model
        adapter = BaselineAdapter(
            model_id=model_id,
            bot_display_name=_bot_display_name(messages, meta["bot_user_id"]),
            guild_name=meta["guild_name"],
            channel_name=meta["channel_name"],
            cadence_seconds=args.every,
        )
    record = await run_simulation(
        messages=messages,
        channel_name=meta["channel_name"],
        guild_name=meta["guild_name"],
        bot_user_id=meta["bot_user_id"],
        adapter=adapter,
        adapter_name=args.adapter,
        model_id=model_id,
        cadence_seconds=args.every,
        history_size=args.history_size,
        activation_cost=model_cost_calculator(model_id),
        fixture_name=args.fixture.name,
    )
    out_path = args.out or _default_out_path(
        args.fixture, args.adapter, model_id, args.every
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    totals = record["totals"]
    print(
        f"\nWrote run record to {out_path}\n"
        f"{totals['activations']} activations "
        f"({totals['activations_with_messages']} with messages, "
        f"{totals['activations_with_responses']} with responses, "
        f"{totals['responses']} responses)\n"
    )
    print(format_cost_summary(record["cost_summary"]))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="simulate",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--every", type=int, default=300)
    parser.add_argument("--model", default="gemini-3.5-flash-lite")
    parser.add_argument("--adapter", choices=("baseline", "silent"), default="baseline")
    parser.add_argument("--history-size", type=int, default=60)
    parser.add_argument("--out", type=Path, default=None)
    return parser


def main() -> None:
    asyncio.run(run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
