"""Site search facade, anonymous runs: a signed-out visitor using a member's
search link. Never saved to Postgres, kept in Redis for 30 minutes, billed to
the link's owner, under the /s/ pages' anonymous limits.

Redis is fakeredis; the agent worker queue and notifications are replaced."""

import asyncio
import json
from datetime import UTC
from datetime import datetime
from datetime import timedelta
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
from sqlalchemy import func
from sqlalchemy import select

from smarter_dev.shared.database import Base
from smarter_dev.shared.database import async_sessionmaker
from smarter_dev.web import search_link_controller as links
from smarter_dev.web.api_native import site_guards
from smarter_dev.web.api_native import site_search
from smarter_dev.web.api_native.site_search import SiteSearchController
from smarter_dev.web.models import UsageCostRow
from smarter_dev.web.models import WebSearchLink
from smarter_dev.web.models import WebSearchRun
from smarter_dev.web.web_search import anonymous
from smarter_dev.web.web_search import pipeline
from tests.web.search_link_test import _request
from tests.web.web_search_test import search_env  # noqa: F401 - fixture

ROOT = "/api/site/search"
VISITOR = "v1_" + "a" * 40
NOT_FOUND = "This search link doesn't exist."


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
        scoped_permission_list=["site-search"],
    )
    monkeypatch.setattr(
        site_guards, "resolve_request_api_key", AsyncMock(return_value=key)
    )
    redis = fakeredis.FakeAsyncRedis()
    monkeypatch.setattr(site_search, "get_redis_client", lambda: redis)
    monkeypatch.setattr(links, "get_redis_client", lambda: redis)
    submitted: list[dict] = []

    async def submit(job_type, payload, **options):
        submitted.append({"job_type": job_type, **payload, **options})

    import skrift.workers

    monkeypatch.setattr(skrift.workers, "submit", submit)
    monkeypatch.setattr(
        "smarter_dev.web.chat.dispatch.ensure_submission_handler",
        lambda job_type: None,
    )
    with create_test_client(
        route_handlers=[SiteSearchController],
        plugins=[PydanticPlugin()],
        dependencies={"db_session": Provide(session)},
    ) as tc:
        yield SimpleNamespace(
            tc=tc, key=key, redis=redis, submitted=submitted, session=db_session
        )


async def _owner(session, *, active=True, link=True) -> User:
    user = User(email=f"{uuid4()}@example.test", name="Owner", is_active=active)
    session.add(user)
    await session.commit()
    if link:
        session.add(
            WebSearchLink(
                owner_user_id=user.id, token=uuid4().hex[:24], open_addresses=False
            )
        )
        await session.commit()
    return user


def _start(tc, owner_id, request="why is my build slow", visitor=VISITOR):
    return tc.post(
        f"{ROOT}/users/{owner_id}/anonymous-runs",
        json={"request": request, "visitor": visitor},
    )


async def _row_counts(session) -> dict[str, int]:
    tables = {
        table.name: table
        for table in [*Base.metadata.sorted_tables, *SkriftBase.metadata.sorted_tables]
    }
    counts = {}
    for name, table in tables.items():
        try:
            counts[name] = await session.scalar(select(func.count()).select_from(table))
        except Exception:  # noqa: BLE001 - a table this test DB doesn't create
            await session.rollback()
    return counts


@pytest.mark.parametrize(
    "principal,service,scopes",
    [
        ("user", None, ["site-search"]),
        ("service", "bot", ["site-search"]),
        ("service", "site", ["site-memory", "site-privacy", "site-billing"]),
    ],
)
def test_both_routes_require_the_site_search_scope(env, principal, service, scopes):
    env.key.principal_type, env.key.service_name, env.key.scoped_permission_list = (
        principal,
        service,
        scopes,
    )
    assert _start(env.tc, uuid4()).status_code == 401
    assert env.tc.get(f"{ROOT}/anonymous-runs/{uuid4()}").status_code == 401


