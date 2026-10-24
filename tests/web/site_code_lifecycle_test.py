"""A site code run the route gave up on never starts, or stops.

The job (``site_code_jobs``) takes the call, then waits for a place in the
agent worker's sandbox budget. If the route timed out, was cancelled or hit an
error meanwhile, the sandbox must not start; if it gave up once the sandbox
was running, the sandbox stops. Real Monty, fakeredis, one event loop.
"""

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import fakeredis
import httpx
import pydantic_monty
import pytest
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


@pytest.fixture
def redis(monkeypatch):
    client = fakeredis.FakeAsyncRedis(decode_responses=True)
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
    assert _live_monty_children() == []
    assert await redis.llen(site_code.result_key(run_id)) == 0


async def test_a_wanted_run_still_runs(redis, started):
    run_id, job = await _job(redis, "6 * 7")
    assert await asyncio.wait_for(job, 10) == {"status": "complete"}
    assert len(started) == 1
    assert await site_code.wait_for_result(redis, run_id, 1) == "return value: 42"


@pytest.fixture
async def app(test_engine, db_session, monkeypatch, redis):
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

    async def submit(job_type, payload, **options):
        queued.append(payload["run_id"])  # the worker is busy: it runs later

    import skrift.workers

    monkeypatch.setattr(skrift.workers, "submit", submit)
    monkeypatch.setattr(
        "smarter_dev.web.chat.dispatch.ensure_submission_handler", lambda job_type: None
    )
    litestar = Litestar(
        route_handlers=[SiteCodeController],
        plugins=[PydanticPlugin()],
        dependencies={"db_session": Provide(session)},
    )
    # httpx's ASGI transport runs the app in the request's own task, so
    # cancelling the request cancels the route, as a dropped connection does.
    transport = httpx.ASGITransport(app=litestar)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield SimpleNamespace(client=client, member=member.id, queued=queued)


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
