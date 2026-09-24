"""A deploy hands the bot over from one process to the next.

Two processes are simulated with two real hikari event managers fed the same
raw gateway payloads, as two gateway sessions would be, sharing one (fake)
Redis. The assertions are about the boundaries: no event runs on both
processes, what neither runs is named, work already accepted finishes where it
was accepted, and at no sampled moment do two processes act.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from datetime import UTC
from datetime import datetime
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

FAST = {
    "ttl": 0.6,
    "margin": 0.15,
    "renew_interval": 0.1,
    "poll_interval": 0.01,
    "peer_interval": 0.03,
    "sole_pod_seconds": 0.1,
}


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


# For tests that stream events for a while: a lease that outlasts event-loop
# stalls on a loaded machine, so only the handover is under test.
STEADY = {"ttl": 3.0, "margin": 1.0}

class Cluster:
    """The bot pods Kubernetes would list, and whose API calls fail."""

    def __init__(self) -> None:
        self.pods: set[str] = set()
        self.api_down: set[str] = set()
        self.api_hangs: set[str] = set()
        self.calls = 0

    def lister(self, me: str):
        async def other_pods() -> list[str]:
            self.calls += 1
            if me in self.api_hangs:
                await asyncio.Event().wait()
            if me in self.api_down:
                raise OSError("kubernetes api unreachable")
            return sorted(self.pods - {me})

        return other_pods


class Process:
    """One bot process in its own pod: a real hikari event manager behind
    the gate. Without a cluster, Kubernetes lists no other pod."""

    def __init__(
        self,
        name: str,
        server: fakeredis.FakeServer,
        cluster: Cluster | None = None,
        **options,
    ) -> None:
        self.name = name
        self.cluster = cluster
        self.app = hikari.GatewayBot("x" * 60, banner=None)
        self.redis = fakeredis.aioredis.FakeRedis(server=server)
        if cluster is not None:
            cluster.pods.add(name)
            options = {"other_pods": cluster.lister(name), **options}
        self.coordinator = Coordinator(self.redis, name, **{**FAST, **options})
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

    async def exit(self) -> None:
        """SIGTERM, then the container exits and the pod goes."""
        await self.stop()
        if self.cluster is not None:
            self.cluster.pods.discard(self.name)


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


@pytest.fixture
def cluster() -> Cluster:
    return Cluster()


@pytest.fixture(autouse=True)
def uninstall():
    yield
    leadership.install(None)
    leadership._accepted.clear()


async def incumbent_and_standby(
    server, cluster: Cluster | None = None, **options
) -> tuple[Process, Process]:
    old = Process("old", server, cluster, **options)
    old.start()
    await until(lambda: old.coordinator.acting)
    new = Process("new", server, cluster, **options)
    new.start()
    await asyncio.sleep(0.05)
    return old, new


class Overlap:
    """Samples the processes and records any moment two act, and any moment
    none does."""

    def __init__(self, *processes: Process) -> None:
        self.processes = processes
        self.seen = False
        self.gap = False
        self.task = asyncio.create_task(self._watch())

    async def _watch(self) -> None:
        while True:
            acting = sum(p.coordinator.acting for p in self.processes)
            self.seen = self.seen or acting > 1
            self.gap = self.gap or acting == 0
            await asyncio.sleep(0.002)

    def stop(self) -> bool:
        self.task.cancel()
        return self.seen


async def down(*_args, **_kwargs):
    raise RedisConnectionError("redis down")


# The handover itself.


async def test_a_standby_handles_nothing_while_the_incumbent_acts(server) -> None:
    old, new = await incumbent_and_standby(server, **STEADY)
    for message_id in range(1, 31):
        old.receive(message_id)
        new.receive(message_id)
    await settle()

    assert old.handled == [str(i) for i in range(1, 31)]
    assert new.handled == []
    await old.stop()
    await new.stop()


async def test_a_handover_mid_stream_runs_every_event_once(server) -> None:
    old, new = await incumbent_and_standby(server, **STEADY)
    overlap = Overlap(old, new)
    for message_id in range(1, 201):
        if message_id == 50:
            new.task.cancel()  # its next poll comes only after 50-59 arrive
            await old.stop()  # SIGTERM
        if message_id == 60:
            new.start()
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
    old, new = await incumbent_and_standby(server, **STEADY)
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
    old, new = await incumbent_and_standby(server, **STEADY)
    await old.stop()
    gap = await until(lambda: new.coordinator.acting)
    assert gap < 0.1
    await new.stop()


async def test_replay_runs_before_later_live_events(server) -> None:
    old, new = await incumbent_and_standby(server, **STEADY)
    await old.coordinator.stop_acting([])  # handled nothing recently
    new.receive(1)  # buffered: arrived before the standby acted
    await until(lambda: new.coordinator.acting)
    new.receive(2)
    await settle()
    assert new.handled == ["1", "2"]
    await new.stop()


async def test_stale_interactions_are_not_replayed(server, caplog) -> None:
    old, new = await incumbent_and_standby(server, **STEADY)
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
    assert losses(caplog) == {"interactions buffered past the time to acknowledge them": 1}
    await new.stop()


async def test_the_standby_buffer_is_bounded(server, monkeypatch, caplog) -> None:
    monkeypatch.setattr(leadership, "STANDBY_BUFFER_EVENTS", 10)
    old, new = await incumbent_and_standby(server, **STEADY)
    for message_id in range(1, 26):
        new.receive(message_id)
    await settle()
    assert len(new.gate._buffer) == 10
    assert new.gate.dropped == 15
    new.gate.summary()
    assert losses(caplog) == {
        "over 10 events buffered on standby; lost only if the handover needs them": 15
    }
    await old.stop()
    await new.stop()


def _event(cls: type) -> hikari.Event:
    return cls(**{f.name.lstrip("_"): Mock() for f in attrs.fields(cls) if f.init})


async def test_an_identical_repeat_without_an_id_is_skipped_right_after_a_handover(
    server,
) -> None:
    """The documented cost of the payload-hash fallback."""
    old, new = await incumbent_and_standby(server, **STEADY)
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
    old, new = await incumbent_and_standby(server, **STEADY)
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


async def test_a_cosmetic_delete_does_not_hold_shutdown() -> None:
    """Deleting a notice after a delay is clean-up, not accepted work."""
    from smarter_dev.bot.plugins import events

    event = Mock()
    event.interaction = Mock(spec=hikari.ComponentInteraction)
    event.interaction.guild_id = 30
    event.interaction.user.id = 40
    event.interaction.custom_id = "cancel_get_input:abc"
    event.interaction.create_initial_response = AsyncMock()
    event.interaction.delete_initial_response = AsyncMock()

    await events.handle_challenge_cancel_get_input_interaction(event)  # real handler
    assert leadership.tracked() == set()
    assert await leadership.drain_tracked(0.05) == []


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


async def test_a_repeating_message_mid_send_at_sigterm_is_sent_and_marked_once(
    server,
) -> None:
    from smarter_dev.bot.services.repeating_message_service import (
        RepeatingMessageService,
    )

    process = Process("old", server)
    process.start()
    await until(lambda: process.coordinator.acting)
    leadership.install(process.coordinator)

    sending, finish = asyncio.Event(), asyncio.Event()
    sent: list[str] = []
    due = {"r1"}  # the server's view: due until marked sent
    service = RepeatingMessageService(Mock(), None, Mock())

    async def due_messages():
        return [{"id": m, "channel_id": "20", "message_content": "hi"} for m in due]

    async def send(_channel_id, _content) -> None:
        sending.set()
        await finish.wait()  # the REST call to Discord
        sent.append("r1")

    async def mark_sent(message_id) -> None:
        await asyncio.sleep(0.01)  # the API call moving next_send_time
        due.discard(message_id)

    service._get_due_repeating_messages = due_messages
    service._send_message_to_channel = send
    service._mark_repeating_message_sent = mark_sent

    minute = asyncio.create_task(service._check_and_send_due_messages())
    await asyncio.wait_for(sending.wait(), 1)
    await process.stop()  # SIGTERM mid-send
    minute.cancel()  # shutdown stops the scheduler loop
    assert len(await leadership.drain_tracked(0.05)) == 1, "shutdown must wait for the send"
    finish.set()
    assert await leadership.drain_tracked(1) == []
    assert sent == ["r1"] and due == set(), "sent, and no longer due next minute"

    await service._check_and_send_due_messages()  # the successor's next minute
    assert sent == ["r1"]


async def test_a_long_background_task_does_not_starve_queued_chat_turns(
    server, monkeypatch
) -> None:
    """Drain runs everything at once under one budget, turns fired first."""
    from smarter_dev.bot import client
    from smarter_dev.bot.plugins import events
    from smarter_dev.bot.services import chat_engine_registry

    registry = chat_engine_registry.ChatEngineRegistry()
    turn = asyncio.Event()
    turn.set()  # the model answers at once
    sent: list[int] = []
    queued = _Engine(3, turn, sent).engine
    registry._engines = {3: queued}
    monkeypatch.setattr(chat_engine_registry, "_registry", registry)
    queued.queue.append(Mock())  # accepted, waiting on its idle timer

    # A cosmetic 5s delete (untracked) and a slow tracked triage.
    interaction = Mock(spec=hikari.ComponentInteraction)
    interaction.guild_id, interaction.user.id = 30, 40
    interaction.custom_id = "cancel_get_input:abc"
    interaction.create_initial_response = AsyncMock()
    interaction.delete_initial_response = AsyncMock()
    await events.handle_challenge_cancel_get_input_interaction(Mock(interaction=interaction))
    slow = leadership.track(asyncio.create_task(asyncio.sleep(60), name="slow triage"))

    process = Process("old", server)
    drain = asyncio.create_task(client.drain_accepted_work(process.gate, budget=0.5))
    answered = await until(lambda: sent == [3], timeout=0.4)
    assert answered < 0.2, "the queued turn waited behind other work"
    await asyncio.wait_for(drain, 2)
    assert slow.cancelled(), "what outlasts the budget is cancelled"


def blocking_reads(commands) -> None:
    """fakeredis ignores XREADGROUP's block; Redis waits, so the loop yields."""
    read = commands.xreadgroup

    async def xreadgroup(*args, **kwargs):
        result = await read(*args, **kwargs)
        if not result and kwargs.get("block"):
            await asyncio.sleep(kwargs["block"] / 1000)
        return result

    commands.xreadgroup = xreadgroup


