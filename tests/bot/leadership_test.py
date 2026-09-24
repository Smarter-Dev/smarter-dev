"""Two connected bots during a deploy: every event runs once, none is lost.

The overlap is simulated with two real hikari event managers fed the same raw
gateway payloads, as two gateway sessions would be, sharing one (fake) Redis.
"""

from __future__ import annotations

import asyncio
from unittest.mock import Mock

import attrs
import fakeredis
import fakeredis.aioredis
import hikari
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from smarter_dev.bot import leadership
from smarter_dev.bot.client import gateway_connected
from smarter_dev.bot.leadership import LEADER_KEY
from smarter_dev.bot.leadership import EventGate
from smarter_dev.bot.leadership import Leadership

FAST = {"ttl": 0.4, "renew_interval": 0.1, "poll_interval": 0.02}


def message_payload(message_id: int, content: str = "spam") -> dict:
    return {
        "id": str(message_id),
        "channel_id": "20",
        "guild_id": "30",
        "author": {"id": "40", "username": "u", "discriminator": "0", "avatar": None},
        "member": {"roles": [], "joined_at": "2026-01-01T00:00:00+00:00", "deaf": False, "mute": False},
        "content": content,
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


class Bot:
    """One connected bot: a real hikari event manager behind an EventGate."""

    def __init__(self, name: str, server: fakeredis.FakeServer, **lease) -> None:
        self.name = name
        self.app = hikari.GatewayBot("x" * 60, banner=None)
        self.redis = fakeredis.aioredis.FakeRedis(server=server)
        self.lease = Leadership(self.redis, name, **(lease or FAST))
        self.gate = EventGate(self.app.event_manager, self.lease)
        self.handled: list[str] = []
        self.shard = Mock(id=0)

        async def on_message(event: hikari.GuildMessageCreateEvent) -> None:
            self.handled.append(str(event.message_id))

        self.app.event_manager.subscribe(hikari.GuildMessageCreateEvent, on_message)

    def receive(self, name: str, payload: dict) -> None:
        self.app.event_manager.consume_raw_event(name, self.shard, payload)


async def settle() -> None:
    # Let the dispatch tasks created by consume_raw_event claim and run.
    for _ in range(20):
        await asyncio.sleep(0)
    await asyncio.sleep(0.02)


@pytest.fixture
def server() -> fakeredis.FakeServer:
    return fakeredis.FakeServer()


async def test_two_connected_bots_run_each_event_once(server) -> None:
    old, new = Bot("old", server), Bot("new", server)
    for message_id in range(100, 150):
        old.receive("MESSAGE_CREATE", message_payload(message_id))
        new.receive("MESSAGE_CREATE", message_payload(message_id))
    await settle()

    handled = old.handled + new.handled
    assert sorted(handled) == [str(i) for i in range(100, 150)]
    assert old.gate.claimed + new.gate.claimed == 50
    assert old.gate.left_to_other + new.gate.left_to_other == 50


async def test_an_event_only_one_bot_receives_still_runs(server) -> None:
    old, new = Bot("old", server), Bot("new", server)
    new.receive("MESSAGE_CREATE", message_payload(7))
    await settle()
    assert new.handled == ["7"]
    assert old.handled == []


async def test_events_around_the_handover_are_neither_lost_nor_doubled(server) -> None:
    old, new = Bot("old", server), Bot("new", server)
    for message_id in range(1, 41):
        if message_id == 20:
            old.gate.close()  # SIGTERM lands mid-stream
        # Gateway delivery order between sessions is not fixed; alternate it.
        first, second = (old, new) if message_id % 2 else (new, old)
        first.receive("MESSAGE_CREATE", message_payload(message_id))
        second.receive("MESSAGE_CREATE", message_payload(message_id))
    await settle()

    assert sorted(old.handled + new.handled, key=int) == [str(i) for i in range(1, 41)]
    assert all(int(i) < 20 for i in old.handled)


async def test_sigterm_during_a_moderation_action_finishes_it_without_a_repeat(
    server,
) -> None:
    old, new = Bot("old", server), Bot("new", server)
    action_started = asyncio.Event()
    finish_action = asyncio.Event()
    actions: list[str] = []

    async def moderate(event: hikari.GuildMessageCreateEvent) -> None:
        action_started.set()
        await finish_action.wait()  # e.g. deleting the message and timing out
        actions.append(f"{event.app is old.app and 'old' or 'new'}:{event.message_id}")

    old.app.event_manager.subscribe(hikari.GuildMessageCreateEvent, moderate)
    new.app.event_manager.subscribe(hikari.GuildMessageCreateEvent, moderate)

    old.receive("MESSAGE_CREATE", message_payload(1))
    await asyncio.wait_for(action_started.wait(), 1)
    new.receive("MESSAGE_CREATE", message_payload(1))  # the other session's copy

    old.gate.close()
    await old.lease.step_down()
    drain = asyncio.create_task(old.gate.drain(timeout=2))
    await settle()
    assert not drain.done(), "drain must wait for the running action"

    finish_action.set()
    assert await drain == 0
    new.receive("MESSAGE_CREATE", message_payload(2))
    old.receive("MESSAGE_CREATE", message_payload(2))
    await settle()

    assert actions == ["old:1", "new:2"]


async def test_drain_reports_handlers_that_outlast_the_grace(server) -> None:
    bot = Bot("old", server)

    async def stuck(_event) -> None:
        await asyncio.Event().wait()

    bot.app.event_manager.subscribe(hikari.GuildMessageCreateEvent, stuck)
    bot.receive("MESSAGE_CREATE", message_payload(1))
    await settle()
    bot.gate.close()
    assert await bot.gate.drain(timeout=0.05) == 1


async def test_claims_fail_open_when_redis_is_down(server) -> None:
    bot = Bot("only", server)

    async def down(*_args, **_kwargs):
        raise RedisConnectionError("redis down")

    bot.redis.set = down
    bot.receive("MESSAGE_CREATE", message_payload(1))
    await settle()
    assert bot.handled == ["1"]


def _event(cls: type) -> hikari.Event:
    return cls(**{f.name.lstrip("_"): Mock() for f in attrs.fields(cls) if f.init})


async def test_connection_state_events_reach_every_bot(server) -> None:
    old, new = Bot("old", server), Bot("new", server)
    seen: list[str] = []

    for bot in (old, new):
        async def on_ready(_event, name=bot.name) -> None:
            seen.append(name)

        bot.app.event_manager.subscribe(hikari.ShardReadyEvent, on_ready)

    token = leadership._raw_event.set(("READY", "same-key-on-both"))
    try:
        await old.app.event_manager.dispatch(_event(hikari.ShardReadyEvent))
        await new.app.event_manager.dispatch(_event(hikari.ShardReadyEvent))
    finally:
        leadership._raw_event.reset(token)
    assert sorted(seen) == ["new", "old"]


async def test_started_event_waits_for_the_lease(server) -> None:
    bot = Bot("new", server)
    started: list[bool] = []

    async def on_started(_event) -> None:
        started.append(True)

    bot.app.event_manager.subscribe(hikari.StartedEvent, on_started)
    await bot.app.event_manager.dispatch(_event(hikari.StartedEvent))
    assert started == []

    runner = asyncio.create_task(bot.lease.run(bot.gate.on_acquire))
    await asyncio.sleep(0.1)
    assert bot.lease.is_leader
    assert started == [True]
    await bot.lease.step_down()
    runner.cancel()


async def _run(lease: Leadership, acquired: list[str], name: str) -> asyncio.Task:
    async def on_acquire() -> None:
        acquired.append(name)

    return asyncio.create_task(lease.run(on_acquire))


async def test_one_leader_then_a_prompt_handover_on_step_down(server) -> None:
    old = Leadership(fakeredis.aioredis.FakeRedis(server=server), "old", **FAST)
    new = Leadership(fakeredis.aioredis.FakeRedis(server=server), "new", **FAST)
    acquired: list[str] = []
    old_task = await _run(old, acquired, "old")
    await asyncio.sleep(0.05)
    new_task = await _run(new, acquired, "new")
    await asyncio.sleep(0.3)
    assert (old.is_leader, new.is_leader) == (True, False)

    await old.step_down()
    await asyncio.sleep(0.1)  # a few polls, far less than the ttl
    assert (old.is_leader, new.is_leader) == (False, True)
    assert acquired == ["old", "new"]
    for task in (old_task, new_task):
        task.cancel()


async def test_a_crashed_leader_is_replaced_after_its_lease_expires(server) -> None:
    crashed = Leadership(fakeredis.aioredis.FakeRedis(server=server), "crashed", **FAST)
    standby = Leadership(fakeredis.aioredis.FakeRedis(server=server), "standby", **FAST)
    acquired: list[str] = []
    crashed_task = await _run(crashed, acquired, "crashed")
    await asyncio.sleep(0.05)
    standby_task = await _run(standby, acquired, "standby")
    crashed_task.cancel()  # dies without releasing

    await asyncio.sleep(0.2)
    assert not standby.is_leader, "must not take over before the lease expires"
    await asyncio.sleep(0.5)
    assert standby.is_leader
    standby_task.cancel()


async def test_a_leader_that_lost_its_lease_stands_by(server) -> None:
    redis = fakeredis.aioredis.FakeRedis(server=server)
    lease = Leadership(redis, "slow", **FAST)
    task = await _run(lease, [], "slow")
    await asyncio.sleep(0.05)
    assert lease.is_leader

    # Its lease expired during a pause and another bot took it.
    await redis.set(LEADER_KEY, "other")
    await asyncio.sleep(0.2)
    assert not lease.is_leader

    await lease.step_down()
    assert await redis.get(LEADER_KEY) == b"other", "must not release another's lease"
    task.cancel()


async def test_redis_outage_keeps_a_leader_and_eventually_elects_one(server) -> None:
    async def down(*_args, **_kwargs):
        raise RedisConnectionError("redis down")

    leader = Leadership(fakeredis.aioredis.FakeRedis(server=server), "leader", **FAST)
    task = await _run(leader, [], "leader")
    await asyncio.sleep(0.05)
    leader._redis.eval = down
    await asyncio.sleep(0.6)
    assert leader.is_leader, "an outage must not silence the acting bot"
    task.cancel()

    orphan = Leadership(fakeredis.aioredis.FakeRedis(server=server), "orphan", **FAST)
    orphan._redis.set = down
    orphan._redis.eval = down
    acquired: list[str] = []
    orphan_task = await _run(orphan, acquired, "orphan")
    await asyncio.sleep(0.2)
    assert not orphan.is_leader
    await asyncio.sleep(0.4)
    assert orphan.is_leader and acquired == ["orphan"]
    orphan_task.cancel()


async def test_a_leader_retakes_a_lease_that_lapsed_unclaimed(server) -> None:
    redis = fakeredis.aioredis.FakeRedis(server=server)
    lease = Leadership(redis, "leader", **FAST)
    acquired: list[str] = []
    task = await _run(lease, acquired, "leader")
    await asyncio.sleep(0.05)
    await redis.delete(LEADER_KEY)  # lapsed during a pause; nobody took it
    await asyncio.sleep(0.2)
    assert lease.is_leader
    assert await redis.get(LEADER_KEY) == b"leader"
    assert acquired == ["leader"], "the start-up hook must not rerun"
    task.cancel()


def test_scheduled_work_runs_without_a_lease_in_use() -> None:
    leadership.install(None)
    assert leadership.is_leader()


def test_scheduled_work_follows_the_lease() -> None:
    lease = Leadership(Mock(), "x")
    leadership.install(lease)
    try:
        assert not leadership.is_leader()
        lease.is_leader = True
        assert leadership.is_leader()
    finally:
        leadership.install(None)


def test_ready_tracks_the_gateway_connection() -> None:
    shard = Mock(is_alive=True, is_connected=True)
    bot = Mock(shards={0: shard})
    assert gateway_connected(bot)

    shard.is_connected = False  # disconnected, hikari reconnecting
    assert not gateway_connected(bot)

    shard.is_connected = True  # resumed
    assert gateway_connected(bot)

    assert not gateway_connected(Mock(shards={}))  # still starting


@pytest.fixture
def installed_lease(server):
    redis = fakeredis.aioredis.FakeRedis(server=server)
    leadership.install(Leadership(redis, "bot", **FAST))
    yield redis
    leadership.install(None)


def _services(cls: type) -> list:
    return [cls(Mock(), None, Mock()) for _ in range(2)]


async def test_a_scheduled_message_both_bots_queued_is_sent_once(installed_lease) -> None:
    from smarter_dev.bot.services.scheduled_message_service import (
        ScheduledMessageService,
    )

    sent: list[str] = []
    message = {"id": "m1", "scheduled_time": "2026-09-24T20:00:00Z", "title": "t"}
    services = _services(ScheduledMessageService)
    for service in services:
        async def send(data, name=str(id(service))) -> None:
            sent.append(data["id"])

        service._send_scheduled_message = send
    await asyncio.gather(*(s._queue_and_send_message(dict(message)) for s in services))
    assert sent == ["m1"]


async def test_a_challenge_and_a_quest_are_announced_once(installed_lease) -> None:
    from smarter_dev.bot.services.challenge_service import ChallengeService
    from smarter_dev.bot.services.quests_service import QuestService

    announced: list[str] = []

    async def announce(data) -> None:
        announced.append(data["id"])

    challenges = _services(ChallengeService)
    quests = _services(QuestService)
    for service in challenges:
        service._announce_challenge = announce
    for service in quests:
        service._announce_quest = announce
    challenge = {"id": "c1", "release_time": "2026-09-24T20:00:00Z", "title": "t"}
    quest = {"id": "q1", "release_time": "2026-09-24T20:00:00Z"}
    await asyncio.gather(
        *(s._queue_and_announce_challenge(dict(challenge)) for s in challenges),
        *(s._queue_and_announce_quest(dict(quest)) for s in quests),
    )
    assert sorted(announced) == ["c1", "q1"]


async def test_a_due_repeating_message_is_sent_once_and_can_retry_next_minute(
    installed_lease,
) -> None:
    from smarter_dev.bot.services.repeating_message_service import (
        RepeatingMessageService,
    )

    sent: list[str] = []
    due = [{"id": "r1", "next_send_time": "2026-09-24T20:00:00Z"}]
    services = _services(RepeatingMessageService)
    for service in services:
        async def get_due() -> list:
            return [dict(item) for item in due]

        async def process(data) -> None:
            sent.append(data["id"])

        service._get_due_repeating_messages = get_due
        service._process_repeating_message = process
    await asyncio.gather(*(s._check_and_send_due_messages() for s in services))
    assert sent == ["r1"]

    keys = [k async for k in installed_lease.scan_iter("*repeating-message*")]
    assert 0 < await installed_lease.pttl(keys[0]) < 60_000, "must lapse before the next check"
