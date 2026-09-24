"""Hand the bot over between processes during a deploy, one actor at a time.

A deploy connects the new bot while the old one is still connected, and for
a while both gateway sessions receive every event. Only one of them may act,
or automod deletes, times out and warns twice and every reply doubles. And
the old one must keep acting until the new one can take over, or nobody acts.

One Redis key, the *acting lease*, says which process acts:

- **Standby.** A bot that finds the lease held connects, initializes and
  reports Ready, but runs no gateway listener and no timed send. Kubernetes
  stops the old bot once the standby is Ready.
- **Handover.** On SIGTERM the old bot stops acting and releases the lease at
  once, and the standby, polling every ``poll_interval``, takes it and acts.
  The old bot then finishes the work it had already accepted (``run_bot``).
- **Bootstrap.** A bot that finds the lease free cannot tell a crashed
  predecessor from one that never coordinates (the first deploy of this code
  replaces a bot without it). It reports Ready so Kubernetes stops any such
  predecessor, and takes the lease only after ``bootstrap_delay``.

Steady state is unchanged: one bot, holding the lease, renewing it every few
seconds; no Redis call on any event path.

What this does not guarantee, measured or bounded rather than hidden:

- Events that reach either session between the old bot stopping and the new
  one taking the lease (one poll, plus a Redis round trip) are handled by
  neither.
- During bootstrap, events after the predecessor stops and before
  ``bootstrap_delay`` ends are handled by neither.
- Work already running on the old bot when it stops (a moderation action, a
  chat turn) finishes there; if it outlasts the grace period it is cut off.
- If the acting bot loses Redis it keeps acting, and a standby takes over
  only after the outage outlasts the lease; the two can then both act until
  Kubernetes stops the old one. That is logged as degraded coordination.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections.abc import Callable
from typing import Any

import hikari
from hikari.events import shard_events

logger = logging.getLogger(__name__)

LEASE_KEY = "smarter-dev:bot:acting"
# How long a queued timed send on a standby waits for a handover in progress;
# covers the bootstrap delay, so nothing due during bootstrap is dropped.
HANDOVER_WAIT_SECONDS = 30.0
RELEASED_AT_KEY = "smarter-dev:bot:released-at"

# Only the holder may extend or delete the lease; a bot that lost it must not
# clobber the new holder's. A holder whose lease lapsed with nobody taking it
# (a pause, or a Redis outage it acted through) takes it back.
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

_current: Coordinator | None = None


def install(coordinator: Coordinator | None) -> None:
    global _current
    _current = coordinator


def is_acting() -> bool:
    """Whether this process may act. True when no coordinator is installed."""
    return _current is None or _current.acting


def coordination_degraded() -> bool:
    """Whether Redis is failing, so a second process could act alongside."""
    return _current is not None and _current.degraded


async def wait_until_acting(timeout: float) -> bool:
    """Wait up to ``timeout`` for this process to act; False if it never will."""
    if _current is None:
        return True
    return await _current.wait_until_acting(timeout)


async def should_send(due: float, *, wait: float) -> bool:
    """Whether this process sends a timed message that fell due at ``due``.

    ``due`` is a Unix time. Every process queues timed messages, so exactly one
    must send each: the one that was acting when it fell due. A standby waits
    up to ``wait`` seconds for a handover in progress before deciding.
    """
    if _current is None:
        return True
    return await _current.should_send(due, wait=wait)


def holder_id() -> str:
    return f"{os.environ.get('HOSTNAME', 'local')}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class Coordinator:
    """This process's hold on the acting lease."""

    def __init__(
        self,
        redis: Any,
        holder: str,
        *,
        ttl: float = 15.0,
        renew_interval: float = 5.0,
        poll_interval: float = 0.1,
        bootstrap_delay: float = 20.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._redis = redis
        self.holder = holder
        self._ttl = ttl
        self._ttl_ms = int(ttl * 1000)
        self._renew_interval = renew_interval
        self._poll_interval = poll_interval
        self._bootstrap_delay = bootstrap_delay
        self._clock = clock

        self.acting = False
        self.stopping = False
        self.degraded = False
        # Unix times: when this process started acting, and when the process
        # before it stopped (None if unknown: it crashed or never recorded it).
        self.acting_since: float | None = None
        self.predecessor_stopped_at: float | None = None
        self._acting_changed = asyncio.Event()
        self._on_acting: list[Callable[[], Any]] = []
        self._redis_ok_at = clock()

    def on_acting(self, callback: Callable[[], Any]) -> None:
        """Call ``callback`` once, when this process first acts."""
        self._on_acting.append(callback)

    async def run(self) -> None:
        """Take the lease and keep it; call once the bot is connected and ready."""
        self._redis_ok_at = self._clock()
        earliest = self._clock()
        try:
            if not await self._redis.exists(LEASE_KEY):
                earliest += self._bootstrap_delay
                logger.info(
                    "no bot holds the lease; taking it in %.0fs", self._bootstrap_delay
                )
            else:
                logger.info("standing by for the lease")
        except Exception as error:  # noqa: BLE001
            earliest += self._bootstrap_delay
            self._set_degraded(True, error)

        while not self.stopping:
            now = self._clock()
            try:
                if self.acting:
                    await self._renew()
                elif now >= earliest and await self._redis.set(
                    LEASE_KEY, self.holder, nx=True, px=self._ttl_ms
                ):
                    stopped_at = await self._redis.get(RELEASED_AT_KEY)
                    self.predecessor_stopped_at = float(stopped_at) if stopped_at else None
                    self._start_acting("took the lease")
                self._redis_ok_at = now
                self._set_degraded(False)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - any Redis failure
                self._set_degraded(True, error)
                outage = now - self._redis_ok_at
                if not self.acting and now >= earliest and outage > self._ttl:
                    self._start_acting(f"Redis unreachable for {outage:.0f}s")
            await asyncio.sleep(
                self._renew_interval if self.acting else self._poll_interval
            )

    async def _renew(self) -> None:
        if not await self._redis.eval(_RENEW, 1, LEASE_KEY, self.holder, self._ttl_ms):
            # Another process holds it; it acts now, so we must not.
            self.acting = False
            self._acting_changed.set()
            logger.error("lost the acting lease to another bot; standing by")

    def _start_acting(self, reason: str) -> None:
        if self.stopping:
            return
        self.acting = True
        self.acting_since = time.time()
        self._acting_changed.set()
        logger.info("acting: %s (%s)", reason, self.holder)
        callbacks, self._on_acting = self._on_acting, []
        for callback in callbacks:
            try:
                callback()
            except Exception:
                logger.exception("acting start-up hook failed")

    def _set_degraded(self, degraded: bool, error: Exception | None = None) -> None:
        if degraded and not self.degraded:
            logger.warning("coordination degraded, Redis unreachable: %s", error)
        elif not degraded and self.degraded:
            logger.info("coordination restored")
        self.degraded = degraded

    async def wait_until_acting(self, timeout: float) -> bool:
        deadline = self._clock() + timeout
        while not self.acting:
            if self.stopping:
                return False
            remaining = deadline - self._clock()
            if remaining <= 0:
                return False
            self._acting_changed.clear()
            try:
                await asyncio.wait_for(self._acting_changed.wait(), min(remaining, 1.0))
            except TimeoutError:
                pass
        return True

    async def should_send(self, due: float, *, wait: float) -> bool:
        if self.stopping:
            return False
        if not self.acting and not await self.wait_until_acting(wait):
            return False
        assert self.acting_since is not None
        if self.acting_since <= due:
            return True
        # We took over after it fell due: send it only if our predecessor had
        # already stopped by then. Unknown means it crashed; sending risks a
        # duplicate, skipping risks losing it, and a crash rarely lands on the
        # second a message falls due.
        stopped = self.predecessor_stopped_at
        return stopped is None or stopped <= due

    async def stop_acting(self) -> None:
        """Stop acting now and hand the lease to the standby."""
        self.stopping = True
        was_acting = self.acting
        self.acting = False
        self._acting_changed.set()
        if not was_acting:
            return
        try:
            # Record when we stopped first, so the successor can never read
            # the lease free without it.
            await self._redis.set(RELEASED_AT_KEY, str(time.time()), ex=3600)
            await self._redis.eval(_RELEASE, 1, LEASE_KEY, self.holder)
            logger.info("released the acting lease")
        except Exception as error:  # noqa: BLE001
            logger.warning(
                "could not release the acting lease; it expires in %.0fs: %s",
                self._ttl,
                error,
            )


def _is_gateway_event(event: hikari.Event) -> bool:
    """Events about the guild, as opposed to this bot's connection state."""
    return isinstance(event, shard_events.ShardEvent) and not isinstance(
        event, shard_events.ShardStateEvent
    )


class EventGate:
    """Run gateway listeners only while this process acts.

    Replaces ``dispatch`` on the bot's event manager. Cache updates happen
    before dispatch and connection-state events (ready, resumed, disconnected)
    are never gated, so a standby's cache and connection state stay current.

    ``StartedEvent`` reaches the framework's own listener at once (lightbulb
    fetches the application and syncs commands there, which a standby needs
    before it can act), and reaches the app's listeners, which start the
    background work, only once this process acts.
    """

    def __init__(self, app: Any, coordinator: Coordinator) -> None:
        self._app = app
        self._coordinator = coordinator
        self._in_flight: set[asyncio.Future[Any]] = set()
        self._started_task: asyncio.Task[Any] | None = None
        self.handled = 0
        self.skipped = 0

        # hikari's event manager uses __slots__, so ``dispatch`` cannot be
        # replaced on the instance; swap in a subclass that routes through us.
        event_manager = app.event_manager
        manager_type = type(event_manager)
        original_dispatch = manager_type.dispatch
        gate = self

        class _GatedEventManager(manager_type):  # type: ignore[misc, valid-type]
            __slots__ = ()

            def dispatch(self, event: hikari.Event) -> asyncio.Future[Any]:
                return gate.dispatch(event)

        self._event_manager = event_manager
        self._dispatch = lambda event: original_dispatch(event_manager, event)
        event_manager.__class__ = _GatedEventManager

    def dispatch(self, event: hikari.Event) -> asyncio.Future[Any]:
        if isinstance(event, hikari.StartedEvent) and not self._coordinator.acting:
            return self._start_framework_only(event)
        if not _is_gateway_event(event):
            return self._dispatch(event)
        if not self._coordinator.acting:
            self.skipped += 1
            return _done()
        self.handled += 1
        future = self._dispatch(event)
        self._in_flight.add(future)
        future.add_done_callback(self._in_flight.discard)
        return future

    def _start_framework_only(self, event: hikari.StartedEvent) -> asyncio.Future[Any]:
        listeners = self._event_manager.get_listeners(hikari.StartedEvent)
        framework = [cb for cb in listeners if getattr(cb, "__self__", None) is self._app]
        app_listeners = [cb for cb in listeners if cb not in framework]

        def start_app_work() -> None:
            self._started_task = asyncio.ensure_future(
                asyncio.gather(*(_run_listener(cb, event) for cb in app_listeners))
            )

        self._coordinator.on_acting(start_app_work)
        return asyncio.ensure_future(
            asyncio.gather(*(_run_listener(cb, event) for cb in framework))
        )

    async def drain(self, timeout: float) -> int:
        """Wait for listeners already running; return how many outlasted it."""
        pending = set(self._in_flight)
        if not pending:
            return 0
        _, still_running = await asyncio.wait(pending, timeout=timeout)
        return len(still_running)

    def summary(self) -> str:
        return f"handled {self.handled} events, skipped {self.skipped} while not acting"


async def _run_listener(callback: Callable[[Any], Any], event: hikari.Event) -> None:
    try:
        await callback(event)
    except Exception:
        logger.exception("StartedEvent listener %s failed", getattr(callback, "__name__", callback))


def _done() -> asyncio.Future[Any]:
    future = asyncio.get_running_loop().create_future()
    future.set_result(None)
    return future
