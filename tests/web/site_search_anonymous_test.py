"""Site search facade, anonymous runs: a signed-out visitor using a member's
search link. Never saved to Postgres, kept in Redis for 30 minutes, billed to
the link's owner, under the /s/ pages' anonymous limits.

Redis is fakeredis; the agent worker queue and notifications are replaced."""

import asyncio
import json
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
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
from smarter_dev.web import web_search_jobs
from smarter_dev.web.api_native import site_guards
from smarter_dev.web.api_native import site_search
from smarter_dev.web.api_native.site_search import SiteSearchController
from smarter_dev.web.models import UsageCostRow
from smarter_dev.web.models import WebSearchLink
from smarter_dev.web.models import WebSearchRun
from smarter_dev.web.web_search import anonymous
from smarter_dev.web.web_search import answer
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


async def test_an_unknown_or_inactive_owner_is_refused(env):
    inactive = await _owner(env.session, active=False)
    inactive_linkless = await _owner(env.session, active=False, link=False)

    assert _start(env.tc, "not-a-uuid").json() == {"detail": "Invalid account ID"}
    for owner_id in (uuid4(), inactive.id, inactive_linkless.id):
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
    assert body["per_link"] is False
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
    assert body["per_link"] is True
    assert limited.headers["Retry-After"] == str(body["wait"])
    assert limited.headers["Cache-Control"] == "private, no-store"
    assert int(await env.redis.get(key)) == anonymous.LINK_PER_DAY_LIMIT
    # Nothing was counted under the owner's own key.
    assert await env.redis.get(f"web_search:anonymous:link:owner:{owner.id}") is None


async def test_an_owner_whose_link_only_the_site_has_runs_under_its_own_day(env):
    # next.smarter.dev keeps search links in D1; Python has no row for them.
    owner = await _owner(env.session, link=False)
    other = await _owner(env.session, link=False)

    started = _start(env.tc, owner.id)

    assert started.status_code == 201
    assert started.json()["search"]["status"] == "queued"
    [submitted] = env.submitted
    entry = await anonymous.load(env.redis, submitted["search_id"])
    assert entry["owner_user_id"] == str(owner.id)
    key = f"web_search:anonymous:link:owner:{owner.id}"
    assert int(await env.redis.get(key)) == 1
    assert 0 < await env.redis.ttl(key) <= 86_400

    await env.redis.set(key, anonymous.LINK_PER_DAY_LIMIT, ex=86_400)
    limited = _start(env.tc, owner.id, visitor="d" * 32)
    assert limited.status_code == 429
    assert limited.json()["per_link"] is True
    assert limited.json()["detail"] == anonymous.LINK_LIMITED
    # Another owner's day is their own.
    assert _start(env.tc, other.id, visitor="e" * 32).status_code == 201
    assert int(await env.redis.get(f"web_search:anonymous:link:owner:{other.id}")) == 1


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
    # What it had already spent is still billed to the owner, once.
    assert await _billed(search_env["session"]) == {
        "web_search_queries": [None],
        "web_search_brave": [1],
    }


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


async def _billed(session) -> dict[str, list]:
    rows = list(await session.scalars(select(UsageCostRow)))
    assert all(row.details["anonymous"] for row in rows)
    billed: dict[str, list] = {}
    for row in rows:
        billed.setdefault(row.operation_type, []).append(row.details.get("requests"))
    return billed


@pytest.mark.parametrize(
    ("stage", "billed"),
    [
        # Each paid step is billed before the write that finds the search gone.
        ("plan", {"web_search_queries": [None]}),
        (
            "second query",
            {"web_search_queries": [None], "web_search_brave": [2]},
        ),
        (
            "rank",
            {
                "web_search_queries": [None],
                "web_search_brave": [5],
                "web_search_ranking": [None],
            },
        ),
    ],
)
async def test_a_search_that_expires_at_any_paid_step_bills_what_it_spent(
    search_env,  # noqa: F811 - the imported fixture
    monkeypatch,
    stage,
    billed,
):
    from smarter_dev.web.web_search import brave
    from smarter_dev.web.web_search import queries
    from smarter_dev.web.web_search import ranking

    redis = fakeredis.FakeAsyncRedis()
    monkeypatch.setattr(
        "smarter_dev.shared.redis_client.get_redis_client", lambda: redis
    )
    state = await anonymous.start(
        redis, owner_user_id=uuid4(), nid=None, request="why is it slow"
    )
    key = f"web_search:anonymous:{state['id']}"

    def expiring(module, name, when=lambda *args: True):
        real = getattr(module, name)

        async def call(*args, **kwargs):
            # The 30 minutes run out while this paid call is in flight.
            if when(*args):
                await redis.delete(key)
            return await real(*args, **kwargs)

        monkeypatch.setattr(module, name, call)

    if stage == "plan":
        expiring(queries, "plan_queries")
    elif stage == "second query":
        expiring(brave, "search", when=lambda client, query: query == "query 1")
    else:
        expiring(ranking, "rank")

    result = await web_search_jobs.run_anonymous_web_search_job(
        web_search_jobs.WebSearchPayload(search_id=state["id"])
    )
    again = await web_search_jobs.run_anonymous_web_search_job(
        web_search_jobs.WebSearchPayload(search_id=state["id"])
    )

    assert result == again == {"status": "expired"}
    assert await redis.exists(key) == 0
    # A second delivery finds nothing to run and bills nothing more.
    assert await _billed(search_env["session"]) == billed