async def test_a_fenced_process_reads_no_proactive_control_command(
    server, cluster, monkeypatch
) -> None:
    from smarter_dev.bot.plugins import proactive
    from smarter_dev.bot.proactive.contracts import ControlCommand

    process = Process("only", server, cluster)
    leadership.install(process.coordinator)  # not acting yet
    applied: list[str] = []

    async def apply(_run, command) -> None:
        applied.append(str(command.command_id))

    monkeypatch.setattr(proactive, "_apply_control_command", apply)
    monkeypatch.setattr(proactive, "CONTROL_IDLE_SECONDS", 0.01)
    monkeypatch.setattr(proactive, "CONTROL_BLOCK_MS", 20)
    commands = fakeredis.aioredis.FakeRedis(server=server)
    blocking_reads(commands)
    run = Mock(bot=Mock(d={"chat_memory_redis": commands}))
    loop = asyncio.create_task(proactive._control_loop(run))
    command = ControlCommand(
        guild_id="30", channel_id="20", mode="active", minutes=5, created_at=datetime.now(UTC)
    )
    await commands.xadd(proactive.CONTROL_STREAM_KEY, {"payload": command.model_dump_json()})
    await asyncio.sleep(0.2)
    assert applied == []
    assert (await commands.xpending(proactive.CONTROL_STREAM_KEY, proactive.CONTROL_GROUP))[
        "pending"
    ] == 0, "not even read"

    process.start()
    await until(lambda: applied == [str(command.command_id)], timeout=1)

    # Fenced: Redis gone for the lease and another bot pod exists.
    cluster.pods.add("new-pending")
    process.redis.eval = down
    await until(lambda: not process.coordinator.acting, timeout=1)
    await commands.xadd(proactive.CONTROL_STREAM_KEY, {"payload": command.model_copy(
        update={"command_id": uuid.uuid4()}).model_dump_json()})
    await asyncio.sleep(0.2)
    assert len(applied) == 1, "a fenced process applied a command"
    loop.cancel()
    await process.stop()


