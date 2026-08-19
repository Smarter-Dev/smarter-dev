"""What the two-pass bot can see and touch during one wake.

`ChannelEnvironment` is the message store the agent's tools operate on: only
messages visible at activation time (fixture history + the bot's own earlier
responses), never the future. `InstructionStore` carries the watcher's wake
criteria: an immutable seed (the response policy) plus an agent-replaceable
addendum — the K3 agent's only channel for cross-wake continuity hints,
since the watcher itself is stateless.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

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


MAX_WATCH_INSTRUCTIONS = 5
MAX_WATCH_INSTRUCTION_TTL_SECONDS = int(timedelta(hours=24).total_seconds())
DEFAULT_WATCH_INSTRUCTION_TTL_SECONDS = int(timedelta(hours=1).total_seconds())
# Legacy plain-text addenda (pre-TTL rows) load as one entry with this life.
LEGACY_ADDENDUM_TTL_SECONDS = MAX_WATCH_INSTRUCTION_TTL_SECONDS


@dataclass(frozen=True)
class WatchInstruction:
    """One agent-written wake criterion with an expiry."""

    instruction_id: str
    text: str
    expires_at: datetime


@dataclass
class InstructionStore:
    """Watcher wake criteria: immutable seed + agent-set TTL'd instructions.

    The stateless watcher reads ``current()`` on every call; the agent
    manages entries through its set/clear tools. ``updates`` counts writes so
    callers know when to persist ``to_stored()``.
    """

    seed: str
    entries: list[WatchInstruction] = field(default_factory=list)
    updates: int = 0
    _next_id: int = 1

    def current(self, now: datetime | None = None) -> str:
        now = now or datetime.now(UTC)
        active = [e for e in self.entries if e.expires_at > now]
        if not active:
            return self.seed
        lines = "\n".join(
            f"- [{e.instruction_id}, until {e.expires_at:%H:%M} UTC] {e.text}"
            for e in active
        )
        return (
            f"{self.seed}\n\n"
            f"AGENT WATCH INSTRUCTIONS (set by the chat agent on earlier "
            f"wakes; treat as additional wake criteria):\n{lines}"
        )

    def set_instruction(
        self,
        text: str,
        *,
        ttl_seconds: int = DEFAULT_WATCH_INSTRUCTION_TTL_SECONDS,
        now: datetime | None = None,
    ) -> WatchInstruction:
        now = now or datetime.now(UTC)
        self.prune_expired(now=now)
        if len(self.entries) >= MAX_WATCH_INSTRUCTIONS:
            raise ValueError(
                f"at most {MAX_WATCH_INSTRUCTIONS} watch instructions may be "
                f"active; clear one first"
            )
        ttl = min(max(int(ttl_seconds), 60), MAX_WATCH_INSTRUCTION_TTL_SECONDS)
        entry = WatchInstruction(
            instruction_id=f"w{self._next_id}",
            text=text,
            expires_at=now + timedelta(seconds=ttl),
        )
        self._next_id += 1
        self.entries.append(entry)
        self.updates += 1
        return entry

    def clear_instruction(self, instruction_id: str) -> bool:
        remaining = [
            e for e in self.entries if e.instruction_id != instruction_id
        ]
        if len(remaining) == len(self.entries):
            return False
        self.entries = remaining
        self.updates += 1
        return True

    def prune_expired(
        self, now: datetime | None = None
    ) -> list[WatchInstruction]:
        now = now or datetime.now(UTC)
        expired = [e for e in self.entries if e.expires_at <= now]
        if expired:
            self.entries = [e for e in self.entries if e.expires_at > now]
            self.updates += 1
        return expired

    def to_stored(self) -> str:
        return json.dumps(
            [
                {
                    "id": e.instruction_id,
                    "text": e.text,
                    "expires_at": e.expires_at.isoformat(),
                }
                for e in self.entries
            ]
        )

    @classmethod
    def from_stored(
        cls, seed: str, stored: str, *, now: datetime | None = None
    ) -> InstructionStore:
        """Load persisted instructions; legacy plain text becomes one entry."""
        now = now or datetime.now(UTC)
        store = cls(seed=seed)
        if not stored:
            return store
        try:
            raw_entries = json.loads(stored)
        except json.JSONDecodeError:
            store.entries.append(
                WatchInstruction(
                    instruction_id="w1",
                    text=stored,
                    expires_at=now
                    + timedelta(seconds=LEGACY_ADDENDUM_TTL_SECONDS),
                )
            )
            store._next_id = 2
            return store
        if not isinstance(raw_entries, list):
            return store
        highest = 0
        for raw in raw_entries:
            entry = WatchInstruction(
                instruction_id=raw["id"],
                text=raw["text"],
                expires_at=datetime.fromisoformat(raw["expires_at"]),
            )
            store.entries.append(entry)
            digits = raw["id"].lstrip("w")
            if digits.isdigit():
                highest = max(highest, int(digits))
        store._next_id = highest + 1
        store.prune_expired(now=now)
        store.updates = 0
        return store
