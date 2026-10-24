"""Real local PostgreSQL transactions for forgetting a site search by its key.

SQLite ignores FOR UPDATE, so only Postgres shows the users-row lock that
every start (``dashboard_controller.create_search``), the site's forget and
the account purge take, in that one order. Opt in with
SITE_SEARCH_TEST_POSTGRES_URL pointing at an ephemeral LOCAL DB. Never use
a shared/remote database: this fixture creates and drops every table.
"""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from litestar import Litestar
from litestar.di import Provide
from litestar.plugins.pydantic import PydanticPlugin
from skrift.db.base import Base as SkriftBase
from skrift.db.models.user import User
from sqlalchemy import delete
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine

from smarter_dev.shared.database import Base
from smarter_dev.web import dashboard_controller
from smarter_dev.web import site_identity
from smarter_dev.web import user_content
from smarter_dev.web.api_native import site_guards
from smarter_dev.web.api_native import site_search
from smarter_dev.web.models import WebSearchForgottenKey
from smarter_dev.web.models import WebSearchRun

URL = os.getenv("SITE_SEARCH_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not URL, reason="ephemeral local PostgreSQL not configured"
)
# How long the first transaction holds its locks: long enough that the
# second must be waiting, never a timing the assertions depend on.
HOLD = 1.0