async def test_a_process_that_stops_mid_batch_leaves_the_rest_of_the_batch(
    server, monkeypatch
) -> None:
    from smarter_dev.bot.plugins import proactive
    from smarter_dev.bot.proactive.contracts import ControlCommand

    process = Process("only", server)
    process.start()
    await until(lambda: process.coordinator.acting)
    leadership.install(process.coordinator)
    applied: list[str] = []

    async def apply(_run, command) -> None:
        applied.append(str(command.command_id))
        await process.coordinator.stop_acting()  # SIGTERM lands mid-batch

    monkeypatch.setattr(proactive, "_apply_control_command", apply)
    monkeypatch.setattr(proactive, "CONTROL_BLOCK_MS", 20)
    commands = fakeredis.aioredis.FakeRedis(server=server)
    blocking_reads(commands)
    for _ in range(2):
        command = ControlCommand(
            guild_id="30", channel_id="20", mode="active", minutes=5, created_at=datetime.now(UTC)
        )
        await commands.xadd(proactive.CONTROL_STREAM_KEY, {"payload": command.model_dump_json()})
    loop = asyncio.create_task(proactive._control_loop(Mock(bot=Mock(d={"chat_memory_redis": commands}))))
    await until(lambda: applied, timeout=1)
    await asyncio.sleep(0.1)
    loop.cancel()
    assert len(applied) == 1, "applied a command after it stopped acting"
    pending = await commands.xpending(proactive.CONTROL_STREAM_KEY, proactive.CONTROL_GROUP)
    assert pending["pending"] == 1, "left for the process that acts next to reclaim"


