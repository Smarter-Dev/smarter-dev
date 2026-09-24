"""A deploy hands the bot over from one process to the next.

Two processes are simulated with two real hikari event managers fed the same
raw gateway payloads, as two gateway sessions would be, sharing one (fake)
Redis. The assertions are about the boundaries: no event runs on both, the
only events handled by neither fall inside the measured handover gap, and work
already accepted by the old process finishes there.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import Mock

import attrs
import fakeredis
import fakeredis.aioredis
import hikari
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from smarter_dev.bot import leadership
from smarter_dev.bot.client import ready_to_act
from smarter_dev.bot.leadership import LEASE_KEY
from smarter_dev.bot.leadership import Coordinator
from smarter_dev.bot.leadership import EventGate

FAST = {"ttl": 0.4, "renew_interval": 0.1, "poll_interval": 0.01, "bootstrap_delay": 0.3}


def message_payload(message_id: int) -> dict:
    return {
        "id": str(message_id),
        "channel_id": "20",
        "guild_id": "30",
        "author": {"id": "40", "username": "u", "discriminator": "0", "avatar": None},
        "member": {"roles": [], "joined_at": "2026-01-01T00:00:00+00:00", "deaf": False, "mute": False},
        "content": "spam",
        "timestamp": "2026-09-24T20:00:00+00:00",
        "edited_timestamp": None,
        "tts": False,
        "mention_everyone": False,
        "mentions": [],
        "mention_roles": [],
        "attachments": [],
        "embeds": [],
        "pinned": False,
        "type": 0,
        "flags": 0,
    }


class Process:
    """One bot process: a real hikari event manager behind the gate."""

    def __init__(self, name: str, server: fakeredis.FakeServer, **timing) -> None:
        self.name = name
        self.app = hikari.GatewayBot("x" * 60, banner=None)
        self.redis = fakeredis.aioredis.FakeRedis(server=server)
        self.coordinator = Coordinator(self.redis, name, **{**FAST, **timing})
        self.gate = EventGate(self.app, self.coordinator)
        self.handled: list[str] = []
        self.task: asyncio.Task | None = None

        async def automod(event: hikari.GuildMessageCreateEvent) -> None:
            self.handled.append(str(event.message_id))

        self.app.event_manager.subscribe(hikari.GuildMessageCreateEvent, automod)

    def receive(self, message_id: int) -> None:
        self.app.event_manager.consume_raw_event(
            "MESSAGE_CREATE", Mock(id=0), message_payload(message_id)
        )

    def start(self) -> None:
        self.task = asyncio.create_task(self.coordinator.run())

    async def stop(self) -> None:
        await self.coordinator.stop_acting()
        if self.task:
            self.task.cancel()


async def settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)
    await asyncio.sleep(0.02)


async def until(predicate, timeout: float = 2.0) -> float:
    started = time.monotonic()
    while not predicate():
        if time.monotonic() - started > timeout:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.005)
    return time.monotonic() - started


@pytest.fixture
def server() -> fakeredis.FakeServer:
    return fakeredis.FakeServer()


@pytest.fixture(autouse=True)
def uninstall():
    yield
    leadership.install(None)


async def incumbent_and_standby(server) -> tuple[Process, Process]:
    old = Process("old", server)
    old.start()
    await until(lambda: old.coordinator.acting)
    new = Process("new", server)
    new.start()
    await asyncio.sleep(0.05)
    return old, new


async def test_a_standby_handles_nothing_while_the_incumbent_acts(server) -> None:
    old, new = await incumbent_and_standby(server)
    for message_id in range(1, 31):
        old.receive(message_id)
        new.receive(message_id)
    await settle()

    assert old.handled == [str(i) for i in range(1, 31)]
    assert new.handled == []
    assert not new.coordinator.acting
    await old.stop()
    await new.stop()


async def test_the_handover_runs_no_event_twice_and_drops_only_inside_the_gap(
    server,
) -> None:
    old, new = await incumbent_and_standby(server)
    stop_at = 50
    for message_id in range(1, 201):
        if message_id == stop_at:
            await old.coordinator.stop_acting()  # SIGTERM
        old.receive(message_id)
        new.receive(message_id)
        await asyncio.sleep(0.001)
    await settle()

    assert set(old.handled).isdisjoint(new.handled), "an event ran on both"
    assert old.handled == [str(i) for i in range(1, stop_at)]
    first_new = int(new.handled[0])
    dropped = set(range(stop_at, first_new))
    handled = {int(i) for i in old.handled + new.handled}
    assert handled | dropped == set(range(1, 201)), "loss outside the handover gap"
    assert len(dropped) < 20, f"gap too wide: {len(dropped)} events"
    await new.stop()


async def test_handover_takes_about_one_poll(server) -> None:
    old, new = await incumbent_and_standby(server)
    await old.coordinator.stop_acting()
    gap = await until(lambda: new.coordinator.acting)
    assert gap < 0.1
    await new.stop()


async def test_sigterm_during_a_moderation_action_finishes_it_once(server) -> None:
    old, new = await incumbent_and_standby(server)
    started, finish = asyncio.Event(), asyncio.Event()
    actions: list[str] = []

    for process in (old, new):
        async def moderate(event, name=process.name) -> None:
            started.set()
            await finish.wait()  # deleting the message, timing the member out
            actions.append(f"{name}:{event.message_id}")

        process.app.event_manager.subscribe(hikari.GuildMessageCreateEvent, moderate)

    old.receive(1)
    new.receive(1)
    await asyncio.wait_for(started.wait(), 1)
    await old.coordinator.stop_acting()
    drain = asyncio.create_task(old.gate.drain(timeout=2))
    await settle()
    assert not drain.done(), "shutdown must wait for the running action"

    finish.set()
    assert await drain == 0
    await until(lambda: new.coordinator.acting)
    old.receive(2)
    new.receive(2)
    await settle()
    assert actions == ["old:1", "new:2"]
    await new.stop()


async def test_drain_reports_work_that_outlasts_the_budget(server) -> None:
    process = Process("old", server)
    process.start()
    await until(lambda: process.coordinator.acting)

    async def stuck(_event) -> None:
        await asyncio.Event().wait()

    process.app.event_manager.subscribe(hikari.GuildMessageCreateEvent, stuck)
    process.receive(1)
    await settle()
    await process.stop()
    assert await process.gate.drain(timeout=0.05) == 1


async def test_a_crashed_incumbent_is_replaced_only_after_its_lease_expires(
    server,
) -> None:
    old, new = await incumbent_and_standby(server)
    old.task.cancel()  # dies without releasing

    await asyncio.sleep(0.2)
    assert not new.coordinator.acting, "must not act while the lease may be live"
    await until(lambda: new.coordinator.acting, timeout=1)
    await new.stop()


async def test_an_incumbent_that_loses_its_lease_stops_acting(server) -> None:
    old = Process("old", server)
    old.start()
    await until(lambda: old.coordinator.acting)

    # Its lease lapsed during a pause and another process took it.
    await old.redis.set(LEASE_KEY, "other")
    await until(lambda: not old.coordinator.acting)
    old.receive(1)
    await settle()
    assert old.handled == []

    await old.stop()
    assert await old.redis.get(LEASE_KEY) == b"other", "must not release another's lease"


async def test_an_incumbent_retakes_a_lease_that_lapsed_unclaimed(server) -> None:
    old = Process("old", server)
    old.start()
    await until(lambda: old.coordinator.acting)
    await old.redis.delete(LEASE_KEY)
    await asyncio.sleep(0.2)
    assert old.coordinator.acting
    assert await old.redis.get(LEASE_KEY) == b"old"
    await old.stop()


async def test_a_redis_outage_never_silences_the_incumbent(server) -> None:
    old, new = await incumbent_and_standby(server)

    async def down(*_args, **_kwargs):
        raise RedisConnectionError("redis down")

    old.redis.eval = down
    new.redis.set = down
    await asyncio.sleep(0.2)
    assert old.coordinator.acting
    assert old.coordinator.degraded

    # The standby takes over once the outage outlasts the lease: from then
    # until Kubernetes stops the old process, both act. Documented, logged.
    await until(lambda: new.coordinator.acting, timeout=1)
    assert new.coordinator.degraded
    leadership.install(new.coordinator)
    assert leadership.coordination_degraded()
    await old.stop()
    await new.stop()


async def test_bootstrap_waits_before_acting_beside_an_uncoordinated_bot(server) -> None:
    process = Process("first", server)
    process.start()
    await asyncio.sleep(0.1)
    assert not process.coordinator.acting, "a predecessor may still be acting"
    await until(lambda: process.coordinator.acting, timeout=1)
    await process.stop()


def _event(cls: type) -> hikari.Event:
    return cls(**{f.name.lstrip("_"): Mock() for f in attrs.fields(cls) if f.init})


async def test_connection_state_events_reach_a_standby(server) -> None:
    old, new = await incumbent_and_standby(server)
    seen: list[str] = []

    async def on_ready(_event) -> None:
        seen.append("ready")

    new.app.event_manager.subscribe(hikari.ShardReadyEvent, on_ready)
    await new.app.event_manager.dispatch(_event(hikari.ShardReadyEvent))
    assert seen == ["ready"]
    await old.stop()
    await new.stop()


async def test_a_standby_initializes_the_framework_but_defers_app_start_up(server) -> None:
    old, new = await incumbent_and_standby(server)
    calls: list[str] = []

    class FrameworkListener:
        """Stands in for lightbulb's listener, a method bound to the app."""

        async def __call__(self, _event) -> None:
            calls.append("framework")

    framework = FrameworkListener()
    framework.__self__ = new.app

    async def app_start_up(_event) -> None:
        calls.append("app")

    new.app.event_manager.subscribe(hikari.StartedEvent, framework)
    new.app.event_manager.subscribe(hikari.StartedEvent, app_start_up)

    await new.app.event_manager.dispatch(_event(hikari.StartedEvent))
    await settle()
    assert calls == ["framework"]

    await old.stop()
    await until(lambda: new.coordinator.acting)
    await settle()
    assert calls == ["framework", "app"]
    await new.stop()


