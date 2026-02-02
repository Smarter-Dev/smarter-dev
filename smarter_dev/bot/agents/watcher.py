"""Watcher data structures for multi-agent mention pipeline.

This module defines the data structures used by the watcher system:
- Watcher: Represents a topic watcher monitoring a channel
- WatcherContext: Context about what triggered the watcher
- UpdateFrequency: How often the watcher should evaluate new messages
- ResponseAgentOutput: Structured output from the response agent
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any


class UpdateFrequency(Enum):
    """How often a watcher should check for relevant messages."""

    TEN_SECONDS = "10s"
    ONE_MINUTE = "1m"
    FIVE_MINUTES = "5m"

    def to_seconds(self) -> int:
        """Convert to seconds."""
        if self == UpdateFrequency.TEN_SECONDS:
            return 10
        elif self == UpdateFrequency.ONE_MINUTE:
            return 60
        elif self == UpdateFrequency.FIVE_MINUTES:
            return 300
        return 60  # Default to 1 minute


@dataclass
class WatcherContext:
    """Context about what triggered the watcher and what it's watching for."""

    relevant_message_ids: list[str]
    """Message IDs that were relevant to the original trigger."""

    relevant_messages_summary: str
    """Summary of the relevant messages for context."""

    watching_for: str
    """Description of what the watcher is looking for."""

    original_trigger_message_id: str
    """The message ID that originally triggered this watcher."""


@dataclass
class Watcher:
    """Represents a topic watcher monitoring a channel for specific content."""

    id: str
    """Unique identifier for this watcher."""

    channel_id: int
    """Discord channel ID being watched."""

    guild_id: int
    """Discord guild ID."""

    context: WatcherContext
    """Context about what triggered this watcher."""

    wait_duration: int
    """How long to wait before expiring (30-300 seconds)."""

    update_frequency: UpdateFrequency
    """How often to evaluate new messages."""

    created_at: datetime
    """When this watcher was created."""

    expires_at: datetime
    """When this watcher will expire if not triggered."""

    queued_messages: list[dict] = field(default_factory=list)
    """Messages queued for evaluation."""

    is_responding: bool = False
    """Whether the response agent is currently running for this watcher."""

    response_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    """Lock to prevent concurrent response agent invocations."""

    last_evaluation_at: datetime | None = None
    """When this watcher was last evaluated."""

    def is_expired(self) -> bool:
        """Check if this watcher has expired."""
        return datetime.now(UTC) >= self.expires_at

    def should_evaluate(self) -> bool:
        """Check if this watcher should be evaluated based on update frequency."""
        if self.is_responding:
            return False

        if not self.last_evaluation_at:
            return bool(self.queued_messages)

        elapsed = (datetime.now(UTC) - self.last_evaluation_at).total_seconds()
        return elapsed >= self.update_frequency.to_seconds() and bool(self.queued_messages)


@dataclass
class ResponseAgentOutput:
    """Structured output from the response agent."""

    continue_watching: bool
    """Whether to continue watching for follow-up messages."""

    watching_for: str
    """What the watcher should look for if continue_watching is True."""

    wait_duration: int
    """How long to wait before expiring (30-300 seconds)."""

    update_frequency: UpdateFrequency
    """How often to check for new messages."""

    tokens_used: int
    """Number of tokens consumed by the agent."""

    @classmethod
    def from_agent_result(cls, result: Any) -> "ResponseAgentOutput":
        """Parse response agent output from DSPy result.

        Args:
            result: DSPy prediction result with structured output fields

        Returns:
            ResponseAgentOutput instance
        """
        # Parse continue_watching (boolean)
        continue_watching_raw = getattr(result, "continue_watching", "false")
        if isinstance(continue_watching_raw, bool):
            continue_watching = continue_watching_raw
        else:
            continue_watching = str(continue_watching_raw).lower().strip() in ("true", "yes", "1")

        # Parse watching_for (string)
        watching_for = str(getattr(result, "watching_for", "")).strip()

        # Parse wait_duration (int, clamped to 30-300)
        wait_duration_raw = getattr(result, "wait_duration", 60)
        try:
            wait_duration = int(wait_duration_raw)
            wait_duration = max(30, min(300, wait_duration))
        except (ValueError, TypeError):
            wait_duration = 60

        # Parse update_frequency (enum)
        freq_raw = str(getattr(result, "update_frequency", "1m")).strip().lower()
        if freq_raw in ("10s", "10"):
            update_frequency = UpdateFrequency.TEN_SECONDS
        elif freq_raw in ("5m", "5", "300"):
            update_frequency = UpdateFrequency.FIVE_MINUTES
        else:
            update_frequency = UpdateFrequency.ONE_MINUTE

        return cls(
            continue_watching=continue_watching,
            watching_for=watching_for,
            wait_duration=wait_duration,
            update_frequency=update_frequency,
            tokens_used=0  # Will be set by caller
        )
