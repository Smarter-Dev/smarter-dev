"""Keep the bot acting exactly once while a deploy overlaps two of them.

A deploy connects the new bot while the old one is still connected, so for a
while two gateway sessions receive every event. Without coordination both
would delete the same spam, time out the same member, answer the same command
and send the same scheduled message; and if the old one stopped before the
new one connected, nobody would.

Two mechanisms, both in the Redis the bot already uses:

- **Gateway events are claimed one by one.** Each connected bot fills its
  cache from every event as usual, but before running listeners it claims the
  event in Redis (``SET NX`` on a hash of the event's name and payload). Only
  the bot that wins runs the listeners, so there is no moment at which an
  event belongs to nobody, and no event runs twice. A stopping bot claims
  nothing new and finishes what it claimed. Should two sessions ever receive
  different payloads for one event, both run it: a duplicate, never a loss.
- **Timed sends are claimed too.** Scheduled, repeating, challenge and quest
  messages are queued by every connected bot and claimed (``claim``) at send
  time, so whichever bot is still alive sends each one, once.
- **Other background work follows a lease.** Loops that are not keyed sends
  run only on the leader: ``StartedEvent``, which starts most of them, is
  held back until the lease is won, and the remaining loops check
  ``is_leader()`` before each pass. On SIGTERM the old bot releases the lease
  and the new one takes it on its next poll; a pass skipped in between runs on
  the next tick.

Redis failures fail open: a bot that cannot claim an event runs it, and a bot
with no reachable leader takes the lease over once the outage outlasts it. A
Redis outage during a deploy can therefore duplicate events, but never
silences the bot.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import os
import time
import uuid
from collections.abc import Awaitable
from collections.abc import Callable
from typing import Any

import hikari
from hikari.events import shard_events

logger = logging.getLogger(__name__)

LEADER_KEY = "smarter-dev:bot:leader"
CLAIM_PREFIX = "smarter-dev:bot:claim"
CLAIM_TTL_MS = 10 * 60 * 1000

# Only the holder may extend or delete the lease; a bot that lost it must not
# clobber the new holder's. A leader whose lease lapsed with nobody taking it
# (a pause, or a Redis outage it led through) takes it back.
_RENEW = """
local holder = redis.call('get', KEYS[1])
if holder == ARGV[1] then
    return redis.call('pexpire', KEYS[1], ARGV[2])
end
if not holder then
    redis.call('set', KEYS[1], ARGV[1], 'PX', ARGV[2])
    return 1
end
return 0
"""
_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""

_current: Leadership | None = None


def is_leader() -> bool:
    """Whether this process should act. True when no lease is in use."""
    return _current is None or _current.is_leader


async def claim(key: str, ttl_ms: int | None = None) -> bool:
    """Whether this bot should do the one-off action ``key``; first caller wins.

    For work every connected bot would otherwise do, such as sending a
    scheduled message at its time. True when no lease is in use, and when
    Redis fails (duplicates beat silence).
    """
    return _current is None or await _current.claim(key, ttl_ms)


def install(leadership: Leadership | None) -> None:
    global _current
    _current = leadership


def holder_id() -> str:
    return f"{os.environ.get('HOSTNAME', 'local')}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class Leadership:
    """Hold, renew and release the leader lease in Redis."""

    def __init__(
        self,
        redis: Any,
        holder: str,
        *,
        ttl: float = 15.0,
        renew_interval: float = 5.0,
        poll_interval: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._redis = redis
        self.holder = holder
        self._ttl_ms = int(ttl * 1000)
        self._ttl = ttl
        self._renew_interval = renew_interval
        self._poll_interval = poll_interval
        self._clock = clock
        self.is_leader = False
        self._stepping_down = False
        self._redis_ok_at = clock()

    async def run(self, on_acquire: Callable[[], Awaitable[None]]) -> None:
        """Contend for the lease until ``step_down``; call ``on_acquire`` on each win."""
        self._redis_ok_at = self._clock()
        while not self._stepping_down:
            try:
                if self.is_leader:
                    await self._renew()
                elif await self._redis.set(
                    LEADER_KEY, self.holder, nx=True, px=self._ttl_ms
                ):
                    await self._become_leader(on_acquire, "took the lease")
                self._redis_ok_at = self._clock()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - any Redis failure
                outage = self._clock() - self._redis_ok_at
                if not self.is_leader and outage > self._ttl:
                    await self._become_leader(
                        on_acquire, f"Redis unreachable for {outage:.0f}s ({error})"
                    )
                else:
                    logger.warning("leader lease check failed: %s", error)
            await asyncio.sleep(
                self._renew_interval if self.is_leader else self._poll_interval
            )

    async def _renew(self) -> None:
        if not await self._redis.eval(_RENEW, 1, LEADER_KEY, self.holder, self._ttl_ms):
            self.is_leader = False
            logger.error("lost the leader lease to another bot; standing by")

    async def _become_leader(
        self, on_acquire: Callable[[], Awaitable[None]], reason: str
    ) -> None:
        if self._stepping_down:
            return
        self.is_leader = True
        logger.info("leader: %s (%s)", reason, self.holder)
        try:
            await on_acquire()
        except Exception:
            logger.exception("leader start-up hook failed")

    async def claim(self, key: str, ttl_ms: int | None = None) -> bool:
        try:
            return bool(
                await self._redis.set(
                    f"{CLAIM_PREFIX}:{key}",
                    self.holder,
                    nx=True,
                    px=ttl_ms or CLAIM_TTL_MS,
                )
            )
        except Exception as error:  # noqa: BLE001 - fail open
            logger.warning("claim for %s failed, acting anyway: %s", key, error)
            return True

    async def step_down(self) -> None:
        """Stop acting now and hand the lease to whoever polls next."""
        self._stepping_down = True
        was_leader = self.is_leader
        self.is_leader = False
        if not was_leader:
            return
        try:
            await self._redis.eval(_RELEASE, 1, LEADER_KEY, self.holder)
            logger.info("leader: released the lease")
        except Exception as error:  # noqa: BLE001
            logger.warning(
                "could not release the leader lease; it expires in %.0fs: %s",
                self._ttl,
                error,
            )



# The raw gateway event the current dispatch task came from. hikari runs each
# gateway event's handling in its own task, created inside consume_raw_event,
# which copies this context.
_raw_event: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar(
    "raw_gateway_event", default=None
)


def event_key(name: str, payload: Any) -> str:
    """Same for every session that receives the same gateway event."""
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "event:" + hashlib.sha256(f"{name}:{body}".encode()).hexdigest()[:32]


def _is_gateway_event(event: hikari.Event) -> bool:
    """Events about the guild, as opposed to this bot's connection state."""
    return isinstance(event, shard_events.ShardEvent) and not isinstance(
        event, shard_events.ShardStateEvent
    )


