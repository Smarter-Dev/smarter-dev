"""Shared message and activation types for the proactive bot.

`ChannelMessage` mirrors the eval fixture JSONL schema (the eval keeps
calling it `FixtureMessage`); the activation dataclasses are the contract
between the wake loop and any bot implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class ChannelMessage:
    """One channel message — replayed fixture line, live event, or an
    injected/sent bot response."""

    id: str
    timestamp: datetime
    author_id: str
    author_name: str
    author_display: str
    is_bot: bool
    content: str
    reply_to_id: str | None
    mention_user_ids: tuple[str, ...]
    mention_everyone: bool
    attachment_count: int
    sticker_count: int
    message_type: int
    reaction_counts: dict[str, int] = field(default_factory=dict)
    injected_bot_response: bool = False
    # Guild role names of the author, when the runtime knows them (live
    # gateway messages); fixtures and injected messages leave this empty.
    roles: tuple[str, ...] = ()

    @classmethod
    def from_record(cls, record: dict) -> ChannelMessage:
        return cls(
            id=record["id"],
            timestamp=datetime.fromisoformat(record["timestamp"]),
            author_id=record["author_id"],
            author_name=record["author_name"],
            author_display=record["author_display"],
            is_bot=record["is_bot"],
            content=record["content"],
            reply_to_id=record["reply_to_id"],
            mention_user_ids=tuple(record["mention_user_ids"]),
            mention_everyone=record["mention_everyone"],
            attachment_count=record["attachment_count"],
            sticker_count=record["sticker_count"],
            message_type=record["message_type"],
            reaction_counts=dict(record["reaction_counts"]),
            roles=tuple(record.get("roles", ())),
        )

    def to_record(self) -> dict:
        """Dict shaped like a fixture JSONL line (for transcript rendering)."""
        return {
            "id": self.id,
            "timestamp": self.timestamp.isoformat(),
            "author_id": self.author_id,
            "author_name": self.author_name,
            "author_display": self.author_display,
            "is_bot": self.is_bot,
            "content": self.content,
            "reply_to_id": self.reply_to_id,
            "mention_user_ids": list(self.mention_user_ids),
            "mention_everyone": self.mention_everyone,
            "attachment_count": self.attachment_count,
            "sticker_count": self.sticker_count,
            "reaction_counts": dict(self.reaction_counts),
            "message_type": self.message_type,
            "roles": list(self.roles),
        }


@dataclass(frozen=True)
class ActivationContext:
    channel_name: str
    guild_name: str
    bot_user_id: str
    activated_at: datetime
    history: list[ChannelMessage]
    new_messages: list[ChannelMessage]
    channel_id: str = ""


@dataclass(frozen=True)
class ProposedResponse:
    reply_to_id: str | None
    content: str
    channel_id: str = ""


@dataclass(frozen=True)
class ProposedReaction:
    message_id: str
    emoji: str
    channel_id: str = ""


@dataclass(frozen=True)
class ActivationResult:
    responses: list[ProposedResponse]
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    model_id: str
    # Reactions are recorded in eval run records but not scored.
    reactions: tuple[ProposedReaction, ...] = ()
    # Multi-model adapters break usage down per model id here; cost
    # accounting prices each entry at its own list price.
    usage_by_model: dict[str, dict] | None = None
    # Adapter-specific extras (watcher decision, instruction updates, …).
    # Must be JSON-serializable.
    details: dict | None = None


class ProactiveBotAdapter(Protocol):
    async def activate(self, context: ActivationContext) -> ActivationResult: ...


def injected_response_message(
    response: ProposedResponse,
    *,
    bot_user_id: str,
    activated_at: datetime,
    activation_index: int,
    response_index: int,
) -> ChannelMessage:
    return ChannelMessage(
        id=f"injected-{activation_index}-{response_index}",
        timestamp=activated_at,
        author_id=bot_user_id,
        author_name="proactive-bot",
        author_display="proactive-bot",
        is_bot=True,
        content=response.content,
        reply_to_id=response.reply_to_id,
        mention_user_ids=(),
        mention_everyone=False,
        attachment_count=0,
        sticker_count=0,
        message_type=19 if response.reply_to_id else 0,
        injected_bot_response=True,
    )


FixtureMessage = ChannelMessage
