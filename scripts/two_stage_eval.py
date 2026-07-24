#!/usr/bin/env python
"""Local eval harness for TWO-STAGE chat mode (worker -> writer).

Runs YAML scenario files through the real two-stage pipeline without touching
Discord:

    small WORKER (default Gemini 3.5 Flash Lite)  runs the agentic turn and
      emits a context brief (BriefingDecision)
    large WRITER (default GPT-5.6 Terra)          turns the brief's rendered
      prompt into the friendly Discord reply (WriterOutput)

It writes a Markdown report showing, per scenario: the conversation, the brief
prompt the small model produced (what actually gets piped to the large model),
the large model's generated message, and the in / out / cache tokens for BOTH
the little and the large model.

Usage:
    uv run python scripts/two_stage_eval.py                       # all built-in scenarios
    uv run python scripts/two_stage_eval.py path/to/scenario.yaml # specific file(s)
    uv run python scripts/two_stage_eval.py some_dir --out report.md
    uv run python scripts/two_stage_eval.py --worker gemini-3-5-flash-lite --writer gpt-5-6-terra

Scenario YAML schema (same shape as scripts/chat_eval.py, plus name/note):

    name: direct-question          # report label (default: file stem)
    note: one-line description      # optional, shown in the report
    kind: initial                   # or "followup" (default "initial")
    me: {user_id: "bot", username: "smarterbot"}
    channel: {id: "100", name: "general", description: "..."}   # description optional
    topic: null                     # optional durable memory
    notes: null                     # optional durable memory
    authors:
      - {user_id: "1", username: "alice", nickname: "Al", roles: ["dev"]}
    history:                        # messages BEFORE the trigger (oldest first)
      - {id: "10", from: "1", body: "hey", reply_to: "9", mentions_bot: false}
    trigger:                        # the message the bot is asked to react to
      {id: "12", from: "1", body: "@smarterbot any tips?", mentions_bot: true}

The worker/writer arguments take catalog KEYS (see smarter_dev/shared/model_catalog.py).
Requires provider API keys in the environment (loaded from .env if present):
GEMINI_API_KEY / GOOGLE_API_KEY for Gemini, OPENAI_API_KEY for GPT.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic_ai.messages import ModelRequest, UserPromptPart

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
load_dotenv(REPO_ROOT / ".env")

from smarter_dev.bot.agents.chat_agent import get_worker_agent  # noqa: E402
from smarter_dev.bot.agents.chat_input_format import (  # noqa: E402
    build_agent_call,
    render_message_xml,
)
from smarter_dev.bot.agents.chat_models import (  # noqa: E402
    Author,
    BriefingDecision,
    ChannelInfo,
    FollowupAgentInput,
    InitialAgentInput,
    Me,
    Message,
    MessageAttachment,
)
from smarter_dev.bot.agents.chat_tools import ChatDeps  # noqa: E402
from smarter_dev.bot.agents.writer_agent import (  # noqa: E402
    build_writer_prompt,
    get_writer_agent,
)
from smarter_dev.shared.model_catalog import get_model  # noqa: E402

DEFAULT_SCENARIO_DIR = Path(__file__).parent / "two_stage_eval_scenarios"


# --------------------------------------------------------------------------- #
# Scenario parsing (mirrors scripts/chat_eval.py, plus name/note)
# --------------------------------------------------------------------------- #


@dataclass
class ParsedScenario:
    name: str
    note: str | None
    me: Me
    channel: ChannelInfo
    authors: list[Author]
    history: list[Message]
    trigger: Message
    topic: str | None
    notes: str | None
    kind: str  # "initial" or "followup"


def _parse_attachments(raw: Any) -> list[MessageAttachment]:
    if not raw or not isinstance(raw, list):
        return []
    out: list[MessageAttachment] = []
    for item in raw:
        if isinstance(item, str):
            out.append(MessageAttachment(url=item))
        elif isinstance(item, dict):
            out.append(
                MessageAttachment(
                    url=item["url"],
                    media_type=item.get("media_type"),
                    filename=item.get("filename"),
                )
            )
    return out


def parse_scenario(path: Path) -> ParsedScenario:
    data = yaml.safe_load(path.read_text()) or {}

    me_data = data.get("me") or {}
    me = Me(
        user_id=str(me_data.get("user_id", "bot")),
        username=me_data.get("username", "smarterbot"),
    )

    ch_data = data.get("channel") or {}
    channel = ChannelInfo(
        channel_id=str(ch_data.get("id", "0")),
        name=ch_data.get("name", "test"),
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
    authors_by_id = {a.user_id: a for a in authors}

    base_time = datetime.now(UTC) - timedelta(minutes=10)

    def _build_message(entry: dict[str, Any], *, seq: int, so_far: dict[str, Message]) -> Message:
        reply_to_id = entry.get("reply_to")
        reply_to_id = str(reply_to_id) if reply_to_id is not None else None
        reply_to_author_id: str | None = None
        reply_to_is_self = False
        if reply_to_id and reply_to_id in so_far:
            target = so_far[reply_to_id]
            reply_to_author_id = target.author_id
            reply_to_is_self = target.author_id == me.user_id
        author_id = str(entry["from"])
        if author_id not in authors_by_id and author_id != me.user_id:
            raise SystemExit(
                f"{path.name}: message id={entry.get('id')} references author_id="
                f"{author_id!r} not listed under `authors` (and it isn't `me`)."
            )
        return Message(
            message_id=str(entry["id"]),
            author_id=author_id,
            reply_to_message_id=reply_to_id,
            reply_to_author_id=reply_to_author_id,
            reply_to_is_self=reply_to_is_self,
            body=entry.get("body", ""),
            reactions=list(entry.get("reactions") or []),
            attachments=_parse_attachments(entry.get("attachments")),
            sent_at=base_time + timedelta(seconds=seq * 30),
            mentions_bot=bool(entry.get("mentions_bot", False)),
        )

    so_far: dict[str, Message] = {}
    history: list[Message] = []
    for i, entry in enumerate(data.get("history") or []):
        msg = _build_message(entry, seq=i, so_far=so_far)
        history.append(msg)
        so_far[msg.message_id] = msg

    trigger_entry = data.get("trigger")
    if not trigger_entry:
        raise SystemExit(f"{path.name}: missing `trigger` block.")
    trigger = _build_message(trigger_entry, seq=len(history), so_far=so_far)

    kind = (data.get("kind") or "initial").lower()
    if kind not in ("initial", "followup"):
        raise SystemExit(f"{path.name}: kind must be 'initial' or 'followup', got {kind!r}")

    return ParsedScenario(
        name=str(data.get("name") or path.stem),
        note=data.get("note"),
        me=me,
        channel=channel,
        authors=authors,
        history=history,
        trigger=trigger,
        topic=data.get("topic"),
        notes=data.get("notes"),
        kind=kind,
    )


def build_call(scenario: ParsedScenario, worker_model_id: str, worker_reasoning: str | None) -> tuple[str, list[Any]]:
    """Build (user_prompt, message_history) honouring scenario.kind."""
    if scenario.kind == "initial":
        agent_input = InitialAgentInput(
            me=scenario.me,
            channel_history=scenario.history,
            activation_message=scenario.trigger,
            authors=scenario.authors,
            channel=scenario.channel,
            now_utc=datetime.now(UTC),
            topic=scenario.topic,
            notes=scenario.notes,
        )
        return build_agent_call(
            agent_input,
            prior_history=[],
            model_name=worker_model_id,
            reasoning_level=worker_reasoning,
        )

    # Followup: bake `history` into prior_history as the UserPromptPart entries
    # the initial turn would have produced, then pass ONLY the trigger as new.
    prior_history: list[Any] = [
        ModelRequest(
            parts=[
                UserPromptPart(
                    content=render_message_xml(msg, me=scenario.me, authors=scenario.authors)
                )
            ]
        )
        for msg in scenario.history
    ]
    agent_input = FollowupAgentInput(
        me=scenario.me,
        new_messages=[scenario.trigger],
        authors=scenario.authors,
        channel=scenario.channel,
        now_utc=datetime.now(UTC),
        topic=scenario.topic,
        notes=scenario.notes,
    )
    return build_agent_call(
        agent_input,
        prior_history=prior_history,
        model_name=worker_model_id,
        reasoning_level=worker_reasoning,
    )


# --------------------------------------------------------------------------- #
# Bot stub — tools that try to touch Discord become logged no-ops.
# --------------------------------------------------------------------------- #


class _StubRest:
    async def create_message(self, channel_id: int, content: str, **kwargs: Any) -> None:
        print(f"[stub bot] create_message({channel_id}): {content!r}", file=sys.stderr)

    async def add_reaction(self, channel_id: int, message_id: int, emoji: str) -> None:
        print(f"[stub bot] add_reaction({channel_id}, {message_id}, {emoji!r})", file=sys.stderr)

    async def fetch_guild_emojis(self, guild_id: int) -> list[Any]:
        return []


class _StubBot:
    def __init__(self) -> None:
        self.rest = _StubRest()


# --------------------------------------------------------------------------- #
# Token accounting + results
# --------------------------------------------------------------------------- #


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


@dataclass
class StageResult:
    tokens: TokenUse = field(default_factory=TokenUse)
    error: str | None = None


@dataclass
class ScenarioResult:
    name: str
    note: str | None
    transcript: list[tuple[str, str]]  # (speaker, body)
    trigger_line: str
    worker: StageResult
    writer: StageResult
    stayed_silent: bool = False
    brief_prompt: str | None = None  # what the small model outputs, piped to the large model
    writer_message: str | None = None
    writer_voice_summary: str | None = None


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #


def _speaker(author_id: str, scenario: ParsedScenario) -> str:
    if author_id == scenario.me.user_id:
        return scenario.me.username
    for author in scenario.authors:
        if author.user_id == author_id:
            return author.nickname or author.username
    return author_id


async def run_scenario(
    scenario: ParsedScenario,
    worker_model_id: str,
    writer_model_id: str,
    worker_reasoning: str | None,
) -> ScenarioResult:
    transcript = [(_speaker(m.author_id, scenario), m.body) for m in scenario.history]
    trigger_line = f"{_speaker(scenario.trigger.author_id, scenario)}: {scenario.trigger.body}"

    user_prompt, message_history = build_call(scenario, worker_model_id, worker_reasoning)

    result = ScenarioResult(
        name=scenario.name,
        note=scenario.note,
        transcript=transcript,
        trigger_line=trigger_line,
        worker=StageResult(),
        writer=StageResult(),
    )

    channel_id = int(scenario.channel.channel_id) if scenario.channel.channel_id.isdigit() else 0
    deps = ChatDeps(bot=_StubBot(), channel_id=channel_id, guild_id=0)

    # --- WORKER stage --------------------------------------------------------
    try:
        worker_agent = get_worker_agent(worker_model_id, worker_reasoning)
        worker_run = await worker_agent.run(
            user_prompt=user_prompt,
            message_history=message_history,
            deps=deps,
        )
    except Exception as exc:  # noqa: BLE001 — harness records and continues
        result.worker.error = f"{type(exc).__name__}: {exc}"
        return result

    result.worker.tokens = TokenUse.from_usage(worker_run.usage())
    briefing: BriefingDecision = worker_run.output

    if briefing.brief is None:
        result.stayed_silent = True
        return result

    result.brief_prompt = build_writer_prompt(briefing.brief)

    # --- WRITER stage --------------------------------------------------------
    try:
        writer_agent = get_writer_agent(writer_model_id)
        writer_run = await writer_agent.run(result.brief_prompt)
    except Exception as exc:  # noqa: BLE001 — harness records and continues
        result.writer.error = f"{type(exc).__name__}: {exc}"
        return result

    result.writer.tokens = TokenUse.from_usage(writer_run.usage())
    result.writer_message = writer_run.output.message
    result.writer_voice_summary = writer_run.output.voice_summary
    return result


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


def _token_row(label: str, tokens: TokenUse) -> str:
    return (
        f"| {label} | {tokens.input_tokens} | {tokens.output_tokens} "
        f"| {tokens.cache_read_tokens} | {tokens.cache_write_tokens} |"
    )


def _sum_tokens(stages: list[StageResult]) -> TokenUse:
    return TokenUse(
        input_tokens=sum(s.tokens.input_tokens for s in stages),
        output_tokens=sum(s.tokens.output_tokens for s in stages),
        cache_read_tokens=sum(s.tokens.cache_read_tokens for s in stages),
        cache_write_tokens=sum(s.tokens.cache_write_tokens for s in stages),
    )


def build_report(results: list[ScenarioResult], worker_label: str, writer_label: str) -> str:
    lines: list[str] = [
        "# Two-stage chat mode — eval report",
        "",
        f"- **Worker (small):** {worker_label}",
        f"- **Writer (large):** {writer_label}",
        f"- **Generated:** {datetime.now(UTC).isoformat(timespec='seconds')}",
        "",
        "## Token totals",
        "",
        "| Model | In | Out | Cache read | Cache write |",
        "| --- | ---: | ---: | ---: | ---: |",
        _token_row(f"Worker · {worker_label}", _sum_tokens([r.worker for r in results])),
        _token_row(f"Writer · {writer_label}", _sum_tokens([r.writer for r in results])),
        "",
    ]

    for result in results:
        lines.append(f"## {result.name}")
        lines.append("")
        if result.note:
            lines.append(f"_{result.note.strip()}_")
            lines.append("")

        lines.append("**Conversation**")
        lines.append("")
        for speaker, body in result.transcript:
            lines.append(f"> **{speaker}:** {body}")
        lines.append(f"> **➡ {result.trigger_line}**")
        lines.append("")

        if result.worker.error:
            lines.append(f"> ⚠️ **Worker error:** `{result.worker.error}`")
            lines.append("")
            continue

        if result.stayed_silent:
            lines.append("**Outcome:** worker stayed **silent** (brief is `None`) — the writer never runs.")
            lines.append("")
            lines.append("| Model | In | Out | Cache read | Cache write |")
            lines.append("| --- | ---: | ---: | ---: | ---: |")
            lines.append(_token_row("Worker", result.worker.tokens))
            lines.append("")
            continue

        lines.append("**Brief prompt the worker produced** (piped into the writer)")
        lines.append("")
        lines.append("```text")
        lines.append(result.brief_prompt or "")
        lines.append("```")
        lines.append("")

        if result.writer.error:
            lines.append(f"> ⚠️ **Writer error:** `{result.writer.error}`")
            lines.append("")
        else:
            lines.append("**Writer's generated Discord message**")
            lines.append("")
            for para in (result.writer_message or "").split("\n"):
                lines.append(f"> {para}" if para else ">")
            lines.append("")
            if result.writer_voice_summary:
                lines.append(f"**Voice summary:** {result.writer_voice_summary}")
                lines.append("")

        lines.append("| Model | In | Out | Cache read | Cache write |")
        lines.append("| --- | ---: | ---: | ---: | ---: |")
        lines.append(_token_row("Worker (small)", result.worker.tokens))
        if not result.writer.error:
            lines.append(_token_row("Writer (large)", result.writer.tokens))
        lines.append("")

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def _collect_scenario_paths(raw_paths: list[str]) -> list[Path]:
    """Resolve CLI paths (files or dirs) into a sorted list of YAML files."""
    if not raw_paths:
        raw_paths = [str(DEFAULT_SCENARIO_DIR)]
    files: list[Path] = []
    for raw in raw_paths:
        path = Path(raw)
        if path.is_dir():
            files.extend(sorted(path.glob("*.yaml")) + sorted(path.glob("*.yml")))
        elif path.exists():
            files.append(path)
        else:
            raise SystemExit(f"scenario path not found: {path}")
    if not files:
        raise SystemExit("no scenario YAML files found")
    return files


async def _main_async(args: argparse.Namespace) -> int:
    worker = get_model(args.worker)
    writer = get_model(args.writer)
    if worker is None:
        print(f"unknown worker model key: {args.worker!r}", file=sys.stderr)
        return 2
    if writer is None:
        print(f"unknown writer model key: {args.writer!r}", file=sys.stderr)
        return 2

    scenario_files = _collect_scenario_paths(args.paths)
    print(
        f"Worker: {worker.label} ({worker.model_id})   "
        f"Writer: {writer.label} ({writer.model_id})   "
        f"Scenarios: {len(scenario_files)}",
        file=sys.stderr,
    )

    results: list[ScenarioResult] = []
    for path in scenario_files:
        scenario = parse_scenario(path)
        print(f"  running {scenario.name} ({path.name}) ...", file=sys.stderr)
        results.append(
            await run_scenario(
                scenario,
                worker_model_id=worker.model_id,
                writer_model_id=writer.model_id,
                worker_reasoning=args.worker_reasoning,
            )
        )

    report = build_report(results, worker.label, writer.label)
    Path(args.out).write_text(report, encoding="utf-8")
    print(f"\nReport written to {args.out}", file=sys.stderr)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", help="scenario YAML files or dirs (default: scripts/two_stage_eval_scenarios)")
    parser.add_argument("--worker", default="gemini-3-5-flash-lite", help="worker model catalog key")
    parser.add_argument("--writer", default="gpt-5-6-terra", help="writer model catalog key")
    parser.add_argument("--worker-reasoning", default=None, help="worker reasoning level (default: model default)")
    parser.add_argument("--out", default="two_stage_eval_report.md", help="output Markdown path")
    args = parser.parse_args()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
