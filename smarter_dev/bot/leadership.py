"""Hand the bot over between processes during a deploy, one actor at a time.

A deploy connects the new bot while the old one is still connected, and for
a while both gateway sessions receive every event. Only one of them may act,
or automod deletes, times out and warns twice and every reply doubles. And
the old one must keep acting until the new one takes over, or nobody acts.

One Redis key, the *acting lease*, says which process acts:

- **Standby.** A process that finds the lease held connects, sets up and
  reports Ready (only while it can reach Redis, so it could take over), but
  runs no gateway listener and no timed send. It buffers the gateway events
  it receives. Kubernetes stops the old process once the standby is Ready.
- **Handover.** On SIGTERM the old process stops acting and writes a handover
  record, the events it handled in its last ``HANDOVER_WINDOW`` seconds and
  its stop time, then releases the lease. The standby takes the lease on its
  next poll and acts. It replays the buffered events the record does not
  list, and for a minute skips any event the record does list. So an event
  delivered to the two sessions at different times, on either side of the
  handover, still runs exactly once (given the identity below).
- **Fencing.** A holder that cannot renew its lease stops acting before the
  lease can expire (``ttl - margin`` after its last renewal was sent),
  checked on every event, so a paused or partitioned holder never acts
  beside a successor. A standby never takes over without Redis.
- **No handover.** A free lease without a fresh handover record means the
  previous holder crashed, failed to hand over, or never ran this code (the
  first deploy of it). Such a process reports Ready, so Kubernetes stops any
  other bot pod, and takes the lease only once ``predecessor_gone`` confirms
  from the Kubernetes API that no other bot pod has a running container. It
  replays nothing.

Steady state is unchanged: one process holding the lease, renewing it every
few seconds; no Redis call on any event path.

Event identity is Discord's id where it has one (messages, deletes,
interactions, edits) and a hash of the payload otherwise; two sessions are
expected to receive identical payloads for the latter, which is unverified.

What this does not guarantee:

- Events reaching both sessions between the old process stopping and the new
  one acting are replayed from the standby's buffer, so the handover itself
  loses none; the buffer holds ``STANDBY_BUFFER_SECONDS``. A handover that
  takes longer than that (Redis slow to answer) loses the older ones.
- Warm takeover (a standby is connected) is bounded by the handover: the
  stopping process's two Redis calls, one poll and the standby's two calls,
  each call bounded by the client's socket timeout. If the release fails the
  standby waits for the lease to expire (``ttl``), replaying up to
  ``STANDBY_BUFFER_SECONDS`` of buffered events. If the whole handover
  record fails too, it waits until the old pod has stopped and replays
  nothing.
- Cold recovery (the only process crashed, no standby) is a restart: the
  container restart backoff, then startup (about 70s at one CPU), then the
  lease. Nothing is replayed; events in between are lost.
- A holder that loses Redis stops acting after ``ttl - margin`` until Redis
  returns: an outage over about ten seconds silences the bot rather than risk
  two actors. Shorter blips change nothing.
- Work already accepted by the old process when it stops (running listeners,
  work they handed to background tasks registered with ``track``, and chat
  turns) finishes there within the drain budget in ``run_bot``; what is still
  running then is cancelled and logged. A listener that started before its
  process fenced itself also still acts.
- Where the identity falls back to a payload hash, a legitimately identical
  repeat within ``HANDOVER_MEMORY_SECONDS`` of a handover is skipped.
- Interactions buffered for longer than ``INTERACTION_REPLAY_SECONDS`` are
  not replayed: Discord's three-second window to acknowledge has passed.
- Embedded proactive consumers (``consumer_task``) are not drained. In
  production every guild runs the proactive agent externally
  (``k8s/configmap.yaml``), so the bot starts none.
- Rolling back to an image without this code brings back the old overlap: that
  process acts as soon as it connects.
"""

from __future__ import annotations

import asyncio
import collections
import contextvars
import hashlib
import json
import logging
import os
import time
import uuid
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Iterable
from typing import Any

import hikari
from hikari.events import shard_events

logger = logging.getLogger(__name__)

