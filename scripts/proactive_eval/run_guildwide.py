#!/usr/bin/env python
"""Replay several same-day channel fixtures through ONE guild-wide agent.

Models the deployed guild architecture: each channel keeps its own producer
(two-pass burst windows + watcher) pushing channel-labeled notifications into
one shared queue, and a single agent with one persistent history consumes
every wake. Responses are dispatched to the channel each ProposedResponse
names, injected into that channel's timeline, and become part of later
context there.

Outputs one combined run record (with channel-targeting metrics) plus one
score_run-compatible per-channel record per fixture, so the existing judge
pipeline scores each channel's responses against its own transcript.

Usage:
    uv run python -m scripts.proactive_eval.run_guildwide \
        scripts/proactive_eval/data/<fixture-a>.jsonl \
        scripts/proactive_eval/data/<fixture-b>.jsonl \
        [--model gemini-3.7-flash] [--watcher-model z-ai/glm-5.3-flash] \
        [--history-size 60] [--run-name guildwide]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(SCRIPTS_DIR))
load_dotenv(REPO_ROOT / ".env")

import eval_prices  # noqa: E402

from scripts.proactive_eval.replay_tools import replay_parity_tools  # noqa: E402
from scripts.proactive_eval.simulate import model_cost_calculator  # noqa: E402
from scripts.proactive_eval.simulation import build_cost_summary  # noqa: E402
from smarter_dev.bot.proactive.adapter import (  # noqa: E402
    AgentConsumer,
    WatcherProducer,
)
from smarter_dev.bot.proactive.agent import (  # noqa: E402
    OPERATING_POLICY_BRIEF,
    KimiAgentRunner,
    build_guild_agent_system_prompt,
    build_kimi_agent,
)
from smarter_dev.bot.proactive.environment import (  # noqa: E402
    ChannelEnvironment,
    InstructionStore,
)
from smarter_dev.bot.proactive.models import (  # noqa: E402
    build_twopass_model,
    ensure_openrouter_key_alias,
    resolve_agent_model_id,
)
from smarter_dev.bot.proactive.notifications import NotificationQueue  # noqa: E402
from smarter_dev.bot.proactive.parity import ProactiveDeps  # noqa: E402
from smarter_dev.bot.proactive.types import (  # noqa: E402
    ActivationContext,
    FixtureMessage,
    injected_response_message,
)
from smarter_dev.bot.proactive.watcher import SkimRunner, WatcherRunner  # noqa: E402
from smarter_dev.bot.proactive.windows import two_pass_windows  # noqa: E402

eval_prices.install()

DATA_DIR = Path(__file__).resolve().parent / "data"
RUNS_DIR = DATA_DIR / "runs"
DEFAULT_AGENT_MODEL = "gemini-3.7-flash"
DEFAULT_WATCHER_MODEL = "z-ai/glm-5.3-flash"


@dataclass
class ChannelReplay:
    """One channel's fixture, producer state and evolving timeline."""

    channel_id: str
    channel_name: str
    fixture_path: Path
    messages: list[FixtureMessage]
    instruction_store: InstructionStore
    producer: WatcherProducer
    cursor: int = 0
    timeline: list[FixtureMessage] = field(default_factory=list)
    message_ids: set[str] = field(default_factory=set)


def _load_fixture(fixture_path: Path) -> tuple[list[FixtureMessage], dict]:
    messages = [
        FixtureMessage.from_record(json.loads(line))
        for line in fixture_path.read_text(encoding="utf-8").splitlines()
    ]
    meta = json.loads(
        (fixture_path.parent / f"{fixture_path.stem}.meta.json").read_text(
            encoding="utf-8"
        )
    )
    return messages, meta


def _bot_display_name(channels: list[ChannelReplay], bot_user_id: str) -> str:
    for channel in channels:
        for message in channel.messages:
            if message.author_id == bot_user_id:
                return message.author_display
    return "the proactive bot"


def _fixture_ref(fixture_path: Path) -> str:
    try:
        return str(fixture_path.resolve().relative_to(DATA_DIR.resolve()))
    except ValueError:
        return fixture_path.name


