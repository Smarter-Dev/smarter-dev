"""Site code facade: the site Worker's run_code, run on the agent worker.

Redis is fakeredis. The agent worker queue is replaced by a submit that runs
the real job handler on the same event loop, so a run goes web route → Redis
→ job → real Monty → Redis → web route, as in production minus Postgres.
"""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import fakeredis
import pytest
from litestar.di import Provide
from litestar.plugins.pydantic import PydanticPlugin
from litestar.testing import create_test_client
from skrift.db.base import Base as SkriftBase
from skrift.db.models.user import User

from smarter_dev.shared.database import async_sessionmaker
from smarter_dev.web import site_code
from smarter_dev.web import site_code_jobs
from smarter_dev.web.api_native import site_code as site_code_api
from smarter_dev.web.api_native import site_guards
from smarter_dev.web.api_native.site_code import SiteCodeController
from smarter_dev.web.chat.toolsets import run_code

ROOT = "/api/site/code"
SECRET_CODE = "print('sekrit-output-7731')\n'sekrit-value-7731'"


@pytest.fixture
async def env(test_engine, db_session, monkeypatch):
    async with test_engine.begin() as conn:
        await conn.run_sync(SkriftBase.metadata.create_all)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)

    async def session():
        async with maker() as db:
            yield db

    monkeypatch.setattr(site_guards, "bot_api_auth_guard", AsyncMock())
    key = SimpleNamespace(
        principal_type="service",
        service_name="site",
        scoped_permission_list=["site-code"],
    )
    monkeypatch.setattr(
        site_guards, "resolve_request_api_key", AsyncMock(return_value=key)
    )
    # The app runs on the test client's own loop: it gets its own client on
    # the shared server, so no connection is used from two loops.
    server = fakeredis.FakeServer()
    app_redis = fakeredis.FakeAsyncRedis(server=server, decode_responses=True)
    redis = fakeredis.FakeAsyncRedis(server=server, decode_responses=True)
    monkeypatch.setattr(site_code_api, "get_redis_client", lambda: app_redis)
    monkeypatch.setattr(site_code_jobs, "get_redis_client", lambda: app_redis)
    state = SimpleNamespace(submitted=[], jobs=[], worker="run", fail_submit=False)

    async def submit(job_type, payload, **options):
        if state.fail_submit:
            raise ConnectionError("queue down")
        state.submitted.append({"job_type": job_type, **payload, **options})
        if state.worker == "run":
            state.jobs.append(
                asyncio.ensure_future(
                    site_code_jobs.run_site_code_job(
                        site_code_jobs.SiteCodePayload(**payload)
                    )
                )
            )

    import skrift.workers

    monkeypatch.setattr(skrift.workers, "submit", submit)
    monkeypatch.setattr(
        "smarter_dev.web.chat.dispatch.ensure_submission_handler",
        lambda job_type: None,
    )
    with create_test_client(
        route_handlers=[SiteCodeController],
        plugins=[PydanticPlugin()],
        dependencies={"db_session": Provide(session)},
    ) as tc:
        yield SimpleNamespace(
            tc=tc, key=key, redis=redis, state=state, session=db_session
        )


async def _member(session, *, active=True) -> User:
    user = User(email=f"{uuid4()}@example.test", name="Member", is_active=active)
    session.add(user)
    await session.commit()
    return user


def _run(tc, user_id, code="1 + 1", reason="adding"):
    return tc.post(
        f"{ROOT}/users/{user_id}/runs", json={"reason": reason, "code": code}
    )


@pytest.mark.parametrize(
    "principal,service,scopes",
    [
        ("user", None, ["site-code"]),
        ("service", "bot", ["site-code"]),
        ("service", "site", ["site-search"]),
        ("service", "site", []),
    ],
)
async def test_only_a_site_key_with_the_site_code_scope_runs_code(
    env, principal, service, scopes
):
    user = await _member(env.session)
    env.key.principal_type = principal
    env.key.service_name = service
    env.key.scoped_permission_list = scopes
    response = _run(env.tc, user.id)
    assert response.status_code == 401
    assert env.state.submitted == []


async def test_a_run_returns_exactly_what_python_chat_returns(env):
    user = await _member(env.session)
    code = "import math\nprint(math.factorial(10))\nmath.pi"
    response = _run(env.tc, user.id, code=code)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json() == {"result": await run_code("adding", code)}
    assert response.json()["result"] == (
        "stdout:\n3628800\n\nreturn value: 3.141592653589793"
    )


async def test_the_job_carries_only_an_id_and_nothing_is_left_in_redis(env):
    user = await _member(env.session)
    response = _run(env.tc, user.id, code=SECRET_CODE)
    assert response.status_code == 200
    (job,) = env.state.submitted
    assert job["job_type"] == site_code.JOB_TYPE
    assert job["queue"] == "agents"
    assert set(job) == {"job_type", "run_id", "queue", "job_id"}
    assert "sekrit" not in repr(job)
    # The job pushed its result before the route answered.
    assert await env.redis.keys("*") == []