async def test_a_reclaimed_control_command_already_applied_is_not_applied_again(
    server, monkeypatch
) -> None:
    from smarter_dev.bot.plugins import proactive
    from smarter_dev.bot.proactive.contracts import ControlCommand

    applied: list[str] = []

    async def apply(_run, command) -> None:
        applied.append(str(command.command_id))

    monkeypatch.setattr(proactive, "_apply_control_command", apply)
    monkeypatch.setattr(proactive, "CONTROL_RECLAIM_MS", 0)
    monkeypatch.setattr(proactive, "CONTROL_BLOCK_MS", 20)
    commands = fakeredis.aioredis.FakeRedis(server=server)
    blocking_reads(commands)
    run = Mock()
    command = ControlCommand(
        guild_id="30", channel_id="20", mode="active", minutes=5, created_at=datetime.now(UTC)
    )
    await commands.xgroup_create(
        proactive.CONTROL_STREAM_KEY, proactive.CONTROL_GROUP, id="0", mkstream=True
    )
    await commands.xadd(proactive.CONTROL_STREAM_KEY, {"payload": command.model_dump_json()})
    # A process read it, applied and marked it, then died before acking.
    await commands.xreadgroup(
        proactive.CONTROL_GROUP, "dead", {proactive.CONTROL_STREAM_KEY: ">"}, count=1
    )
    await commands.set(f"{proactive.CONTROL_PROCESSED_PREFIX}:{command.command_id}", "1")

    run.bot.d = {"chat_memory_redis": commands}
    loop = asyncio.create_task(proactive._control_loop(run))
    await asyncio.sleep(0.2)
    loop.cancel()
    assert applied == [], "reclaimed, found marked, acked without applying"
    assert await commands.xlen(proactive.CONTROL_STREAM_KEY) == 0


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


async def test_an_incumbent_cut_off_from_redis_is_replaced_only_once_its_pod_is_gone(
    server, cluster
) -> None:
    """Asymmetric: the incumbent loses Redis, the standby still reaches it."""
    old, new = await incumbent_and_standby(server, cluster)
    overlap = Overlap(old, new)
    old.redis.eval = down

    await until(lambda: not old.coordinator.acting, timeout=1)  # sees the standby
    old.receive(1)
    await settle()
    assert old.handled == [], "a fenced process must not act"
    await asyncio.sleep(0.9)  # the lease has long expired
    assert not new.coordinator.acting, "the old pod still exists"
    assert new.coordinator.can_take_over, "Ready, so Kubernetes stops the old pod"

    await old.exit()  # its handover fails: no Redis
    await until(lambda: new.coordinator.acting, timeout=1)
    assert not overlap.stop(), "two processes acted at once"
    await new.stop()


async def test_a_standby_without_redis_never_takes_over(server) -> None:
    old, new = await incumbent_and_standby(server)
    new.redis.eval = down
    await asyncio.sleep(1.0)  # well past the lease
    assert old.coordinator.acting and not new.coordinator.acting
    assert not new.coordinator.can_take_over
    await old.stop()
    await new.stop()


async def test_the_only_bot_pod_keeps_acting_through_a_redis_outage(server, cluster) -> None:
    process = Process("only", server, cluster)
    process.start()
    await until(lambda: process.coordinator.acting)
    process.redis.eval = down

    for _ in range(150):  # 1.5s: more than twice the lease
        assert process.coordinator.acting
        await asyncio.sleep(0.01)
    assert process.coordinator.degraded
    process.receive(1)
    await settle()
    assert process.handled == ["1"]
    await process.stop()


@pytest.mark.parametrize("peer", ["new-pending", "old-terminating"])
async def test_without_redis_another_bot_pod_appearing_fences_the_holder(
    server, cluster, peer
) -> None:
    """Kubernetes lists Pending and Terminating pods alike (peer_pods)."""
    process = Process("only", server, cluster)
    process.start()
    await until(lambda: process.coordinator.acting)
    process.redis.eval = down
    await asyncio.sleep(0.8)
    assert process.coordinator.acting

    cluster.pods.add(peer)
    await until(lambda: not process.coordinator.acting, timeout=0.5)
    process.receive(1)
    await settle()
    assert process.handled == []
    await process.stop()


async def test_without_redis_a_failing_kubernetes_api_fences_the_holder(server, cluster) -> None:
    process = Process("only", server, cluster)
    process.start()
    await until(lambda: process.coordinator.acting)
    process.redis.eval = down
    await asyncio.sleep(0.8)
    assert process.coordinator.acting

    cluster.api_down.add("only")
    await until(lambda: not process.coordinator.acting, timeout=0.5)
    await asyncio.sleep(0.5)
    assert not process.coordinator.acting, "no amount of failures turns into acting"
    await process.stop()


async def test_without_redis_a_stale_kubernetes_answer_fences_the_holder(server, cluster) -> None:
    process = Process("only", server, cluster)
    process.start()
    await until(lambda: process.coordinator.acting)
    process.redis.eval = down
    await asyncio.sleep(0.8)

    cluster.api_hangs.add("only")  # the call never returns
    elapsed = await until(lambda: not process.coordinator.acting, timeout=0.5)
    assert elapsed <= FAST["sole_pod_seconds"] + FAST["peer_interval"] + 0.05
    await process.stop()