async def test_a_start_is_kept_in_redis_only_and_queued_for_the_owner(env):
    owner = await _owner(env.session)
    before = await _row_counts(env.session)

    response = _start(env.tc, owner.id, request="  why   is it slow ")

    assert response.status_code == 201
    assert response.headers["Cache-Control"] == "private, no-store"
    search = response.json()["search"]
    assert search["request"] == "why is it slow"
    assert search["status"] == "queued" and search["active"] is True
    assert search["answer"] is None and "html" not in str(search)
    assert env.submitted == [
        {
            "job_type": anonymous.JOB_TYPE,
            "search_id": search["id"],
            "queue": "agents",
            "job_id": f"anonymous:{search['id']}",
        }
    ]
    entry = await anonymous.load(env.redis, search["id"])
    assert entry["owner_user_id"] == str(owner.id)
    assert entry["nid"] is None
    assert entry["site"] == search
    ttl = await env.redis.ttl(f"web_search:anonymous:{search['id']}")
    assert 0 < ttl <= anonymous.TTL_SECONDS == 30 * 60
    # The visitor key is a limiter key only; it is never kept with the search.
    assert VISITOR not in str(entry)
    # Nothing about the search reaches Postgres.
    assert await _row_counts(env.session) == before
    assert env.tc.get(f"{ROOT}/anonymous-runs/{search['id']}").json() == {
        "search": search
    }


async def test_the_request_and_visitor_are_bounded(env):
    owner = await _owner(env.session)
    assert _start(env.tc, owner.id, request="   ").status_code == 422
    assert _start(env.tc, owner.id, request="").status_code == 422
    assert _start(env.tc, owner.id, request="x" * 2001).status_code == 422
    for visitor in (
        "short",
        "a" * 129,
        "203.0.113.7" + "0" * 10,
        "2001:db8::1" + "0" * 10,
        "a" * 20 + " ",
        "a" * 20 + "/",
    ):
        assert _start(env.tc, owner.id, visitor=visitor).status_code == 422, visitor
    assert _start(env.tc, owner.id, visitor="A-z_9" * 3 + "0").status_code == 201
    long = _start(env.tc, owner.id, request="word " * 300)
    assert long.status_code == 201
    assert len(long.json()["search"]["request"]) <= site_search.MAX_REQUEST_CHARS
    assert (
        env.tc.post(
            f"{ROOT}/users/{owner.id}/anonymous-runs",
            json={"request": "hello", "visitor": VISITOR, "nid": "x"},
        ).status_code
        == 422
    )


async def test_an_owner_the_link_page_would_refuse_is_refused(env):
    inactive = await _owner(env.session, active=False)
    linkless = await _owner(env.session, link=False)

    assert _start(env.tc, "not-a-uuid").json() == {"detail": "Invalid account ID"}
    for owner_id in (uuid4(), inactive.id, linkless.id):
        response = _start(env.tc, owner_id)
        assert response.status_code == 404
        assert response.json() == {"detail": NOT_FOUND}
    # Refused before any limit is counted or anything is started.
    assert await env.redis.keys("*") == []
    assert env.submitted == []


async def test_a_third_search_in_a_minute_waits_like_the_link_page(env):
    owner = await _owner(env.session)
    for _ in range(anonymous.PER_MINUTE_LIMIT):
        assert _start(env.tc, owner.id).status_code == 201

    limited = _start(env.tc, owner.id)

    assert limited.status_code == 429
    body = limited.json()
    assert body["detail"] == "Searches without logging in are limited to 2 a minute."
    assert 0 < body["wait"] <= 60
    assert limited.headers["Retry-After"] == str(body["wait"])
    assert limited.headers["Cache-Control"] == "private, no-store"
    assert len(env.submitted) == anonymous.PER_MINUTE_LIMIT
    # Another visitor has their own allowance.
    assert _start(env.tc, owner.id, visitor="b" * 32).status_code == 201