LEASE_KEY = "smarter-dev:bot:acting"
HANDED_OVER_KEY = "smarter-dev:bot:handed-over"
RELEASED_AT_KEY = "smarter-dev:bot:released-at"

# How far back the stopping process lists what it handled, how long and how
# much the standby buffers (enough to cover a lease expiring after a failed
# release), and how long the successor honours the list.
HANDOVER_WINDOW = 30.0
STANDBY_BUFFER_SECONDS = 20.0
STANDBY_BUFFER_EVENTS = 5000
HANDOVER_MEMORY_SECONDS = 60.0
INTERACTION_REPLAY_SECONDS = 2.0
# How long a queued timed send on a standby waits for a handover in progress.
HANDOVER_WAIT_SECONDS = 60.0

# Only the holder may extend or delete the lease; a process that lost it must
# not clobber the new holder's. A holder whose lease lapsed with nobody taking
# it (a pause, a Redis blip) takes it back.
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
_accepted: set[asyncio.Task[Any]] = set()


def install(coordinator: Coordinator | None) -> None:
    global _current
    _current = coordinator


def is_acting() -> bool:
    """Whether this process may act. True when no coordinator is installed."""
    return _current is None or _current.acting


def can_take_over() -> bool:
    """Whether this process acts, or could if handed the lease now."""
    return _current is None or _current.can_take_over


def coordination_degraded() -> bool:
    """Whether Redis is failing, so this process may be fenced or stuck."""
    return _current is not None and _current.degraded


async def should_send(due: float, *, wait: float = HANDOVER_WAIT_SECONDS) -> bool:
    """Whether this process sends a timed message that fell due at ``due``.

    ``due`` is a Unix time. Every process queues timed messages, so exactly one
    must send each: the one that was acting when it fell due. A standby waits
    up to ``wait`` seconds for a handover in progress before deciding.
    """
    if _current is None:
        return True
    return await _current.should_send(due, wait=wait)


def track(task: asyncio.Task[Any]) -> asyncio.Task[Any]:
    """Register background work a listener started, so shutdown waits for it."""
    _accepted.add(task)
    task.add_done_callback(_accepted.discard)
    return task


async def run_accepted(work: Awaitable[Any]) -> Any:
    """Run ``work`` as tracked background work and wait for it."""
    return await track(asyncio.ensure_future(work))


async def drain_tracked(timeout: float) -> list[asyncio.Task[Any]]:
    """Wait for tracked work; return the tasks still running at ``timeout``."""
    pending = set(_accepted)
    if not pending:
        return []
    _, still_running = await asyncio.wait(pending, timeout=timeout)
    return list(still_running)


def on_stop(callback: Callable[[], Any]) -> None:
    """Call ``callback`` the moment this process stops acting."""
    if _current is not None:
        _current.on_stop(callback)


async def _no_peers() -> bool:
    return True


def holder_id() -> str:
    return f"{os.environ.get('HOSTNAME', 'local')}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class Handover:
    """What the previous holder left: when it stopped and what it handled."""

    def __init__(self, stopped_at: float, handled: set[str]) -> None:
        self.stopped_at = stopped_at
        self.handled = handled


