"""A deploy hands the bot over from one process to the next.

Two processes are simulated with two real hikari event managers fed the same
raw gateway payloads, as two gateway sessions would be, sharing one (fake)
Redis. The assertions are about the boundaries: no event runs on both
processes, what neither runs is named, work already accepted finishes where it
was accepted, and at no sampled moment do two processes act.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock
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

FAST = {"ttl": 0.4, "margin": 0.15, "renew_interval": 0.1, "poll_interval": 0.01}


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


async def _always_gone() -> bool:
    return True


class Process:
    """One bot process: a real hikari event manager behind the gate."""

    def __init__(self, name: str, server: fakeredis.FakeServer, **options) -> None:
        self.name = name
        self.app = hikari.GatewayBot("x" * 60, banner=None)
        self.redis = fakeredis.aioredis.FakeRedis(server=server)
        self.coordinator = Coordinator(
            self.redis, name, **{**FAST, "predecessor_gone": _always_gone, **options}
        )
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
        await self.coordinator.stop_acting(self.gate.recent_handled())
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
    leadership._accepted.clear()


async def incumbent_and_standby(server, **new_options) -> tuple[Process, Process]:
    old = Process("old", server)
    old.start()
    await until(lambda: old.coordinator.acting)
    new = Process("new", server, **new_options)
    new.start()
    await asyncio.sleep(0.05)
    return old, new


class Overlap:
    """Samples both processes and records any moment both act."""

    def __init__(self, *processes: Process) -> None:
        self.processes = processes
        self.seen = False
        self.task = asyncio.create_task(self._watch())

    async def _watch(self) -> None:
        while True:
            if sum(p.coordinator.acting for p in self.processes) > 1:
                self.seen = True
            await asyncio.sleep(0.002)

    def stop(self) -> bool:
        self.task.cancel()
        return self.seen


async def down(*_args, **_kwargs):
    raise RedisConnectionError("redis down")


# The handover itself.


async def test_a_standby_handles_nothing_while_the_incumbent_acts(server) -> None:
    old, new = await incumbent_and_standby(server)
    for message_id in range(1, 31):
        old.receive(message_id)
        new.receive(message_id)
    await settle()

    assert old.handled == [str(i) for i in range(1, 31)]
    assert new.handled == []
    await old.stop()
    await new.stop()


async def test_a_handover_mid_stream_runs_every_event_once(server) -> None:
    old, new = await incumbent_and_standby(server)
    overlap = Overlap(old, new)
    for message_id in range(1, 201):
        if message_id == 50:
            await old.stop()  # SIGTERM
        old.receive(message_id)
        new.receive(message_id)
        await asyncio.sleep(0.001)
    await settle()

    assert not overlap.stop(), "two processes acted at once"
    assert set(old.handled).isdisjoint(new.handled), "an event ran on both"
    dropped = set(range(1, 201)) - {int(i) for i in old.handled + new.handled}
    assert dropped == set(), f"dropped at the handover: {sorted(dropped)}"
    assert new.gate.replayed > 0, "the gap was covered by the standby's buffer"
    await new.stop()


async def test_delivery_skew_across_the_cutoff_neither_doubles_nor_drops(server) -> None:
    """The two sessions see events at different times: the new one 30 events
    behind the old one for the first half, then the old one behind the new."""
    old, new = await incumbent_and_standby(server)
    lagging: list[int] = []
    for message_id in range(1, 121):
        old.receive(message_id)
        lagging.append(message_id)
        if len(lagging) > 30:
            new.receive(lagging.pop(0))  # the new session hears it late
        if message_id == 60:
            await old.stop()
            await until(lambda: new.coordinator.acting)
        await asyncio.sleep(0.001)
    for message_id in lagging:
        new.receive(message_id)
    # And events the old session only hears after it stopped.
    for message_id in range(121, 131):
        new.receive(message_id)
        old.receive(message_id)
    await settle()

    assert set(old.handled).isdisjoint(new.handled), "an event ran on both"
    handled = {int(i) for i in old.handled + new.handled}
    assert handled == set(range(1, 131)), f"dropped: {sorted(set(range(1, 131)) - handled)}"
    assert new.gate.already_handled > 0, "late copies of handled events were skipped"
    await new.stop()


async def test_handover_takes_about_one_poll(server) -> None:
    old, new = await incumbent_and_standby(server)
    await old.stop()
    gap = await until(lambda: new.coordinator.acting)
    assert gap < 0.1
    await new.stop()


async def test_replay_runs_before_later_live_events(server) -> None:
    old, new = await incumbent_and_standby(server)
    await old.coordinator.stop_acting([])  # handled nothing recently
    new.receive(1)  # buffered: arrived before the standby acted
    await until(lambda: new.coordinator.acting)
    new.receive(2)
    await settle()
    assert new.handled == ["1", "2"]
    await new.stop()


async def test_stale_interactions_are_not_replayed(server) -> None:
    old, new = await incumbent_and_standby(server)
    replayed: list[str] = []
    new.gate._dispatch = lambda event: replayed.append(event) or leadership._done()
    now = time.monotonic()
    new.gate._buffer.extend(
        [
            (now - 5, "INTERACTION_CREATE:1", "stale interaction"),
            (now, "INTERACTION_CREATE:2", "fresh interaction"),
            (now - 5, "MESSAGE_CREATE:3", "older message"),
        ]
    )
    await old.stop()
    await until(lambda: new.coordinator.acting)
    assert replayed == ["fresh interaction", "older message"]
    await new.stop()


async def test_the_standby_buffer_is_bounded(server, monkeypatch) -> None:
    monkeypatch.setattr(leadership, "STANDBY_BUFFER_EVENTS", 10)
    old, new = await incumbent_and_standby(server)
    for message_id in range(1, 26):
        new.receive(message_id)
    await settle()
    assert len(new.gate._buffer) == 10
    assert new.gate.dropped == 15
    await old.stop()
    await new.stop()


def _event(cls: type) -> hikari.Event:
    return cls(**{f.name.lstrip("_"): Mock() for f in attrs.fields(cls) if f.init})


async def test_an_identical_repeat_without_an_id_is_skipped_right_after_a_handover(
    server,
) -> None:
    """The documented cost of the payload-hash fallback."""
    old, new = await incumbent_and_standby(server)
    await old.coordinator.stop_acting(["MESSAGE_REACTION_ADD:#same"])
    await until(lambda: new.coordinator.acting)
    ran: list[int] = []

    async def on_reaction(_event) -> None:
        ran.append(1)

    new.app.event_manager.subscribe(hikari.GuildReactionAddEvent, on_reaction)
    token = leadership._raw_event.set("MESSAGE_REACTION_ADD:#same")
    try:
        await new.app.event_manager.dispatch(_event(hikari.GuildReactionAddEvent))
    finally:
        leadership._raw_event.reset(token)
    assert ran == []
    assert new.gate.already_handled == 1
    await new.stop()


# Accepted work finishes where it was accepted.


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
    await old.stop()
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


async def test_mod_monitor_triage_started_before_sigterm_is_drained(monkeypatch) -> None:
    from smarter_dev.bot.plugins import mod_monitor

    triage_started, finish = asyncio.Event(), asyncio.Event()
    triaged: list[int] = []

    async def triage(event, _config) -> None:
        triage_started.set()
        await finish.wait()
        triaged.append(event.message.id)

    monkeypatch.setattr(mod_monitor, "_handle_moderation", triage)
    monkeypatch.setitem(mod_monitor._guild_configs, "30", {"monitored_role_ids": {"99"}})
    event = Mock(guild_id=30, channel_id=20)
    event.message.author.is_bot = False
    event.message.role_mention_ids = [99]
    event.message.id = 5
    event.message.content = "@mods help"

    await mod_monitor.on_message_create(event)  # the real listener
    await asyncio.wait_for(triage_started.wait(), 1)
    assert len(await leadership.drain_tracked(0.05)) == 1, "shutdown must wait for triage"
    finish.set()
    assert await leadership.drain_tracked(1) == []
    assert triaged == [5]


async def test_a_delayed_delete_is_tracked_for_shutdown() -> None:
    from smarter_dev.bot.plugins import events

    event = Mock()
    event.interaction = Mock(spec=hikari.ComponentInteraction)
    event.interaction.guild_id = 30
    event.interaction.user.id = 40
    event.interaction.custom_id = "cancel_get_input:abc"
    event.interaction.create_initial_response = AsyncMock()
    event.interaction.delete_initial_response = AsyncMock()

    await events.handle_challenge_cancel_get_input_interaction(event)  # real handler
    pending = await leadership.drain_tracked(0.05)
    assert [task.get_name() for task in pending] == ["delete_after_delay"]
    for task in pending:
        task.cancel()


async def test_a_timed_send_mid_rest_at_sigterm_finishes(server) -> None:
    from smarter_dev.bot.services.scheduled_message_service import (
        ScheduledMessageService,
    )

    process = Process("old", server)
    process.start()
    await until(lambda: process.coordinator.acting)
    leadership.install(process.coordinator)

    sending, finish = asyncio.Event(), asyncio.Event()
    sent: list[str] = []
    service = ScheduledMessageService(Mock(), None, Mock())

    async def send(data) -> None:
        sending.set()
        await finish.wait()  # the REST call to Discord
        sent.append(data["id"])

    service._send_scheduled_message = send
    message = {"id": "m1", "scheduled_time": "2026-09-24T20:00:00Z", "title": "t"}
    queued = asyncio.create_task(service._queue_and_send_message(message))
    await asyncio.wait_for(sending.wait(), 1)

    await process.stop()  # SIGTERM mid-send
    assert len(await leadership.drain_tracked(0.05)) == 1, "shutdown must wait for the send"
    finish.set()
    assert await leadership.drain_tracked(1) == []
    await queued
    assert sent == ["m1"]


async def test_stopping_cancels_the_proactive_control_loop(server, monkeypatch) -> None:
    from smarter_dev.bot.plugins import proactive

    process = Process("old", server)
    process.start()
    await until(lambda: process.coordinator.acting)
    leadership.install(process.coordinator)

    async def forever(*_args) -> None:
        await asyncio.Event().wait()

    run = Mock(passive_task=None, sync_execution_ownership=AsyncMock())
    monkeypatch.setattr(proactive, "runtime", run)
    monkeypatch.setattr(proactive, "_passive_ticker", forever)
    monkeypatch.setattr(proactive, "_recover_channels", forever)
    monkeypatch.setattr(proactive, "_control_loop", forever)

    await proactive.on_started(Mock())  # the real listener
    assert not run.control_task.done()
    await process.stop()
    await settle()
    assert run.control_task.cancelled()
    run.passive_task.cancel()
    run.recovery_task.cancel()


async def test_drain_reports_listeners_that_outlast_the_budget(server) -> None:
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


# Losing the lease, Redis failures and crashes.


async def test_a_partitioned_incumbent_fences_itself_before_a_successor_acts(server) -> None:
    old, new = await incumbent_and_standby(server)
    overlap = Overlap(old, new)
    old.redis.eval = down  # only the incumbent loses Redis

    await until(lambda: not old.coordinator.acting, timeout=1)
    await until(lambda: new.coordinator.acting, timeout=1)
    assert not overlap.stop(), "two processes acted at once"
    old.receive(1)
    await settle()
    assert old.handled == [], "a fenced process must not act"
    await old.stop()
    await new.stop()


async def test_a_standby_without_redis_never_takes_over(server) -> None:
    old, new = await incumbent_and_standby(server)
    new.redis.set = down
    new.redis.exists = down
    await asyncio.sleep(0.8)  # well past the lease
    assert old.coordinator.acting and not new.coordinator.acting
    assert not new.coordinator.can_take_over
    await old.stop()
    await new.stop()


async def test_a_redis_outage_fences_the_incumbent_until_redis_returns(server) -> None:
    process = Process("only", server)
    process.start()
    await until(lambda: process.coordinator.acting)
    real_eval = process.redis.eval
    process.redis.eval = down

    await until(lambda: not process.coordinator.acting, timeout=1)
    assert process.coordinator.degraded
    process.redis.eval = real_eval
    await until(lambda: process.coordinator.acting, timeout=1)
    await process.stop()


async def test_a_blip_shorter_than_the_margin_changes_nothing(server) -> None:
    process = Process("only", server, ttl=1.0, margin=0.3)
    process.start()
    await until(lambda: process.coordinator.acting)
    real_eval = process.redis.eval
    process.redis.eval = down
    for _ in range(20):
        await asyncio.sleep(0.01)
        assert process.coordinator.acting
    process.redis.eval = real_eval
    await process.stop()


async def test_a_failed_release_hands_over_when_the_lease_expires(server) -> None:
    old, new = await incumbent_and_standby(server)
    overlap = Overlap(old, new)
    old.redis.eval = down  # the release fails; the record was written
    for message_id in range(1, 11):
        old.receive(message_id)
    await settle()
    await old.stop()
    for message_id in range(11, 21):  # the gap until the lease expires
        new.receive(message_id)
        old.receive(message_id)
    gap = await until(lambda: new.coordinator.acting, timeout=2)
    await settle()

    assert not overlap.stop()
    assert gap > 0.1, "must wait for the lease to expire"
    assert old.handled == [str(i) for i in range(1, 11)]
    assert new.handled == [str(i) for i in range(11, 21)], "the gap is replayed"
    await new.stop()


async def test_with_no_handover_record_it_waits_for_the_old_pod(server) -> None:
    gone = False

    async def predecessor_gone() -> bool:
        return gone

    old, new = await incumbent_and_standby(server, predecessor_gone=predecessor_gone)
    old.task.cancel()  # crashed: no release, no record
    await asyncio.sleep(0.8)  # the lease has expired
    assert not new.coordinator.acting, "the old pod may still be running"
    assert new.coordinator.can_take_over, "Ready, so Kubernetes stops the old pod"
    new.receive(1)  # arrives while waiting

    gone = True
    await until(lambda: new.coordinator.acting, timeout=4)
    await settle()
    assert new.handled == [], "without a record nothing is replayed"
    await new.stop()


async def test_a_crashed_incumbent_is_replaced_only_after_its_lease_expires(server) -> None:
    old, new = await incumbent_and_standby(server)
    old.task.cancel()  # dies without releasing

    await asyncio.sleep(0.2)
    assert not new.coordinator.acting, "must not act while the lease may be live"
    await until(lambda: new.coordinator.acting, timeout=4)
    await new.stop()


async def test_an_incumbent_that_loses_its_lease_stops_acting(server) -> None:
    old = Process("old", server)
    old.start()
    await until(lambda: old.coordinator.acting)

    await old.redis.set(LEASE_KEY, "other")  # lapsed during a pause; taken
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


# Start-up.


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
    await old.stop()
    leadership.install(old.coordinator)
    assert not await leadership.should_send(time.time(), wait=0.2)
    await new.stop()


async def test_a_message_due_before_the_handover_is_left_to_the_incumbent(server) -> None:
    old, new = await incumbent_and_standby(server)
    due = time.time()
    await asyncio.sleep(0.01)
    await old.stop()  # it was acting when this fell due

    leadership.install(new.coordinator)
    assert not await leadership.should_send(due, wait=0.5)
    await new.stop()


async def test_a_message_due_inside_the_gap_goes_to_the_successor(server) -> None:
    old, new = await incumbent_and_standby(server)
    await old.stop()
    due = time.time() + 0.001  # fell due before the successor took over
    leadership.install(new.coordinator)
    assert await leadership.should_send(due, wait=0.5)
    await new.stop()


async def test_without_a_handover_what_fell_due_while_the_old_pod_ran_is_left_to_it(
    server,
) -> None:
    gone = False

    async def predecessor_gone() -> bool:
        return gone

    process = Process("first", server, predecessor_gone=predecessor_gone)
    process.start()  # lease free, no record: the first deploy of this code
    await asyncio.sleep(0.05)
    due_while_old_ran = time.time()
    await asyncio.sleep(2.1)  # the next peer check still sees the old pod
    gone = True
    await until(lambda: process.coordinator.acting, timeout=4)
    due_after = time.time() - 0.001
    leadership.install(process.coordinator)
    assert not await leadership.should_send(due_while_old_ran, wait=0.1)
    assert await leadership.should_send(due_after, wait=0.1)
    await process.stop()


# Chat engines: a turn accepted before SIGTERM is answered before exit.


class _Engine:
    """A real ChannelEngine whose turn blocks until released."""

    def __init__(self, channel_id: int, turn: asyncio.Event, sent: list) -> None:
        from smarter_dev.bot.services.chat_engine import ChannelEngine

        self.engine = ChannelEngine(
            bot=Mock(), channel_id=channel_id, guild_id=1, voice_send=Mock(), on_deactivate=Mock()
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


async def test_shutdown_stays_in_budget_when_work_outlasts_it(server, monkeypatch) -> None:
    from smarter_dev.bot import client
    from smarter_dev.bot.services import chat_engine_registry

    registry = chat_engine_registry.ChatEngineRegistry()
    stuck = _Engine(9, asyncio.Event(), []).engine
    registry._engines = {9: stuck}
    monkeypatch.setattr(chat_engine_registry, "_registry", registry)
    stuck.fire_now()
    await settle()
    background = leadership.track(asyncio.create_task(asyncio.Event().wait()))

    process = Process("old", server)
    started = time.monotonic()
    await asyncio.wait_for(client.drain_accepted_work(process.gate, budget=0.2), 2)
    assert time.monotonic() - started < 1.0, "shutdown waited past its budget"
    await settle()
    assert stuck._runner_task.cancelled()
    assert background.cancelled()


# Readiness.


def test_ready_means_connected_set_up_and_able_to_take_over(server) -> None:
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


async def test_a_standby_that_cannot_reach_redis_is_not_ready(server) -> None:
    old, new = await incumbent_and_standby(server)
    leadership.install(new.coordinator)
    bot = Mock(
        shards={0: Mock(is_alive=True, is_connected=True)},
        d={"_services": {"s": object()}},
        application=object(),
    )
    assert ready_to_act(bot)

    new.redis.exists = down
    new.redis.set = down
    new.coordinator._redis_ok_at -= 10  # its last answer was a while ago
    await asyncio.sleep(0.05)
    assert not ready_to_act(bot), "Kubernetes must keep the old bot"
    assert old.coordinator.acting
    await old.stop()
    await new.stop()