async def test_a_link_runs_a_hundred_a_day_across_both_sites(env):
    owner = await _owner(env.session)
    link = await env.session.scalar(
        select(WebSearchLink).where(WebSearchLink.owner_user_id == owner.id)
    )
    # The /s/ page and the site API share one daily allowance per link.
    await links.SearchLinkController.run.fn(
        None, _request(), env.session, link.token, "hello"
    )
    key = f"web_search:anonymous:link:{link.id}"
    assert int(await env.redis.get(key)) == 1
    await env.redis.set(key, anonymous.LINK_PER_DAY_LIMIT - 1, ex=86_400)
    assert _start(env.tc, owner.id).status_code == 201

    limited = _start(env.tc, owner.id, visitor="c" * 32)

    assert limited.status_code == 429
    body = limited.json()
    assert body["detail"] == (
        "This search link has run all its searches without logging in for today."
    )
    assert 0 < body["wait"] <= 86_400
    assert int(await env.redis.get(key)) == anonymous.LINK_PER_DAY_LIMIT


async def test_unknown_expired_and_link_page_searches_are_not_found(env):
    owner = await _owner(env.session)
    search_id = _start(env.tc, owner.id).json()["search"]["id"]
    from_link_page = await anonymous.start(
        env.redis, owner_user_id=owner.id, nid="browser-nid", request="hello"
    )

    for unknown in (uuid4(), "not-an-id", from_link_page["id"]):
        response = env.tc.get(f"{ROOT}/anonymous-runs/{unknown}")
        assert response.status_code == 404
        assert response.json() == {"detail": "This search has expired."}

    assert env.tc.get(f"{ROOT}/anonymous-runs/{search_id}").status_code == 200
    await env.redis.pexpire(f"web_search:anonymous:{search_id}", 1)
    await asyncio.sleep(0.01)
    assert env.tc.get(f"{ROOT}/anonymous-runs/{search_id}").status_code == 404


async def test_a_start_the_queue_refuses_reads_as_an_error(env, monkeypatch):
    import skrift.workers

    async def refuse(*args, **kwargs):
        raise ConnectionError

    monkeypatch.setattr(skrift.workers, "submit", refuse)
    owner = await _owner(env.session)

    search = _start(env.tc, owner.id).json()["search"]

    assert search["status"] == "error" and search["active"] is False
    assert search["error"] == "Couldn't start the search. Try again."
    read = env.tc.get(f"{ROOT}/anonymous-runs/{search['id']}").json()["search"]
    assert read == search


async def test_a_site_search_runs_without_a_row_or_a_notification(
    search_env,  # noqa: F811 - the imported fixture
    monkeypatch,
):
    redis = fakeredis.FakeAsyncRedis()
    owner_id = uuid4()
    state = await anonymous.start(
        redis, owner_user_id=owner_id, nid=None, request="why is it slow"
    )
    sent: list[str | None] = []

    async def notify_session(nid, event_type, **payload):
        sent.append(nid)

    import skrift.notifications

    monkeypatch.setattr(skrift.notifications, "notify_session", notify_session)
    entry = await anonymous.load(redis, state["id"])

    assert await pipeline.search(anonymous.UnsavedRun(redis, entry)) == "complete"

    assert sent == []
    saved = await anonymous.load(redis, state["id"])
    site = saved["site"]
    assert site["status"] == "complete" and site["ranked"] is True
    assert site["results"] and "queries" not in site["results"][0]
    assert site["version"] == saved["search"]["version"] > 0
    session = search_env["session"]
    assert await session.scalar(select(func.count()).select_from(WebSearchRun)) == 0
    rows = list(await session.scalars(select(UsageCostRow)))
    assert rows
    assert all(row.user_id == owner_id and row.details["anonymous"] for row in rows)


