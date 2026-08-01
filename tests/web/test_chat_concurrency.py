"""Distributed sub-agent semaphore behaviour: FIFO, hard cap, and fencing.

These exercise the real Lua scripts.  A live Redis is used when
``SMARTER_DEV_TEST_REDIS_URL`` (or a local server on the default URL) is
reachable; otherwise ``fakeredis`` provides a scripting-capable stand-in so the
guarantees are still verified in CI.  Everything synchronises on awaited task
completion or on explicit Redis state, never on wall-clock sleeps.
"""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
import pytest_asyncio

from smarter_dev.web.chat import jobs as chat_jobs
from smarter_dev.web.chat.concurrency import MAX_RUNNING_SUBAGENTS
from smarter_dev.web.chat.concurrency import Lease
from smarter_dev.web.chat.concurrency import RedisSubagentSemaphore
from smarter_dev.web.chat.concurrency import SemaphoreUnavailable

DEADLINE = 10.0


async def _open_redis():
    url = os.environ.get("SMARTER_DEV_TEST_REDIS_URL") or os.environ.get(
        "TEST_REDIS_URL"
    )
    if url:
        import redis.asyncio as redis_asyncio

        client = redis_asyncio.Redis.from_url(url, decode_responses=True)
        await client.ping()
        return client
    fake = pytest.importorskip("fakeredis.aioredis")
    return fake.FakeRedis(decode_responses=True)


@pytest_asyncio.fixture
async def semaphore():
    client = await _open_redis()
    instance = RedisSubagentSemaphore(
        client, namespace=f"test:chat:subagents:{uuid4().hex}", lease_seconds=60
    )
    try:
        yield instance
    finally:
        await client.delete(
            instance.leases_key,
            instance.queue_key,
            instance.fence_key,
            instance.queue_seq_key,
        )
        close = getattr(client, "aclose", None) or client.close
        await close()


async def _fill(semaphore, count=MAX_RUNNING_SUBAGENTS, prefix="holder"):
    leases = []
    for index in range(count):
        lease = await semaphore.acquire(ticket=f"{prefix}-{index}", wait=False)
        assert lease is not None
        leases.append(lease)
    return leases


@pytest.mark.asyncio
async def test_semaphore_admits_exactly_five_concurrent_holders(semaphore):
    held = await _fill(semaphore)

    assert len(held) == 5
    assert await semaphore.redis.zcard(semaphore.leases_key) == 5
    assert [lease.fence for lease in held] == sorted(lease.fence for lease in held)
    assert await semaphore.acquire(ticket="sixth", wait=False) is None
    assert await semaphore.redis.zcard(semaphore.leases_key) == 5

    await held[0].release()

    assert await semaphore.redis.zcard(semaphore.leases_key) == 4
    sixth = await semaphore.acquire(ticket="sixth", wait=False)
    assert sixth is not None
    assert sixth.fence > max(lease.fence for lease in held)
    assert await semaphore.redis.zcard(semaphore.leases_key) == 5


@pytest.mark.asyncio
async def test_only_the_queue_head_may_take_a_freed_slot(semaphore):
    held = await _fill(semaphore)
    # Registration order is the arrival order the FIFO must honour.
    for ticket in ("first", "second", "third"):
        assert await semaphore.acquire(ticket=ticket, wait=False) is None

    await held[0].release()
    assert await semaphore.acquire(ticket="third", wait=False) is None
    assert await semaphore.acquire(ticket="second", wait=False) is None
    first = await semaphore.acquire(ticket="first", wait=False)
    assert first is not None

    await held[1].release()
    assert await semaphore.acquire(ticket="third", wait=False) is None
    second = await semaphore.acquire(ticket="second", wait=False)
    assert second is not None
    assert second.fence > first.fence


@pytest.mark.asyncio
async def test_waiting_workers_acquire_in_arrival_order(semaphore):
    held = await _fill(semaphore)
    order = ["alpha", "beta", "gamma"]
    for ticket in order:
        assert await semaphore.acquire(ticket=ticket, wait=False) is None

    waiters = {
        ticket: asyncio.create_task(semaphore.acquire(ticket=ticket, wait=True))
        for ticket in order
    }
    for lease in held[:3]:
        await lease.release()

    done, pending = await asyncio.wait(waiters.values(), timeout=DEADLINE)
    assert not pending, "waiters never acquired their slot"
    granted = {ticket: task.result() for ticket, task in waiters.items()}
    assert all(lease is not None for lease in granted.values())
    fences = [granted[ticket].fence for ticket in order]
    assert fences == sorted(fences), f"FIFO violated: {fences}"
    assert len(done) == 3
    assert await semaphore.redis.zcard(semaphore.leases_key) == 5
    assert await semaphore.redis.zcard(semaphore.queue_key) == 0