async def run_guildwide(args: argparse.Namespace) -> None:
    ensure_openrouter_key_alias()
    agent_model_id = resolve_agent_model_id(args.model)
    watcher_model_id = args.watcher_model
    watcher = WatcherRunner(build_twopass_model(watcher_model_id))
    skim = SkimRunner(build_twopass_model(watcher_model_id))
    guild_queue = NotificationQueue()

    metas = []
    channels: list[ChannelReplay] = []
    for fixture_path in args.fixtures:
        messages, meta = _load_fixture(fixture_path)
        metas.append(meta)
        store = InstructionStore(seed=OPERATING_POLICY_BRIEF)
        channels.append(
            ChannelReplay(
                channel_id=str(meta["channel_id"]),
                channel_name=meta["channel_name"],
                fixture_path=fixture_path,
                messages=messages,
                instruction_store=store,
                producer=WatcherProducer(
                    watcher=watcher,
                    instruction_store=store,
                    watcher_model_id=watcher_model_id,
                    notification_queue=guild_queue,
                ),
                message_ids={m.id for m in messages},
            )
        )
    bot_user_id = metas[0]["bot_user_id"]
    guild_name = metas[0]["guild_name"]
    if any(m["bot_user_id"] != bot_user_id for m in metas):
        raise SystemExit("fixtures disagree on bot_user_id")

    bot_display_name = _bot_display_name(channels, bot_user_id)
    for channel in channels:
        channel.producer.bot_display_name = bot_display_name
    enabled_channels = {c.channel_id: c.channel_name for c in channels}
    instruction_stores = {c.channel_id: c.instruction_store for c in channels}
    by_channel_id = {c.channel_id: c for c in channels}

    async def compaction_summarize(text: str) -> str:
        summary, usage = await skim.skim(text)
        print(f"history compaction: {usage}", file=sys.stderr, flush=True)
        return summary

    agent_runner = KimiAgentRunner(
        agent=build_kimi_agent(
            build_twopass_model(agent_model_id),
            system_prompt=build_guild_agent_system_prompt(
                bot_display_name=bot_display_name,
                bot_user_id=bot_user_id,
                guild_name=guild_name,
            ),
            extra_tools=replay_parity_tools(),
            deps_type=ProactiveDeps,
        ),
        summarize=compaction_summarize,
    )

    def replay_deps_factory(**kwargs):
        return ProactiveDeps(
            bot=None,
            channel_id=0,
            guild_id=0,
            channel_name=guild_name,
            **kwargs,
        )

    activation_cost = model_cost_calculator(
        f"{agent_model_id}+{watcher_model_id}"
    )

    events = sorted(
        (
            (window_end, window_start, channel)
            for channel in channels
            for window_start, window_end in two_pass_windows(
                [m.timestamp for m in channel.messages]
            )
        ),
        key=lambda event: event[0],
    )

    started_at = datetime.now(tz=events[0][0].tzinfo)
    activations: list[dict] = []
    per_channel_activations: dict[str, list[dict]] = {
        c.channel_id: [] for c in channels
    }
    targeting = {
        "responses": 0,
        "reply_targets_checked": 0,
        "reply_target_wrong_channel": 0,
        "response_channel_not_enabled": 0,
        "cross_channel_responses": 0,
    }

    for index, (window_end, window_start, channel) in enumerate(events):
        new_messages: list[FixtureMessage] = []
        while (
            channel.cursor < len(channel.messages)
            and channel.messages[channel.cursor].timestamp < window_end
        ):
            new_messages.append(channel.messages[channel.cursor])
            channel.cursor += 1

        base_record = {
            "index": index,
            "channel_id": channel.channel_id,
            "channel_name": channel.channel_name,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
        }
        if not new_messages:
            record = {**base_record, "skipped": True, "new_message_count": 0}
            activations.append(record)
            per_channel_activations[channel.channel_id].append(record)
            continue

        history = channel.timeline[-args.history_size :]
        context = ActivationContext(
            channel_name=channel.channel_name,
            channel_id=channel.channel_id,
            guild_name=guild_name,
            bot_user_id=bot_user_id,
            activated_at=window_end,
            history=history,
            new_messages=new_messages,
        )
        watcher_usage = await channel.producer.produce(context)
        channel.timeline.extend(new_messages)

        usage_by_model = {
            model: dict(usage) for model, usage in watcher_usage.items()
        }
        responses_record: list[dict] = []
        reactions_record: list[dict] = []
        details: dict = dict(channel.producer.details)

        if channel.producer.wake_produced:
            consumer = AgentConsumer(
                agent_runner=agent_runner,
                skim=skim,
                agent_model_id=agent_model_id,
                notification_queue=guild_queue,
                watcher_model_id=watcher_model_id,
                deps_factory=replay_deps_factory,
                instruction_stores=instruction_stores,
                enabled_channels=enabled_channels,
                channel_envs={
                    c.channel_id: ChannelEnvironment(
                        visible=c.timeline[-args.history_size :],
                        bot_user_id=bot_user_id,
                    )
                    for c in channels
                },
            )
            result = await consumer.consume(context)
            details.update(result.details or {})
            for model, usage in (result.usage_by_model or {}).items():
                merged = usage_by_model.setdefault(
                    model,
                    {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cache_read_tokens": 0,
                    },
                )
                for key, value in usage.items():
                    merged[key] = merged.get(key, 0) + value

            for response_index, response in enumerate(result.responses):
                response_channel_id = response.channel_id or channel.channel_id
                target = by_channel_id.get(response_channel_id)
                targeting["responses"] += 1
                if target is None:
                    targeting["response_channel_not_enabled"] += 1
                    print(
                        f"DROPPED response to non-enabled channel "
                        f"{response_channel_id!r}: {response.content[:80]}",
                        file=sys.stderr,
                        flush=True,
                    )
                    continue
                if target.channel_id != channel.channel_id:
                    targeting["cross_channel_responses"] += 1
                if response.reply_to_id:
                    targeting["reply_targets_checked"] += 1
                    known_ids = target.message_ids | {
                        m.id for m in target.timeline
                    }
                    if response.reply_to_id not in known_ids:
                        targeting["reply_target_wrong_channel"] += 1
                target.timeline.append(
                    injected_response_message(
                        response,
                        bot_user_id=bot_user_id,
                        activated_at=window_end,
                        activation_index=index,
                        response_index=response_index,
                    )
                )
                responses_record.append(
                    {
                        "reply_to_id": response.reply_to_id,
                        "content": response.content,
                        "channel_id": target.channel_id,
                        "channel_name": target.channel_name,
                    }
                )
            reactions_record = [
                {"message_id": r.message_id, "emoji": r.emoji}
                for r in result.reactions
            ]

        totals_for_record = {
            key: sum(usage.get(key, 0) for usage in usage_by_model.values())
            for key in ("input_tokens", "output_tokens", "cache_read_tokens")
        }
        record = {
            **base_record,
            "skipped": False,
            "new_message_count": len(new_messages),
            "history_count": len(history),
            "responses": responses_record,
            "input_tokens": totals_for_record["input_tokens"],
            "output_tokens": totals_for_record["output_tokens"],
            "cache_read_tokens": totals_for_record["cache_read_tokens"],
            "usage_by_model": usage_by_model,
            "details": details,
        }
        record["cost_usd"] = float(
            activation_cost(_CostView(usage_by_model))
        )
        if reactions_record:
            record["reactions"] = reactions_record
        activations.append(record)

        per_channel_activations[channel.channel_id].append(
            {**record, "responses": [
                r for r in responses_record
                if r["channel_id"] == channel.channel_id
            ]}
        )
        for response in responses_record:
            if response["channel_id"] == channel.channel_id:
                continue
            # A cross-channel response is scored against ITS channel: give
            # that channel a synthetic activation at the wake time so the
            # judge pulls the right transcript excerpt.
            per_channel_activations[response["channel_id"]].append(
                {
                    "index": index,
                    "channel_id": response["channel_id"],
                    "channel_name": response["channel_name"],
                    "window_start": window_start.isoformat(),
                    "window_end": window_end.isoformat(),
                    "skipped": False,
                    "new_message_count": 0,
                    "history_count": 0,
                    "responses": [response],
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                    "cost_usd": 0.0,
                    "cross_channel_from": channel.channel_id,
                }
            )

        wake = details.get("watcher", {}).get("wake")
        print(
            f"event {index + 1}/{len(events)} [#{channel.channel_name}]: "
            f"{len(new_messages)} new, wake={wake}, "
            f"{len(responses_record)} responses",
            file=sys.stderr,
            flush=True,
        )

    model_id = f"{agent_model_id}+{watcher_model_id}"
    non_empty = [a for a in activations if not a["skipped"]]
    totals = {
        "activations": len(activations),
        "activations_with_messages": len(non_empty),
        "activations_with_responses": sum(
            1 for a in non_empty if a.get("responses")
        ),
        "responses": sum(len(a.get("responses", ())) for a in non_empty),
        "input_tokens": sum(a["input_tokens"] for a in non_empty),
        "output_tokens": sum(a["output_tokens"] for a in non_empty),
        "cache_read_tokens": sum(a["cache_read_tokens"] for a in non_empty),
        "cost_usd": sum(a["cost_usd"] for a in non_empty),
    }
    all_timestamps = sorted(
        m.timestamp for c in channels for m in c.messages
    )
    combined = {
        "fixture": [_fixture_ref(c.fixture_path) for c in channels],
        "adapter": "guildwide",
        "model_id": model_id,
        "cadence_seconds": 0,
        "history_size": args.history_size,
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(tz=started_at.tzinfo).isoformat(),
        "enabled_channels": enabled_channels,
        "activations": activations,
        "totals": totals,
        "targeting": targeting,
        "cost_summary": build_cost_summary(
            activations,
            totals,
            message_timestamps=all_timestamps,
            cadence_seconds=0,
        ),
    }
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    run_name = args.run_name
    combined_path = RUNS_DIR / f"{run_name}.combined.json"
    combined_path.write_text(
        json.dumps(combined, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    channel_run_paths = []
    for channel in channels:
        channel_activations = per_channel_activations[channel.channel_id]
        channel_non_empty = [
            a for a in channel_activations if not a["skipped"]
        ]
        channel_totals = {
            "activations": len(channel_activations),
            "activations_with_messages": len(channel_non_empty),
            "activations_with_responses": sum(
                1 for a in channel_non_empty if a.get("responses")
            ),
            "responses": sum(
                len(a.get("responses", ())) for a in channel_non_empty
            ),
            "input_tokens": sum(
                a["input_tokens"] for a in channel_non_empty
            ),
            "output_tokens": sum(
                a["output_tokens"] for a in channel_non_empty
            ),
            "cache_read_tokens": sum(
                a["cache_read_tokens"] for a in channel_non_empty
            ),
            "cost_usd": sum(a["cost_usd"] for a in channel_non_empty),
        }
        channel_record = {
            "fixture": _fixture_ref(channel.fixture_path),
            "adapter": "guildwide",
            "model_id": model_id,
            "cadence_seconds": 0,
            "history_size": args.history_size,
            "started_at": combined["started_at"],
            "finished_at": combined["finished_at"],
            "activations": channel_activations,
            "totals": channel_totals,
            "cost_summary": build_cost_summary(
                channel_activations,
                channel_totals,
                message_timestamps=[
                    m.timestamp for m in channel.messages
                ],
                cadence_seconds=0,
            ),
        }
        channel_path = (
            RUNS_DIR / f"{run_name}.{channel.fixture_path.stem}.json"
        )
        channel_path.write_text(
            json.dumps(channel_record, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        channel_run_paths.append(channel_path)

    print(f"\nWrote combined run record to {combined_path}")
    for path in channel_run_paths:
        print(f"Wrote channel run record to {path}")
    print(
        f"\n{totals['activations']} producer events "
        f"({totals['activations_with_messages']} with messages, "
        f"{totals['activations_with_responses']} with responses, "
        f"{totals['responses']} responses)"
    )
    print(f"Targeting: {json.dumps(targeting)}")
    print(f"Total cost: ${totals['cost_usd']:.4f}")


class _CostView:
    """Adapts a usage_by_model dict to model_cost_calculator's input."""

    def __init__(self, usage_by_model: dict):
        self.usage_by_model = usage_by_model
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_guildwide",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("fixtures", type=Path, nargs="+")
    parser.add_argument("--model", default=DEFAULT_AGENT_MODEL)
    parser.add_argument("--watcher-model", default=DEFAULT_WATCHER_MODEL)
    parser.add_argument("--history-size", type=int, default=60)
    parser.add_argument("--run-name", default="guildwide")
    return parser


def main() -> None:
    asyncio.run(run_guildwide(build_parser().parse_args()))


if __name__ == "__main__":
    main()
