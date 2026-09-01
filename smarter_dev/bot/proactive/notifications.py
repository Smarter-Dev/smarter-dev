"""The proactive bot's notification model.

Everything the agent learns about arrives as a typed notification: watcher
summaries (with the relevant message ids and user metadata), @mentions and
replies to the bot (verbatim), monitoring-mode changes, watch-instruction
expiries, and restart recoveries. Deterministic engagement (mention/reply)
and wake-worthy watcher summaries wake the agent; mode changes, expiries and
recoveries queue and ride along with the next wake's brief. Watcher
summaries that don't wake are deliberately discarded.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime

from smarter_dev.bot.proactive.types import ChannelMessage

NOTIFICATION_QUEUE_LIMIT = 20


@dataclass(frozen=True)
class Notification:
    kind: str
    created_at: datetime
    body: str
    channel_id: str = ""
    channel_name: str = ""
    message_ids: tuple[str, ...] = ()
    # Whether this notification wakes the agent by itself; non-waking ones
    # queue until something else wakes it.
    wakes: bool = False


def _user_metadata(message: ChannelMessage) -> str:
    bot_marker = ", bot" if message.is_bot else ""
    roles = (
        f", roles: {', '.join(message.roles)}" if message.roles else ""
    )
    return (
        f"{message.author_display} (username {message.author_name}, "
        f"id {message.author_id}{bot_marker}{roles})"
    )


def mention_notification(
    message: ChannelMessage,
    channel_id: str = "",
    channel_name: str = "",
) -> Notification:
    return Notification(
        kind="mention",
        created_at=message.timestamp,
        body=(
            f"You were @mentioned by {_user_metadata(message)} in message "
            f"id={message.id}:\n> {message.content}"
        ),
        channel_id=channel_id,
        channel_name=channel_name,
        message_ids=(message.id,),
        wakes=True,
    )


def reply_notification(
    message: ChannelMessage,
    replied_to: ChannelMessage | None,
    channel_id: str = "",
    channel_name: str = "",
) -> Notification:
    replied_line = (
        f'your message id={replied_to.id} ("{replied_to.content[:120]}")'
        if replied_to is not None
        else "one of your messages"
    )
    return Notification(
        kind="reply_to_bot",
        created_at=message.timestamp,
        body=(
            f"{_user_metadata(message)} replied to {replied_line} with "
            f"message id={message.id}:\n> {message.content}"
        ),
        channel_id=channel_id,
        channel_name=channel_name,
        message_ids=(message.id,),
        wakes=True,
    )


def watcher_summary_notification(
    *,
    summary: str,
    message_ids: list[str],
    wake: bool,
    created_at: datetime,
    channel_id: str = "",
    channel_name: str = "",
) -> Notification:
    id_list = ", ".join(message_ids) if message_ids else "none flagged"
    return Notification(
        kind="watcher_summary",
        created_at=created_at,
        body=f"Watcher summary: {summary} (relevant message ids: {id_list})",
        channel_id=channel_id,
        channel_name=channel_name,
        message_ids=tuple(message_ids),
        wakes=wake,
    )


def new_messages_notification(
    *,
    summary: str,
    message_ids: list[str],
    created_at: datetime,
    channel_id: str = "",
    channel_name: str = "",
) -> Notification:
    """Mid-run batch: messages that arrived while the agent was working,
    grouped and summarized by the watcher model. Mentions never take this
    path — they queue individually and verbatim."""
    return Notification(
        kind="new_messages",
        created_at=created_at,
        body=(
            f"{len(message_ids)} new messages arrived while you were "
            f"working: {summary}"
        ),
        channel_id=channel_id,
        channel_name=channel_name,
        message_ids=tuple(message_ids),
    )


def mode_change_notification(
    *,
    mode: str,
    cause: str,
    until: datetime | None,
    created_at: datetime,
    channel_id: str = "",
    channel_name: str = "",
) -> Notification:
    until_text = f" until {until:%H:%M} UTC" if until else ""
    return Notification(
        kind="mode_change",
        created_at=created_at,
        body=f"Monitoring mode changed to {mode}{until_text} — {cause}.",
        channel_id=channel_id,
        channel_name=channel_name,
    )


def instruction_expired_notification(
    *,
    instruction_id: str,
    text: str,
    created_at: datetime,
    channel_id: str = "",
    channel_name: str = "",
) -> Notification:
    return Notification(
        kind="instruction_expired",
        created_at=created_at,
        body=f'Watch instruction {instruction_id} expired: "{text}"',
        channel_id=channel_id,
        channel_name=channel_name,
    )


def recovery_notification(
    *,
    missed_count: int,
    created_at: datetime,
    channel_id: str = "",
    channel_name: str = "",
) -> Notification:
    return Notification(
        kind="recovery",
        created_at=created_at,
        body=(
            f"The bot restarted; {missed_count} messages arrived while it "
            f"was down and are included in this wake."
        ),
        channel_id=channel_id,
        channel_name=channel_name,
    )


def reaction_notification(
    *,
    reactor_name: str,
    reactor_id: str,
    emoji: str,
    message_id: str,
    message_preview: str,
    created_at: datetime,
    channel_id: str = "",
    channel_name: str = "",
) -> Notification:
    """A member reacted to one of the bot's messages.

    Low signal by design: it queues like a plain message and rides along
    with whatever wakes the agent next, never waking it by itself.
    """
    preview = message_preview[:80]
    return Notification(
        kind="reaction",
        created_at=created_at,
        body=(
            f"{reactor_name} (id {reactor_id}) reacted {emoji} to your "
            f'message {message_id} ("{preview}"). A reaction is a LOW '
            "signal — usually simple acknowledgment. Engage only if it "
            "clearly invites a response (a question or pointed emoji, or it "
            "continues an active exchange); otherwise let it stand."
        ),
        message_ids=(message_id,),
        channel_id=channel_id,
        channel_name=channel_name,
    )


def channel_enabled_notification(
    *,
    created_at: datetime,
    channel_id: str = "",
    channel_name: str = "",
) -> Notification:
    """Wakes the agent when a moderator switches a new channel on."""
    return Notification(
        kind="channel_enabled",
        created_at=created_at,
        body=(
            "You were just enabled in this channel. Get oriented: pull its "
            "recent history and see what the space is about. If a brief, "
            "natural introduction or contribution fits the room, send one — "
            "otherwise just take note of what you learned."
        ),
        wakes=True,
        channel_id=channel_id,
        channel_name=channel_name,
    )


@dataclass
class NotificationQueue:
    """Per-channel pending notifications, newest kept when over the limit."""

    limit: int = NOTIFICATION_QUEUE_LIMIT
    items: list[Notification] = field(default_factory=list)
    dropped: int = 0
    _wake_event: asyncio.Event = field(
        default_factory=asyncio.Event,
        init=False,
        repr=False,
        compare=False,
    )

    def push(self, notification: Notification) -> None:
        self.items.append(notification)
        if notification.wakes:
            self._wake_event.set()
        if len(self.items) > self.limit:
            overflow = len(self.items) - self.limit
            self.items = self.items[overflow:]
            self.dropped += overflow

    async def wait_for_wake(self) -> None:
        await self._wake_event.wait()

    def drain(self) -> tuple[list[Notification], int]:
        items, dropped = self.items, self.dropped
        self.items, self.dropped = [], 0
        self._wake_event.clear()
        return items, dropped


def render_notifications(items: list[Notification], dropped: int = 0) -> str:
    lines = ["NOTIFICATIONS since your last wake (oldest first):"]
    if dropped:
        lines.append(f"({dropped} older notifications were dropped)")
    for notification in items:
        stamp = notification.created_at.astimezone(UTC).strftime("%H:%M")
        channel = notification.channel_name or notification.channel_id
        channel_prefix = f"[#{channel}] " if channel else ""
        lines.append(
            f"{channel_prefix}[{stamp} UTC, {notification.kind}] "
            f"{notification.body}"
        )
    return "\n".join(lines)
