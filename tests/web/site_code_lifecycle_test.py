"""A site code run the route gave up on never starts, or stops.

The job (``site_code_jobs``) takes the call, then waits for a place in the
agent worker's sandbox budget. If the route timed out, was cancelled or hit an
error meanwhile, the sandbox must not start; if it gave up once the sandbox
was running, the sandbox stops. Real Monty, fakeredis, one event loop.
"""

import asyncio
import contextlib
import json
import os
import socket
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import fakeredis
import fakeredis.aioredis
import httpx
import pydantic_monty
import pytest
from hypercorn.asyncio import serve
from hypercorn.config import Config
from litestar import Litestar
from litestar.di import Provide
from litestar.plugins.pydantic import PydanticPlugin
from skrift.db.base import Base as SkriftBase
from skrift.db.models.user import User

from smarter_dev.shared import code_sandbox
from smarter_dev.shared.database import async_sessionmaker
from smarter_dev.web import site_code
from smarter_dev.web import site_code_jobs
from smarter_dev.web.api_native import site_code as site_code_api
from smarter_dev.web.api_native import site_guards
from smarter_dev.web.api_native.site_code import SiteCodeController


def _live_monty_children() -> list[int]:
    """This process's ``monty`` workers that still hold memory."""
    me, live = os.getpid(), []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
        except OSError:
            continue
        name = stat[stat.index("(") + 1 : stat.rindex(")")]
        state, ppid = stat[stat.rindex(")") + 2 :].split()[:2]
        if name == "monty" and int(ppid) == me and state != "Z":
            live.append(int(entry.name))
    return live


class _DropsOnCancel(fakeredis.aioredis.FakeAsyncRedisConnection):
    """A fake connection that, like redis-py's, drops itself when a read is
    cut off (``redis.asyncio.connection.Connection.read_response``).

    fakeredis 2.37 keeps the cut-off command's reply queued, so every later
    call on that connection reads the reply before its own: a route
    cancelled in its BLPOP then sees ``keys`` answer ``None``."""

    async def read_response(self, **kwargs):
        try:
            return await super().read_response(**kwargs)
        except BaseException:
            await self.disconnect()
            raise


@pytest.fixture
def redis(monkeypatch):
    client = fakeredis.FakeAsyncRedis(
        decode_responses=True, connection_class=_DropsOnCancel
    )
    monkeypatch.setattr(site_code_jobs, "get_redis_client", lambda: client)
    monkeypatch.setattr(site_code_api, "get_redis_client", lambda: client)
    return client


@pytest.fixture
def started(monkeypatch):
    """Every Monty pool the sandbox opens, i.e. every run that started."""
    pools: list[object] = []
    real = pydantic_monty.AsyncMonty

    def spy(*args, **kwargs):
        pools.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(code_sandbox.monty, "AsyncMonty", spy)
    return pools


async def _job(redis, code: str) -> tuple[str, asyncio.Task]:
    run_id = uuid4().hex
    await site_code.store_request(redis, run_id, reason="checking", code=code)
    job = asyncio.create_task(
        site_code_jobs.run_site_code_job(site_code_jobs.SiteCodePayload(run_id=run_id))
    )
    return run_id, job


async def _waiting_for_a_place(job: asyncio.Task) -> None:
    """Let the job take the call and queue for the budget."""
    for _ in range(50):
        await asyncio.sleep(0.01)
        if code_sandbox._budget()._waiting:
            return
    raise AssertionError("the job never queued for the sandbox budget")


@pytest.mark.parametrize("gave_up", ["abandoned", "timed out"])
async def test_a_run_given_up_while_it_waits_never_starts(
    redis, started, monkeypatch, gave_up
):
    # Every place is taken: the job holds the call and waits.
    budget = code_sandbox._budget()
    full = asyncio.Event()

    async def hold_every_place():
        async with budget.reserve(budget.total):
            await full.wait()

    holder = asyncio.create_task(hold_every_place())
    await asyncio.sleep(0)
    if gave_up == "timed out":
        monkeypatch.setattr(site_code, "WAIT_SECONDS", 0.2)
    run_id, job = await _job(redis, "7")
    await _waiting_for_a_place(job)
    if gave_up == "abandoned":
        await site_code.abandon(redis, run_id)
    else:
        await asyncio.sleep(0.3)  # past the route's deadline
    # A place frees up.
    full.set()
    await holder
    assert await asyncio.wait_for(job, 10) == {"status": "abandoned"}
    assert started == []  # Monty never started
    assert await redis.llen(site_code.result_key(run_id)) == 0
    assert not await redis.exists(site_code.running_key(run_id))
    assert budget.used == 0


