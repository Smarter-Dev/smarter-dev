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
- ``get_guild_member_count()`` -> int — the guild's approximate total member
  count (worker-side REST ``with_counts`` read, both tiers). Spends the
  discord_reads budget; callable from schedule/timer fires (no gateway needed).
- ``create_thread(name, message_id=None)`` -> thread id and
  ``create_post(title, content, tag_names=None)`` -> thread id, both on the home
  channel, spending the message budget + channel window like ``send_message``.
- ``memory_get(key, default=None)`` / ``memory_set(key, value)`` /
  ``memory_all()`` / ``memory_delete(key)`` -> persistent per-handler key/value
  store that survives across fires.
- ``schedule_timer(delay_seconds, payload)`` -> True — durably arm a ONE-SHOT
  re-fire of THIS handler at ``now + delay_seconds`` (both tiers). The re-fire
  survives worker restarts (it rides the same job store recurring schedules do)
  and runs the same script with context ``{"trigger_type": "timer", "payload":
  payload, "scheduled_at": "<ISO armed>"}`` — so a message/member handler can
  serve its own delayed follow-ups by branching on ``context["trigger_type"]``.
  ``delay_seconds`` is clamped to ``[60, 30*86400]`` (out of bounds raises,
  failing the fire), ``payload`` must be JSON-serializable and ≤4 KB, and each
  fire may arm at most ``max_timers`` (2 standard / 5 admin), with a per-handler
  30/hour arming window across fires.

Admin handlers (``actor`` set) additionally get ``edit_message(message_id,
content, channel_id=None)`` -> message id (edits a bot-authored message in place,
spending the message budget like ``add_reaction`` but not the channel window; a
non-bot message is a REST 403 that errors the fire) and ``rename_channel(
channel_id, name)`` -> bool (spends a mod_action, checks the same channel scope
as admin ``list_threads``, and charges a per-channel 2-per-600s rename window —
Discord's hard limit — before the REST call), plus ``close_thread`` /
``lock_thread`` / ``reopen_thread`` / ``delete_thread`` -> bool (each spends the
thread_ops budget + guild thread-op window before the REST call; a gone thread
yields ``False`` without erroring the fire), plus ``add_role(user_id, role_id,
reason=None)`` / ``remove_role(user_id, role_id, reason=None)`` -> bool (the
role_id must be in the handler's host-owned ``allowed_role_ids`` allowlist —
otherwise ``CapExceeded("role_not_allowed")`` before any budget/window/REST; then
spends the role_changes budget + guild role-change window before the REST call; a
gone member yields ``False`` without erroring the fire), plus ``ban_user(user_id,
reason=None, delete_message_seconds=0)`` which can purge the banned member's
recent messages, plus ``send_dm(user_id, content)`` -> bool (DM a user — the most
abuse-sensitive emit, so its own cap family: spends the shared per-fire message
pool, a per-recipient 30/hour window, and a global 10/min window before the REST
call; returns ``False`` on closed DMs / no mutual guild / unknown user without
erroring the fire, so a relay script branches to ❌), plus ``guild_memory_get`` /
``guild_memory_set`` / ``guild_memory_all`` / ``guild_memory_delete`` — a
guild-scoped key/value store SHARED by every admin handler in the guild (for
state that must cross handler rows, e.g. a DM-relay bind target), bounded by the
same 16KB cap as per-handler ``memory_*`` and DB-only (spends nothing), plus
``delete_webhook(webhook_url)`` -> bool (DELETE a leaked Discord webhook URL —
the actor rejects any non-Discord URL host-side before the REST call; spends a
mod_action; False on 404), plus the mod-audit reads ``list_mod_actions(user_id,
limit=10)`` -> list[dict] (this guild's recent actions for a member, newest
first, via an injected DB reader), ``get_member_info(user_id)`` -> dict (member
profile; a departed user comes back with ``in_guild=False``), and
``search_guild_members(query, limit=10)`` -> dict (prefix name/nick search with a
per-row top role and an overflow count) — each of the three spends the lookups
budget.

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
import json
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
    DM_USER_WINDOW_SECONDS,
    DMS_PER_USER_PER_HOUR,
    GLOBAL_AGENT_CALLS_PER_MIN,
    GLOBAL_DMS_PER_MIN,
    GUILD_ROLE_CHANGES_PER_MIN,
    GUILD_THREAD_OPS_PER_MIN,
    HANDLER_TIMERS_PER_HOUR,
    RENAME_WINDOW_SECONDS,
    TIMER_ARMING_WINDOW_SECONDS,
    RENAMES_PER_WINDOW,
    WindowedLimiter,
    channel_message_key,
    channel_rename_key,
    dm_user_key,
    global_agent_key,
    global_dm_key,
    guild_role_changes_key,
    guild_thread_ops_key,
    handler_timer_arm_key,
)
from smarter_dev.web.handler_schedule import validate_timer_delay
from smarter_dev.web.handler_emitter import DiscordEmitter
from smarter_dev.web.handler_guild_memory import GuildMemory
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

