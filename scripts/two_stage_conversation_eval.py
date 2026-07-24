#!/usr/bin/env python
"""Cost comparison: two-stage (worker+writer) vs single-stage chat bot.

Plays one sustained multi-turn conversation (a YAML file with a `turns` list)
through the real chat pipeline TWICE and contrasts the token cost:

  1. TWO-STAGE — small WORKER (default Gemini 3.5 Flash Lite) runs the agentic
     turn and emits a brief; large WRITER (default GPT-5.6 Terra) writes the
     reply. The worker carries conversation history across turns (exactly like
     the engine, which persists ``result.all_messages()``); the writer only
     ever sees a fresh, small brief.
  2. SINGLE-STAGE — the ordinary chat bot on the SAME large model
     (default GPT-5.6 Terra) does the whole turn, carrying full history itself.

Each turn is replayed with growing history, so the report shows how the two
approaches' token curves — and cost — diverge over a long conversation.

The report gives per-turn and total in / out / cache-read / cache-write tokens
for the little and large model, the aggregated cost of each, and the final
contrast (two-stage total vs single-stage total, savings and ratio).

Usage:
    uv run python scripts/two_stage_conversation_eval.py
    uv run python scripts/two_stage_conversation_eval.py path/to/conversation.yaml --out report.md
    uv run python scripts/two_stage_conversation_eval.py --worker gemini-3-5-flash-lite --large gpt-5-6-terra

Requires provider API keys in the environment (loaded from .env): GEMINI_API_KEY
/ GOOGLE_API_KEY for Gemini, OPENAI_API_KEY for GPT.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from genai_prices import Usage, calc_price
from pydantic_ai.messages import ModelRequest, UserPromptPart

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(SCRIPTS_DIR))
load_dotenv(REPO_ROOT / ".env")

import eval_prices  # noqa: E402  — registers prices for models newer than the snapshot

from smarter_dev.bot.agents.chat_agent import get_chat_agent, get_worker_agent  # noqa: E402
from smarter_dev.bot.agents.chat_input_format import build_agent_call  # noqa: E402
from smarter_dev.bot.agents.chat_models import (  # noqa: E402
    Author,
    BriefingDecision,
    ChannelInfo,
    FollowupAgentInput,
    InitialAgentInput,
    Me,
    Message,
    TurnDecision,
)
from smarter_dev.bot.agents.chat_tools import ChatDeps  # noqa: E402
from smarter_dev.bot.agents.writer_agent import build_writer_prompt, get_writer_agent  # noqa: E402
from smarter_dev.shared.model_catalog import CatalogModel, ModelProvider, get_model  # noqa: E402

DEFAULT_CONVERSATION = SCRIPTS_DIR / "two_stage_conversation.yaml"

_PROVIDER_IDS = {
    ModelProvider.GOOGLE: "google",
    ModelProvider.OPENAI: "openai",
    ModelProvider.ANTHROPIC: "anthropic",
    ModelProvider.OPENROUTER: "openrouter",
}


# --------------------------------------------------------------------------- #
# Conversation parsing
# --------------------------------------------------------------------------- #


@dataclass
class Conversation:
    name: str
    me: Me
    channel: ChannelInfo
    authors: list[Author]
    turns: list[Message]  # one user message per turn, in order


def parse_conversation(path: Path) -> Conversation:
    data = yaml.safe_load(path.read_text()) or {}

    me_data = data.get("me") or {}
    me = Me(user_id=str(me_data.get("user_id", "bot")), username=me_data.get("username", "smarterbot"))

    ch_data = data.get("channel") or {}
    channel = ChannelInfo(
        channel_id=str(ch_data.get("id", "100")),
        name=ch_data.get("name", "dev-help"),
        description=ch_data.get("description"),
    )

    authors = [
        Author(
            user_id=str(a["user_id"]),
            username=a["username"],
            nickname=a.get("nickname"),
            role_names=list(a.get("roles") or []),
        )
        for a in (data.get("authors") or [])
    ]
    if not authors:
        raise SystemExit(f"{path.name}: at least one author is required.")
    speaker_id = authors[0].user_id

    raw_turns = data.get("turns") or []
    if not raw_turns:
        raise SystemExit(f"{path.name}: `turns` is empty.")

    base_time = datetime.now(UTC) - timedelta(minutes=len(raw_turns) * 2)
    turns: list[Message] = []
    for i, entry in enumerate(raw_turns):
        # A turn is either a bare string or a dict {from, body, mentions_bot}.
        if isinstance(entry, str):
            body, from_id, mentions = entry, speaker_id, True
        else:
            body = entry["body"]
            from_id = str(entry.get("from", speaker_id))
            mentions = bool(entry.get("mentions_bot", True))
        turns.append(
            Message(
                message_id=str(100 + i),
                author_id=from_id,
                body=body,
                sent_at=base_time + timedelta(minutes=i * 2),
                mentions_bot=mentions,
            )
        )

    return Conversation(
        name=str(data.get("name") or path.stem),
        me=me,
        channel=channel,
        authors=authors,
        turns=turns,
    )


# --------------------------------------------------------------------------- #
# Bot stub + token accounting
# --------------------------------------------------------------------------- #


class _StubRest:
    async def create_message(self, channel_id: int, content: str, **kwargs: Any) -> None:
        print(f"[stub bot] create_message({channel_id}): {content!r}", file=sys.stderr)

    async def add_reaction(self, channel_id: int, message_id: int, emoji: str) -> None:
        pass

    async def fetch_guild_emojis(self, guild_id: int) -> list[Any]:
        return []


class _StubBot:
    def __init__(self) -> None:
        self.rest = _StubRest()


@dataclass
class TokenUse:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @classmethod
    def from_usage(cls, usage: Any) -> TokenUse:
        return cls(
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            cache_read_tokens=int(getattr(usage, "cache_read_tokens", 0) or 0),
            cache_write_tokens=int(getattr(usage, "cache_write_tokens", 0) or 0),
        )

    def __add__(self, other: TokenUse) -> TokenUse:
        return TokenUse(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
        )


def total_tokens(tokens: list[TokenUse]) -> TokenUse:
    running = TokenUse()
    for t in tokens:
        running = running + t
    return running


def stage_cost(tokens: TokenUse, model: CatalogModel) -> Decimal:
    """List-price cost of ``tokens`` on ``model`` via genai-prices.

    genai-prices treats ``input_tokens`` as the TOTAL prompt (cache-read and
    cache-write are subsets billed at their own rates), matching how pydantic-ai
    reports usage — so the fields pass straight through.
    """
    provider_id = _PROVIDER_IDS.get(model.provider, model.provider.name.lower())
    priced = calc_price(
        Usage(
            input_tokens=tokens.input_tokens,
            output_tokens=tokens.output_tokens,
            cache_read_tokens=tokens.cache_read_tokens,
            cache_write_tokens=tokens.cache_write_tokens,
        ),
        model_ref=model.model_id,
        provider_id=provider_id,
    )
    return priced.total_price


# --------------------------------------------------------------------------- #
# Turn input building (mirrors the engine: turn 0 = initial, rest = followup)
# --------------------------------------------------------------------------- #


def build_turn_call(
    turn_index: int,
    turn: Message,
    prior_history: list[Any],
    conversation: Conversation,
    model_id: str,
) -> tuple[str, list[Any]]:
    if turn_index == 0:
        agent_input: InitialAgentInput | FollowupAgentInput = InitialAgentInput(
            me=conversation.me,
            channel_history=[],
            activation_message=turn,
            authors=conversation.authors,
            channel=conversation.channel,
            now_utc=datetime.now(UTC),
        )
    else:
        agent_input = FollowupAgentInput(
            me=conversation.me,
            new_messages=[turn],
            authors=conversation.authors,
            channel=conversation.channel,
            now_utc=datetime.now(UTC),
        )
    return build_agent_call(agent_input, prior_history=prior_history, model_name=model_id)


# --------------------------------------------------------------------------- #
# Playthroughs
# --------------------------------------------------------------------------- #


@dataclass
class TurnRecord:
    worker: TokenUse = field(default_factory=TokenUse)  # or the single large model
    writer: TokenUse | None = None
    silent: bool = False
    responded: bool = False
    error: str | None = None


@dataclass
class Playthrough:
    label: str
    per_turn: list[TurnRecord] = field(default_factory=list)


async def play_two_stage(conversation: Conversation, worker: CatalogModel, writer: CatalogModel) -> Playthrough:
    play = Playthrough(label="two-stage")
    history: list[Any] = []
    for i, turn in enumerate(conversation.turns):
        user_prompt, message_history = build_turn_call(i, turn, history, conversation, worker.model_id)
        deps = ChatDeps(bot=_StubBot(), channel_id=100, guild_id=0)
        try:
            worker_agent = get_worker_agent(worker.model_id)
            worker_run = await worker_agent.run(
                user_prompt=user_prompt, message_history=message_history, deps=deps
            )
        except Exception as exc:  # noqa: BLE001 — record turn error, keep the run going
            play.per_turn.append(TurnRecord(error=f"worker: {type(exc).__name__}: {exc}"))
            print(f"  [two-stage] turn {i + 1} WORKER ERROR: {exc}", file=sys.stderr)
            continue
        record = TurnRecord(worker=TokenUse.from_usage(worker_run.usage()))
        briefing: BriefingDecision = worker_run.output
        if briefing.brief is not None:
            try:
                writer_agent = get_writer_agent(writer.model_id)
                writer_run = await writer_agent.run(build_writer_prompt(briefing.brief))
                record.writer = TokenUse.from_usage(writer_run.usage())
                record.responded = True
            except Exception as exc:  # noqa: BLE001 — keep the worker tokens, note the writer failure
                record.error = f"writer: {type(exc).__name__}: {exc}"
                print(f"  [two-stage] turn {i + 1} WRITER ERROR: {exc}", file=sys.stderr)
        else:
            record.silent = True
        play.per_turn.append(record)
        # Faithful to the engine: the WORKER's history is what persists between
        # turns (chat_engine.py persists result.all_messages()); the writer is
        # stateless and never re-enters the worker's context.
        history = list(worker_run.all_messages())
        print(f"  [two-stage] turn {i + 1}/{len(conversation.turns)} done", file=sys.stderr)
    return play


async def play_single_stage(conversation: Conversation, large: CatalogModel) -> Playthrough:
    play = Playthrough(label="single-stage")
    history: list[Any] = []
    for i, turn in enumerate(conversation.turns):
        user_prompt, message_history = build_turn_call(i, turn, history, conversation, large.model_id)
        deps = ChatDeps(bot=_StubBot(), channel_id=100, guild_id=0)
        try:
            agent = get_chat_agent(large.model_id)
            run = await agent.run(user_prompt=user_prompt, message_history=message_history, deps=deps)
        except Exception as exc:  # noqa: BLE001 — record turn error, keep the run going
            play.per_turn.append(TurnRecord(error=f"{type(exc).__name__}: {exc}"))
            print(f"  [single-stage] turn {i + 1} ERROR: {exc}", file=sys.stderr)
            continue
        decision: TurnDecision = run.output
        record = TurnRecord(
            worker=TokenUse.from_usage(run.usage()),
            responded=decision.response is not None,
            silent=decision.response is None,
        )
        play.per_turn.append(record)
        history = list(run.all_messages())
        print(f"  [single-stage] turn {i + 1}/{len(conversation.turns)} done", file=sys.stderr)
    return play


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


def _row(cells: list[Any]) -> str:
    return "| " + " | ".join(str(c) for c in cells) + " |"


def _money(value: Decimal) -> str:
    return f"${value:.5f}"


def _token_table(header: str, records: list[TurnRecord], pick) -> list[str]:
    lines = [f"**{header}**", "", _row(["Turn", "In", "Out", "Cache read", "Cache write"]),
             _row(["---", "---:", "---:", "---:", "---:"])]
    for i, rec in enumerate(records):
        tok = pick(rec)
        if tok is None:
            lines.append(_row([i + 1, "—", "—", "—", "—"]))
        else:
            lines.append(_row([i + 1, tok.input_tokens, tok.output_tokens, tok.cache_read_tokens, tok.cache_write_tokens]))
    return lines


def build_report(
    conversation: Conversation,
    two_stage: Playthrough,
    single_stage: Playthrough,
    worker: CatalogModel,
    writer: CatalogModel,
    large: CatalogModel,
) -> str:
    worker_total = total_tokens([r.worker for r in two_stage.per_turn])
    writer_total = total_tokens([r.writer for r in two_stage.per_turn if r.writer is not None])
    single_total = total_tokens([r.worker for r in single_stage.per_turn])

    worker_cost = stage_cost(worker_total, worker)
    writer_cost = stage_cost(writer_total, writer)
    two_stage_cost = worker_cost + writer_cost
    single_cost = stage_cost(single_total, large)

    savings = single_cost - two_stage_cost
    ratio = (single_cost / two_stage_cost) if two_stage_cost > 0 else Decimal(0)
    n_turns = len(conversation.turns)

    lines: list[str] = [
        f"# Two-stage vs single-stage — cost over a {n_turns}-turn conversation",
        "",
        f"- **Conversation:** `{conversation.name}` ({n_turns} turns)",
        f"- **Two-stage:** worker **{worker.label}** (`{worker.model_id}`) + writer **{writer.label}** (`{writer.model_id}`)",
        f"- **Single-stage:** **{large.label}** (`{large.model_id}`) as an ordinary chat bot",
        f"- **Generated:** {datetime.now(UTC).isoformat(timespec='seconds')}",
        "",
        "## Bottom line",
        "",
        _row(["Approach", "In", "Out", "Cache read", "Cache write", "Cost"]),
        _row(["---", "---:", "---:", "---:", "---:", "---:"]),
        _row([f"Worker · {worker.label}", worker_total.input_tokens, worker_total.output_tokens,
              worker_total.cache_read_tokens, worker_total.cache_write_tokens, _money(worker_cost)]),
        _row([f"Writer · {writer.label}", writer_total.input_tokens, writer_total.output_tokens,
              writer_total.cache_read_tokens, writer_total.cache_write_tokens, _money(writer_cost)]),
        _row(["**Two-stage total**", "", "", "", "", f"**{_money(two_stage_cost)}**"]),
        _row([f"Single-stage · {large.label}", single_total.input_tokens, single_total.output_tokens,
              single_total.cache_read_tokens, single_total.cache_write_tokens, f"**{_money(single_cost)}**"]),
        "",
    ]

    if two_stage_cost > 0:
        cheaper = "cheaper" if savings > 0 else "more expensive"
        lines += [
            f"**Two-stage costs {_money(two_stage_cost)} vs single-stage {_money(single_cost)}** — "
            f"a difference of {_money(abs(savings))} ({ratio:.2f}× {cheaper}) over {n_turns} turns.",
            "",
            f"Extrapolated to 1,000 such conversations: two-stage **{_money(two_stage_cost * 1000)}** "
            f"vs single-stage **{_money(single_cost * 1000)}** (saves {_money(abs(savings) * 1000)}).",
            "",
        ]

    # Per-turn detail
    lines += ["## Two-stage — per turn", ""]
    lines += _token_table(f"Worker ({worker.label})", two_stage.per_turn, lambda r: r.worker)
    lines += [""]
    lines += _token_table(f"Writer ({writer.label})", two_stage.per_turn, lambda r: r.writer)
    lines += [""]

    lines += ["## Single-stage — per turn", ""]
    lines += _token_table(f"{large.label}", single_stage.per_turn, lambda r: r.worker)
    lines += [""]

    # Conversation transcript
    lines += ["## Conversation", ""]
    for i, turn in enumerate(conversation.turns):
        lines.append(f"{i + 1}. {turn.body}")
    lines += [""]

    silent_two = [i + 1 for i, r in enumerate(two_stage.per_turn) if r.silent]
    silent_single = [i + 1 for i, r in enumerate(single_stage.per_turn) if r.silent]
    errors_two = [(i + 1, r.error) for i, r in enumerate(two_stage.per_turn) if r.error]
    errors_single = [(i + 1, r.error) for i, r in enumerate(single_stage.per_turn) if r.error]
    if silent_two or silent_single or errors_two or errors_single:
        lines += [
            "## Notes",
            "",
            f"- Two-stage stayed silent on turns: {silent_two or 'none'}.",
            f"- Single-stage stayed silent on turns: {silent_single or 'none'}.",
            "  (A silent turn spends worker/model tokens deciding but sends nothing; the writer never runs.)",
        ]
        for turn_no, err in errors_two:
            lines.append(f"- ⚠️ Two-stage turn {turn_no} error: `{err}`")
        for turn_no, err in errors_single:
            lines.append(f"- ⚠️ Single-stage turn {turn_no} error: `{err}`")
        lines += [""]

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


async def _main_async(args: argparse.Namespace) -> int:
    eval_prices.install()

    worker = get_model(args.worker)
    large = get_model(args.large)
    writer = large if args.writer is None else get_model(args.writer)
    for key, model in (("worker", worker), ("large", large), ("writer", writer)):
        if model is None:
            print(f"unknown {key} model key", file=sys.stderr)
            return 2

    path = Path(args.conversation)
    if not path.exists():
        print(f"conversation not found: {path}", file=sys.stderr)
        return 2
    conversation = parse_conversation(path)

    print(
        f"Two-stage: {worker.label} + {writer.label}   |   Single-stage: {large.label}   |   "
        f"{len(conversation.turns)} turns",
        file=sys.stderr,
    )

    print("Playing two-stage ...", file=sys.stderr)
    two_stage = await play_two_stage(conversation, worker, writer)
    print("Playing single-stage ...", file=sys.stderr)
    single_stage = await play_single_stage(conversation, large)

    report = build_report(conversation, two_stage, single_stage, worker, writer, large)
    Path(args.out).write_text(report, encoding="utf-8")
    print(f"\nReport written to {args.out}", file=sys.stderr)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("conversation", nargs="?", default=str(DEFAULT_CONVERSATION), help="conversation YAML path")
    parser.add_argument("--worker", default="gemini-3-5-flash-lite", help="two-stage worker model catalog key")
    parser.add_argument("--writer", default=None, help="two-stage writer model catalog key (default: same as --large)")
    parser.add_argument("--large", default="gpt-5-6-terra", help="large model catalog key (writer + single-stage bot)")
    parser.add_argument("--out", default="two_stage_conversation_report.md", help="output Markdown path")
    args = parser.parse_args()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