async def test_redis_returning_to_a_sole_holder_hands_it_back_to_the_lease(
    server, cluster
) -> None:
    process = Process("only", server, cluster)
    process.start()
    await until(lambda: process.coordinator.acting)
    real_eval = process.redis.eval
    process.redis.eval = down
    await asyncio.sleep(0.9)  # the lease has expired; acting as the sole pod
    assert await process.redis.get(LEASE_KEY) is None
    overlap = Overlap(process)

    process.redis.eval = real_eval
    await until(lambda: not process.coordinator.degraded, timeout=1)
    assert await process.redis.get(LEASE_KEY) == b"only", "retook the lapsed lease"
    cluster.pods.add("new-pending")  # the lease, not Kubernetes, rules again
    await asyncio.sleep(0.3)
    assert process.coordinator.acting
    overlap.stop()
    assert not overlap.gap, "stopped acting on the way back"
    await process.stop()


async def test_a_peer_appearing_between_sole_pod_checks_never_acts_beside_the_holder(
    server, cluster
) -> None:
    """The interval after a sole-pod answer: a new pod starts, and Redis
    returns to the holder before its next check sees the pod."""
    holder = Process("holder", server, cluster, peer_interval=0.5, sole_pod_seconds=1.5)
    holder.start()
    await until(lambda: holder.coordinator.acting)
    real_eval = holder.redis.eval
    holder.redis.eval = down
    await asyncio.sleep(0.9)  # lease expired; acting on the last "only pod" answer
    assert holder.coordinator.acting
    assert await holder.redis.get(LEASE_KEY) is None

    newcomer = Process("newcomer", server, cluster)  # reaches Redis, finds it free
    overlap = Overlap(holder, newcomer)
    newcomer.start()
    await asyncio.sleep(0.1)  # before the holder's next check
    assert not newcomer.coordinator.acting, "no record, and the holder's pod exists"
    holder.redis.eval = real_eval  # Redis returns inside the interval
    await until(lambda: not holder.coordinator.degraded, timeout=1)
    assert await holder.redis.get(LEASE_KEY) == b"holder"
    await asyncio.sleep(0.5)
    assert holder.coordinator.acting and not newcomer.coordinator.acting

    await holder.exit()  # the roll: a warm handover with a record
    await until(lambda: newcomer.coordinator.acting, timeout=1)
    assert newcomer.coordinator.handover is not None
    assert not overlap.stop(), "two processes acted at once"
    await newcomer.stop()


async def test_redis_returning_with_the_lease_taken_stands_the_holder_down_at_once(
    server, cluster
) -> None:
    process = Process("only", server, cluster)
    process.start()
    await until(lambda: process.coordinator.acting)
    real_eval = process.redis.eval
    process.redis.eval = down
    await asyncio.sleep(0.9)
    assert process.coordinator.acting

    # Cannot happen while this pod exists (a taker needs a record or no other
    # pod); if it did anyway, the holder must yield on its first renewal.
    await process.redis.set(LEASE_KEY, "other")
    renewed = asyncio.Event()

    async def eval_then_note(*args, **kwargs):
        result = await real_eval(*args, **kwargs)
        renewed.set()
        return result

    process.redis.eval = eval_then_note
    await asyncio.wait_for(renewed.wait(), 1)
    assert not process.coordinator.acting, "sole-acting must end in the same step"
    await process.stop()
    assert await process.redis.get(LEASE_KEY) == b"other"


def losses(caplog) -> dict[str, int]:
    """Drop counts by reason, summed across the (rate-limited) log lines."""
    counts: dict[str, int] = {}
    for record in caplog.records:
        match = re.match(r"dropped (\d+) gateway events: (.*)", record.getMessage())
        if match:
            counts[match[2]] = counts.get(match[2], 0) + int(match[1])
    return counts


def held_renewals(process: Process) -> asyncio.Event:
    """Renewals wait until the returned event is set: a renewal running late."""
    release = asyncio.Event()
    real_eval = process.redis.eval

    async def late(script, *args, **kwargs):
        if script == leadership._RENEW:
            await release.wait()
        return await real_eval(script, *args, **kwargs)

    process.redis.eval = late
    return release


async def test_a_stall_past_the_margin_with_the_lease_kept_runs_each_event_once(
    server, caplog
) -> None:
    """The loop stalls past ttl - margin but not ttl: the lease is still this
    holder's, so what arrived during the stall runs, once."""
    process = Process("only", server, ttl=2.0, margin=1.5)
    process.start()
    await until(lambda: process.coordinator.acting)
    process.receive(1)
    await settle()

    time.sleep(0.8)  # the event loop stalls
    assert process.coordinator.fenced
    process.receive(2)  # delivered while the lease is unconfirmed
    await until(lambda: process.coordinator.acting, timeout=1)
    process.receive(3)
    await settle()

    assert process.handled == ["1", "2", "3"]
    assert process.gate.replayed == 1 and process.gate.dropped == 0
    assert losses(caplog) == {}
    await process.stop()