# A script-armed timer's payload rides the durable job store as JSON, so it is
# bounded like HandlerMemory.set: an over-cap payload is a metered breach.
TIMER_PAYLOAD_MAX_BYTES = 4 * 1024

# An async function (prompt, has_tools, budget) -> plaintext. Injectable so
# tests can run the runtime fully offline.
AgentRunner = Callable[[str, bool, HandlerBudget], Awaitable[str]]

# An async function (fire_at, refire_context) -> None that durably enqueues a
# one-shot re-fire of THIS handler. Injected by the fire job (which owns the
# payload class and handler_id), keeping the runtime import-clean of the worker
# submit machinery — the same discipline as AgentRunner.
TimerScheduler = Callable[[datetime.datetime, dict[str, Any]], Awaitable[None]]

# An async function (target_user_id, limit) -> list of mod-action rows for the
# fire's guild, newest first. Injected by the admin fire job, which owns the DB
# session context and binds the guild id host-side (so a script can never read
# another guild's mod history) — the same injection discipline as AgentRunner.
ModActionReader = Callable[[str, int], Awaitable[list[dict[str, Any]]]]


async def _no_agent(prompt: str, has_tools: bool, budget: HandlerBudget) -> str:
    raise RuntimeError("no agent runner configured for this handler execution")


async def _no_timer(fire_at: datetime.datetime, refire_context: dict[str, Any]) -> None:
    raise RuntimeError("no timer scheduler configured for this handler execution")


async def _no_mod_action_reader(user_id: str, limit: int) -> list[dict[str, Any]]:
    raise RuntimeError("no mod-action reader configured for this handler execution")


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
    # Guild-shared memory changes this fire made, for the caller to persist per
    # key (only when ``guild_memory_changed``). Writes are upserted, deletes are
    # removed — untouched keys are never rewritten, keeping concurrent
    # different-key fires from clobbering each other.
    guild_memory_writes: dict[str, Any] = field(default_factory=dict)
    guild_memory_deletes: list[str] = field(default_factory=list)
    guild_memory_changed: bool = False