async def test_a_run_given_up_while_it_runs_is_stopped(redis, started, monkeypatch):
    monkeypatch.setattr(site_code, "HEARTBEAT_SECONDS", 0.1)
    # The workers still alive at the moment each run gives its place back.
    at_release: list[list[int]] = []
    reserve = code_sandbox._Budget.reserve

    @contextlib.asynccontextmanager
    async def watched(self, size):
        async with reserve(self, size):
            try:
                yield
            finally:
                at_release.append(_live_monty_children())

    monkeypatch.setattr(code_sandbox._Budget, "reserve", watched)
    run_id, job = await _job(redis, "while True:\n    pass\n")
    for _ in range(100):
        await asyncio.sleep(0.02)
        if started:
            break
    assert started, "the run never started"
    await asyncio.sleep(0.2)
    await site_code.abandon(redis, run_id)
    loop = asyncio.get_running_loop()
    began = loop.time()
    assert await asyncio.wait_for(job, 10) == {"status": "abandoned"}
    # Stopped at the next heartbeat, long before its 10 s limit, and its
    # place given back only once its worker had exited.
    assert loop.time() - began < 2
    assert code_sandbox._budget().used == 0
    assert at_release == [[]]
    assert await redis.llen(site_code.result_key(run_id)) == 0


async def test_a_wanted_run_still_runs(redis, started):
    run_id, job = await _job(redis, "6 * 7")
    assert await asyncio.wait_for(job, 10) == {"status": "complete"}
    assert len(started) == 1
    assert await site_code.wait_for_result(redis, run_id, 1) == "return value: 42"


@pytest.fixture
async def site(test_engine, db_session, monkeypatch, redis):
    """The route in a Litestar app, with every run it queues recorded.

    Queued runs wait (the worker is busy) unless ``start_jobs`` is set, when
    each one's job starts at once."""
    async with test_engine.begin() as conn:
        await conn.run_sync(SkriftBase.metadata.create_all)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)

    async def session():
        async with maker() as db:
            yield db

    member = User(email=f"{uuid4()}@example.test", name="Member", is_active=True)
    db_session.add(member)
    await db_session.commit()
    monkeypatch.setattr(site_guards, "bot_api_auth_guard", AsyncMock())
    key = SimpleNamespace(
        principal_type="service",
        service_name="site",
        scoped_permission_list=["site-code"],
    )
    monkeypatch.setattr(
        site_guards, "resolve_request_api_key", AsyncMock(return_value=key)
    )
    queued: list[str] = []
    jobs: list[asyncio.Task] = []
    parts = SimpleNamespace(member=member.id, queued=queued, jobs=jobs)
    parts.start_jobs = False

    async def submit(job_type, payload, **options):
        queued.append(payload["run_id"])
        if parts.start_jobs:
            jobs.append(
                asyncio.create_task(
                    site_code_jobs.run_site_code_job(
                        site_code_jobs.SiteCodePayload(**payload)
                    )
                )
            )

    import skrift.workers

    monkeypatch.setattr(skrift.workers, "submit", submit)
    monkeypatch.setattr(
        "smarter_dev.web.chat.dispatch.ensure_submission_handler", lambda job_type: None
    )
    parts.litestar = Litestar(
        route_handlers=[SiteCodeController],
        plugins=[PydanticPlugin()],
        dependencies={"db_session": Provide(session)},
    )
    yield parts
    for job in jobs:
        job.cancel()
    await asyncio.gather(*jobs, return_exceptions=True)


@pytest.fixture
async def app(site):
    # httpx's ASGI transport runs the app in the request's own task, so
    # cancelling the request cancels the route.
    transport = httpx.ASGITransport(app=site.litestar)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield SimpleNamespace(client=client, member=site.member, queued=site.queued)


