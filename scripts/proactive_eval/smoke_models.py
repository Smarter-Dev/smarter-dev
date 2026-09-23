#!/usr/bin/env python
"""Run one synthetic watcher request per configured model.

Dry-run is the default. ``--confirm-paid`` is required and the command makes
exactly one request for each ``--model``. It prints only model metadata,
latency, token counts, and decision state; credentials are never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv

from smarter_dev.bot.proactive.models import build_watcher_runner
from smarter_dev.bot.proactive.models import typesafe_key_present

REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")

DEFAULT_MODELS = ("typesafe:jev-latest", "z-ai/glm-5.3-flash")


def _glm_key_present() -> bool:
    proxy_ready = bool(os.getenv("LITELLM_ENDPOINT") and os.getenv("LITELLM_API_KEY"))
    openrouter_ready = bool(
        os.getenv("OPENROUTER_API_KEY")
        or os.getenv("OPEN_ROUTER_API_KEY")
        or os.getenv("OPEN_ROUTER")
    )
    return proxy_ready or openrouter_ready


def _check_credentials(model_id: str) -> None:
    if model_id.startswith("typesafe:") and not typesafe_key_present():
        raise SystemExit("TYPESAFE_API_KEY or JEV_API_KEY is not set")
    if model_id == "z-ai/glm-5.3-flash" and not _glm_key_present():
        raise SystemExit(
            "GLM needs LITELLM_ENDPOINT plus LITELLM_API_KEY, or an "
            "OpenRouter key"
        )


async def _smoke(model_id: str, timeout_seconds: float) -> dict:
    runner = build_watcher_runner(
        model_id,
        minimum_confidence=0.0,
        timeout_seconds=timeout_seconds,
    )
    started = time.perf_counter()
    decision, usage = await runner.decide(
        instructions=(
            "Wake when the bot is directly addressed or can answer a concrete "
            "open question."
        ),
        context_transcript="",
        new_transcript=(
            "[id=smoke-1] Reviewer (user id 1): helperbot, are you available?"
        ),
        bot_user_id="999",
        bot_display_name="helperbot",
        new_message_ids=["smoke-1"],
    )
    classifier = decision.details().get("classifier", {})
    return {
        "model_id": model_id,
        "resolved_model_name": classifier.get("resolved_model_name"),
        "provider": classifier.get("provider"),
        "requests": classifier.get("requests", 0),
        "failure": classifier.get("failure"),
        "wake": decision.wake,
        "latency_ms": (time.perf_counter() - started) * 1000,
        "usage": usage,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", action="append", dest="models")
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--confirm-paid", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    models = args.models or list(DEFAULT_MODELS)
    if not args.confirm_paid:
        print(json.dumps({"dry_run": True, "models": models, "maximum_calls": len(models)}))
        return
    for model_id in models:
        _check_credentials(model_id)
    results = [
        asyncio.run(_smoke(model_id, args.timeout_seconds)) for model_id in models
    ]
    print(json.dumps({"dry_run": False, "results": results}, indent=2))
    if any(result["failure"] for result in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
