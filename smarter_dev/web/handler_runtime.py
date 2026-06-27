"""The handler runtime — runs an approved script in Monty under all the rails.

This is the hot path. One :class:`~smarter_dev.web.handler_budget.HandlerBudget`
is created per fire and threaded into the script's external functions *and* into
every agent the script spawns, so all caps draw from one shared pool. The script
can reach Discord only through the metered external functions here — that single
chokepoint is what makes "code is the only emitter" and "agents only gather"
true in practice.

Script-facing surface (Monty external functions):
- ``send_message(content)`` -> message id
- ``add_reaction(message_id, emoji)`` -> True
- ``post_voice(text)`` -> True
- ``spawn_agent(prompt, has_tools=False)`` -> plaintext str

Script-facing data (Monty input): ``context`` — a plain dict describing the
trigger (its keys depend on ``context["trigger_type"]``).

Gathering is agent-only: the script itself has no web access. A spawned agent
returns plaintext and cannot emit. The budget bounds both the agent's *input*
(``enforce_agent_context``, regardless of ``has_tools``) and any searches/reads
it performs (shared with the script's pool).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import pydantic_monty as monty

from smarter_dev.web.handler_budget import CapExceeded, HandlerBudget
from smarter_dev.web.handler_caps import (
    CHANNEL_MESSAGES_PER_MIN,
    GLOBAL_AGENT_CALLS_PER_MIN,
    WindowedLimiter,
    channel_message_key,
    global_agent_key,
)
from smarter_dev.web.handler_emitter import DiscordEmitter

logger = logging.getLogger(__name__)

# Hard sandbox limits, independent of the per-fire budget's wall-clock. Memory
# and recursion bound a runaway script; max_duration_secs is a backstop a few
# seconds past the budget's own deadline so the budget fails first with a clear
# cap name.
SANDBOX_MAX_MEMORY = 256 * 1024 * 1024
SANDBOX_MAX_RECURSION_DEPTH = 500
_DURATION_SLACK_SECONDS = 5.0

# An async function (prompt, has_tools, budget) -> plaintext. Injectable so
# tests can run the runtime fully offline.
AgentRunner = Callable[[str, bool, HandlerBudget], Awaitable[str]]


async def _no_agent(prompt: str, has_tools: bool, budget: HandlerBudget) -> str:
    raise RuntimeError("no agent runner configured for this handler execution")


@dataclass
class HandlerResult:
    """Outcome of one handler firing, for the durable run record."""

    outcome: str  # "ok" | "cap_exceeded" | "error"
    usage: dict[str, int]
    duration_ms: int
    error: str | None = None
    cap: str | None = None


@dataclass
class HandlerExecution:
    """Binds a single fire's budget + emitter + limiter into external functions."""

    channel_id: str
    guild_id: str
    budget: HandlerBudget
    emitter: DiscordEmitter
    limiter: WindowedLimiter
    agent_runner: AgentRunner = _no_agent
    # The cap that stopped this fire, captured here because Monty rewraps the
    # exception that crosses out of an external function (losing its type), so
    # we record the real CapExceeded on the host side before re-raising.
    breach: CapExceeded | None = None

    def external_functions(self) -> dict[str, Callable[..., Any]]:
        return {
            "send_message": self._guard(self._send_message),
            "add_reaction": self._guard(self._add_reaction),
            "post_voice": self._guard(self._post_voice),
            "spawn_agent": self._guard(self._spawn_agent),
        }

    def _guard(self, fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        """Wrap an external function so a CapExceeded is recorded before it is
        re-raised into the sandbox (and rewrapped beyond recognition)."""

        async def wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except CapExceeded as exc:
                self.breach = exc
                raise

        return wrapper

    async def _send_message(self, content: str) -> str:
        self.budget.spend_message()
        within = await self.limiter.hit(
            channel_message_key(self.channel_id), CHANNEL_MESSAGES_PER_MIN
        )
        if not within:
            raise CapExceeded(
                "channel_messages_per_min",
                f"channel hit its {CHANNEL_MESSAGES_PER_MIN} messages/min cap",
            )
        return await self.emitter.create_message(self.channel_id, str(content))

    async def _add_reaction(self, message_id: str, emoji: str) -> bool:
        # Reactions are emits too — metered against the per-fire emit cap, but
        # not against the per-channel *message* window (they are not messages).
        self.budget.spend_message()
        await self.emitter.add_reaction(self.channel_id, str(message_id), str(emoji))
        return True

    async def _post_voice(self, text: str) -> bool:
        self.budget.spend_message()
        # Voice synthesis is not wired in the worker yet; the metered surface
        # exists so authors can target it. Surface a clear, non-fatal note.
        logger.info("post_voice requested but not yet supported in the worker tier")
        raise CapExceeded("voice_unsupported", "post_voice is not available yet")

    async def _spawn_agent(self, prompt: str, has_tools: bool = False) -> str:
        prompt = str(prompt)
        # Bound the *input* regardless of has_tools — a no-tools agent's output
        # is unbounded and may flow into a second agent.
        self.budget.enforce_agent_context(prompt)
        self.budget.spend_agent()
        within = await self.limiter.hit(
            global_agent_key(), GLOBAL_AGENT_CALLS_PER_MIN
        )
        if not within:
            raise CapExceeded(
                "global_agent_calls_per_min",
                f"system hit its {GLOBAL_AGENT_CALLS_PER_MIN} agent-calls/min cap",
            )
        return await self.agent_runner(prompt, bool(has_tools), self.budget)


async def run_handler_script(
    script: str,
    context: dict[str, Any],
    *,
    channel_id: str,
    guild_id: str,
    emitter: DiscordEmitter,
    limiter: WindowedLimiter,
    agent_runner: AgentRunner = _no_agent,
    budget: HandlerBudget | None = None,
) -> HandlerResult:
    """Compile and run ``script`` in Monty with every rail enforced.

    Effects are not transactional: on a cap breach or error mid-script, whatever
    was already emitted stays. We stop, capture the cause, and always return a
    :class:`HandlerResult` for the durable run record — never raise to the caller.
    """
    budget = budget or HandlerBudget()
    execution = HandlerExecution(
        channel_id=channel_id,
        guild_id=guild_id,
        budget=budget,
        emitter=emitter,
        limiter=limiter,
        agent_runner=agent_runner,
    )
    started = time.monotonic()

    def _result(outcome: str, error: str | None = None, cap: str | None = None):
        return HandlerResult(
            outcome=outcome,
            usage=budget.usage(),
            duration_ms=int((time.monotonic() - started) * 1000),
            error=error,
            cap=cap,
        )

    try:
        compiled = monty.Monty(script, inputs=["context"], type_check=False)
    except monty.MontyError as exc:
        return _result("error", error=f"compile: {type(exc).__name__}: {exc}")

    limits: dict[str, Any] = {
        "max_memory": SANDBOX_MAX_MEMORY,
        "max_recursion_depth": SANDBOX_MAX_RECURSION_DEPTH,
        "max_duration_secs": budget.wall_clock_seconds + _DURATION_SLACK_SECONDS,
    }
    try:
        await compiled.run_async(
            inputs={"context": context},
            limits=limits,
            external_functions=execution.external_functions(),
        )
    except CapExceeded as exc:  # defensive: a direct (non-sandbox) breach
        logger.info("handler hit cap %s (channel=%s)", exc.cap, channel_id)
        return _result("cap_exceeded", error=str(exc), cap=exc.cap)
    except monty.MontyError as exc:
        if execution.breach is not None:
            breach = execution.breach
            logger.info("handler hit cap %s (channel=%s)", breach.cap, channel_id)
            return _result("cap_exceeded", error=str(breach), cap=breach.cap)
        return _result("error", error=f"runtime: {type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001 — never let a fire crash the worker
        logger.exception("handler script crashed (channel=%s)", channel_id)
        return _result("error", error=f"{type(exc).__name__}: {exc}")

    return _result("ok")
