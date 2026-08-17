"""What the two-pass bot can see and touch during one wake.

`ChannelEnvironment` is the message store the agent's tools operate on: only
messages visible at activation time (fixture history + the bot's own earlier
responses), never the future. `InstructionStore` carries the watcher's wake
criteria: an immutable seed (the response policy) plus an agent-replaceable
addendum — the K3 agent's only channel for cross-wake continuity hints,
since the watcher itself is stateless.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from smarter_dev.bot.proactive.transcript import (
    render_transcript_line,
    speaker_tags,
)
from smarter_dev.bot.proactive.types import (
    ChannelMessage,
    ProposedReaction,
    ProposedResponse,
)


@dataclass
class ChannelEnvironment:
    visible: list[ChannelMessage]
    bot_user_id: str

    def __post_init__(self) -> None:
        self._by_id = {message.id: message for message in self.visible}
        self._tags = speaker_tags([m.to_record() for m in self.visible])

    def lookup(self, message_id: str) -> ChannelMessage | None:
        return self._by_id.get(message_id)

    def history(
        self, limit: int, before_id: str | None = None
    ) -> list[ChannelMessage]:
        pool = self.visible
        if before_id is not None:
            positions = [i for i, m in enumerate(pool) if m.id == before_id]
            pool = pool[: positions[0]] if positions else []
        return pool[-limit:]

    def slice_around(
        self, message_id: str, *, radius: int
    ) -> list[ChannelMessage]:
        positions = [i for i, m in enumerate(self.visible) if m.id == message_id]
        if not positions:
            return []
        position = positions[0]
        return self.visible[max(0, position - radius) : position + radius + 1]

    def render(self, messages: list[ChannelMessage]) -> str:
        """Transcript lines with speaker tags stable across the whole wake."""
        return "\n".join(
            render_transcript_line(m.to_record(), self._tags) for m in messages
        ) or "(no messages)"


@dataclass
class WakeActions:
    """Everything the agent did during one wake."""

    sent: list[ProposedResponse] = field(default_factory=list)
    reactions: list[ProposedReaction] = field(default_factory=list)


@dataclass
class InstructionStore:
    """Watcher wake criteria: immutable seed + agent-replaceable addendum."""

    seed: str
    addendum: str = ""
    updates: int = 0

    def current(self) -> str:
        if not self.addendum:
            return self.seed
        return (
            f"{self.seed}\n\n"
            f"AGENT ADDENDUM (written by the chat agent on an earlier wake; "
            f"treat as additional wake criteria):\n{self.addendum}"
        )

    def update(self, addendum: str) -> None:
        self.addendum = addendum
        self.updates += 1
