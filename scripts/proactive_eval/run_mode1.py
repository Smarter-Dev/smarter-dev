#!/usr/bin/env python
"""Run a mode-1 scenario against a bot adapter and write scoreable artifacts.

Materializes the scenario (turns bind to the bot's live responses), writes
the fixture/meta/labels under data/mode1/ and the run record under
data/runs/, then prints where to point score_run.

Usage:
    uv run python -m scripts.proactive_eval.run_mode1 scripts/proactive_eval/scenarios/<name>.yaml \
        [--adapter twopass|silent] [--model kimi-k3] [--watcher-model deepseek/deepseek-v4-flash]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(SCRIPTS_DIR))
load_dotenv(REPO_ROOT / ".env")

from scripts.proactive_eval import simulate  # noqa: E402
from scripts.proactive_eval.adapters import SilentAdapter  # noqa: E402
from scripts.proactive_eval.mode1 import (  # noqa: E402
    SCENARIO_BOT_DISPLAY,
    SCENARIO_BOT_USER_ID,
    SCENARIO_CHANNEL,
    SCENARIO_GUILD,
    load_scenario,
    run_mode1,
    write_artifacts,
)

DATA_DIR = Path(__file__).resolve().parent / "data"


async def run(args: argparse.Namespace) -> None:
    scenario = load_scenario(args.scenario)
    if args.adapter == "silent":
        adapter = SilentAdapter()
        model_id = "none"
    else:
        meta = {
            "bot_user_id": SCENARIO_BOT_USER_ID,
            "channel_name": SCENARIO_CHANNEL,
            "guild_name": SCENARIO_GUILD,
        }
        adapter, agent_model_id = simulate._build_twopass_adapter(
            args, [], meta, bot_display_name=SCENARIO_BOT_DISPLAY
        )
        model_id = f"{agent_model_id}+{args.watcher_model}"
    result = await run_mode1(
        scenario,
        adapter,
        adapter_name=args.adapter,
        model_id=model_id,
        activation_cost=simulate.model_cost_calculator(model_id),
    )
    run_path = write_artifacts(result, data_dir=DATA_DIR)
    totals = result.run_record["totals"]
    print(
        f"\n{scenario.name}: {totals['activations']} activations, "
        f"{totals['responses']} responses, ${totals['cost_usd']:.4f}"
    )
    if result.skipped_turn_keys:
        print(
            "Skipped turns (condition unmet): "
            + ", ".join(result.skipped_turn_keys)
        )
    print(f"Run record: {run_path}")
    print(
        f"Score it with:\n  uv run python -m scripts.proactive_eval.score_run "
        f'"{run_path}"'
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_mode1",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("scenario", type=Path)
    parser.add_argument("--adapter", choices=("twopass", "silent"), default="twopass")
    parser.add_argument("--model", default=None)
    parser.add_argument(
        "--watcher-model", default=simulate.DEFAULT_TWOPASS_WATCHER_MODEL
    )
    return parser


def main() -> None:
    asyncio.run(run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