@pytest.fixture
async def served(site):
    """The route behind a real Hypercorn server, as on the web pod.

    Hypercorn doesn't cancel a route whose client went away, as httpx's ASGI
    transport does: it only says so on the request's ``receive``."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    site.port = listener.getsockname()[1]
    config = Config()
    config.bind = [f"fd://{listener.detach()}"]  # Hypercorn closes it
    config.accesslog = None
    config.graceful_timeout = 1
    stop = asyncio.Event()
    server = asyncio.create_task(
        serve(site.litestar, config, shutdown_trigger=stop.wait)
    )
    yield site
    stop.set()
    # Shutdown waits for open connections: one whose route never noticed
    # its caller leave holds it until the 45 s wait ends. The test has
    # already failed on that.
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(server, 5)


async def _post_then_hang_up(site, code: str, ready) -> None:
    """POST a run over TCP, wait for ``ready()``, then close the socket
    without reading the reply, as the site Worker does when it aborts."""
    reader, writer = await asyncio.open_connection("127.0.0.1", site.port)
    body = json.dumps({"reason": "checking", "code": code}).encode()
    head = (
        f"POST /api/site/code/users/{site.member}/runs HTTP/1.1\r\n"
        "Host: test\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    )
    writer.write(head.encode() + body)
    await writer.drain()
    for _ in range(500):
        await asyncio.sleep(0.01)
        if ready():
            break
    else:
        raise AssertionError("the run never got that far")
    await asyncio.sleep(0.2)  # the route waits on the result
    assert not reader.at_eof()  # no reply yet: the route is still waiting
    writer.close()
    await writer.wait_closed()


async def _settled(check, seconds: float = 3.0) -> bool:
    for _ in range(int(seconds / 0.02)):
        if await check():
            return True
        await asyncio.sleep(0.02)
    return False


async def test_a_request_cancelled_at_its_wait_leaves_nothing_to_run(
    app, redis, started
):
    request = asyncio.create_task(
        app.client.post(
            f"/api/site/code/users/{app.member}/runs",
            json={"reason": "checking", "code": "print('sekrit')\n7"},
        )
    )
    for _ in range(200):
        await asyncio.sleep(0.01)
        if app.queued:
            break
    (run_id,) = app.queued
    await asyncio.sleep(0.1)  # waiting on the result
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    await asyncio.sleep(0.05)
    # Nothing of the call is left: no code, no leases; only the abandon mark.
    keys = await redis.keys("*")
    assert keys == [site_code.abandoned_key(run_id)]
    assert await redis.get(keys[0]) == "1"
    # The worker gets to the job later: it runs nothing.
    outcome = await site_code_jobs.run_site_code_job(
        site_code_jobs.SiteCodePayload(run_id=run_id)
    )
    assert outcome == {"status": "expired"}
    assert started == []


async def test_a_finished_run_is_never_read_as_a_dead_worker(redis, monkeypatch):
    # Between the run ending and its text being published, the route must
    # still see the job alive (#335: a pause here read as a dead worker).
    publish = site_code.push_result
    paused, go = asyncio.Event(), asyncio.Event()

    async def slow_publish(client, run_id, text):
        paused.set()
        await go.wait()
        await publish(client, run_id, text)

    monkeypatch.setattr(site_code, "push_result", slow_publish)
    run_id, job = await _job(redis, "6 * 7")
    await asyncio.wait_for(paused.wait(), 10)
    assert not await site_code.worker_gone(redis, run_id)
    go.set()
    assert await asyncio.wait_for(job, 10) == {"status": "complete"}
    assert await site_code.wait_for_result(redis, run_id, 1) == "return value: 42"
    assert not await redis.exists(site_code.running_key(run_id))


async def test_a_caller_that_hangs_up_while_its_run_waits_leaves_nothing_to_run(
    served, redis, started
):
    await _post_then_hang_up(served, "print('sekrit')\n7", lambda: served.queued)
    (run_id,) = served.queued

    async def only_the_abandon_mark() -> bool:
        return await redis.keys("*") == [site_code.abandoned_key(run_id)]

    # Within moments, not at the 45 s wait: no code, no leases, the mark.
    assert await _settled(only_the_abandon_mark), await redis.keys("*")
    outcome = await site_code_jobs.run_site_code_job(
        site_code_jobs.SiteCodePayload(run_id=run_id)
    )
    assert outcome == {"status": "expired"}
    assert started == []


async def test_a_caller_that_hangs_up_while_its_run_runs_stops_it(
    served, redis, started, monkeypatch
):
    monkeypatch.setattr(site_code, "HEARTBEAT_SECONDS", 0.1)
    served.start_jobs = True
    await _post_then_hang_up(served, "while True:\n    pass\n", lambda: started)
    (run_id,) = served.queued
    (job,) = served.jobs
    # Stopped at its next heartbeat, long before its 10 s limit.
    assert await asyncio.wait_for(job, 3) == {"status": "abandoned"}
    assert await redis.zcard(site_code.ALL_LEASES_KEY) == 0
    assert await redis.zcard(site_code.user_leases_key(served.member)) == 0
    assert await redis.llen(site_code.result_key(run_id)) == 0
