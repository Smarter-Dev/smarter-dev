"""The handler runtime — runs an approved script in Monty under all the rails.

This is the hot path. One :class:`~smarter_dev.web.handler_budget.HandlerBudget`
is created per fire and threaded into the script's external functions *and* into
every agent the script spawns, so all caps draw from one shared pool. The script
can reach Discord only through the metered external functions here — that single
chokepoint is what makes "code is the only emitter" and "agents only gather"
true in practice.

Script-facing surface (Monty external functions):
- ``send_message(content)`` -> message id (may also target a thread of the
  handler's home channel; admin handlers may target any channel in the guild)
- ``add_reaction(message_id, emoji)`` -> True
- ``post_voice(text)`` -> True
- ``spawn_agent(prompt, has_tools=False)`` -> plaintext str
- ``list_threads(channel_id=None)`` -> list[dict] of the channel's threads
  (home channel when omitted; a foreign channel needs an admin handler). Spends
  the discord_reads budget; a gone channel yields ``[]`` without erroring.
- ``create_thread(name, message_id=None)`` -> thread id and
  ``create_post(title, content, tag_names=None)`` -> thread id, both on the home
  channel, spending the message budget + channel window like ``send_message``.
- ``memory_get(key, default=None)`` / ``memory_set(key, value)`` /
  ``memory_all()`` / ``memory_delete(key)`` -> persistent per-handler key/value
  store that survives across fires.

Admin handlers (``actor`` set) additionally get ``close_thread`` /
``lock_thread`` / ``reopen_thread`` / ``delete_thread`` -> bool (each spends the
thread_ops budget + guild thread-op window before the REST call; a gone thread
yields ``False`` without erroring the fire).

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
    GUILD_THREAD_OPS_PER_MIN,
    WindowedLimiter,
    channel_message_key,
    global_agent_key,
    guild_thread_ops_key,
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
    # The admin handler's channel scope. Empty means guild-wide; a non-empty
    # scope bounds which channels admin list_threads(channel_id) may read.
    channel_ids: list[str] = field(default_factory=list)
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
    # Per-execution cache of a thread's parent channel id, so the send_message
    # home-thread relaxation verifies each target once per fire. This is a host
    # rail (one cached fetch), NOT metered against discord_reads.
    _thread_parent_cache: dict[str, str | None] = field(default_factory=dict)

    def external_functions(self) -> dict[str, Callable[..., Any]]:
        funcs = {
            "send_message": self._guard(self._send_message),
            "add_reaction": self._guard(self._add_reaction),
            "post_voice": self._guard(self._post_voice),
            "spawn_agent": self._guard(self._spawn_agent),
            "list_threads": self._guard(self._list_threads),
            "create_thread": self._guard(self._create_thread),
            "create_post": self._guard(self._create_post),
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
                    "close_thread": self._guard(self._close_thread),
                    "lock_thread": self._guard(self._lock_thread),
                    "reopen_thread": self._guard(self._reopen_thread),
                    "delete_thread": self._guard(self._delete_thread),
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
        # Standard handlers may only post to their own channel or a thread OF
        # that channel; admin handlers (actor set) may post anywhere in the guild
        # (e.g. mod-chat). The thread-of-home verdict is cached per fire so
        # repeated sends into the same thread don't re-fetch its parent.
        if self.actor is None and target != self.channel_id:
            if not await self._is_home_channel_thread(target):
                raise CapExceeded(
                    "cross_channel_send",
                    "standard handlers can only post to their own channel "
                    "or a thread of it",
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

    async def _is_home_channel_thread(self, channel_id: str) -> bool:
        """Whether ``channel_id`` is a thread whose parent is the home channel.

        Host rail behind the ``send_message`` relaxation: cached per execution so
        repeated sends into the same thread cost one parent fetch, and NOT metered
        against the ``discord_reads`` budget.
        """
        if channel_id not in self._thread_parent_cache:
            self._thread_parent_cache[channel_id] = (
                await self.emitter.get_thread_parent_id(channel_id)
            )
        return self._thread_parent_cache[channel_id] == self.channel_id

    # -- thread reads/creates (both tiers, home-channel scoped by default) --

    async def _list_threads(self, channel_id: str | None = None) -> list[dict]:
        """Active + recently-archived threads of a channel; spends a discord read.

        ``channel_id`` omitted (or falsy) -> the handler's HOME channel, allowed
        for both tiers. A foreign ``channel_id`` requires an admin handler (actor
        set); a standard handler naming another channel raises the same
        ``cross_channel_send`` cap ``send_message`` uses. For an admin handler the
        target must be within its ``channel_ids`` scope (empty scope = guild-wide,
        and the runtime still verifies the channel belongs to this guild so a
        foreign-guild id can't leak archived threads) — otherwise
        ``out_of_scope_channel`` is raised. The scope rail is a host check (not
        metered) and runs before the read is spent. A gone channel (404) flows
        through from the emitter as ``[]`` and does NOT error the fire.
        """
        target = str(channel_id) if channel_id else self.channel_id
        if target != self.channel_id:
            if self.actor is None:
                raise CapExceeded(
                    "cross_channel_send",
                    "standard handlers can only list threads of their own channel",
                )
            if not await self._admin_channel_in_scope(target):
                raise CapExceeded(
                    "out_of_scope_channel",
                    "admin list_threads target is outside the handler's channel "
                    "scope / guild",
                )
        self.budget.spend_discord_read()
        return await self.emitter.list_threads(target)

    async def _admin_channel_in_scope(self, channel_id: str) -> bool:
        """Whether an admin handler may read ``channel_id``'s threads.

        A non-empty ``channel_ids`` scope is an exact allow-list (pure check). An
        empty scope is guild-wide, so the channel is verified to belong to this
        fire's guild via one host fetch (NOT metered against discord_reads).
        """
        if self.channel_ids:
            return channel_id in self.channel_ids
        return await self.emitter.get_channel_guild_id(channel_id) == self.guild_id

    async def _create_thread(self, name: str, message_id: str | None = None) -> str:
        """Create a thread on the HOME channel; return its id.

        A create is an emit: it spends the message budget and the home channel's
        per-minute message window exactly like ``send_message``. Raises on any
        REST failure (a create has no sensible falsy return).
        """
        await self._spend_home_channel_emit()
        message = str(message_id) if message_id else None
        return await self.emitter.create_thread(self.channel_id, str(name), message)

    async def _create_post(
        self, title: str, content: str, tag_names: list[str] | None = None
    ) -> str:
        """Create a forum post on the HOME channel; return its thread id.

        Spends the message budget and the home channel's message window like
        ``send_message``. An unknown forum tag name raises ``ValueError`` from the
        emitter (fail fast); any REST failure raises (no falsy return for a create).
        """
        await self._spend_home_channel_emit()
        return await self.emitter.create_post(
            self.channel_id, str(title), str(content), tag_names
        )

    async def _spend_home_channel_emit(self) -> None:
        """Meter a thread create (create_thread/create_post): the message budget
        and the channel's per-minute message window (matching ``send_message``),
        then the guild thread-op window — creation is a mutating thread op and
        must not escape the guild ceiling just because it spends the message
        budget rather than the thread_ops counter (spec §5.3)."""
        self.budget.spend_message()
        within = await self.limiter.hit(
            channel_message_key(self.channel_id), CHANNEL_MESSAGES_PER_MIN
        )
        if not within:
            raise CapExceeded(
                "channel_messages_per_min",
                f"channel hit its {CHANNEL_MESSAGES_PER_MIN} messages/min cap",
            )
        await self._hit_guild_thread_ops_window()

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

    # -- admin-only thread mutations (only present when self.actor is set) --

    async def _close_thread(self, thread_id: str) -> bool:
        """Archive a thread. A gone thread (404) is a silent no-op -> False."""
        await self._spend_thread_op()
        return await self.actor.close_thread(str(thread_id))

    async def _lock_thread(self, thread_id: str) -> bool:
        """Lock and archive a thread. A gone thread (404) -> False."""
        await self._spend_thread_op()
        return await self.actor.lock_thread(str(thread_id))

    async def _reopen_thread(self, thread_id: str) -> bool:
        """Unarchive a thread. A gone thread (404) -> False."""
        await self._spend_thread_op()
        return await self.actor.reopen_thread(str(thread_id))

    async def _delete_thread(self, thread_id: str) -> bool:
        """Delete a thread. A gone thread (404) is a silent no-op -> False."""
        await self._spend_thread_op()
        return await self.actor.delete_thread(str(thread_id))

    async def _spend_thread_op(self) -> None:
        """Meter a mutating thread op (close/lock/reopen/delete): per-fire
        thread_ops budget then the guild thread-op window, before the REST call —
        a breach of either fails the fire mid-flight, the same shape the
        channel-message window uses."""
        self.budget.spend_thread_op()
        await self._hit_guild_thread_ops_window()

    async def _hit_guild_thread_ops_window(self) -> None:
        """Charge the guild thread-op window, raising ``CapExceeded`` on breach.

        Shared by every mutating thread op (create/close/lock/reopen/delete) so
        all six draw the one guild ceiling (spec §5.3)."""
        within = await self.limiter.hit(
            guild_thread_ops_key(self.guild_id), GUILD_THREAD_OPS_PER_MIN
        )
        if not within:
            raise CapExceeded(
                "guild_thread_ops_per_min",
                f"guild hit its {GUILD_THREAD_OPS_PER_MIN} thread-ops/min cap",
            )

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
    channel_ids: list[str] | None = None,
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
        channel_ids=list(channel_ids or []),
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
