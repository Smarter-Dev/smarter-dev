"""Per-fire budget for a single handler execution.

This is the load-bearing rail: ONE :class:`HandlerBudget` is created per handler
firing and threaded into the sandbox's external functions *and* into every agent
the script spawns. Every metered action goes through this object, so the script
and its agents draw from a single shared pool — an agent that reads two pages
leaves the script one read, not three.

All metering lives here, in trusted host code. The sandboxed script never sees
this object; it only sees the external functions that call into it. When a cap
is hit the spend method raises :class:`CapExceeded` immediately — effects are
not transactional, so a handler that breaches on its third message has already
sent the first two. That is expected: cap breach fails loud, mid-flight.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# Generous preset (see the build plan). These are the per-fire ceilings, shared
# across the script and every agent it spawns.
DEFAULT_MAX_MESSAGES = 3
DEFAULT_MAX_WEB_SEARCHES = 3
DEFAULT_MAX_WEB_READS = 3
DEFAULT_MAX_AGENT_CALLS = 2
DEFAULT_MAX_AGENT_CONTEXT_BYTES = 32 * 1024
DEFAULT_WALL_CLOCK_SECONDS = 60.0
# Standard handlers have no moderation powers, so zero mod actions.
DEFAULT_MAX_MOD_ACTIONS = 0
# Metered Discord reads (list_threads). Standard handlers get a couple; mutating
# thread ops are zero for them, exactly like mod actions.
DEFAULT_MAX_DISCORD_READS = 2
DEFAULT_MAX_THREAD_OPS = 0

# Admin handlers are admin-created and trusted: looser per-fire caps, and a
# moderation-action budget (bans/kicks/timeouts/deletes), e.g. cleaning up a
# scammer's messages = several deletes in one fire.
ADMIN_MAX_MESSAGES = 5
ADMIN_MAX_AGENT_CALLS = 3
ADMIN_MAX_MOD_ACTIONS = 25
ADMIN_MAX_DISCORD_READS = 5
ADMIN_MAX_THREAD_OPS = 10
ADMIN_WALL_CLOCK_SECONDS = 120.0


class CapExceeded(Exception):
    """Raised the moment a per-fire cap would be exceeded.

    ``cap`` names the limit that was hit (e.g. ``"messages"``) so callers can
    record which rail stopped the run.
    """

    def __init__(self, cap: str, message: str) -> None:
        super().__init__(message)
        self.cap = cap


@dataclass
class HandlerBudget:
    """A single firing's spend, shared into the script and its agents.

    Counters start at zero and only ever increase. ``deadline`` is an absolute
    :func:`time.monotonic` instant computed once at construction; ``check_deadline``
    compares against it so wall-clock is enforced uniformly across every stage.
    """

    max_messages: int = DEFAULT_MAX_MESSAGES
    max_web_searches: int = DEFAULT_MAX_WEB_SEARCHES
    max_web_reads: int = DEFAULT_MAX_WEB_READS
    max_agent_calls: int = DEFAULT_MAX_AGENT_CALLS
    max_agent_context_bytes: int = DEFAULT_MAX_AGENT_CONTEXT_BYTES
    wall_clock_seconds: float = DEFAULT_WALL_CLOCK_SECONDS
    max_mod_actions: int = DEFAULT_MAX_MOD_ACTIONS
    max_discord_reads: int = DEFAULT_MAX_DISCORD_READS
    max_thread_ops: int = DEFAULT_MAX_THREAD_OPS

    messages_sent: int = 0
    web_searches: int = 0
    web_reads: int = 0
    agent_calls: int = 0
    mod_actions: int = 0
    discord_reads: int = 0
    thread_ops: int = 0

    started_at: float = field(default_factory=time.monotonic)

    @property
    def deadline(self) -> float:
        """Absolute monotonic instant after which the fire is out of time."""
        return self.started_at + self.wall_clock_seconds

    def check_deadline(self) -> None:
        """Raise if the wall-clock budget for this fire has elapsed."""
        if time.monotonic() >= self.deadline:
            raise CapExceeded(
                "wall_clock",
                f"handler exceeded its {self.wall_clock_seconds:g}s wall-clock budget",
            )

    def spend_message(self) -> None:
        """Account for one emitted message/reaction/voice post."""
        self.check_deadline()
        if self.messages_sent >= self.max_messages:
            raise CapExceeded(
                "messages",
                f"handler hit its {self.max_messages}-message cap",
            )
        self.messages_sent += 1

    def spend_web_search(self) -> None:
        """Account for one web search (drawn from the shared pool)."""
        self.check_deadline()
        if self.web_searches >= self.max_web_searches:
            raise CapExceeded(
                "web_searches",
                f"handler hit its {self.max_web_searches}-web-search cap",
            )
        self.web_searches += 1

    def spend_web_read(self) -> None:
        """Account for one web read/fetch (drawn from the shared pool)."""
        self.check_deadline()
        if self.web_reads >= self.max_web_reads:
            raise CapExceeded(
                "web_reads",
                f"handler hit its {self.max_web_reads}-web-read cap",
            )
        self.web_reads += 1

    def spend_agent(self) -> None:
        """Account for one agent invocation."""
        self.check_deadline()
        if self.agent_calls >= self.max_agent_calls:
            raise CapExceeded(
                "agent_calls",
                f"handler hit its {self.max_agent_calls}-agent-call cap",
            )
        self.agent_calls += 1

    def spend_mod_action(self) -> None:
        """Account for one moderation action (ban/kick/timeout/delete)."""
        self.check_deadline()
        if self.mod_actions >= self.max_mod_actions:
            raise CapExceeded(
                "mod_actions",
                f"handler hit its {self.max_mod_actions}-moderation-action cap",
            )
        self.mod_actions += 1

    def spend_discord_read(self) -> None:
        """Account for one metered Discord read (e.g. list_threads)."""
        self.check_deadline()
        if self.discord_reads >= self.max_discord_reads:
            raise CapExceeded(
                "discord_reads",
                f"handler hit its {self.max_discord_reads}-discord-read cap",
            )
        self.discord_reads += 1

    def spend_thread_op(self) -> None:
        """Account for one mutating thread op (create/close/lock/reopen/delete)."""
        self.check_deadline()
        if self.thread_ops >= self.max_thread_ops:
            raise CapExceeded(
                "thread_ops",
                f"handler hit its {self.max_thread_ops}-thread-op cap",
            )
        self.thread_ops += 1

    def enforce_agent_context(self, argument: str) -> None:
        """Reject an agent argument larger than the context-bytes cap.

        Enforced on the *input* to every agent regardless of its ``has_tools``
        flag: a no-tools agent's output is unbounded and may flow into a second
        agent, so the runtime bounds what crosses between stages here.
        """
        size = len(argument.encode("utf-8"))
        if size > self.max_agent_context_bytes:
            raise CapExceeded(
                "agent_context_bytes",
                f"agent context of {size} bytes exceeds the "
                f"{self.max_agent_context_bytes}-byte cap",
            )

    def usage(self) -> dict[str, int]:
        """Snapshot of consumed counters, for the durable run record."""
        return {
            "messages_sent": self.messages_sent,
            "web_searches": self.web_searches,
            "web_reads": self.web_reads,
            "agent_calls": self.agent_calls,
            "mod_actions": self.mod_actions,
            "discord_reads": self.discord_reads,
            "thread_ops": self.thread_ops,
        }


def admin_budget() -> "HandlerBudget":
    """A trusted, looser per-fire budget for admin handlers (incl. mod actions)."""
    return HandlerBudget(
        max_messages=ADMIN_MAX_MESSAGES,
        max_agent_calls=ADMIN_MAX_AGENT_CALLS,
        max_mod_actions=ADMIN_MAX_MOD_ACTIONS,
        max_discord_reads=ADMIN_MAX_DISCORD_READS,
        max_thread_ops=ADMIN_MAX_THREAD_OPS,
        wall_clock_seconds=ADMIN_WALL_CLOCK_SECONDS,
    )
