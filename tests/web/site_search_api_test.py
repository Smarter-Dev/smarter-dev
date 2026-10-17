"""Site search facade: principal isolation, idempotent starts, limits,
structured snapshots without HTML, ownership and deletion."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID
from uuid import uuid4

import pytest
from litestar.di import Provide
from litestar.plugins.pydantic import PydanticPlugin
from litestar.testing import create_test_client
from skrift.db.base import Base as SkriftBase
from skrift.db.models.user import User

from smarter_dev.shared.database import async_sessionmaker
from smarter_dev.web import dashboard_controller
from smarter_dev.web.api_native import site_guards
from smarter_dev.web.api_native.site_search import SiteSearchController
from smarter_dev.web.models import WebSearchRun

ROOT = "/api/site/search"


@pytest.fixture
async def client(test_engine, db_session, monkeypatch):
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
    # The agent worker is not part of this test: the outbox row is enough.
    monkeypatch.setattr(dashboard_controller, "dispatch_one", AsyncMock())
    dashboard_controller._recent_starts.clear()
    with create_test_client(
        route_handlers=[SiteSearchController],
        plugins=[PydanticPlugin()],
        dependencies={"db_session": Provide(session)},
    ) as tc:
        yield tc, key


@pytest.fixture
async def member(db_session):
    user = User(email=f"{uuid4()}@example.test", name="Member", is_active=True)
    db_session.add(user)
    await db_session.commit()
    return user


def _start(tc, user_id, request="latest Python release notes", key=None):
    return tc.post(
        f"{ROOT}/users/{user_id}/runs",
        json={"request": request, "key": key or uuid4().hex},
    )


@pytest.mark.parametrize(
    "principal,service,scopes",
    [
        ("user", None, ["site-search"]),
        ("service", "bot", ["site-search"]),
        ("service", "site", ["site-memory", "site-privacy", "site-billing"]),
        ("service", "site", []),
    ],
)
def test_every_route_requires_the_site_search_scope(client, principal, service, scopes):
    tc, key = client
    key.principal_type, key.service_name, key.scoped_permission_list = (
        principal,
        service,
        scopes,
    )
    path = f"{ROOT}/users/{uuid4()}/runs"
    for response in (
        tc.post(path, json={"request": "anything at all", "key": uuid4().hex}),
        tc.get(f"{path}/{uuid4()}"),
        tc.delete(f"{path}/{uuid4()}"),
    ):
        assert response.status_code == 401


async def test_start_is_idempotent_per_turn_key(client, member, db_session):
    tc, _ = client
    key = uuid4().hex
    first = _start(tc, member.id, key=key)
    assert first.status_code == 201
    assert first.headers["Cache-Control"] == "private, no-store"
    search = first.json()["search"]
    assert search["status"] == "queued" and search["active"] is True
    again = _start(tc, member.id, request="something else entirely", key=key)
    assert again.json() == {"search": search, "idempotent": True}
    runs = (await db_session.execute(WebSearchRun.__table__.select())).all()
    assert len(runs) == 1
    dashboard_controller.dispatch_one.assert_awaited_once()


async def test_start_validates_the_member_request_and_key(client, member):
    tc, _ = client
    assert _start(tc, "not-a-uuid").status_code == 400
    assert _start(tc, uuid4()).status_code == 404
    assert _start(tc, member.id, request="  hi ").status_code == 422
    assert _start(tc, member.id, key="short").status_code == 422
    long = _start(tc, member.id, request="word " * 300)
    assert long.status_code == 201
    assert (
        len(long.json()["search"]["request"]) <= dashboard_controller.MAX_REQUEST_CHARS
    )


async def test_starts_share_the_dashboards_limits(client, member):
    tc, _ = client
    for _ in range(dashboard_controller.PER_MINUTE_LIMIT):
        assert _start(tc, member.id).status_code == 201
    limited = _start(tc, member.id)
    assert limited.status_code == 429
    assert (
        limited.json()["detail"] == "That's a lot of searches. Try again in a minute."
    )


async def test_snapshot_is_structured_markdown_never_html(client, member, db_session):
    tc, _ = client
    run_id = _start(tc, member.id).json()["search"]["id"]
    run = await db_session.get(WebSearchRun, UUID(run_id))
    run.status = "complete"
    run.ranked = True
    run.needs_answer = True
    run.results = [
        {
            "title": "Release notes",
            "url": "https://docs.python.org/3/whatsnew/",
            "domain": "docs.python.org",
            "snippet": "What's new",
            "favicon": "https://docs.python.org/favicon.ico",
            "age": "",
            "queries": [0],
            "score": 0.9,
            "relevant": True,
            "best": True,
            "internal": "dropped",
        }
    ]
    run.answer = {
        "status": "done",
        "reads": [{"number": 1, "domain": "docs.python.org", "status": "done"}],
        "html": '<p>See <a href="https://docs.python.org">[1]</a></p>',
        "markdown": "See the notes [1].",
    }
    await db_session.commit()
    search = tc.get(f"{ROOT}/users/{member.id}/runs/{run_id}").json()["search"]
    assert search["active"] is False
    assert search["results"] == [
        {
            "title": "Release notes",
            "url": "https://docs.python.org/3/whatsnew/",
            "domain": "docs.python.org",
            "snippet": "What's new",
            "favicon": "https://docs.python.org/favicon.ico",
            "age": "",
            "relevant": True,
            "best": True,
            "score": 0.9,
        }
    ]
    assert search["answer"] == {
        "status": "done",
        "markdown": "See the notes [1].",
        "reads": [{"number": 1, "domain": "docs.python.org", "status": "done"}],
        "error": None,
    }
    assert "html" not in str(search)


async def test_runs_are_private_to_their_owner(client, member, db_session):
    tc, _ = client
    other = User(email=f"{uuid4()}@example.test", name="Other", is_active=True)
    db_session.add(other)
    await db_session.commit()
    run_id = _start(tc, member.id).json()["search"]["id"]
    assert tc.get(f"{ROOT}/users/{other.id}/runs/{run_id}").status_code == 404
    assert tc.delete(f"{ROOT}/users/{other.id}/runs/{run_id}").status_code == 404
    assert tc.get(f"{ROOT}/users/{member.id}/runs/not-a-run").status_code == 404


async def test_delete_waits_for_a_running_search(client, member, db_session):
    tc, _ = client
    run_id = _start(tc, member.id).json()["search"]["id"]
    assert tc.delete(f"{ROOT}/users/{member.id}/runs/{run_id}").status_code == 409
    run = await db_session.get(WebSearchRun, UUID(run_id))
    run.status = "complete"
    await db_session.commit()
    assert tc.delete(f"{ROOT}/users/{member.id}/runs/{run_id}").json() == {
        "status": "deleted"
    }
    assert tc.get(f"{ROOT}/users/{member.id}/runs/{run_id}").status_code == 404


async def test_inactive_members_cannot_search(client, member, db_session):
    tc, _ = client
    member.is_active = False
    await db_session.commit()
    assert _start(tc, member.id).status_code == 404


async def test_overlapping_starts_for_one_turn_return_the_same_run(
    client, member, test_engine, monkeypatch
):
    """Two starts with one key: the second passes the "already started?"
    check, then the first saves its run on another connection before the
    second inserts. The second must answer as an idempotent repeat."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    tc, _ = client
    key = uuid4().hex
    other = create_async_engine(test_engine.url, poolclass=NullPool)
    real_limits = dashboard_controller._enforce_limits

    async def the_other_start_wins(session, user_id):
        await real_limits(session, user_id)
        async with async_sessionmaker(other, expire_on_commit=False)() as rival:
            await dashboard_controller.create_search(
                rival, user_id, "latest Python release notes", key
            )

    monkeypatch.setattr(dashboard_controller, "_enforce_limits", the_other_start_wins)
    try:
        response = _start(tc, member.id, key=key)
    finally:
        await other.dispose()
    assert response.status_code == 201
    body = response.json()
    assert body["idempotent"] is True
    monkeypatch.setattr(dashboard_controller, "_enforce_limits", real_limits)
    repeat = _start(tc, member.id, key=key)
    assert repeat.json() == body