class EventGate:
    """Run each gateway event's listeners on exactly one connected bot.

    Wraps ``consume_raw_event`` and ``dispatch`` on the bot's event manager.
    Cache updates and connection-state events (ready, resumed, disconnected)
    are never gated, so a bot that loses a claim still has a current cache and
    a correct connection state.
    """

    def __init__(self, event_manager: Any, leadership: Leadership) -> None:
        self._leadership = leadership
        self._held_started: hikari.StartedEvent | None = None
        self._in_flight: set[asyncio.Future[Any]] = set()
        self._started_task: asyncio.Task[Any] | None = None
        self._closed = False
        self.claimed = 0
        self.left_to_other = 0

        # hikari's event manager uses __slots__, so its methods cannot be
        # replaced on the instance; swap in a subclass that routes through us.
        manager_type = type(event_manager)
        original_dispatch = manager_type.dispatch
        original_consume = manager_type.consume_raw_event
        gate = self

        class _GatedEventManager(manager_type):  # type: ignore[misc, valid-type]
            __slots__ = ()

            def consume_raw_event(self, name: str, shard: Any, payload: Any) -> None:
                token = _raw_event.set((name, event_key(name, payload)))
                try:
                    original_consume(self, name, shard, payload)
                finally:
                    _raw_event.reset(token)

            def dispatch(self, event: hikari.Event) -> asyncio.Future[Any]:
                return gate.dispatch(event)

        self._dispatch = lambda event: original_dispatch(event_manager, event)
        event_manager.__class__ = _GatedEventManager

    def dispatch(self, event: hikari.Event) -> asyncio.Future[Any]:
        if isinstance(event, hikari.StartedEvent) and not self._leadership.is_leader:
            self._held_started = event
            return _done()
        raw = _raw_event.get()
        if raw is None or not _is_gateway_event(event):
            return self._dispatch(event)
        if self._closed:
            return _done()
        future = asyncio.ensure_future(self._claim_then_dispatch(*raw, event))
        self._in_flight.add(future)
        future.add_done_callback(self._in_flight.discard)
        return future

    async def _claim_then_dispatch(self, name: str, key: str, event: hikari.Event) -> None:
        if await self._leadership.claim(key):
            self.claimed += 1
            await self._dispatch(event)
        else:
            # Only happens while another bot is connected, i.e. during a roll.
            self.left_to_other += 1
            logger.info("%s handled by the other connected bot", name)

    async def on_acquire(self) -> None:
        """Start the leader-only work that ``StartedEvent`` listeners own."""
        event, self._held_started = self._held_started, None
        if event is not None:
            # Its listeners can run for minutes; the lease must keep renewing.
            self._started_task = asyncio.create_task(self._dispatch(event))

    def close(self) -> None:
        """Claim no more events; the other connected bot takes them all."""
        self._closed = True
        logger.info(
            "stopped claiming events: handled %d, left %d to another bot",
            self.claimed,
            self.left_to_other,
        )

    async def drain(self, timeout: float) -> int:
        """Wait for claimed events still running; return how many outlasted it."""
        pending = set(self._in_flight)
        if not pending:
            return 0
        _, still_running = await asyncio.wait(pending, timeout=timeout)
        return len(still_running)


def _done() -> asyncio.Future[Any]:
    future = asyncio.get_running_loop().create_future()
    future.set_result(None)
    return future