async def test_a_late_update_never_brings_back_an_expired_search(env):
    owner = await _owner(env.session)
    search_id = _start(env.tc, owner.id).json()["search"]["id"]
    key = f"web_search:anonymous:{search_id}"
    # The worker loaded the search just before its 30 minutes ran out.
    run = anonymous.UnsavedRun(env.redis, await anonymous.load(env.redis, search_id))
    await env.redis.pexpire(key, 1)
    await asyncio.sleep(0.01)
    assert env.tc.get(f"{ROOT}/anonymous-runs/{search_id}").status_code == 404

    def searching(row):
        row.status = "searching"

    try:
        await run.update(searching)
        stopped = False
    except anonymous.Expired:
        stopped = True

    assert env.tc.get(f"{ROOT}/anonymous-runs/{search_id}").status_code == 404
    assert await env.redis.exists(key) == 0
    # The late write also ends the run, rather than carrying on unsaved.
    assert stopped


async def test_a_search_is_never_served_past_its_thirty_minutes(env):
    owner = await _owner(env.session)
    search_id = _start(env.tc, owner.id).json()["search"]["id"]
    key = f"web_search:anonymous:{search_id}"
    entry = await anonymous.load(env.redis, search_id)

    async def started_ago(minutes):
        # A key that lost its TTL somehow: only the start time can end it.
        entry["search"]["created_at"] = (
            datetime.now(UTC) - timedelta(minutes=minutes)
        ).isoformat()
        await env.redis.set(key, json.dumps(entry))
        assert await env.redis.ttl(key) == -1

    await started_ago(29)
    assert env.tc.get(f"{ROOT}/anonymous-runs/{search_id}").status_code == 200
    await started_ago(30)
    response = env.tc.get(f"{ROOT}/anonymous-runs/{search_id}")
    assert response.status_code == 404
    assert response.json() == {"detail": "This search has expired."}


async def test_a_search_that_expires_mid_run_stops_without_a_retry(
    search_env,  # noqa: F811 - the imported fixture
    monkeypatch,
):
    from smarter_dev.web import web_search_jobs
    from smarter_dev.web.web_search import brave

    redis = fakeredis.FakeAsyncRedis()
    monkeypatch.setattr(
        "smarter_dev.shared.redis_client.get_redis_client", lambda: redis
    )
    state = await anonymous.start(
        redis, owner_user_id=uuid4(), nid=None, request="why is it slow"
    )
    key = f"web_search:anonymous:{state['id']}"
    searched: list[str] = []

    async def search_then_expire(client, query):
        # The 30 minutes run out while Brave is answering the first query.
        searched.append(query)
        await redis.delete(key)
        return search_env["hits"][query]

    monkeypatch.setattr(brave, "search", search_then_expire)

    result = await web_search_jobs.run_anonymous_web_search_job(
        web_search_jobs.WebSearchPayload(search_id=state["id"])
    )

    assert result == {"status": "expired"}
    # The next write found it gone and the run stopped there.
    assert searched == ["query 0"]
    assert await redis.exists(key) == 0


async def test_a_refusal_at_the_window_edge_takes_nothing_from_the_next_window():
    redis = fakeredis.FakeAsyncRedis()
    key = "web_search:anonymous:visitor:edge"
    limit = anonymous.PER_MINUTE_LIMIT
    for _ in range(limit):
        assert await anonymous.wait_seconds(redis, key, limit, 60) == 0
    admitted: list[bool] = []

    async def roll_over():
        # The window ends and a new visitor's search starts in the new one.
        await redis.delete(key)
        admitted.append(await anonymous.wait_seconds(redis, key, limit, 60) == 0)

    # A refusal that rolls back in a second step would run that step here,
    # after the window has rolled over.
    real_decr = redis.decr

    async def decr_after_rollover(name, amount=1):
        await roll_over()
        return await real_decr(name, amount)

    redis.decr = decr_after_rollover
    assert await anonymous.wait_seconds(redis, key, limit, 60) > 0
    if not admitted:
        await roll_over()
    while await anonymous.wait_seconds(redis, key, limit, 60) == 0:
        admitted.append(True)

    assert admitted == [True] * limit
    assert int(await redis.get(key)) == limit
    assert 0 < await redis.ttl(key) <= 60