class Coordinator:
    """This process's hold on the acting lease."""

    def __init__(
        self,
        redis: Any,
        holder: str,
        *,
        ttl: float = 15.0,
        margin: float = 5.0,
        renew_interval: float = 3.0,
        poll_interval: float = 0.1,
        predecessor_gone: Callable[[], Awaitable[bool]] = _no_peers,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._redis = redis
        self.holder = holder
        self._ttl = ttl
        self._ttl_ms = int(ttl * 1000)
        self._margin = margin
        self._renew_interval = renew_interval
        self._poll_interval = poll_interval
        self._predecessor_gone = predecessor_gone
        self._clock = clock

        self.stopping = False
        self.degraded = False
        self.handover: Handover | None = None
        # Unix time this process last started acting.
        self.acting_since: float | None = None
        self._holding = False
        self._valid_until = 0.0
        self._fenced_logged = False
        self._redis_ok_at: float | None = None
        self._acting_changed = asyncio.Event()
        self._hooks: list[tuple[Callable[[], Any], bool]] = []
        self._stop_hooks: list[Callable[[], Any]] = []
        self._waiting_logged = False
        self._peers_checked_at: float | None = None

    @property
    def acting(self) -> bool:
        """Holding the lease, not stopping, and sure the lease has not expired."""
        return self._holding and not self.stopping and self._clock() < self._valid_until

    @property
    def can_take_over(self) -> bool:
        if self.acting:
            return True
        ok_at = self._redis_ok_at
        return (
            not self.stopping
            and ok_at is not None
            and self._clock() - ok_at < min(3.0, self._ttl)
        )

    def on_acting(self, callback: Callable[[], Any], *, once: bool = True) -> None:
        """Call ``callback`` when this process starts acting (every time if not once)."""
        self._hooks.append((callback, once))

    def on_stop(self, callback: Callable[[], Any]) -> None:
        self._stop_hooks.append(callback)

    async def run(self) -> None:
        """Take the lease and keep it; call once the bot is connected and set up."""
        while not self.stopping:
            sent = self._clock()
            try:
                if self._holding:
                    await self._renew(sent)
                elif await self._may_take_free_lease() and await self._redis.set(
                    LEASE_KEY, self.holder, nx=True, px=self._ttl_ms
                ):
                    self._valid_until = sent + self._ttl - self._margin
                    await self._take_over()
                self._redis_ok_at = self._clock()
                self._set_degraded(False)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - any Redis failure
                self._set_degraded(True, error)
            if self._holding and not self.acting and not self._fenced_logged:
                self._fenced_logged = True
                logger.error(
                    "acting lease unconfirmed for %.0fs; not acting until Redis answers",
                    self._ttl - self._margin,
                )
            healthy_holder = self._holding and not self.degraded
            await asyncio.sleep(
                self._renew_interval if healthy_holder else self._poll_interval
            )

    async def _may_take_free_lease(self) -> bool:
        """Whether nothing but a lease we could take stands between us and acting."""
        if await self._redis.exists(LEASE_KEY):
            return False  # held: stand by
        stopped_at = await self._redis.get(RELEASED_AT_KEY)
        if stopped_at and time.time() - float(stopped_at) < HANDOVER_MEMORY_SECONDS:
            return True  # handed over
        # Nobody handed over: whoever held it last may still be running and
        # not coordinating. Act only once it is gone (asking at most every 2s).
        now = self._clock()
        if self._peers_checked_at is not None and now - self._peers_checked_at < 2.0:
            return False
        self._peers_checked_at = now
        if await self._predecessor_gone():
            return True
        if not self._waiting_logged:
            self._waiting_logged = True
            logger.info("lease free without a handover; waiting for the other bot pod to stop")
        return False

    async def _renew(self, sent: float) -> None:
        if await self._redis.eval(_RENEW, 1, LEASE_KEY, self.holder, self._ttl_ms):
            self._valid_until = sent + self._ttl - self._margin
            self._fenced_logged = False
        else:
            self._holding = False
            self._acting_changed.set()
            logger.error("lost the acting lease to another bot; standing by")

    async def _take_over(self) -> None:
        pipe = self._redis.pipeline()
        pipe.get(RELEASED_AT_KEY)
        pipe.smembers(HANDED_OVER_KEY)
        stopped_at, handled = await pipe.execute()
        self.handover = None
        if stopped_at and time.time() - float(stopped_at) < HANDOVER_MEMORY_SECONDS:
            self.handover = Handover(
                float(stopped_at),
                {k.decode() if isinstance(k, bytes) else k for k in handled},
            )
        self._holding = True
        self.acting_since = time.time()
        self._fenced_logged = False
        self._acting_changed.set()
        logger.info(
            "acting (%s): %s",
            self.holder,
            f"handed over, {len(self.handover.handled)} events already handled"
            if self.handover
            else "no handover record",
        )
        keep = []
        for callback, once in self._hooks:
            try:
                callback()
            except Exception:
                logger.exception("acting start-up hook failed")
            if not once:
                keep.append((callback, once))
        self._hooks = keep

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
                await asyncio.wait_for(self._acting_changed.wait(), min(remaining, 0.5))
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
        # already stopped by then. Without a handover record it crashed or
        # never coordinated; sending risks a duplicate, skipping risks a loss,
        # and a crash rarely lands on the second a message falls due.
        return self.handover is None or self.handover.stopped_at <= due

    async def stop_acting(self, handled: Iterable[str] = ()) -> None:
        """Stop acting now, leave a handover record, and release the lease."""
        self.stopping = True
        self._acting_changed.set()
        for callback in self._stop_hooks:
            try:
                callback()
            except Exception:
                logger.exception("stop hook failed")
        if not self._holding:
            return
        self._holding = False
        keys = list(handled)
        try:
            # The record goes first, so a successor never holds the lease
            # without it.
            pipe = self._redis.pipeline(transaction=True)
            pipe.delete(HANDED_OVER_KEY)
            if keys:
                pipe.sadd(HANDED_OVER_KEY, *keys)
            pipe.expire(HANDED_OVER_KEY, int(HANDOVER_MEMORY_SECONDS * 2))
            pipe.set(RELEASED_AT_KEY, str(time.time()), ex=int(HANDOVER_MEMORY_SECONDS * 2))
            await pipe.execute()
            await self._redis.eval(_RELEASE, 1, LEASE_KEY, self.holder)
            logger.info("released the acting lease (%d recent events handed over)", len(keys))
        except Exception as error:  # noqa: BLE001
            logger.warning(
                "could not hand over; the lease expires within %.0fs: %s", self._ttl, error
            )


# The raw gateway event the current dispatch task came from. hikari runs each
# gateway event's handling in its own task, created inside consume_raw_event,
# which copies this context.
_raw_event: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "raw_gateway_event", default=None
)

