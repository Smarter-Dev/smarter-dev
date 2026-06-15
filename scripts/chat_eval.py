#!/usr/bin/env python
"""Local eval harness for the Discord chat agent.

Drives the real `chat_agent` (Gemini 3.1 Flash Lite) against a scenario
file that describes a channel state + an activation message, without
touching Discord. Prints the rendered XML user_prompt, the message
history sent to the model, and the structured agent output for
human-as-judge review.

Usage:
    uv run python scripts/chat_eval.py scripts/chat_eval_scenarios/reply_to_other_user.yaml
    uv run python scripts/chat_eval.py path/to/scenario.yaml --json

Scenario YAML schema:

    me:
      user_id: "999"           # bot id (default "bot")
      username: "smarterbot"
    channel:
      id: "100"
      name: "general"
      description: "main chat"  # optional
    topic: null                  # optional durable memory
    notes: null                  # optional durable memory
    authors:
      - user_id: "1"
        username: "alice"
        nickname: "Al"           # optional
        roles: ["dev"]           # optional
    history:                     # messages BEFORE the trigger (oldest first)
      - id: "10"
        from: "1"
        body: "hey bob"
        reply_to: "9"            # optional; id of a prior message
        mentions_bot: false      # optional
        attachments: false       # optional
        reactions: ["👍"]        # optional
    trigger:                     # the message the agent is being asked to react to
      id: "12"
      from: "1"
      body: "@smarterbot any tips?"
      mentions_bot: true
      reply_to: "11"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from smarter_dev.bot.agents.chat_agent import get_chat_agent  # noqa: E402
from smarter_dev.bot.agents.chat_input_format import build_agent_call  # noqa: E402
from smarter_dev.bot.agents.chat_models import (  # noqa: E402
    Author,
    ChannelInfo,
    FollowupAgentInput,
    InitialAgentInput,
    Me,
    Message,
)
from smarter_dev.bot.agents.chat_tools import ChatDeps  # noqa: E402


class _StubRest:
    """Minimal hikari REST stub for tools that touch the bot.

    Tools that try to write to Discord (create_message, add_reaction) are
    no-ops that log to stderr so you can see when the agent reaches for
    them. Read-only calls return empty / sensible defaults.
    """

    async def create_message(self, channel_id: int, content: str, **kwargs: Any) -> None:
        print(f"[stub bot] create_message({channel_id}): {content!r}", file=sys.stderr)

    async def add_reaction(self, channel_id: int, message_id: int, emoji: str) -> None:
        print(
            f"[stub bot] add_reaction({channel_id}, {message_id}, {emoji!r})",
            file=sys.stderr,
        )

    async def fetch_guild_emojis(self, guild_id: int) -> list[Any]:
        return []


class _StubBot:
    def __init__(self) -> None:
        self.rest = _StubRest()


@dataclass
class _ParsedScenario:
    me: Me
    channel: ChannelInfo
    authors: list[Author]
    history: list[Message]
    trigger: Message
    topic: str | None
    notes: str | None
    kind: str  # "initial" or "followup"


def _parse_scenario(path: Path) -> _ParsedScenario:
    data = yaml.safe_load(path.read_text())

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

    # Synthetic timeline: messages get sequential timestamps so the model
    # sees them in chronological order. Start a few minutes ago.
    base_time = datetime.now(UTC) - timedelta(minutes=10)

    def _build_message(
        entry: dict[str, Any],
        *,
        seq: int,
        all_so_far: dict[str, Message],
    ) -> Message:
        reply_to_id = entry.get("reply_to")
        reply_to_id = str(reply_to_id) if reply_to_id is not None else None
        reply_to_author_id: str | None = None
        reply_to_is_self = False
        if reply_to_id and reply_to_id in all_so_far:
            target = all_so_far[reply_to_id]
            reply_to_author_id = target.author_id
            reply_to_is_self = target.author_id == me.user_id
        author_id = str(entry["from"])
        if author_id not in authors_by_id and author_id != me.user_id:
            raise SystemExit(
                f"Scenario error: message id={entry.get('id')} references "
                f"author_id={author_id!r} not listed under `authors`."
            )
        return Message(
            message_id=str(entry["id"]),
            author_id=author_id,
            reply_to_message_id=reply_to_id,
            reply_to_author_id=reply_to_author_id,
            reply_to_is_self=reply_to_is_self,
            body=entry.get("body", ""),
            reactions=list(entry.get("reactions") or []),
            has_attachments=bool(entry.get("attachments", False)),
            sent_at=base_time + timedelta(seconds=seq * 30),
            mentions_bot=bool(entry.get("mentions_bot", False)),
        )

    all_so_far: dict[str, Message] = {}
    history: list[Message] = []
    for i, entry in enumerate(data.get("history") or []):
        msg = _build_message(entry, seq=i, all_so_far=all_so_far)
        history.append(msg)
        all_so_far[msg.message_id] = msg

    trigger_entry = data.get("trigger")
    if not trigger_entry:
        raise SystemExit("Scenario error: missing `trigger` block.")
    trigger = _build_message(
        trigger_entry, seq=len(history), all_so_far=all_so_far
    )

    kind = (data.get("kind") or "initial").lower()
    if kind not in ("initial", "followup"):
        raise SystemExit(f"Scenario error: kind must be 'initial' or 'followup', got {kind!r}")

    return _ParsedScenario(
        me=me,
        channel=channel,
        authors=authors,
        history=history,
        trigger=trigger,
        topic=data.get("topic"),
        notes=data.get("notes"),
        kind=kind,
    )


def _build_call(scenario: _ParsedScenario) -> tuple[str, list[Any]]:
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
        return build_agent_call(agent_input, prior_history=[])

    # Followup: bake the scenario's `history` into prior_history as
    # UserPromptPart entries (the same shape `build_agent_call` would
    # produce on the original initial turn), then pass ONLY the trigger as
    # the single new message.
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    from smarter_dev.bot.agents.chat_input_format import render_message_xml

    prior_history: list[Any] = []
    for msg in scenario.history:
        prior_history.append(
            ModelRequest(
                parts=[
                    UserPromptPart(
                        content=render_message_xml(
                            msg, me=scenario.me, authors=scenario.authors
                        )
                    )
                ]
            )
        )
    agent_input = FollowupAgentInput(
        me=scenario.me,
        new_messages=[scenario.trigger],
        authors=scenario.authors,
        channel=scenario.channel,
        now_utc=datetime.now(UTC),
        topic=scenario.topic,
        notes=scenario.notes,
    )
    return build_agent_call(agent_input, prior_history=prior_history)


def _render_call_preview(scenario: _ParsedScenario) -> tuple[str, list[Any]]:
    return _build_call(scenario)


async def _run(scenario: _ParsedScenario) -> dict[str, Any]:
    user_prompt, history = _build_call(scenario)
    agent = get_chat_agent()
    deps = ChatDeps(
        bot=_StubBot(),
        channel_id=int(scenario.channel.channel_id) if scenario.channel.channel_id.isdigit() else 0,
        guild_id=0,
    )
    result = await agent.run(
        user_prompt=user_prompt,
        message_history=history,
        deps=deps,
    )
    output = result.output
    return {
        "user_prompt": user_prompt,
        "history": [
            part.content
            for msg in history
            for part in msg.parts
            if hasattr(part, "content")
        ],
        "output": output.model_dump(mode="json"),
        "output_kind": (
            "TurnDecision(response)"
            if output.response is not None
            else "TurnDecision(no response)"
        ),
    }


def _print_human(payload: dict[str, Any]) -> None:
    print("=" * 72)
    print("HISTORY (prior <message> entries)")
    print("=" * 72)
    if not payload["history"]:
        print("(empty)")
    for i, content in enumerate(payload["history"]):
        print(f"-- entry {i} --")
        print(content)
        print()
    print("=" * 72)
    print("USER PROMPT (metadata + newest <message>)")
    print("=" * 72)
    print(payload["user_prompt"])
    print()
    print("=" * 72)
    print(f"AGENT OUTPUT — {payload['output_kind']}")
    print("=" * 72)
    print(json.dumps(payload["output"], indent=2, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", type=Path, help="Path to scenario YAML")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of pretty text")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Render the XML input but don't call the model.",
    )
    args = parser.parse_args()

    if not args.scenario.exists():
        print(f"scenario not found: {args.scenario}", file=sys.stderr)
        return 2

    scenario = _parse_scenario(args.scenario)

    if args.dry_run:
        user_prompt, history = _render_call_preview(scenario)
        payload = {
            "user_prompt": user_prompt,
            "history": [
                part.content
                for msg in history
                for part in msg.parts
                if hasattr(part, "content")
            ],
            "output": None,
            "output_kind": "(dry-run)",
        }
    else:
        payload = asyncio.run(_run(scenario))

    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        _print_human(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