@pytest.mark.parametrize(
    "created_at",
    ["missing", None, 12345, "not a time", "2026-10-20T10:00:00"],
)
async def test_an_unreadable_start_time_reads_as_expired(env, created_at):
    owner = await _owner(env.session)
    search_id = _start(env.tc, owner.id).json()["search"]["id"]
    key = f"web_search:anonymous:{search_id}"
    entry = json.loads(await env.redis.get(key))
    if created_at == "missing":
        del entry["search"]["created_at"]
    else:
        entry["search"]["created_at"] = created_at
    await env.redis.set(key, json.dumps(entry), keepttl=True)

    assert await anonymous.load(env.redis, search_id) is None
    response = env.tc.get(f"{ROOT}/anonymous-runs/{search_id}")
    assert response.status_code == 404
    assert response.json() == {"detail": "This search has expired."}


@pytest.fixture
def answering(search_env, monkeypatch):  # noqa: F811 - the imported fixture
    """An anonymous search that plans an answer, written by the real answer
    agent on a stand-in model: it reads result 1 when ``read`` is set, then
    streams its answer. ``expire()`` ends the search's 30 minutes."""
    from pydantic_ai.messages import ToolReturnPart
    from pydantic_ai.models.function import DeltaToolCall
    from pydantic_ai.models.function import FunctionModel

    from smarter_dev.web.web_search import metering
    from smarter_dev.web.web_search import queries

    redis = fakeredis.FakeAsyncRedis()
    monkeypatch.setattr(
        "smarter_dev.shared.redis_client.get_redis_client", lambda: redis
    )
    stub_plan = queries.plan_queries

    async def plan(request, meter=None):
        planned, _, usage = await stub_plan(request, meter=meter)
        return planned, True, usage

    monkeypatch.setattr(queries, "plan_queries", plan)
    env = SimpleNamespace(redis=redis, read=False, expire_on=None, spent=[])

    async def expire(step):
        if env.expire_on == step:
            await redis.delete(env.key)

    async def respond(messages, info):
        if env.read and not any(
            isinstance(part, ToolReturnPart)
            for message in messages
            for part in getattr(message, "parts", [])
        ):
            yield {0: DeltaToolCall(name="read_result", json_args='{"number": 1}')}
            return
        await expire("writing")
        for chunk in ["The answer ", "is [1]."]:
            yield chunk

    monkeypatch.setattr(
        queries,
        "model_for",
        lambda name: FunctionModel(stream_function=respond, model_name=name),
    )

    async def read_page(client, url):
        # Jina answers and bills the page; the 30 minutes run out meanwhile.
        await expire("reading")
        return "The page.", 2_000_000

    monkeypatch.setattr(answer, "read_page", read_page)
    real_failed_usage = metering.failed_usage

    def failed_usage(meter, model):
        env.spent.append(real_failed_usage(meter, model))
        return env.spent[-1]

    monkeypatch.setattr(metering, "failed_usage", failed_usage)

    async def run_twice():
        state = await anonymous.start(
            redis, owner_user_id=uuid4(), nid=None, request="why is it slow"
        )
        env.key = f"web_search:anonymous:{state['id']}"
        payload = web_search_jobs.WebSearchPayload(search_id=state["id"])
        first = await web_search_jobs.run_anonymous_web_search_job(payload)
        again = await web_search_jobs.run_anonymous_web_search_job(payload)
        return first, again

    env.run_twice = run_twice
    env.session = search_env["session"]
    return env


async def _ledger(session) -> dict[str, UsageCostRow]:
    rows = list(await session.scalars(select(UsageCostRow)))
    assert all(row.details["anonymous"] for row in rows)
    ledger = {row.operation_type: row for row in rows}
    assert len(ledger) == len(rows)
    return ledger


def _answer_billed(row: UsageCostRow, spent: dict) -> None:
    assert spent["failed"] is True and spent["requests"] >= 1
    assert (row.input_tokens, row.output_tokens) == (
        spent["input_tokens"],
        spent["output_tokens"],
    )
    assert row.output_tokens > 0


async def test_a_page_read_the_search_outlives_is_billed_once(answering):
    # The reviewer's probe: Jina reads and bills a page, then the write that
    # shows the read done finds the search gone.
    answering.read = True
    answering.expire_on = "reading"

    assert await answering.run_twice() == ({"status": "expired"},) * 2

    ledger = await _ledger(answering.session)
    assert set(ledger) == {
        "web_search_queries",
        "web_search_brave",
        "web_search_ranking",
        "web_search_answer",
        "web_search_reader",
    }
    reader = ledger["web_search_reader"]
    assert reader.provider_key == "jina" and reader.model_id == "jina-reader"
    assert reader.output_tokens == 2_000_000
    assert reader.cost_usd == 2 * Decimal(answer.JINA_PRICE_PER_MILLION_TOKENS_USD)
    # The model's request for the page was billed too.
    [spent] = answering.spent
    assert spent["requests"] == 1
    _answer_billed(ledger["web_search_answer"], spent)


async def test_an_answer_the_search_outlives_bills_the_model_once(answering):
    # The model streams its answer and the write that shows it finds the
    # search gone: no page was read.
    answering.expire_on = "writing"

    assert await answering.run_twice() == ({"status": "expired"},) * 2

    ledger = await _ledger(answering.session)
    assert set(ledger) == {
        "web_search_queries",
        "web_search_brave",
        "web_search_ranking",
        "web_search_answer",
    }
    [spent] = answering.spent
    assert spent["requests"] == 1
    _answer_billed(ledger["web_search_answer"], spent)