@pytest.fixture
async def db(monkeypatch):
    assert make_url(URL).host in {"127.0.0.1", "localhost"}
    engine = create_async_engine(URL)
    async with engine.begin() as conn:
        await conn.run_sync(SkriftBase.metadata.create_all)
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def session():
        async with maker() as s:
            yield s

    monkeypatch.setattr(site_guards, "bot_api_auth_guard", AsyncMock())
    key = SimpleNamespace(
        principal_type="service",
        service_name="site",
        scoped_permission_list=["site-search"],
    )
    monkeypatch.setattr(
        site_guards, "resolve_request_api_key", AsyncMock(return_value=key)
    )
    monkeypatch.setattr(dashboard_controller, "dispatch_one", AsyncMock())
    # The identity fence comes before the users row and is outside this cycle.
    monkeypatch.setattr(site_identity, "fence_account_deletion", AsyncMock())
    app = Litestar(
        route_handlers=[site_search.SiteSearchController],
        plugins=[PydanticPlugin()],
        dependencies={"db_session": Provide(session)},
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield maker, client
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(SkriftBase.metadata.drop_all)
    await engine.dispose()


async def _member(maker, active=True):
    async with maker() as s:
        user = User(email=f"{uuid4()}@example.test", name="M", is_active=active)
        s.add(user)
        await s.commit()
        return user.id


async def _count(maker, model, owner):
    async with maker() as s:
        return len(
            (await s.scalars(select(model).where(model.owner_user_id == owner))).all()
        )


async def _finished_site_run(maker, owner):
    key = uuid4().hex + uuid4().hex  # 64 hex, as a site turn's key is
    async with maker() as s:
        run = await dashboard_controller.create_search(s, owner, "a request", key)
        await s.execute(
            update(WebSearchRun)
            .where(WebSearchRun.id == run.id)
            .values(status="complete")
        )
        await s.commit()
    return run.id, key


async def _waiting(task):
    await asyncio.sleep(HOLD / 4)
    assert not task.done(), "the second transaction must wait on the users row"


@pytest.mark.parametrize("forget_first", [True, False])
async def test_a_dashboard_start_and_a_forget_of_its_key(db, monkeypatch, forget_first):
    """Every start makes its run in create_search, under the users-row lock
    the forget takes: forget first refuses the start; start first, the
    forget waits, finds the run (409) and deletes it once it has finished."""
    maker, client = db
    owner, key = await _member(maker), uuid4().hex
    forget = f"/api/site/search/users/{owner}/keys/{key}"
    if forget_first:
        held = asyncio.Event()
        real = site_search._run_for_key

        async def holding(s, o, k):
            out = await real(s, o, k)
            held.set()
            await asyncio.sleep(HOLD)
            return out

        monkeypatch.setattr(site_search, "_run_for_key", holding)
        forgot = asyncio.create_task(client.delete(forget))
        await held.wait()

        async def start():
            async with maker() as s:
                await dashboard_controller.create_search(s, owner, "a request", key)

        started = asyncio.create_task(start())
        await _waiting(started)
        assert (await forgot).json() == {"status": "forgotten"}
        with pytest.raises(dashboard_controller.SearchKeyForgotten):
            await started
        assert await _count(maker, WebSearchRun, owner) == 0
        return
    held = asyncio.Event()
    real = dashboard_controller.create_dispatch

    async def holding_dispatch(*args, **kwargs):
        held.set()
        await asyncio.sleep(HOLD)
        return await real(*args, **kwargs)

    monkeypatch.setattr(dashboard_controller, "create_dispatch", holding_dispatch)

    async def start():
        async with maker() as s:
            return await dashboard_controller.create_search(s, owner, "a request", key)

    started = asyncio.create_task(start())
    await held.wait()
    forgot = asyncio.create_task(client.delete(forget))
    await _waiting(forgot)
    await started
    assert (await forgot).status_code == 409
    async with maker() as s:
        await s.execute(
            update(WebSearchRun)
            .where(WebSearchRun.owner_user_id == owner)
            .values(status="complete")
        )
        await s.commit()
    assert (await client.delete(forget)).json() == {"status": "forgotten"}
    assert await _count(maker, WebSearchRun, owner) == 0
    monkeypatch.setattr(dashboard_controller, "create_dispatch", real)
    async with maker() as s:
        with pytest.raises(dashboard_controller.SearchKeyForgotten):
            await dashboard_controller.create_search(s, owner, "a request", key)


@pytest.mark.parametrize("purge_first", [True, False])
async def test_the_account_purge_and_a_delete_never_deadlock(
    db, monkeypatch, purge_first
):
    """A closing account with a finished run, purged while the site deletes
    that run. Both take the users row before the run and its dispatches,
    so one waits for the other and neither is aborted; nothing is left.
    (Purge first: the delete waits on the users row. Delete first, holding
    the users row: the purge waits there too, before it touches a run.)"""
    maker, client = db
    owner = await _member(maker)
    run_id, _key = await _finished_site_run(maker, owner)
    async with maker() as s:
        await s.execute(update(User).where(User.id == owner).values(is_active=False))
        await s.commit()
    held = asyncio.Event()

    async def purge():
        # The account deletion's transaction: the leftovers, then the user.
        async with maker() as s:
            await user_content.delete_account_leftovers(s, owner)
            if purge_first:
                held.set()
                await asyncio.sleep(HOLD)
            await s.execute(delete(User).where(User.id == owner))
            await s.commit()

    async def remove():
        async with maker() as s:
            await user_content.delete_search(s, await s.get(WebSearchRun, run_id))

    if purge_first:
        purged = asyncio.create_task(purge())
        await held.wait()
        removed = asyncio.create_task(remove())
        await _waiting(removed)
        await purged
        await removed
    else:
        real = user_content.lock_member

        async def holding(s, user_id):
            out = await real(s, user_id)
            held.set()
            await asyncio.sleep(HOLD)
            return out

        monkeypatch.setattr(user_content, "lock_member", holding)
        removed = asyncio.create_task(remove())
        await held.wait()
        monkeypatch.setattr(user_content, "lock_member", real)
        purged = asyncio.create_task(purge())
        await _waiting(purged)
        await removed
        await purged
    assert await _count(maker, WebSearchRun, owner) == 0
    assert await _count(maker, WebSearchForgottenKey, owner) == 0
    async with maker() as s:
        assert await s.get(User, owner) is None


async def test_a_delete_holding_the_lock_refuses_a_start_with_the_site_key(
    db, monkeypatch
):
    """The dashboard's own delete of a site run (delete_search) marks the
    key under the users-row lock: a site start retried with that key waits
    for the delete, then gets 410, and no run is made again."""
    maker, client = db
    owner = await _member(maker)
    key = "ab" * 32
    async with maker() as s:
        run = await dashboard_controller.create_search(s, owner, "a request", key)
        await s.execute(
            update(WebSearchRun)
            .where(WebSearchRun.id == run.id)
            .values(status="complete")
        )
        await s.commit()
    held = asyncio.Event()
    real = user_content.forget_dispatches

    async def holding(*args, **kwargs):
        held.set()
        await asyncio.sleep(HOLD)
        return await real(*args, **kwargs)

    monkeypatch.setattr(user_content, "forget_dispatches", holding)

    async def remove():
        async with maker() as s:
            found = await s.get(WebSearchRun, run.id)
            await user_content.delete_search(s, found)

    removed = asyncio.create_task(remove())
    await held.wait()
    started = asyncio.create_task(
        client.post(
            f"/api/site/search/users/{owner}/runs",
            json={"request": "a request", "key": key},
        )
    )
    await _waiting(started)
    await removed
    assert (await started).status_code == 410
    assert await _count(maker, WebSearchRun, owner) == 0
    assert await _count(maker, WebSearchForgottenKey, owner) == 1


@pytest.mark.parametrize("entry", ["dashboard one", "dashboard all", "site remove"])
async def test_every_delete_entry_point_forgets_a_site_key(db, entry):
    """Deleted by the dashboard's single delete, its delete-all or the
    site's delete by id: a site start retried with the run's key gets 410
    and makes nothing."""
    maker, client = db
    owner = await _member(maker)
    run_id, key = await _finished_site_run(maker, owner)
    if entry == "dashboard one":
        # As DashboardController.delete_one_search: the owner's run, then delete_search.
        async with maker() as s:
            await user_content.delete_search(
                s, await dashboard_controller._owned(s, owner, run_id)
            )
    elif entry == "dashboard all":
        async with maker() as s:
            assert (await user_content.delete_all_searches(s, owner)).deleted == 1
    else:
        removed = await client.delete(f"/api/site/search/users/{owner}/runs/{run_id}")
        assert removed.json() == {"status": "deleted"}
    assert await _count(maker, WebSearchRun, owner) == 0
    again = await client.post(
        f"/api/site/search/users/{owner}/runs",
        json={"request": "a request", "key": key},
    )
    assert again.status_code == 410
    assert await _count(maker, WebSearchRun, owner) == 0


async def test_a_start_that_waited_out_a_forget_and_the_purge_makes_no_run(
    db, monkeypatch
):
    """A dashboard start paused after its own checks; meanwhile the site
    forgets its key and the account is purged, user row and all. The start
    then finds no owner: no orphan run or dispatch."""
    maker, client = db
    owner, key = await _member(maker), uuid4().hex
    paused, resume = asyncio.Event(), asyncio.Event()

    async def start():
        async with maker() as s:
            paused.set()
            await resume.wait()  # its idempotency check and limits are done
            await dashboard_controller.create_search(s, owner, "a request", key)

    started = asyncio.create_task(start())
    await paused.wait()
    assert (
        await client.delete(f"/api/site/search/users/{owner}/keys/{key}")
    ).status_code == 200
    async with maker() as s:
        await user_content.delete_account_leftovers(s, owner)
        await s.execute(delete(User).where(User.id == owner))
        await s.commit()
    resume.set()
    with pytest.raises(dashboard_controller.SearchKeyForgotten):
        await started
    assert await _count(maker, WebSearchRun, owner) == 0
    async with maker() as s:
        from smarter_dev.web.models import WorkDispatch

        assert (
            await s.scalars(
                select(WorkDispatch).where(WorkDispatch.job_type == "web_search.run")
            )
        ).all() == []