@pytest.mark.parametrize("user_id,status", [("not-a-uuid", 400), (str(uuid4()), 404)])
async def test_an_unknown_member_is_refused(env, user_id, status):
    response = _run(env.tc, user_id)
    assert response.status_code == status
    assert env.state.submitted == []


async def test_an_inactive_member_is_refused(env):
    user = await _member(env.session, active=False)
    assert _run(env.tc, user.id).status_code == 404


async def test_the_request_body_is_bounded(env):
    user = await _member(env.session)
    too_long = "x = 1\n" * (site_code.MAX_CODE_CHARS // 6 + 1)
    assert _run(env.tc, user.id, code=too_long).status_code == 422
    assert _run(env.tc, user.id, reason="r" * 501).status_code == 422
    huge = env.tc.post(
        f"{ROOT}/users/{user.id}/runs",
        content=b'{"reason": "x", "code": "'
        + b"a" * (site_code.MAX_BODY_BYTES + 1)
        + b'"}',
        headers={"content-type": "application/json"},
    )
    assert huge.status_code == 413
    assert env.state.submitted == []


async def test_blank_input_gets_pythons_own_answer(env):
    user = await _member(env.session)
    response = _run(env.tc, user.id, code="   ", reason="")
    assert response.json() == {"result": "error: reason and code are required"}


async def test_a_member_runs_at_most_two_at_once(env):
    user = await _member(env.session)
    for ticket in ("a", "b"):
        assert await site_code.admit(env.redis, user.id, ticket) is None
    response = _run(env.tc, user.id)
    assert response.status_code == 429
    assert response.json() == {"detail": site_code.BUSY_USER, "wait": 5}
    assert response.headers["retry-after"] == "5"
    other = await _member(env.session)
    assert _run(env.tc, other.id).status_code == 200
    await site_code.release(env.redis, user.id, "a")
    assert _run(env.tc, user.id).status_code == 200


async def test_the_service_runs_at_most_four_at_once(env):
    for ticket in "abcd":
        assert await site_code.admit(env.redis, uuid4(), ticket) is None
    user = await _member(env.session)
    response = _run(env.tc, user.id)
    assert response.status_code == 429
    assert response.json()["detail"] == site_code.BUSY_ALL


async def test_a_lease_left_by_a_killed_pod_expires(env):
    user = await _member(env.session)
    for ticket in ("a", "b"):
        await site_code.admit(env.redis, user.id, ticket)
    # Age both leases past their expiry, as if their pod died mid-wait.
    for key in (site_code.user_leases_key(user.id), site_code.ALL_LEASES_KEY):
        for ticket in ("a", "b"):
            await env.redis.zadd(key, {ticket: 0})
    assert _run(env.tc, user.id).status_code == 200


async def test_leases_are_released_after_every_outcome(env, monkeypatch):
    user = await _member(env.session)
    assert _run(env.tc, user.id, code="1 / 0").status_code == 200
    env.state.fail_submit = True
    assert _run(env.tc, user.id).status_code == 503
    env.state.fail_submit = False
    env.state.worker = "never"
    monkeypatch.setattr(site_code, "WAIT_SECONDS", 0.2)
    assert _run(env.tc, user.id).status_code == 504
    assert await env.redis.zcard(site_code.user_leases_key(user.id)) == 0
    assert await env.redis.zcard(site_code.ALL_LEASES_KEY) == 0


async def test_a_timed_out_run_is_abandoned_and_a_late_job_runs_nothing(
    env, monkeypatch
):
    user = await _member(env.session)
    env.state.worker = "never"
    monkeypatch.setattr(site_code, "WAIT_SECONDS", 0.2)
    response = _run(env.tc, user.id, code=SECRET_CODE)
    assert response.status_code == 504
    assert response.json() == {"detail": site_code.TIMED_OUT}
    (job,) = env.state.submitted
    late = await site_code_jobs.run_site_code_job(
        site_code_jobs.SiteCodePayload(run_id=job["run_id"])
    )
    assert late == {"status": "expired"}
    assert await env.redis.keys("*") == []


async def test_an_unqueued_run_is_a_plain_503(env):
    user = await _member(env.session)
    env.state.fail_submit = True
    response = _run(env.tc, user.id, code=SECRET_CODE)
    assert response.status_code == 503
    assert response.json() == {"detail": site_code_api.START_FAILED}
    assert await env.redis.keys("*") == []


async def test_nothing_logs_the_code_its_reason_or_its_output(env, caplog):
    caplog.set_level(logging.DEBUG)
    user = await _member(env.session)
    assert (
        _run(env.tc, user.id, code=SECRET_CODE, reason="sekrit-reason").status_code
        == 200
    )
    env.state.fail_submit = True
    assert (
        _run(env.tc, user.id, code=SECRET_CODE, reason="sekrit-reason").status_code
        == 503
    )
    logged = "\n".join(
        f"{record.getMessage()} {record.args!r} {record.exc_text or ''}"
        for record in caplog.records
    )
    assert "sekrit" not in logged