@pytest.mark.asyncio
async def test_duplicate_delivery_never_receives_a_second_fence(semaphore):
    child = str(uuid4())
    lease = await semaphore.acquire(ticket=child, wait=False)

    assert lease is not None
    assert await semaphore.acquire(ticket=child, wait=False) is None
    assert await semaphore.redis.zcard(semaphore.leases_key) == 1
    # The duplicate must not even take a queue position behind itself.
    assert await semaphore.redis.zcard(semaphore.queue_key) == 0


@pytest.mark.asyncio
async def test_stale_fence_cannot_release_or_extend_the_current_holder(semaphore):
    ticket = str(uuid4())
    original = await semaphore.acquire(ticket=ticket, wait=False)
    await original.release()
    current = await semaphore.acquire(ticket=ticket, wait=False)
    assert current.fence > original.fence
    stale = Lease(semaphore, ticket, original.fence, current.expires_at_ms)

    await stale.release()

    assert await semaphore.redis.zcard(semaphore.leases_key) == 1
    assert await semaphore.heartbeat(stale) == (False, 0)
    assert await current.heartbeat() is True
    members = await semaphore.redis.zrange(semaphore.leases_key, 0, -1)
    assert members == [f"{ticket}:{current.fence}"]


class _InstantSleepAsyncio:
    """Proxy asyncio for one module so watch intervals do not gate the test."""

    def __getattr__(self, name):
        return getattr(asyncio, name)

    async def sleep(self, _delay, *_args, **_kwargs):
        return None


@pytest.mark.asyncio
async def test_expired_lease_frees_its_slot_and_is_reported_lost(
    semaphore, monkeypatch
):
    held = await _fill(semaphore)
    abandoned = held[2]
    member = f"{abandoned.ticket}:{abandoned.fence}"
    await semaphore.redis.zadd(semaphore.leases_key, {member: 1})

    replacement = await semaphore.acquire(ticket="replacement", wait=False)

    assert replacement is not None, "expired lease did not free the global slot"
    assert await semaphore.redis.zcard(semaphore.leases_key) == 5
    assert await abandoned.heartbeat() is False
    monkeypatch.setattr(chat_jobs, "asyncio", _InstantSleepAsyncio())
    with pytest.raises(SemaphoreUnavailable):
        await chat_jobs._lease_watch(abandoned, uuid4())


@pytest.mark.asyncio
async def test_cancelled_wait_leaves_no_queue_entry(semaphore):
    await _fill(semaphore)
    waiter = asyncio.create_task(
        semaphore.acquire(ticket="abandoned", wait=True, timeout=0.0)
    )

    assert await asyncio.wait_for(waiter, timeout=DEADLINE) is None
    assert await semaphore.redis.zscore(semaphore.queue_key, "abandoned") is None


@pytest.mark.asyncio
async def test_redis_failures_fail_closed(semaphore):
    class BrokenRedis:
        async def eval(self, *_args):
            raise ConnectionError("redis is gone")

        async def zrem(self, *_args):
            raise ConnectionError("redis is gone")

    broken = RedisSubagentSemaphore(BrokenRedis())
    lease = Lease(broken, "ticket", 1, 0)

    with pytest.raises(SemaphoreUnavailable):
        await broken.acquire(ticket="ticket", wait=False)
    with pytest.raises(SemaphoreUnavailable):
        await broken.heartbeat(lease)
    with pytest.raises(SemaphoreUnavailable):
        await broken.release(lease)
    with pytest.raises(SemaphoreUnavailable):
        await broken.cancel("ticket")


@pytest.mark.asyncio
async def test_lease_context_manager_releases_once(semaphore):
    async with await semaphore.acquire(ticket="scoped", wait=False) as lease:
        assert await semaphore.redis.zcard(semaphore.leases_key) == 1

    assert lease.released is True
    assert await semaphore.redis.zcard(semaphore.leases_key) == 0
    await lease.release()
    assert await semaphore.redis.zcard(semaphore.leases_key) == 0