# The id that makes each of these events unique, as sent by Discord.
_ID_FIELDS = {"MESSAGE_CREATE": "id", "MESSAGE_DELETE": "id", "INTERACTION_CREATE": "id"}


def event_identity(name: str, payload: Any) -> str:
    """The same for every session that receives this gateway event."""
    if isinstance(payload, dict):
        field = _ID_FIELDS.get(name)
        if field and payload.get(field):
            return f"{name}:{payload[field]}"
        if name == "MESSAGE_UPDATE" and payload.get("id") and payload.get("edited_timestamp"):
            return f"{name}:{payload['id']}:{payload['edited_timestamp']}"
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return f"{name}:#" + hashlib.sha256(body.encode()).hexdigest()[:24]


def _is_gateway_event(event: hikari.Event) -> bool:
    """Events about the guild, as opposed to this bot's connection state."""
    return isinstance(event, shard_events.ShardEvent) and not isinstance(
        event, shard_events.ShardStateEvent
    )


class EventGate:
    """Run gateway listeners only while this process acts, once per handover.

    Wraps ``consume_raw_event`` and ``dispatch`` on the bot's event manager.
    Cache updates happen before dispatch and connection-state events (ready,
    resumed, disconnected) are never gated, so a standby's cache and
    connection state stay current.

    ``StartedEvent`` reaches the framework's own listener at once (lightbulb
    fetches the application and syncs commands there, which a standby needs
    before it can act), and reaches the app's listeners, which start the
    background work, only once this process acts.
    """

    def __init__(
        self, app: Any, coordinator: Coordinator, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._app = app
        self._coordinator = coordinator
        self._clock = clock
        self._in_flight: set[asyncio.Future[Any]] = set()
        self._started_task: asyncio.Task[Any] | None = None
        self._recent: collections.deque[tuple[float, str]] = collections.deque()
        self._buffer: collections.deque[tuple[float, str, hikari.Event]] = collections.deque()
        self._predecessor_handled: set[str] = set()
        self._skip_until = 0.0
        self.handled = 0
        self.replayed = 0
        self.already_handled = 0
        self.dropped = 0

        # hikari's event manager uses __slots__, so its methods cannot be
        # replaced on the instance; swap in a subclass that routes through us.
        event_manager = app.event_manager
        manager_type = type(event_manager)
        original_dispatch = manager_type.dispatch
        original_consume = manager_type.consume_raw_event
        gate = self

        class _GatedEventManager(manager_type):  # type: ignore[misc, valid-type]
            __slots__ = ()

            def consume_raw_event(self, name: str, shard: Any, payload: Any) -> None:
                token = _raw_event.set(event_identity(name, payload))
                try:
                    original_consume(self, name, shard, payload)
                finally:
                    _raw_event.reset(token)

            def dispatch(self, event: hikari.Event) -> asyncio.Future[Any]:
                return gate.dispatch(event)

        self._event_manager = event_manager
        self._dispatch = lambda event: original_dispatch(event_manager, event)
        event_manager.__class__ = _GatedEventManager
        coordinator.on_acting(self._take_over, once=False)

    def dispatch(self, event: hikari.Event) -> asyncio.Future[Any]:
        if isinstance(event, hikari.StartedEvent) and not self._coordinator.acting:
            return self._start_framework_only(event)
        key = _raw_event.get()
        if key is None or not _is_gateway_event(event):
            return self._dispatch(event)
        now = self._clock()
        if self._coordinator.acting:
            if now < self._skip_until and key in self._predecessor_handled:
                self.already_handled += 1
                return _done()
            return self._run(now, key, event)
        if not self._coordinator.stopping and self._coordinator.acting_since is None:
            # A standby: keep it in case the handover lands before it is handled.
            self._buffer.append((now, key, event))
            self._prune(self._buffer, now - STANDBY_BUFFER_SECONDS)
            while len(self._buffer) > STANDBY_BUFFER_EVENTS:
                self._buffer.popleft()
                self.dropped += 1
        else:
            self.dropped += 1  # stopping, or fenced while Redis is unreachable
        return _done()

    def _run(self, now: float, key: str, event: hikari.Event) -> asyncio.Future[Any]:
        self.handled += 1
        self._recent.append((now, key))
        self._prune(self._recent, now - HANDOVER_WINDOW)
        future = self._dispatch(event)
        self._in_flight.add(future)
        future.add_done_callback(self._in_flight.discard)
        return future

    @staticmethod
    def _prune(entries: collections.deque, older_than: float) -> None:
        while entries and entries[0][0] < older_than:
            entries.popleft()

    def _take_over(self) -> None:
        """Replay what the predecessor did not handle; skip what it did."""
        handover = self._coordinator.handover
        buffered, self._buffer = list(self._buffer), collections.deque()
        if handover is None:
            # Crash, or a predecessor that never coordinated: it may have
            # handled any of these, so replaying could double them.
            self.dropped += len(buffered)
            return
        now = self._clock()
        self._predecessor_handled = handover.handled
        self._skip_until = now + HANDOVER_MEMORY_SECONDS
        # In arrival order, and before any live event: this runs inside the
        # coordinator's task, so no dispatch can interleave.
        for received, key, event in buffered:
            if key in handover.handled:
                self.already_handled += 1
            elif (
                key.startswith("INTERACTION_CREATE:")
                and now - received > INTERACTION_REPLAY_SECONDS
            ):
                self.dropped += 1  # too late to acknowledge
            else:
                self.replayed += 1
                self._run(now, key, event)

    def recent_handled(self) -> list[str]:
        """The events this process handled within the handover window."""
        self._prune(self._recent, self._clock() - HANDOVER_WINDOW)
        return [key for _, key in self._recent]

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
        return (
            f"handled {self.handled} events ({self.replayed} replayed from standby), "
            f"skipped {self.already_handled} already handled, dropped {self.dropped}"
        )


async def _run_listener(callback: Callable[[Any], Any], event: hikari.Event) -> None:
    try:
        await callback(event)
    except Exception:
        logger.exception(
            "StartedEvent listener %s failed", getattr(callback, "__name__", callback)
        )


def _done() -> asyncio.Future[Any]:
    future = asyncio.get_running_loop().create_future()
    future.set_result(None)
    return future