async def test_a_stall_past_the_lease_retakes_it_and_drops_what_it_buffered_loudly(
    server, caplog
) -> None:
    """Past ttl the lease lapsed: taking it back proves nothing about the
    gap, so nothing buffered is replayed, and the drop is logged."""
    process = Process("only", server)
    process.start()
    await until(lambda: process.coordinator.acting)

    time.sleep(0.8)  # longer than the 0.6s lease
    assert await process.redis.get(LEASE_KEY) is None
    process.receive(1)
    process.receive(2)
    await until(lambda: process.coordinator.acting, timeout=1)
    process.receive(3)
    await settle()

    assert await process.redis.get(LEASE_KEY) == b"only", "retaken"
    assert process.handled == ["3"]
    assert process.gate.replayed == 0 and process.gate.dropped == 2
    assert losses(caplog) == {
        "the lease had lapsed; taking it back does not prove continuity": 2
    }
    await process.stop()


async def test_a_late_renewal_that_finds_the_lease_taken_drops_what_it_buffered_loudly(
    server, caplog
) -> None:
    process = Process("only", server, ttl=3.0, margin=2.7)
    process.start()
    await until(lambda: process.coordinator.acting)
    release = held_renewals(process)
    await until(lambda: process.coordinator.fenced, timeout=1)
    process.receive(1)
    await settle()

    await process.redis.set(LEASE_KEY, "other")
    release.set()
    await until(lambda: not process.coordinator.fenced, timeout=1)
    assert not process.coordinator.acting
    assert process.handled == []
    assert losses(caplog) == {"the acting lease was lost to another bot": 1}
    await process.stop()


async def test_a_fenced_buffer_past_its_bounds_logs_what_it_drops(
    server, caplog, monkeypatch
) -> None:
    monkeypatch.setattr(leadership, "STANDBY_BUFFER_EVENTS", 3)
    monkeypatch.setattr(leadership, "STANDBY_BUFFER_SECONDS", 0.2)
    process = Process("only", server, ttl=3.0, margin=2.7)
    process.start()
    await until(lambda: process.coordinator.acting)
    release = held_renewals(process)
    await until(lambda: process.coordinator.fenced, timeout=1)

    process.receive(1)
    await settle()
    await asyncio.sleep(0.25)  # 1 is now older than the buffer holds
    for message_id in range(2, 7):  # 2 and 3 overflow
        process.receive(message_id)
    await settle()
    assert losses(caplog) == {
        "lease unconfirmed for over 0s": 1,
        "over 3 events buffered while the lease was unconfirmed": 1,
    }, "the first of each is logged at once"

    release.set()
    await until(lambda: process.coordinator.acting, timeout=1)
    await settle()
    assert process.handled == ["4", "5", "6"]
    assert losses(caplog) == {
        "lease unconfirmed for over 0s": 1,
        "over 3 events buffered while the lease was unconfirmed": 2,
    }, "the rate-limited rest are logged by the renewal"
    assert process.gate.dropped == 3
    await process.stop()


async def test_the_kubernetes_api_answering_before_redis_replays_what_was_buffered(
    server, cluster, caplog
) -> None:
    """Redis and the API both down; the API recovers first. A fresh sole-pod
    answer proves nobody else acted (no process takes the lease while this
    pod exists), so the buffer runs, in order, before the next live event."""
    process = Process("only", server, cluster)
    process.start()
    await until(lambda: process.coordinator.acting)
    process.redis.eval = down
    cluster.api_down.add("only")
    await until(lambda: process.coordinator.fenced, timeout=1)
    for message_id in (1, 2, 3):
        process.receive(message_id)
    await settle()
    assert process.handled == []

    cluster.api_down.clear()
    await until(lambda: process.coordinator.acting, timeout=1)
    process.receive(4)
    await settle()
    assert process.handled == ["1", "2", "3", "4"]
    assert process.gate.replayed == 3 and losses(caplog) == {}
    await process.stop()


async def test_a_sole_pod_replay_drops_interactions_too_old_to_acknowledge(
    server, cluster, caplog
) -> None:
    process = Process("only", server, cluster)
    process.start()
    await until(lambda: process.coordinator.acting)
    process.redis.eval = down
    cluster.api_down.add("only")
    await until(lambda: process.coordinator.fenced, timeout=1)
    replayed: list[str] = []
    process.gate._dispatch = lambda event: replayed.append(event) or leadership._done()
    now = time.monotonic()
    process.gate._fenced.extend(
        [
            (now - 5, "INTERACTION_CREATE:1", "stale interaction"),
            (now, "MESSAGE_CREATE:2", "message"),
        ]
    )

    cluster.api_down.clear()
    await until(lambda: process.coordinator.acting, timeout=1)
    assert replayed == ["message"]
    assert losses(caplog) == {"interactions buffered past the time to acknowledge them": 1}
    await process.stop()