# Timed sends: every process queues them; the one acting at the due time sends.


async def test_the_acting_process_sends_what_falls_due(server) -> None:
    old, new = await incumbent_and_standby(server)
    leadership.install(old.coordinator)
    assert await leadership.should_send(time.time(), wait=0.2)
    leadership.install(new.coordinator)
    assert not await leadership.should_send(time.time(), wait=0.1), "standby must not"
    await old.stop()
    await new.stop()


async def test_a_stopping_process_sends_nothing(server) -> None:
    old, new = await incumbent_and_standby(server)
    await old.coordinator.stop_acting()
    leadership.install(old.coordinator)
    assert not await leadership.should_send(time.time(), wait=0.2)
    await new.stop()


async def test_a_message_due_before_the_handover_is_left_to_the_incumbent(server) -> None:
    old, new = await incumbent_and_standby(server)
    due = time.time()
    await asyncio.sleep(0.01)
    await old.coordinator.stop_acting()  # it was acting when this fell due

    leadership.install(new.coordinator)
    assert not await leadership.should_send(due, wait=0.5)
    await new.stop()


async def test_a_message_due_inside_the_gap_goes_to_the_successor(server) -> None:
    old, new = await incumbent_and_standby(server)
    await old.coordinator.stop_acting()
    due = time.time() + 0.001  # fell due before the successor took over
    leadership.install(new.coordinator)
    assert await leadership.should_send(due, wait=0.5)
    await new.stop()


