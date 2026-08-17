"""Periodic-activation simulator core: pure dataclasses, windowing, run records.

The future proactive bot implements :class:`ProactiveBotAdapter`
(see adapters.py) and gets replayed over a fixture day by
:func:`run_simulation`. Stage 4 scores the run record this produces.
"""

from __future__ import annotations

import statistics
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from smarter_dev.bot.proactive.types import (  # noqa: F401
    ActivationContext,
    ActivationResult,
    ChannelMessage,
    FixtureMessage,
    ProactiveBotAdapter,
    ProposedReaction,
    ProposedResponse,
    injected_response_message,
)

CADENCE_SENSITIVITY_SECONDS = (120, 300, 900)


def floor_to_cadence(timestamp: datetime, cadence_seconds: int) -> datetime:
    floored_unix = int(timestamp.timestamp()) // cadence_seconds * cadence_seconds
    return datetime.fromtimestamp(floored_unix, tz=UTC)


def activation_windows(
    first_timestamp: datetime, last_timestamp: datetime, cadence_seconds: int
) -> list[tuple[datetime, datetime]]:
    """Half-open [start, end) windows covering first to last, on cadence
    boundaries."""
    windows = []
    start = floor_to_cadence(first_timestamp, cadence_seconds)
    step = timedelta(seconds=cadence_seconds)
    while start <= last_timestamp:
        windows.append((start, start + step))
        start += step
    return windows


async def run_simulation(
    *,
    messages: list[FixtureMessage],
    channel_name: str,
    guild_name: str,
    bot_user_id: str,
    adapter: ProactiveBotAdapter,
    adapter_name: str,
    model_id: str,
    cadence_seconds: int,
    history_size: int,
    activation_cost: Callable[[ActivationResult], float],
    fixture_name: str = "",
    windows: list[tuple[datetime, datetime]] | None = None,
) -> dict:
    """Replay the fixture day through the adapter; return the run record.

    Sequential by design: responses injected in window N are part of the
    timeline that later windows draw their history from. ``windows``
    overrides the fixed-cadence schedule with a precomputed one (e.g. the
    two-pass burst windows).
    """
    if windows is None:
        windows = activation_windows(
            messages[0].timestamp, messages[-1].timestamp, cadence_seconds
        )
    started_at = datetime.now(UTC)
    timeline: list[FixtureMessage] = []
    message_cursor = 0
    activations = []
    for index, (window_start, window_end) in enumerate(windows):
        new_messages = []
        while (
            message_cursor < len(messages)
            and messages[message_cursor].timestamp < window_end
        ):
            new_messages.append(messages[message_cursor])
            message_cursor += 1
        if not new_messages:
            activations.append(
                {
                    "index": index,
                    "window_start": window_start.isoformat(),
                    "window_end": window_end.isoformat(),
                    "skipped": True,
                    "new_message_count": 0,
                }
            )
            continue

        history = timeline[-history_size:]
        context = ActivationContext(
            channel_name=channel_name,
            guild_name=guild_name,
            bot_user_id=bot_user_id,
            activated_at=window_end,
            history=history,
            new_messages=new_messages,
        )
        result = await adapter.activate(context)
        cost_usd = float(activation_cost(result))
        activation_record = {
            "index": index,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "skipped": False,
            "new_message_count": len(new_messages),
            "history_count": len(history),
            "responses": [
                {"reply_to_id": r.reply_to_id, "content": r.content}
                for r in result.responses
            ],
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "cache_read_tokens": result.cache_read_tokens,
            "cost_usd": cost_usd,
        }
        if result.reactions:
            activation_record["reactions"] = [
                {"message_id": r.message_id, "emoji": r.emoji}
                for r in result.reactions
            ]
        if result.usage_by_model is not None:
            activation_record["usage_by_model"] = result.usage_by_model
        if result.details is not None:
            activation_record["details"] = result.details
        activations.append(activation_record)
        timeline.extend(new_messages)
        for response_index, response in enumerate(result.responses):
            timeline.append(
                injected_response_message(
                    response,
                    bot_user_id=bot_user_id,
                    activated_at=window_end,
                    activation_index=index,
                    response_index=response_index,
                )
            )
        print(
            f"activation {index + 1}/{len(windows)}: "
            f"{len(new_messages)} new, {len(result.responses)} responses, "
            f"${cost_usd:.5f}",
            file=sys.stderr,
            flush=True,
        )

    totals = _totals(activations)
    return {
        "fixture": fixture_name,
        "adapter": adapter_name,
        "model_id": model_id,
        "cadence_seconds": cadence_seconds,
        "history_size": history_size,
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "activations": activations,
        "totals": totals,
        "cost_summary": build_cost_summary(
            activations,
            totals,
            message_timestamps=[m.timestamp for m in messages],
            cadence_seconds=cadence_seconds,
        ),
    }