@dataclass
class HandlerExecution:
    """Binds a single fire's budget + emitter + limiter into external functions."""

    channel_id: str
    guild_id: str
    budget: HandlerBudget
    emitter: DiscordEmitter
    limiter: WindowedLimiter
    agent_runner: AgentRunner = _no_agent
    # DB-backed reader for list_mod_actions, injected by the admin fire job (which
    # binds the fire's guild id host-side). Admin handlers only; standard fires
    # never construct list_mod_actions so this stays the raising default.
    mod_action_reader: ModActionReader = _no_mod_action_reader
    # This handler's id — needed only for the per-handler timer-arming window key.
    # Optional (default "") so existing callers that never arm a timer are unaffected.
    handler_id: str = ""
    # Durable one-shot re-fire enqueuer for schedule_timer, injected by the fire
    # job (default _no_timer raises loud if schedule_timer is called unwired).
    timer_scheduler: TimerScheduler = _no_timer
    # Separate 3600s-window limiter for the timer-arming rate cap; self.limiter is
    # fixed at 60s. Falls back to self.limiter (with a per-call window override) in
    # run_handler_script when the fire job doesn't inject a dedicated one.
    timer_limiter: WindowedLimiter | None = None
    # Separate 3600s-window limiter for send_dm's per-recipient HOUR cap;
    # self.limiter is fixed at 60s (which carries the global per-minute DM cap).
    # Admin-only; standard fires never construct send_dm so this stays unused.
    # Falls back to self.limiter with a per-call window override when unset.
    dm_user_limiter: WindowedLimiter | None = None
    # The admin handler's channel scope. Empty means guild-wide; a non-empty
    # scope bounds which channels admin list_threads(channel_id) may read.
    channel_ids: list[str] = field(default_factory=list)
    # Host-owned allowlist of grantable role ids for add_role/remove_role. This
    # is config read before the fire, NEVER script-mutable. Fail-closed: a role
    # not in this list is rejected before any budget/window/REST spend, and an
    # empty/absent allowlist means NO role is grantable (unlike channel_ids,
    # where empty = guild-wide — role grants are higher-privilege, so empty=deny).
    allowed_role_ids: list[str] = field(default_factory=list)
    # Set only for admin handlers; presence enables the moderation functions and
    # lets send_message target any channel.
    actor: AdminActor | None = None
    # Per-handler persistent key/value store, loaded before the fire. The host
    # owns it; the script reads/writes via the memory_* external functions.
    memory: HandlerMemory = field(default_factory=HandlerMemory)
    # Guild-shared key/value store (admin handlers only), loaded before the fire.
    # Read/written via the guild_memory_* functions; persisted per changed key.
    guild_memory: GuildMemory = field(default_factory=GuildMemory)
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
            # Both tiers: a cheap read-only guild-count read, usable from a
            # schedule fire (worker-side REST, no gateway cache required).
            "get_guild_member_count": self._guard(self._get_guild_member_count),
            "create_thread": self._guard(self._create_thread),
            "create_post": self._guard(self._create_post),
            "memory_get": self._guard(self._memory_get),
            "memory_set": self._guard(self._memory_set),
            "memory_all": self._guard(self._memory_all),
            "memory_delete": self._guard(self._memory_delete),
            # Available to BOTH tiers: a standard message/schedule/timer handler
            # can legitimately self-defer (E3).
            "schedule_timer": self._guard(self._schedule_timer),
        }
        # Randomness as flat globals (Monty can't `import random`); pure compute.
        funcs.update(_random_functions())
        if self.actor is not None:  # admin handler — moderation powers
            funcs.update(
                {
                    "delete_message": self._guard(self._delete_message),
                    "delete_webhook": self._guard(self._delete_webhook),
                    "edit_message": self._guard(self._edit_message),
                    "rename_channel": self._guard(self._rename_channel),
                    "ban_user": self._guard(self._ban_user),
                    "kick_user": self._guard(self._kick_user),
                    "timeout_user": self._guard(self._timeout_user),
                    "add_role": self._guard(self._add_role),
                    "remove_role": self._guard(self._remove_role),
                    "send_dm": self._guard(self._send_dm),
                    # Mod-audit reads (each spends a lookup): the mod-channel
                    # lookup/history/whois commands and the rejoin alert.
                    "list_mod_actions": self._guard(self._list_mod_actions),
                    "get_member_info": self._guard(self._get_member_info),
                    "search_guild_members": self._guard(self._search_guild_members),
                    "close_thread": self._guard(self._close_thread),
                    "lock_thread": self._guard(self._lock_thread),
                    "reopen_thread": self._guard(self._reopen_thread),
                    "delete_thread": self._guard(self._delete_thread),
                    "guild_memory_get": self._guard(self._guild_memory_get),
                    "guild_memory_set": self._guard(self._guild_memory_set),
                    "guild_memory_all": self._guard(self._guild_memory_all),
                    "guild_memory_delete": self._guard(self._guild_memory_delete),
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

    async def _send_message(
        self, content: str, channel_id: str | None = None, ping_role_id: str | None = None
    ) -> str:
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
        # ping_role_id is a mod-escalation rail: forwarded ONLY for admin handlers
        # (actor set). A standard handler's ping_role_id is silently dropped so
        # the emitter's mention-suppressing default stands (fail-safe).
        role_ping = str(ping_role_id) if (ping_role_id and self.actor is not None) else None
        return await self.emitter.create_message(target, str(content), role_ping)

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

    async def _get_guild_member_count(self) -> int:
        """Approximate total guild member count; spends one discord read.

        A worker-side REST read (``with_counts``) available to BOTH tiers and to
        schedule fires, which have no gateway cache. Metered on the shared
        discord_reads pool like ``list_threads``; a REST error propagates and
        errors the fire (no silent 0)."""
        self.budget.spend_discord_read()
        return await self.emitter.get_guild_member_count()

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

    async def _delete_webhook(self, webhook_url: str) -> bool:
        """DELETE a leaked webhook URL; return whether one was killed.

        Destructive, so it spends the mod_actions budget (the 25/fire admin cap
        covers a message with several leaked webhooks). The actor validates the
        URL host-side — a non-Discord URL raises before any REST call — and
        returns False on 404 (already dead), which the script branches on.
        """
        self.budget.spend_mod_action()
        return bool(await self.actor.delete_webhook(str(webhook_url)))

    # -- admin-only mod-audit reads (each spends a lookup) --

    async def _list_mod_actions(self, user_id: str, limit: int = 10) -> list[dict]:
        """Recent mod actions for a member in THIS guild, newest first.

        Backed by the injected DB reader, which binds the fire's guild id
        host-side (a script can never read another guild's history). Rows carry
        channel_id/trigger_message_id straight off the ModerationAction row (either
        may be None) so a script can build "Jump To Action" links."""
        self.budget.spend_lookup()
        return await self.mod_action_reader(str(user_id), int(limit))

    async def _get_member_info(self, user_id: str) -> dict:
        """Profile a member (or a departed user, in_guild=False); spends a lookup."""
        self.budget.spend_lookup()
        return await self.actor.get_member_info(str(user_id))

    async def _search_guild_members(self, query: str, limit: int = 10) -> dict:
        """Prefix-search guild members with per-row top role; spends ONE lookup
        covering the members/search + roles fetch pair."""
        self.budget.spend_lookup()
        return await self.actor.search_guild_members(str(query), int(limit))

    async def _edit_message(
        self, message_id: str, content: str, channel_id: str | None = None
    ) -> str:
        """Edit a bot-authored message in place; return its id.

        An edit is an emit: it spends the per-fire message budget (admin cap 5)
        but NOT the per-channel message window — like ``add_reaction``, it changes
        an existing message rather than adding channel volume. ``channel_id``
        defaults to the trigger channel, symmetric with ``delete_message``.
        Editing a message the bot doesn't own is a REST 403 that errors the fire
        loudly (bot-authored-only is enforced by Discord, not our bookkeeping).
        """
        self.budget.spend_message()
        target = str(channel_id) if channel_id else self.channel_id
        return await self.emitter.edit_message(target, str(message_id), str(content))

    async def _rename_channel(self, channel_id: str, name: str) -> bool:
        """Rename a channel in the handler's scope; return True.

        A rename is a guild mutation, so it spends the mod_actions budget; the
        target must be in the handler's ``channel_ids`` scope (empty scope =
        guild-wide, verified to belong to this fire's guild) exactly like admin
        ``list_threads``. Discord hard-limits renames to 2/10min per channel, so
        the runtime charges a per-channel 600s window before the REST call and
        raises ``CapExceeded("channel_renames_per_10min")`` on breach.
        """
        target = str(channel_id)
        if not await self._admin_channel_in_scope(target):
            raise CapExceeded(
                "out_of_scope_channel",
                "rename_channel target is outside the handler's channel scope / guild",
            )
        self.budget.spend_mod_action()
        within = await self.limiter.hit(
            channel_rename_key(target), RENAMES_PER_WINDOW, RENAME_WINDOW_SECONDS
        )
        if not within:
            raise CapExceeded(
                "channel_renames_per_10min",
                f"channel hit its {RENAMES_PER_WINDOW} renames/"
                f"{RENAME_WINDOW_SECONDS}s cap",
            )
        return await self.emitter.rename_channel(target, str(name))

    async def _ban_user(
        self,
        user_id: str,
        reason: str | None = None,
        delete_message_seconds: int = 0,
    ) -> str:
        # Bans stay in the mod pool; delete_message_seconds purges the banned
        # member's recent messages (onboarding auto-ban sweeps the last hour).
        self.budget.spend_mod_action()
        return await self.actor.ban_user(
            str(user_id), reason, int(delete_message_seconds)
        )

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

    # -- admin-only role mutation (only present when self.actor is set) --

    async def _add_role(
        self, user_id: str, role_id: str, reason: str | None = None
    ) -> bool:
        """Grant a role. A gone member (404) is a silent no-op -> False."""
        return await self._mutate_role("add", str(user_id), str(role_id), reason)

    async def _remove_role(
        self, user_id: str, role_id: str, reason: str | None = None
    ) -> bool:
        """Revoke a role. A gone member (404) is a silent no-op -> False."""
        return await self._mutate_role("remove", str(user_id), str(role_id), reason)

    async def _mutate_role(
        self, op: str, user_id: str, role_id: str, reason: str | None
    ) -> bool:
        """Enforce the allowlist, then meter, then perform a role grant/revoke.

        Ordering (matching ``_spend_thread_op``, allowlist first): (1) the
        host-owned ``allowed_role_ids`` allowlist [cheap, fail-closed — a role
        not on it raises ``CapExceeded("role_not_allowed")`` before ANY spend or
        REST call], (2) the per-fire role_changes budget, (3) the guild
        role-change window, (4) the actor REST call. A gone member (404) returns
        ``False`` from the actor without erroring the fire."""
        if role_id not in self.allowed_role_ids:
            raise CapExceeded(
                "role_not_allowed",
                f"role {role_id} is not in the handler's allowed_role_ids",
            )
        self.budget.spend_role_change()
        await self._hit_guild_role_changes_window()
        if op == "add":
            return await self.actor.add_role(user_id, role_id, reason)
        return await self.actor.remove_role(user_id, role_id, reason)

    async def _hit_guild_role_changes_window(self) -> None:
        """Charge the guild role-change window, raising ``CapExceeded`` on breach.

        Shape mirrors ``_hit_guild_thread_ops_window``: a breach fails the fire
        mid-flight with the cap name so a promotion burst degrades cleanly."""
        within = await self.limiter.hit(
            guild_role_changes_key(self.guild_id), GUILD_ROLE_CHANGES_PER_MIN
        )
        if not within:
            raise CapExceeded(
                "guild_role_changes_per_min",
                f"guild hit its {GUILD_ROLE_CHANGES_PER_MIN} role-changes/min cap",
            )

    # -- admin-only DM emit (only present when self.actor is set) --

    async def _send_dm(self, user_id: str, content: str) -> bool:
        """DM a user; return whether it was delivered.

        The most abuse-sensitive emit, so admin handlers only with its own cap
        family. Spends the shared per-fire message pool, then two windows before
        the REST call: a per-recipient HOUR window (``dm_user_limiter``, a 3600s
        instance) and the global per-minute window (``self.limiter``). The
        emitter returns ``False`` on 403 (DMs closed / no mutual guild) / 404
        (unknown user) — an *expected* outcome the script branches to ❌ on, NOT a
        cap breach and NOT an error. Cap breaches raise ``CapExceeded`` (rails,
        not branches)."""
        self.budget.spend_message()
        dm_limiter = self.dm_user_limiter or self.limiter
        within_user = await dm_limiter.hit(
            dm_user_key(str(user_id)), DMS_PER_USER_PER_HOUR, DM_USER_WINDOW_SECONDS
        )
        if not within_user:
            raise CapExceeded(
                "dm_user_per_hour",
                f"user hit its {DMS_PER_USER_PER_HOUR} DMs/hour cap",
            )
        within_global = await self.limiter.hit(global_dm_key(), GLOBAL_DMS_PER_MIN)
        if not within_global:
            raise CapExceeded(
                "global_dms_per_min",
                f"system hit its {GLOBAL_DMS_PER_MIN} DMs/min cap",
            )
        return bool(await self.emitter.send_dm(str(user_id), str(content)))

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

    # -- persisted one-shot self re-arm (schedule_timer, both tiers) --

    async def _schedule_timer(self, delay_seconds: int, payload: dict) -> bool:
        """Durably arm a one-shot re-fire of THIS handler at now + delay_seconds.

        The re-fire runs the same handler with context ``{"trigger_type": "timer",
        "payload": payload, "scheduled_at": "<ISO armed>"}`` so scripts branch on
        ``context["trigger_type"]``. Ordering charges the window and budget BEFORE
        the enqueue, so a denied arm never enqueues a job:

        1. delay bounds -> ScheduleError (a ValueError -> "error"; author bug,
           rejected not clamped, like the interval floor),
        2. payload JSON + 4 KB cap (non-JSON -> ValueError -> "error"; over-cap ->
           CapExceeded("timer_payload_size"), mirroring HandlerMemory.set),
        3. per-fire budget -> CapExceeded("timers"),
        4. per-handler arming window -> CapExceeded("handler_timers_per_hour"),
        5. the injected durable scheduler enqueues the re-fire.
        """
        delay = validate_timer_delay(delay_seconds)
        try:
            encoded = json.dumps(payload)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"timer payload must be JSON-serializable (str/int/float/bool/"
                f"None/list/dict): {exc}"
            ) from exc
        if len(encoded.encode("utf-8")) > TIMER_PAYLOAD_MAX_BYTES:
            raise CapExceeded(
                "timer_payload_size",
                f"timer payload would exceed {TIMER_PAYLOAD_MAX_BYTES} bytes",
            )
        self.budget.spend_timer()
        limiter = self.timer_limiter or self.limiter
        within = await limiter.hit(
            handler_timer_arm_key(self.handler_id),
            HANDLER_TIMERS_PER_HOUR,
            TIMER_ARMING_WINDOW_SECONDS,
        )
        if not within:
            raise CapExceeded(
                "handler_timers_per_hour",
                f"handler hit its {HANDLER_TIMERS_PER_HOUR} timers/hour arming cap",
            )
        now = datetime.datetime.now(datetime.timezone.utc)
        fire_at = now + datetime.timedelta(seconds=delay)
        refire_context = {
            "trigger_type": "timer",
            "payload": payload,
            "scheduled_at": now.isoformat(),
        }
        await self.timer_scheduler(fire_at, refire_context)
        return True

    # -- persistent per-handler memory (survives across fires) --

    async def _memory_get(self, key: str, default: Any = None) -> Any:
        return self.memory.get(str(key), default)

    async def _memory_set(self, key: str, value: Any) -> bool:
        return self.memory.set(str(key), value)

    async def _memory_all(self) -> dict[str, Any]:
        return self.memory.all()

    async def _memory_delete(self, key: str) -> bool:
        return self.memory.delete(str(key))

    # -- guild-shared memory (admin only; shared across the guild's handlers) --

    async def _guild_memory_get(self, key: str, default: Any = None) -> Any:
        return self.guild_memory.get(str(key), default)

    async def _guild_memory_set(self, key: str, value: Any) -> bool:
        return self.guild_memory.set(str(key), value)

    async def _guild_memory_all(self) -> dict[str, Any]:
        return self.guild_memory.all()

    async def _guild_memory_delete(self, key: str) -> bool:
        return self.guild_memory.delete(str(key))


async def run_handler_script(
    script: str,
    context: dict[str, Any],
    *,
    channel_id: str,
    guild_id: str,
    emitter: DiscordEmitter,
    limiter: WindowedLimiter,
    agent_runner: AgentRunner = _no_agent,
    mod_action_reader: ModActionReader = _no_mod_action_reader,
    handler_id: str = "",
    timer_scheduler: TimerScheduler = _no_timer,
    timer_limiter: WindowedLimiter | None = None,
    dm_user_limiter: WindowedLimiter | None = None,
    budget: HandlerBudget | None = None,
    actor: AdminActor | None = None,
    channel_ids: list[str] | None = None,
    allowed_role_ids: list[str] | None = None,
    memory: dict[str, Any] | None = None,
    guild_memory: dict[str, Any] | None = None,
) -> HandlerResult:
    """Compile and run ``script`` in Monty with every rail enforced.

    Effects are not transactional: on a cap breach or error mid-script, whatever
    was already emitted stays. We stop, capture the cause, and always return a
    :class:`HandlerResult` for the durable run record — never raise to the caller.

    Passing ``actor`` enables the admin moderation functions (and lets
    send_message target any channel). ``memory`` seeds the handler's persistent
    store; the result carries it back (with ``memory_changed``) for the caller to
    persist regardless of outcome — a counter bumped before a later failure is
    not lost, matching the "emitted effects stay" rule. ``guild_memory`` seeds the
    guild-shared store (admin handlers only); the result carries back the changed
    keys the same way (``guild_memory_writes`` / ``guild_memory_deletes`` /
    ``guild_memory_changed``).
    """
    budget = budget or HandlerBudget()
    handler_memory = HandlerMemory(memory or {})
    shared_guild_memory = GuildMemory(guild_memory or {})
    execution = HandlerExecution(
        channel_id=channel_id,
        guild_id=guild_id,
        budget=budget,
        emitter=emitter,
        limiter=limiter,
        agent_runner=agent_runner,
        mod_action_reader=mod_action_reader,
        handler_id=handler_id,
        timer_scheduler=timer_scheduler,
        timer_limiter=timer_limiter or limiter,
        dm_user_limiter=dm_user_limiter or limiter,
        actor=actor,
        channel_ids=list(channel_ids or []),
        allowed_role_ids=list(allowed_role_ids or []),
        memory=handler_memory,
        guild_memory=shared_guild_memory,
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
            guild_memory_writes=shared_guild_memory.writes(),
            guild_memory_deletes=shared_guild_memory.deletes(),
            guild_memory_changed=shared_guild_memory.dirty,
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
