"""Versioned wire contracts shared with the extracted proactive agent.

The canonical JSON Schemas live under ``contracts/proactive/v1``.  These
Pydantic models are the bot-side generated representation and deliberately
contain no queue or agent implementation details.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID
from uuid import uuid4

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from smarter_dev.bot.proactive.notifications import Notification

NotificationKind = Literal[
    "mention",
    "reply_to_bot",
    "watcher_summary",
    "new_messages",
    "mode_change",
    "instruction_expired",
    "recovery",
    "reaction",
    "channel_enabled",
]


class TokenUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int = Field(0, ge=0)
    output_tokens: int = Field(0, ge=0)
    cache_read_tokens: int = Field(0, ge=0)


class NotificationEnvelope(BaseModel):
    """One notification crossing from the bot to an agent worker."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    notification_id: UUID = Field(default_factory=uuid4)
    guild_id: str = Field(pattern=r"^[0-9]{1,20}$")
    channel_id: str = Field(pattern=r"^[0-9]{1,20}$")
    channel_name: str = Field(max_length=100)
    kind: NotificationKind
    created_at: datetime
    body: str
    message_ids: tuple[str, ...] = ()
    wakes: bool
    passive: bool = False
    watcher_usage: dict[str, TokenUsage] = Field(default_factory=dict)
    trace_id: UUID = Field(default_factory=uuid4)

    @classmethod
    def from_notification(
        cls,
        notification: Notification,
        *,
        guild_id: str,
        passive: bool = False,
        watcher_usage: dict[str, dict] | None = None,
        notification_id: UUID | None = None,
        trace_id: UUID | None = None,
    ) -> NotificationEnvelope:
        return cls(
            notification_id=notification_id or uuid4(),
            guild_id=guild_id,
            channel_id=notification.channel_id,
            channel_name=notification.channel_name,
            kind=notification.kind,
            created_at=notification.created_at,
            body=notification.body,
            message_ids=notification.message_ids,
            wakes=notification.wakes,
            passive=passive,
            watcher_usage=watcher_usage or {},
            trace_id=trace_id or uuid4(),
        )

    def to_notification(self) -> Notification:
        return Notification(
            kind=self.kind,
            created_at=self.created_at,
            body=self.body,
            channel_id=self.channel_id,
            channel_name=self.channel_name,
            message_ids=self.message_ids,
            wakes=self.wakes,
        )


class ControlCommand(BaseModel):
    """A guild agent's request for the bot-owned watcher runtime."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    command_id: UUID = Field(default_factory=uuid4)
    guild_id: str = Field(pattern=r"^[0-9]{1,20}$")
    channel_id: str = Field(pattern=r"^[0-9]{1,20}$")
    mode: Literal["active", "passive"]
    minutes: int = Field(ge=0, le=1440)
    created_at: datetime
    trace_id: UUID = Field(default_factory=uuid4)