def _totals(activations: list[dict]) -> dict:
    non_empty = [a for a in activations if not a["skipped"]]
    return {
        "activations": len(activations),
        "activations_with_messages": len(non_empty),
        "activations_with_responses": sum(
            1 for a in non_empty if a["responses"]
        ),
        "responses": sum(len(a["responses"]) for a in non_empty),
        "input_tokens": sum(a["input_tokens"] for a in non_empty),
        "output_tokens": sum(a["output_tokens"] for a in non_empty),
        "cache_read_tokens": sum(a["cache_read_tokens"] for a in non_empty),
        "cost_usd": sum(a["cost_usd"] for a in non_empty),
    }


def build_cost_summary(
    activations: list[dict],
    totals: dict,
    *,
    message_timestamps: list[datetime],
    cadence_seconds: int,
) -> dict:
    non_empty_inputs = sorted(
        a["input_tokens"] for a in activations if not a["skipped"]
    )
    if non_empty_inputs:
        input_stats = {
            "mean": statistics.mean(non_empty_inputs),
            "median": statistics.median(non_empty_inputs),
            "p95": non_empty_inputs[
                min(len(non_empty_inputs) - 1, int(len(non_empty_inputs) * 0.95))
            ],
        }
    else:
        input_stats = {"mean": 0, "median": 0, "p95": 0}
    return {
        "total_cost_usd": totals["cost_usd"],
        "input_tokens": totals["input_tokens"],
        "output_tokens": totals["output_tokens"],
        "non_empty_activation_input_tokens": input_stats,
        "projected_cost_per_day_usd": totals["cost_usd"],
        "projected_cost_30_days_usd": totals["cost_usd"] * 30,
        "cadence_sensitivity": [
            _cadence_bucket_counts(message_timestamps, candidate)
            for candidate in CADENCE_SENSITIVITY_SECONDS
        ],
        "note": (
            "Token cost per activation is measured only at the "
            f"{cadence_seconds}s run cadence; other cadences show window "
            "counts from the same message timestamps."
        ),
    }


def _cadence_bucket_counts(
    message_timestamps: list[datetime], cadence_seconds: int
) -> dict:
    buckets = {
        int(ts.timestamp()) // cadence_seconds for ts in message_timestamps
    }
    first_bucket = min(
        int(ts.timestamp()) // cadence_seconds for ts in message_timestamps
    )
    last_bucket = max(
        int(ts.timestamp()) // cadence_seconds for ts in message_timestamps
    )
    return {
        "cadence_seconds": cadence_seconds,
        "total_windows": last_bucket - first_bucket + 1,
        "windows_with_messages": len(buckets),
    }


def format_cost_summary(summary: dict) -> str:
    stats = summary["non_empty_activation_input_tokens"]
    lines = [
        f"Total cost for the day: ${summary['total_cost_usd']:.4f} "
        f"({summary['input_tokens']:,} in / {summary['output_tokens']:,} out tokens)",
        f"Input tokens per non-empty activation: "
        f"mean {stats['mean']:,.0f}, median {stats['median']:,.0f}, "
        f"p95 {stats['p95']:,.0f}",
        f"Projected: ${summary['projected_cost_per_day_usd']:.4f}/day → "
        f"${summary['projected_cost_30_days_usd']:.2f}/30 days at this cadence",
        "Cadence sensitivity (same messages, re-bucketed):",
    ]
    for row in summary["cadence_sensitivity"]:
        lines.append(
            f"  every {row['cadence_seconds']:>4}s: "
            f"{row['windows_with_messages']:>4} non-empty windows "
            f"of {row['total_windows']}"
        )
    lines.append(f"Note: {summary['note']}")
    return "\n".join(lines)
