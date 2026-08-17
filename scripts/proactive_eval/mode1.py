"""Mode-1 scenario fixtures: multi-turn conversations WITH the bot.

Fixed-history replays can never show a human reacting to the bot, so mode 1
(actively participating) is tested with scripted synthetic scenarios
instead. A scenario's human turns are pre-written — including their
ground-truth labels — but bind to the live run at materialization time:
`reply_to: bot_last` resolves to the bot's actual injected message, and a
turn with `requires_bot_reply: true` is skipped when the bot stayed silent
(a "thanks!" never fires into a void). Scenario files contain no real
member content and are committed under scripts/proactive_eval/scenarios/.

The output artifacts (materialized fixture + meta + labels + run record)
use the exact schemas the rest of the pipeline consumes, so score_run
works unchanged.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.proactive_eval.labels import DIRECTED_AT_CATEGORIES  # noqa: E402
from scripts.proactive_eval.simulation import (  # noqa: E402
    ActivationContext,
    FixtureMessage,
    build_cost_summary,
    injected_response_message,
    _totals,
)

SCENARIO_BOT_USER_ID = "999000000000000001"
SCENARIO_BOT_DISPLAY = "smarter-bot"
SCENARIO_BASE_TIME = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
SCENARIO_CHANNEL = "💬general"
SCENARIO_GUILD = "Smarter Dev"
BOT_MENTION_PLACEHOLDER = "<@BOT>"
QUIET_SECONDS = 15
MAX_WAIT_SECONDS = 60
HISTORY_SIZE = 60


@dataclass(frozen=True)
class ScenarioTurn:
    key: str
    offset_seconds: int
    author_key: str
    content: str
    directed_at: str
    reason: str
    reply_to: str | None = None       # another turn's key, or "bot_last"
    mentions_bot: bool = False
    requires_bot_reply: bool = False
    target_key: str | None = None     # participant key for other_user labels


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    participants: dict[str, dict]     # key -> {"id": …, "display": …}
    turns: list[ScenarioTurn]


def parse_scenario(text: str) -> Scenario:
    raw = yaml.safe_load(text)
    participants = {p["key"]: {"id": p["id"], "display": p["display"]}
                    for p in raw["participants"]}
    turns = []
    turn_keys = set()
    for index, raw_turn in enumerate(raw["turns"]):
        key = raw_turn.get("key") or f"turn-{index:02d}"
        author_key = raw_turn["author"]
        if author_key not in participants:
            raise ValueError(
                f"turn {key}: unknown author {author_key!r} "
                f"(participants: {sorted(participants)})"
            )
        directed_at = raw_turn["directed_at"]
        if directed_at not in DIRECTED_AT_CATEGORIES:
            raise ValueError(
                f"turn {key}: invalid directed_at {directed_at!r} "
                f"(must be one of {DIRECTED_AT_CATEGORIES})"
            )
        reply_to = raw_turn.get("reply_to")
        if reply_to is not None and reply_to != "bot_last" and reply_to not in turn_keys:
            raise ValueError(
                f"turn {key}: reply_to {reply_to!r} is neither 'bot_last' "
                f"nor an earlier turn key"
            )
        target_key = raw_turn.get("target")
        if target_key is not None and target_key not in participants:
            raise ValueError(f"turn {key}: unknown target {target_key!r}")
        turns.append(
            ScenarioTurn(
                key=key,
                offset_seconds=int(raw_turn["offset"]),
                author_key=author_key,
                content=raw_turn["content"],
                directed_at=directed_at,
                reason=raw_turn.get("reason", ""),
                reply_to=reply_to,
                mentions_bot=bool(raw_turn.get("mentions_bot", False)),
                requires_bot_reply=bool(raw_turn.get("requires_bot_reply", False)),
                target_key=target_key,
            )
        )
        turn_keys.add(key)
    return Scenario(
        name=raw["name"],
        description=raw.get("description", ""),
        participants=participants,
        turns=turns,
    )


def load_scenario(path: Path) -> Scenario:
    return parse_scenario(path.read_text(encoding="utf-8"))


@dataclass
class Mode1Result:
    scenario: Scenario
    run_record: dict
    fixture_records: list[dict]
    labels_doc: dict
    meta: dict
    skipped_turn_keys: list[str]


def _turn_message(
    turn: ScenarioTurn, scenario: Scenario, reply_to_id: str | None
) -> FixtureMessage:
    participant = scenario.participants[turn.author_key]
    content = turn.content.replace(
        BOT_MENTION_PLACEHOLDER, f"<@{SCENARIO_BOT_USER_ID}>"
    )
    return FixtureMessage(
        id=turn.key,
        timestamp=SCENARIO_BASE_TIME + timedelta(seconds=turn.offset_seconds),
        author_id=participant["id"],
        author_name=participant["display"],
        author_display=participant["display"],
        is_bot=False,
        content=content,
        reply_to_id=reply_to_id,
        mention_user_ids=(SCENARIO_BOT_USER_ID,) if turn.mentions_bot else (),
        mention_everyone=False,
        attachment_count=0,
        sticker_count=0,
        message_type=19 if reply_to_id else 0,
    )


async def run_mode1(
    scenario: Scenario,
    adapter,
    *,
    adapter_name: str,
    model_id: str,
    activation_cost,
    quiet_seconds: int = QUIET_SECONDS,
    max_wait_seconds: int = MAX_WAIT_SECONDS,
) -> Mode1Result:
    """Materialize the scenario against the adapter, sequentially.

    Turns are grouped into bursts with the same quiet/cap timing the live
    watcher uses; each burst is one activation. The adapter's responses are
    injected into the timeline, so later turns can reply to them and the
    agent's history spans the whole conversation.
    """
    started_at = datetime.now(UTC)
    timeline: list[FixtureMessage] = []
    injected: list[FixtureMessage] = []
    labels: dict[str, dict] = {}
    skipped_turn_keys: list[str] = []
    activations: list[dict] = []
    burst: list[FixtureMessage] = []

    async def flush_burst(window_end: datetime) -> None:
        if not burst:
            return
        history = timeline[-HISTORY_SIZE:]
        context = ActivationContext(
            channel_name=SCENARIO_CHANNEL,
            guild_name=SCENARIO_GUILD,
            bot_user_id=SCENARIO_BOT_USER_ID,
            activated_at=window_end,
            history=history,
            new_messages=list(burst),
        )
        result = await adapter.activate(context)
        cost_usd = float(activation_cost(result))
        index = len(activations)
        activation_record = {
            "index": index,
            "window_start": burst[0].timestamp.isoformat(),
            "window_end": window_end.isoformat(),
            "skipped": False,
            "new_message_count": len(burst),
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
        timeline.extend(burst)
        for response_index, response in enumerate(result.responses):
            message = injected_response_message(
                response,
                bot_user_id=SCENARIO_BOT_USER_ID,
                activated_at=window_end,
                activation_index=index,
                response_index=response_index,
            )
            timeline.append(message)
            injected.append(message)
        burst.clear()
        print(
            f"{scenario.name}: activation {index + 1}, "
            f"{activation_record['new_message_count']} new, "
            f"{len(result.responses)} responses",
            file=sys.stderr,
            flush=True,
        )

    def burst_fire_time() -> datetime:
        return min(
            burst[-1].timestamp + timedelta(seconds=quiet_seconds),
            burst[0].timestamp + timedelta(seconds=max_wait_seconds),
        )

    turn_message_ids: dict[str, str] = {}
    for turn in scenario.turns:
        turn_time = SCENARIO_BASE_TIME + timedelta(seconds=turn.offset_seconds)
        if burst and turn_time >= burst_fire_time():
            await flush_burst(burst_fire_time())
        if turn.requires_bot_reply and not injected:
            skipped_turn_keys.append(turn.key)
            continue
        reply_to_id = None
        if turn.reply_to == "bot_last":
            if not injected:
                skipped_turn_keys.append(turn.key)
                continue
            reply_to_id = injected[-1].id
        elif turn.reply_to is not None:
            if turn.reply_to in skipped_turn_keys:
                skipped_turn_keys.append(turn.key)
                continue
            reply_to_id = turn_message_ids[turn.reply_to]
        message = _turn_message(turn, scenario, reply_to_id)
        turn_message_ids[turn.key] = message.id
        burst.append(message)
        target = (
            scenario.participants[turn.target_key]["id"]
            if turn.target_key
            else None
        )
        labels[message.id] = {
            "directed_at": turn.directed_at,
            "target_user_id": target,
            "ok_to_respond": turn.directed_at in ("anyone", "bot"),
            "reason": turn.reason,
        }
    if burst:
        await flush_burst(burst_fire_time())

    totals = _totals(activations)
    fixture_name = f"mode1/{scenario.name}.jsonl"
    run_record = {
        "fixture": fixture_name,
        "adapter": adapter_name,
        "model_id": model_id,
        "cadence_seconds": 0,
        "history_size": HISTORY_SIZE,
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "activations": activations,
        "totals": totals,
        "cost_summary": build_cost_summary(
            activations,
            totals,
            message_timestamps=[
                m.timestamp for m in timeline if not m.is_bot
            ],
            cadence_seconds=0,
        ),
    }
    fixture_records = [m.to_record() for m in timeline]
    return Mode1Result(
        scenario=scenario,
        run_record=run_record,
        fixture_records=fixture_records,
        labels_doc={
            "fixture": f"{scenario.name}.jsonl",
            "judge_model": "scenario-script",
            "labeled_at": datetime.now(UTC).isoformat(),
            "judge_reported_cost_usd": 0.0,
            "labels": labels,
        },
        meta={
            "guild_id": "scenario",
            "guild_name": SCENARIO_GUILD,
            "channel_id": "scenario",
            "channel_name": SCENARIO_CHANNEL,
            "date": SCENARIO_BASE_TIME.date().isoformat(),
            "scenario": scenario.name,
            "bot_user_id": SCENARIO_BOT_USER_ID,
            "message_count": len(fixture_records),
        },
        skipped_turn_keys=skipped_turn_keys,
    )


def write_artifacts(result: Mode1Result, *, data_dir: Path) -> Path:
    """Write fixture/meta/labels under data/mode1/ and the run record under
    data/runs/; returns the run record path (score_run's input)."""
    mode1_dir = data_dir / "mode1"
    mode1_dir.mkdir(parents=True, exist_ok=True)
    stem = result.scenario.name
    (mode1_dir / f"{stem}.jsonl").write_text(
        "".join(
            json.dumps(r, ensure_ascii=False) + "\n"
            for r in result.fixture_records
        ),
        encoding="utf-8",
    )
    (mode1_dir / f"{stem}.meta.json").write_text(
        json.dumps(result.meta, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (mode1_dir / f"{stem}.labels.json").write_text(
        json.dumps(result.labels_doc, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    runs_dir = data_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    model_slug = result.run_record["model_id"].replace("/", "-")
    run_path = (
        runs_dir
        / f"{stem}.{result.run_record['adapter']}.{model_slug}.mode1.json"
    )
    run_path.write_text(
        json.dumps(result.run_record, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return run_path