async def test_a_stopping_holder_replays_nothing_on_a_sole_pod_answer(
    server, cluster, caplog
) -> None:
    process = Process("only", server, cluster)
    process.start()
    await until(lambda: process.coordinator.acting)
    process.redis.eval = down
    cluster.api_down.add("only")
    await until(lambda: process.coordinator.fenced, timeout=1)
    process.receive(1)
    await settle()

    process.coordinator.stopping = True  # SIGTERM, before its handover call
    process.coordinator._holding = True
    cluster.api_down.clear()
    await process.coordinator._check_peers()  # a sole-pod answer arrives
    await settle()
    assert process.handled == []
    await process.stop()
    assert losses(caplog) == {"stopping; left to the successor's standby buffer": 1}


async def test_a_renewal_after_a_lost_retake_reply_does_not_replay(
    server, cluster, caplog
) -> None:
    """The lease lapsed; a renewal retakes it in Redis but its reply is lost.
    The next renewal finds the lease this holder's, which proves nothing."""
    process = Process("only", server, cluster, ttl=3.0, margin=2.7)
    process.start()
    await until(lambda: process.coordinator.acting)
    cluster.api_down.add("only")  # no sole-pod answer either
    real_eval = process.redis.eval
    release = asyncio.Event()
    lost_reply = []

    async def flaky(script, *args, **kwargs):
        if script == leadership._RENEW:
            if not lost_reply:
                lost_reply.append(await real_eval(script, *args, **kwargs))
                raise RedisConnectionError("reply lost")
            await release.wait()
        return await real_eval(script, *args, **kwargs)

    await process.redis.delete(LEASE_KEY)  # lapsed
    process.redis.eval = flaky
    await until(lambda: process.coordinator.fenced, timeout=1)
    assert lost_reply == [leadership._RETAKEN], "the retake ran in Redis"
    process.receive(1)
    await settle()

    release.set()
    await until(lambda: process.coordinator.acting, timeout=1)
    await settle()
    assert process.handled == []
    assert losses(caplog) == {
        "a renewal failed while the lease was unconfirmed; it may have been retaken": 1
    }
    await process.stop()


async def test_a_timed_send_due_after_the_holder_fenced_goes_to_the_successor_once(
    server, cluster
) -> None:
    """Fenced long before SIGTERM, the record says when it last could act."""
    old, new = await incumbent_and_standby(server, cluster)
    real_eval = old.redis.eval
    old.redis.eval = down
    await until(lambda: not old.coordinator.acting, timeout=1)  # sees the standby
    await asyncio.sleep(0.05)
    due = time.time()  # falls due while old is fenced
    old_decides = asyncio.create_task(old.coordinator.should_send(due, wait=2))
    await asyncio.sleep(0.3)

    old.redis.eval = real_eval  # it reaches Redis for its handover
    await old.exit()
    await until(lambda: new.coordinator.acting, timeout=1)
    assert new.coordinator.handover.stopped_at <= due < time.time() - 0.3
    assert not await old_decides
    assert await new.coordinator.should_send(due, wait=0.1)
    await new.stop()


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


async def test_a_failed_handover_waits_for_the_old_pod_and_replays_nothing(
    server, cluster
) -> None:
    old, new = await incumbent_and_standby(server, cluster)
    overlap = Overlap(old, new)
    for message_id in range(1, 11):
        old.receive(message_id)
    await settle()
    old.redis.eval = down  # no record, no release
    await old.stop()
    for message_id in range(11, 21):  # the gap
        new.receive(message_id)
        old.receive(message_id)
    await asyncio.sleep(0.9)  # the lease has expired
    assert not new.coordinator.acting, "the old pod is still draining"

    cluster.pods.discard("old")
    await until(lambda: new.coordinator.acting, timeout=1)
    await settle()
    assert not overlap.stop()
    assert old.handled == [str(i) for i in range(1, 11)]
    assert new.handled == [], "without a record nothing is replayed: 11-20 are lost"
    await new.stop()


async def test_a_handover_record_hands_over_once(server, cluster) -> None:
    """A later taker, within the record's minute, still needs the pod check."""
    first, second = await incumbent_and_standby(server, cluster)
    await first.exit()
    await until(lambda: second.coordinator.acting)
    assert second.coordinator.handover is not None
    assert await second.redis.get(leadership.RELEASED_AT_KEY) is None, "consumed"

    third = Process("third", server, cluster)
    third.start()
    await asyncio.sleep(0.05)
    overlap = Overlap(second, third)
    second.redis.eval = down  # its lease lapses with the record's minute unspent
    await asyncio.sleep(0.9)
    assert not third.coordinator.acting, "no record to take, and second still exists"
    assert not overlap.stop(), "two processes acted at once"
    await second.exit()
    await until(lambda: third.coordinator.acting, timeout=1)
    assert third.coordinator.handover is None
    await third.stop()


