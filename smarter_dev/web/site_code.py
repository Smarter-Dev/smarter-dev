"""One ``run_code`` call from site chat, carried from the web pod to the agent worker.

The site Worker never runs code. Its chat model's ``run_code`` tool posts the
snippet to ``POST /api/site/code/users/{user_id}/runs``; the web pod cannot
host a sandbox (one can reach ~270 MiB and the pod's limit is 384 MiB), so it
hands the run to the agent worker, where smarter.dev's own chat ``run_code``
already runs, and waits for the text.

Nothing about a run is saved. The snippet waits in Redis under a random id for
at most ``REQUEST_TTL_SECONDS``; the queued job carries only that id, so the
code never reaches the Postgres job queue or a job's stored state. The worker
takes the snippet with GETDEL, runs it through the same function smarter.dev's
chat uses, and pushes the text the model will read to a result list that the
web pod pops; that list lives at most ``RESULT_TTL_SECONDS``. A job that runs
after its web request gave up finds the snippet gone and runs nothing.

Neither side logs the snippet, the reason or the output.

Concurrency is bounded twice. The web pod admits at most ``MAX_RUNS_PER_USER``
runs per member and ``MAX_RUNS_IN_FLIGHT`` in all, as leases in Redis that
expire on their own, so a burst cannot fill the agent worker's job slots; on
the worker every sandbox shares its sandbox memory budget (``code_sandbox``).
"""

from __future__ import annotations

import json
import time
from uuid import UUID

JOB_TYPE = "site_code.run"
JOB_QUEUE = "agents"
JOB_VISIBILITY_TIMEOUT = 120.0

# Bounds on what the site may send. The model writes the snippet; 100k
# characters is far past any calculation and keeps one request small.
MAX_CODE_CHARS = 100_000
MAX_REASON_CHARS = 500
MAX_BODY_BYTES = 512 * 1024

# How long the web pod waits for a result: a queued job, the runs ahead of it
# on the agent worker (its sandbox budget holds one full-size run at a time;
# each takes at most 10 s plus the kill grace) and its own run.
WAIT_SECONDS = 45
REQUEST_TTL_SECONDS = WAIT_SECONDS + 15
RESULT_TTL_SECONDS = 60
LEASE_SECONDS = WAIT_SECONDS + 15
# The job renews its heartbeat every HEARTBEAT_SECONDS; the route counts a
# claimed run whose heartbeat lapsed as a worker that died.
HEARTBEAT_SECONDS = 2
HEARTBEAT_TTL_SECONDS = 6
STOPPED = "Code running stopped before the run finished."

MAX_RUNS_PER_USER = 2
# Three at 12 s each, one after another, fit in WAIT_SECONDS.
MAX_RUNS_IN_FLIGHT = 3

BUSY_USER = "Code is already running for this account. Try again in a moment."
BUSY_ALL = "Code running is busy right now. Try again in a moment."
TIMED_OUT = "The code run didn't finish in time."

# Expired leases drop on every admission, so a web pod killed mid-wait frees
# its places within LEASE_SECONDS. Redis TIME keeps every pod on one clock.
_ADMIT = """
local user_key, all_key = KEYS[1], KEYS[2]
local ticket, lease_ms = ARGV[1], tonumber(ARGV[2])
local user_cap, all_cap = tonumber(ARGV[3]), tonumber(ARGV[4])
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
redis.call('ZREMRANGEBYSCORE', user_key, '-inf', now)
redis.call('ZREMRANGEBYSCORE', all_key, '-inf', now)
if redis.call('ZCARD', user_key) >= user_cap then
  return 1
end
if redis.call('ZCARD', all_key) >= all_cap then
  return 2
end
redis.call('ZADD', user_key, now + lease_ms, ticket)
redis.call('ZADD', all_key, now + lease_ms, ticket)
redis.call('PEXPIRE', user_key, lease_ms)
redis.call('PEXPIRE', all_key, lease_ms)
return 0
"""

ALL_LEASES_KEY = "site-code:leases"


