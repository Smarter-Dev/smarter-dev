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
- ``memory_get(key, default=None)`` / ``memory_set(key, value)`` /
  ``memory_all()`` / ``memory_delete(key)`` -> persistent per-handler key/value
  store that survives across fires.

Script-facing data (Monty input): ``context`` — a plain dict describing the
trigger (its keys depend on ``context["trigger_type"]``).

The wall clock is available: scripts may call ``datetime.datetime.now(tz)`` and
``datetime.date.today()`` (wired through ``_clock_os``). Every other OS surface —
filesystem, env — stays blocked.

Gathering is agent-only: the script itself has no web access. A spawned agent
returns plaintext and cannot emit. The budget bounds both the agent's *input*
(``enforce_agent_context``, regardless of ``has_tools``) and any searches/reads
it performs (shared with the script's pool).
"""

from __future__ import annotations

import datetime
import logging
import random
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
from smarter_dev.web.handler_memory import HandlerMemory
from smarter_dev.web.admin_actions import AdminActor

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


def _clock_os(
    function_name: str, args: tuple[Any, ...], kwargs: dict[str, Any] | None = None
) -> Any:
    """Monty ``os=`` callback that grants ONLY the wall-clock functions.

    Monty treats ``datetime.now(tz)`` and ``date.today()`` as OS calls (they are
    non-deterministic, so the sandbox refuses them — ``datetime.now`` is "not
    implemented" — unless a host opts in). We answer just those two from the real
    host clock and return ``NOT_HANDLED`` for everything else, so filesystem, env,
    and every other OS surface stay blocked exactly as before. Scripts get the
    natural ``datetime.datetime.now(datetime.timezone.utc)`` with no special API.
    """
    if function_name == "datetime.now":
        tz = args[0] if args else None
        return datetime.datetime.now(tz=tz)
    if function_name == "date.today":
        return datetime.date.today()
    return monty.NOT_HANDLED


def _random_functions() -> dict[str, Callable[..., Any]]:
    """Randomness as flat top-level globals, since Monty blocks ``import random``.

    Exposed as plain callables (Monty can only inject flat external functions, not
    a dotted ``random.`` namespace). These are pure compute — no budget cost — and
    return new values rather than mutating in place (``shuffled``/``sample``),
    which keeps them correct across the sandbox boundary.
    """
    return {
        "randint": lambda a, b: random.randint(int(a), int(b)),
        "randrange": lambda a, b=None: (
            random.randrange(int(a)) if b is None else random.randrange(int(a), int(b))
        ),
        "randfloat": random.random,
        "uniform": lambda a, b: random.uniform(float(a), float(b)),
        "choice": lambda seq: random.choice(list(seq)),
        "shuffled": lambda seq: random.sample(list(seq), len(list(seq))),
        "sample": lambda seq, k: random.sample(list(seq), int(k)),
    }


@dataclass
class HandlerResult:
    """Outcome of one handler firing, for the durable run record."""

    outcome: str  # "ok" | "cap_exceeded" | "error"
    usage: dict[str, int]
    duration_ms: int
    error: str | None = None
    cap: str | None = None
    # The handler's memory after the fire, and whether the script changed it.
    # The caller persists ``memory`` back to the handler row only when ``memory_changed``.
    memory: dict[str, Any] = field(default_factory=dict)
    memory_changed: bool = False


@dataclass
class HandlerExecution:
    """Binds a single fire's budget + emitter + limiter into external functions."""

    channel_id: str
    guild_id: str
    budget: HandlerBudget
    emitter: DiscordEmitter
    limiter: WindowedLimiter
    agent_runner: AgentRunner = _no_agent
    # Set only for admin handlers; presence enables the moderation functions and
    # lets send_message target any channel.
    actor: AdminActor | None = None
    # Per-handler persistent key/value store, loaded before the fire. The host
    # owns it; the script reads/writes via the memory_* external functions.
    memory: HandlerMemory = field(default_factory=HandlerMemory)
    # The cap that stopped this fire, captured here because Monty rewraps the
    # exception that crosses out of an external function (losing its type), so
    # we record the real CapExceeded on the host side before re-raising.
    breach: CapExceeded | None = None

    def external_functions(self) -> dict[str, Callable[..., Any]]:
        funcs = {
            "send_message": self._guard(self._send_message),
            "add_reaction": self._guard(self._add_reaction),
            "post_voice": self._guard(self._post_voice),
            "spawn_agent": self._guard(self._spawn_agent),
            "memory_get": self._guard(self._memory_get),
            "memory_set": self._guard(self._memory_set),
            "memory_all": self._guard(self._memory_all),
            "memory_delete": self._guard(self._memory_delete),
        }
        # Randomness as flat globals (Monty can't `import random`); pure compute.
        funcs.update(_random_functions())
        if self.actor is not None:  # admin handler — moderation powers
            funcs.update(
                {
                    "delete_message": self._guard(self._delete_message),
                    "ban_user": self._guard(self._ban_user),
                    "kick_user": self._guard(self._kick_user),
                    "timeout_user": self._guard(self._timeout_user),
                }
            )
        return funcs

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

    async def _send_message(self, content: str, channel_id: str | None = None) -> str:
        target = str(channel_id) if channel_id else self.channel_id
        # Standard handlers may only post to their own channel; admin handlers
        # (actor set) may post anywhere in the guild (e.g. mod-chat).
        if self.actor is None and target != self.channel_id:
            raise CapExceeded(
                "cross_channel_send",
                "standard handlers can only post to their own channel",
            )
        self.budget.spend_message()
        within = await self.limiter.hit(
            channel_message_key(target), CHANNEL_MESSAGES_PER_MIN
        )
        if not within:
            raise CapExceeded(
                "channel_messages_per_min",
                f"channel hit its {CHANNEL_MESSAGES_PER_MIN} messages/min cap",
            )
        return await self.emitter.create_message(target, str(content))

    # -- admin-only moderation functions (only present when self.actor is set) --

    async def _delete_message(self, message_id: str, channel_id: str | None = None) -> str:
        self.budget.spend_mod_action()
        target = str(channel_id) if channel_id else self.channel_id
        return await self.actor.delete_message(target, str(message_id))

    async def _ban_user(self, user_id: str, reason: str | None = None) -> str:
        self.budget.spend_mod_action()
        return await self.actor.ban_user(str(user_id), reason)

    async def _kick_user(self, user_id: str) -> str:
        self.budget.spend_mod_action()
        return await self.actor.kick_user(str(user_id))

    async def _timeout_user(self, user_id: str, duration_seconds: int = 600) -> str:
        self.budget.spend_mod_action()
        return await self.actor.timeout_user(str(user_id), int(duration_seconds))

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

    # -- persistent per-handler memory (survives across fires) --

    async def _memory_get(self, key: str, default: Any = None) -> Any:
        return self.memory.get(str(key), default)

    async def _memory_set(self, key: str, value: Any) -> bool:
        return self.memory.set(str(key), value)

    async def _memory_all(self) -> dict[str, Any]:
        return self.memory.all()

    async def _memory_delete(self, key: str) -> bool:
        return self.memory.delete(str(key))


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
    actor: AdminActor | None = None,
    memory: dict[str, Any] | None = None,
) -> HandlerResult:
    """Compile and run ``script`` in Monty with every rail enforced.

    Effects are not transactional: on a cap breach or error mid-script, whatever
    was already emitted stays. We stop, capture the cause, and always return a
    :class:`HandlerResult` for the durable run record — never raise to the caller.

    Passing ``actor`` enables the admin moderation functions (and lets
    send_message target any channel). ``memory`` seeds the handler's persistent
    store; the result carries it back (with ``memory_changed``) for the caller to
    persist regardless of outcome — a counter bumped before a later failure is
    not lost, matching the "emitted effects stay" rule.
    """
    budget = budget or HandlerBudget()
    handler_memory = HandlerMemory(memory or {})
    execution = HandlerExecution(
        channel_id=channel_id,
        guild_id=guild_id,
        budget=budget,
        emitter=emitter,
        limiter=limiter,
        agent_runner=agent_runner,
        actor=actor,
        memory=handler_memory,
    )
    started = time.monotonic()

    def _result(outcome: str, error: str | None = None, cap: str | None = None):
        return HandlerResult(
            outcome=outcome,
            usage=budget.usage(),
            duration_ms=int((time.monotonic() - started) * 1000),
            error=error,
            cap=cap,
            memory=handler_memory.snapshot(),
            memory_changed=handler_memory.dirty,
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
            os=_clock_os,
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