async def test_a_failing_kubernetes_api_never_lets_a_standby_take_a_free_lease(
    server, cluster
) -> None:
    old, new = await incumbent_and_standby(server, cluster)
    cluster.api_down.add("new")  # only the standby's calls fail
    old.task.cancel()  # crashed: no record; its pod remains
    await asyncio.sleep(1.0)
    assert not new.coordinator.acting
    assert not new.coordinator.can_take_over, "not Ready: cannot check for other pods"

    cluster.pods.discard("old")  # gone, but the standby cannot tell
    await asyncio.sleep(0.5)
    assert not new.coordinator.acting, "an API error is never an answer"
    calls = cluster.calls
    cluster.api_down.clear()
    await until(lambda: new.coordinator.acting, timeout=1)
    assert cluster.calls > calls
    await new.stop()


async def test_with_no_handover_record_it_waits_for_the_old_pod(
    server, cluster, caplog
) -> None:
    old, new = await incumbent_and_standby(server, cluster)
    old.task.cancel()  # crashed: no release, no record
    await asyncio.sleep(1.0)  # the lease has expired
    assert not new.coordinator.acting, "the old pod may still be running"
    assert new.coordinator.can_take_over, "Ready, so Kubernetes stops the old pod"
    new.receive(1)  # arrives while waiting
    await settle()

    cluster.pods.discard("old")
    await until(lambda: new.coordinator.acting, timeout=1)
    await settle()
    assert new.handled == [], "without a record nothing is replayed"
    assert losses(caplog) == {"no handover record; the predecessor may have handled them": 1}
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


async def test_a_holder_stopping_after_losing_its_lease_leaves_no_record(server) -> None:
    """Only a record from the process that held the lease may hand it over."""
    old = Process("old", server)
    old.start()
    await until(lambda: old.coordinator.acting)
    await old.redis.set(LEASE_KEY, "other")  # before its next renewal notices
    await old.stop()
    assert await old.redis.get(LEASE_KEY) == b"other"
    assert await old.redis.get(leadership.RELEASED_AT_KEY) is None


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
    old, new = await incumbent_and_standby(server, **STEADY)
    seen: list[str] = []

    async def on_ready(_event) -> None:
        seen.append("ready")

    new.app.event_manager.subscribe(hikari.ShardReadyEvent, on_ready)
    await new.app.event_manager.dispatch(_event(hikari.ShardReadyEvent))
    assert seen == ["ready"]
    await old.stop()
    await new.stop()


async def test_a_standby_initializes_the_framework_but_defers_app_start_up(server) -> None:
    old, new = await incumbent_and_standby(server, **STEADY)
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
    old, new = await incumbent_and_standby(server, **STEADY)
    leadership.install(old.coordinator)
    assert await leadership.should_send(time.time(), wait=0.2)
    leadership.install(new.coordinator)
    assert not await leadership.should_send(time.time(), wait=0.1), "standby must not"
    await old.stop()
    await new.stop()


async def test_a_stopping_process_sends_nothing(server) -> None:
    old, new = await incumbent_and_standby(server, **STEADY)
    await old.stop()
    leadership.install(old.coordinator)
    assert not await leadership.should_send(time.time(), wait=0.2)
    await new.stop()


async def test_a_message_due_before_the_handover_is_left_to_the_incumbent(server) -> None:
    old, new = await incumbent_and_standby(server, **STEADY)
    due = time.time()
    await asyncio.sleep(0.01)
    await old.stop()  # it was acting when this fell due

    leadership.install(new.coordinator)
    assert not await leadership.should_send(due, wait=0.5)
    await new.stop()


async def test_a_message_due_inside_the_gap_goes_to_the_successor(server) -> None:
    old, new = await incumbent_and_standby(server, **STEADY)
    await old.stop()
    due = time.time() + 0.001  # fell due before the successor took over
    leadership.install(new.coordinator)
    assert await leadership.should_send(due, wait=0.5)
    await new.stop()


async def test_without_a_handover_what_fell_due_while_the_old_pod_ran_is_left_to_it(
    server, cluster
) -> None:
    cluster.pods.add("legacy")  # the first deploy of this code
    process = Process("first", server, cluster)
    process.start()  # lease free, no record
    await asyncio.sleep(0.05)
    due_while_old_ran = time.time()
    await asyncio.sleep(0.1)  # the next peer check still sees the old pod
    cluster.pods.discard("legacy")
    await until(lambda: process.coordinator.acting, timeout=1)
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

    new.redis.eval = down
    new.coordinator._redis_ok_at -= 10  # its last answer was a while ago
    await asyncio.sleep(0.05)
    assert not ready_to_act(bot), "Kubernetes must keep the old bot"
    assert old.coordinator.acting
    await old.stop()
    await new.stop()
