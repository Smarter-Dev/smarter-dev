"""Fair distributed hard-five semaphore for web Chat sub-agents."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from uuid import uuid4

MAX_RUNNING_SUBAGENTS = 5

# Redis TIME avoids worker clock skew. Queue sequence provides deterministic FIFO.
_ACQUIRE = """
local leases, queue, fence_key, queue_seq = KEYS[1], KEYS[2], KEYS[3], KEYS[4]
local ticket, lease_ms, cap, queue_ttl_ms = ARGV[1], tonumber(ARGV[2]), tonumber(ARGV[3]), tonumber(ARGV[4])
local redis_time = redis.call('TIME')
local now = tonumber(redis_time[1]) * 1000 + math.floor(tonumber(redis_time[2]) / 1000)
redis.call('ZREMRANGEBYSCORE', leases, '-inf', now)
-- Waiting tickets are leases too: a killed worker must not remain the global
-- FIFO head forever. Wait timeout is shorter than lease_ms.
redis.call('ZREMRANGEBYSCORE', queue, '-inf', now - queue_ttl_ms)
-- A duplicate delivery for the same durable child must never receive a second
-- fence while its first lease is alive.
local active = redis.call('ZRANGEBYSCORE', leases, now, '+inf')
for _, member in ipairs(active) do
  if string.sub(member, 1, string.len(ticket) + 1) == ticket .. ':' then
    return {0, now}
  end
end
if redis.call('ZSCORE', queue, ticket) == false then
  local sequence = redis.call('INCR', queue_seq)
  -- Millisecond time provides stale cleanup; the fractional monotonic sequence
  -- makes same-millisecond arrivals deterministic FIFO.
  redis.call('ZADD', queue, now + ((sequence % 1000) / 1000), ticket)
end
local first = redis.call('ZRANGE', queue, 0, 0)[1]
if first == ticket and redis.call('ZCARD', leases) < cap then
  redis.call('ZREM', queue, ticket)
  local fence = redis.call('INCR', fence_key)
  local expiry = now + lease_ms
  redis.call('ZADD', leases, expiry, ticket .. ':' .. fence)
  return {fence, expiry}
end
return {0, now}
"""
_RELEASE = """
return redis.call('ZREM', KEYS[1], ARGV[1] .. ':' .. ARGV[2])
"""
_HEARTBEAT = """
local member, lease_ms = ARGV[1] .. ':' .. ARGV[2], tonumber(ARGV[3])
if redis.call('ZSCORE', KEYS[1], member) == false then return {0, 0} end
local redis_time = redis.call('TIME')
local now = tonumber(redis_time[1]) * 1000 + math.floor(tonumber(redis_time[2]) / 1000)
local expiry = now + lease_ms
redis.call('ZADD', KEYS[1], expiry, member)
return {1, expiry}
"""


class SemaphoreUnavailable(RuntimeError):
    """Execution must fail closed or remain durably queued."""


@dataclass(slots=True)
class Lease:
    semaphore: RedisSubagentSemaphore
    ticket: str
    fence: int
    expires_at_ms: int
    released: bool = False

    async def heartbeat(self) -> bool:
        ok, expiry = await self.semaphore.heartbeat(self)
        if ok:
            self.expires_at_ms = expiry
        return ok

    async def release(self) -> None:
        if not self.released:
            await self.semaphore.release(self)
            self.released = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.release()


class RedisSubagentSemaphore:
    def __init__(
        self,
        redis,
        *,
        namespace: str = "smarter-dev:chat:subagents",
        # Longer than the hard child timeout, refreshed every ten seconds. A
        # dead worker's potentially accepted remote request finishes/times out
        # before Redis can reuse its global slot.
        lease_seconds: int = 1_200,
    ):
        self.redis = redis
        self.leases_key = f"{namespace}:leases"
        self.queue_key = f"{namespace}:queue"
        self.fence_key = f"{namespace}:fence"
        self.queue_seq_key = f"{namespace}:queue-sequence"
        self.lease_seconds = lease_seconds

    async def acquire(
        self,
        *,
        ticket: str | None = None,
        wait: bool = True,
        timeout: float | None = None,
    ) -> Lease | None:
        ticket = ticket or uuid4().hex
        start = time.monotonic()
        while True:
            try:
                raw = await self.redis.eval(
                    _ACQUIRE,
                    4,
                    self.leases_key,
                    self.queue_key,
                    self.fence_key,
                    self.queue_seq_key,
                    ticket,
                    self.lease_seconds * 1000,
                    MAX_RUNNING_SUBAGENTS,
                    1_000_000,
                )
                fence, expiry = int(raw[0]), int(raw[1])
            except Exception as exc:
                raise SemaphoreUnavailable(
                    "distributed sub-agent semaphore unavailable"
                ) from exc
            if fence:
                return Lease(self, ticket, fence, expiry)
            if not wait:
                return None
            if timeout is not None and time.monotonic() - start >= timeout:
                await self.cancel(ticket)
                return None
            try:
                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                await self.cancel(ticket)
                raise

    async def heartbeat(self, lease: Lease) -> tuple[bool, int]:
        try:
            raw = await self.redis.eval(
                _HEARTBEAT,
                1,
                self.leases_key,
                lease.ticket,
                lease.fence,
                self.lease_seconds * 1000,
            )
            return bool(int(raw[0])), int(raw[1])
        except Exception as exc:
            raise SemaphoreUnavailable("sub-agent lease heartbeat failed") from exc

    async def release(self, lease: Lease) -> None:
        try:
            await self.redis.eval(
                _RELEASE, 1, self.leases_key, lease.ticket, lease.fence
            )
        except Exception as exc:
            raise SemaphoreUnavailable("sub-agent lease release failed") from exc

    async def cancel(self, ticket: str) -> None:
        try:
            await self.redis.zrem(self.queue_key, ticket)
        except Exception as exc:
            raise SemaphoreUnavailable("sub-agent queue cancellation failed") from exc