async def test_after_a_crash_a_message_due_before_takeover_is_sent(server) -> None:
    old, new = await incumbent_and_standby(server)
    due = time.time()
    old.task.cancel()  # crashed: no stop time recorded
    await until(lambda: new.coordinator.acting, timeout=1)
    leadership.install(new.coordinator)
    assert await leadership.should_send(due, wait=0.1), (
        "unknown stop time: send (possible duplicate) rather than lose it"
    )
    await new.stop()


# Chat engines: a turn accepted before SIGTERM is answered before exit.


class _Engine:
    """The parts of ChannelEngine the drain relies on, with a real runner."""

    def __init__(self, channel_id: int, turn: asyncio.Event, sent: list) -> None:
        from smarter_dev.bot.services.chat_engine import ChannelEngine

        self.engine = ChannelEngine(
            bot=Mock(),
            channel_id=channel_id,
            guild_id=1,
            voice_send=Mock(),
            on_deactivate=Mock(),
        )

        async def run_once(*, first_activation: bool) -> bool:
            async with self.engine.run_lock:
                async with self.engine.queue_lock:
                    self.engine.queue.clear()
                await turn.wait()  # the model call and the reply
                sent.append(channel_id)
            return True

        self.engine._run_once = run_once
        self.engine.start()


async def test_drain_answers_a_running_turn_and_a_queued_one() -> None:
    from smarter_dev.bot.services.chat_engine_registry import ChatEngineRegistry

    registry = ChatEngineRegistry()
    turn = asyncio.Event()
    sent: list[int] = []
    running = _Engine(1, turn, sent).engine
    queued = _Engine(2, turn, sent).engine
    registry._engines = {1: running, 2: queued}

    running.fire_now()  # mid-turn when SIGTERM lands
    await settle()
    queued.queue.append(Mock())  # accepted, waiting on its idle timer

    drain = asyncio.create_task(registry.drain(timeout=2))
    await settle()
    assert not drain.done(), "drain must wait for the turns"
    turn.set()
    assert await drain == []
    assert sorted(sent) == [1, 2]
    await registry.shutdown_all()


async def test_drain_names_the_channels_it_had_to_abandon() -> None:
    from smarter_dev.bot.services.chat_engine_registry import ChatEngineRegistry

    registry = ChatEngineRegistry()
    stuck = _Engine(7, asyncio.Event(), []).engine
    registry._engines = {7: stuck}
    stuck.fire_now()
    await settle()
    assert await registry.drain(timeout=0.1) == [7]
    stuck._runner_task.cancel()


# Readiness.


def test_ready_means_connected_and_set_up() -> None:
    shard = Mock(is_alive=True, is_connected=True)
    services = {"_services": {"bytes_service": object()}}
    bot = Mock(shards={0: shard}, d=services, application=object())
    assert ready_to_act(bot)

    shard.is_connected = False  # disconnected, hikari reconnecting
    assert not ready_to_act(bot)
    shard.is_connected = True  # resumed
    assert ready_to_act(bot)

    assert not ready_to_act(Mock(shards={}, d=services, application=object()))
    assert not ready_to_act(
        Mock(shards={0: shard}, d={"_services": {}}, application=object())
    ), "services failed to set up"
    assert not ready_to_act(Mock(shards={0: shard}, d=services, application=None)), (
        "lightbulb not initialized"
    )