def user_leases_key(user_id: UUID) -> str:
    return f"site-code:leases:user:{user_id}"


def request_key(run_id: str) -> str:
    return f"site-code:run:{run_id}"


def result_key(run_id: str) -> str:
    return f"site-code:result:{run_id}"


async def admit(redis, user_id: UUID, run_id: str) -> str | None:
    """Take a place for ``run_id``; the refusal message when there is none."""
    refused = await redis.eval(
        _ADMIT,
        2,
        user_leases_key(user_id),
        ALL_LEASES_KEY,
        run_id,
        LEASE_SECONDS * 1000,
        MAX_RUNS_PER_USER,
        MAX_RUNS_IN_FLIGHT,
    )
    return {0: None, 1: BUSY_USER}.get(int(refused), BUSY_ALL)


async def release(redis, user_id: UUID, run_id: str) -> None:
    await redis.zrem(user_leases_key(user_id), run_id)
    await redis.zrem(ALL_LEASES_KEY, run_id)


def abandoned_key(run_id: str) -> str:
    return f"site-code:abandoned:{run_id}"


def running_key(run_id: str) -> str:
    return f"site-code:running:{run_id}"


async def store_request(redis, run_id: str, *, reason: str, code: str) -> None:
    """Park the call for the job, with the moment the route stops waiting."""
    await redis.set(
        request_key(run_id),
        json.dumps(
            {"reason": reason, "code": code, "deadline": time.time() + WAIT_SECONDS}
        ),
        ex=REQUEST_TTL_SECONDS,
    )


async def take_request(redis, run_id: str) -> dict | None:
    """The job's claim on the call. The run is marked running first, so the
    route never sees the call gone with nothing running and no result."""
    await heartbeat(redis, run_id)
    raw = await redis.getdel(request_key(run_id))
    if raw is None:
        await redis.delete(running_key(run_id))
        return None
    return json.loads(raw)


async def heartbeat(redis, run_id: str) -> None:
    """The job is alive and holds the call; the route watches for this."""
    await redis.set(running_key(run_id), "1", ex=HEARTBEAT_TTL_SECONDS)


async def still_wanted(redis, run_id: str, deadline: float) -> bool:
    """Whether the route still waits: before its deadline and not abandoned."""
    return time.time() < deadline and not await redis.exists(abandoned_key(run_id))


async def push_result(redis, run_id: str, text: str) -> None:
    """Hand the text over and stop the heartbeat in one step, so the route
    never sees the run gone before its result is there."""
    key = result_key(run_id)
    async with redis.pipeline(transaction=True) as pipe:
        pipe.rpush(key, text)
        pipe.expire(key, RESULT_TTL_SECONDS)
        pipe.delete(running_key(run_id))
        await pipe.execute()


async def finish(redis, run_id: str) -> None:
    """The job is done with the call, with or without a result."""
    await redis.delete(running_key(run_id))


async def wait_for_result(redis, run_id: str, timeout: float) -> str | None:
    popped = await redis.blpop([result_key(run_id)], timeout=timeout)
    if popped is None:
        return None
    value = popped[1]
    return value.decode() if isinstance(value, bytes) else value


async def worker_gone(redis, run_id: str) -> bool:
    """A job claimed the call, then stopped without a result: its heartbeat
    lapsed. Read in one step, so a result handed over meanwhile counts."""
    async with redis.pipeline(transaction=True) as pipe:
        pipe.exists(request_key(run_id))
        pipe.exists(running_key(run_id))
        pipe.llen(result_key(run_id))
        waiting, running, results = await pipe.execute()
    return not waiting and not running and not results


async def abandon(redis, run_id: str) -> None:
    """Drop a run the route stopped waiting for. The job checks the mark
    before its sandbox starts, and stops a sandbox already running."""
    async with redis.pipeline(transaction=True) as pipe:
        pipe.set(abandoned_key(run_id), "1", ex=REQUEST_TTL_SECONDS)
        pipe.delete(request_key(run_id), result_key(run_id))
        await pipe.execute()
