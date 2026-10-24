"""Site search facade: principal isolation, idempotent starts, limits,
structured snapshots without HTML, ownership, counts and deletion."""

import hashlib
from datetime import UTC
from datetime import datetime
from datetime import timedelta
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
from sqlalchemy import func
from sqlalchemy import select

from smarter_dev.shared.database import async_sessionmaker
from smarter_dev.web import dashboard_controller
from smarter_dev.web.api_native import site_guards
from smarter_dev.web.api_native.site_search import SiteSearchController
from smarter_dev.web.models import WebSearchForgottenKey
from smarter_dev.web.models import WebSearchRun
from smarter_dev.web.models import WorkDispatch
from smarter_dev.web.user_content import STALLED_AFTER

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
        tc.get(f"{ROOT}/users/{uuid4()}/keys/{uuid4().hex}"),
        tc.delete(f"{ROOT}/users/{uuid4()}/keys/{uuid4().hex}"),
        tc.get(f"{path}/count"),
        tc.post(f"{path}/delete-all", json={}),
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
            "queries": [0, 2],
            "score": 0.9,
            "relevant": True,
            "best": True,
            "quality": True,
            "internal": "dropped",
        },
        {
            "title": "Unranked",
            "url": "https://example.com/",
            "domain": "example.com",
            "snippet": "",
            "favicon": "",
            "age": "",
            "queries": [1, "2", True, None],
        },
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
            "quality": True,
            "queries": [0, 2],
        },
        {
            "title": "Unranked",
            "url": "https://example.com/",
            "domain": "example.com",
            "snippet": "",
            "favicon": "",
            "age": "",
            "relevant": False,
            "best": False,
            "score": None,
            "quality": False,
            "queries": [1],
        },
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


async def test_a_lost_start_is_found_by_its_key_without_starting_one(
    client, member, db_session
):
    """A start whose reply never reached the site: its key finds the run,
    and a key no start used answers null (never a 404, which is an API
    without this route). Nothing is started either way."""
    tc, _ = client
    key = uuid4().hex
    started = _start(tc, member.id, key=key).json()["search"]
    found = tc.get(f"{ROOT}/users/{member.id}/keys/{key}")
    assert found.status_code == 200
    assert found.headers["Cache-Control"] == "private, no-store"
    assert found.json() == {"search": started}
    missing = tc.get(f"{ROOT}/users/{member.id}/keys/{uuid4().hex}")
    assert missing.status_code == 200 and missing.json() == {"search": None}
    runs = (await db_session.execute(WebSearchRun.__table__.select())).all()
    assert len(runs) == 1
    dashboard_controller.dispatch_one.assert_awaited_once()


async def test_a_key_finds_only_its_owners_run(client, member, db_session):
    tc, _ = client
    other = User(email=f"{uuid4()}@example.test", name="Other", is_active=True)
    db_session.add(other)
    await db_session.commit()
    key = uuid4().hex
    _start(tc, member.id, key=key)
    assert tc.get(f"{ROOT}/users/{other.id}/keys/{key}").json() == {"search": None}
    assert tc.get(f"{ROOT}/users/{member.id}/keys/short").status_code == 422
    assert tc.get(f"{ROOT}/users/not-a-uuid/keys/{key}").status_code == 400
    assert tc.get(f"{ROOT}/users/{uuid4()}/keys/{key}").status_code == 404


def _forget(tc, user_id, key):
    return tc.delete(f"{ROOT}/users/{user_id}/keys/{key}")


async def test_a_key_forgotten_before_its_start_lands_refuses_the_start(
    client, member, db_session
):
    """The start's reply was lost and the site forgot the key while the
    start was still on its way: the start arrives second and makes no run."""
    tc, _ = client
    key = uuid4().hex
    forgot = _forget(tc, member.id, key)
    assert forgot.status_code == 200
    assert forgot.headers["Cache-Control"] == "private, no-store"
    assert forgot.json() == {"status": "forgotten"}
    late = _start(tc, member.id, key=key)
    assert late.status_code == 410
    assert late.json()["detail"] == "This search was deleted."
    assert (await db_session.execute(WebSearchRun.__table__.select())).all() == []
    dashboard_controller.dispatch_one.assert_not_awaited()
    # Forgetting again is the same answer, and other keys still start.
    assert _forget(tc, member.id, key).json() == {"status": "forgotten"}
    assert _start(tc, member.id).status_code == 201


async def test_a_started_key_is_deleted_once_its_run_finishes(
    client, member, db_session
):
    """The start landed first: the forget waits (409) while the run runs,
    the key already refuses a retried start, and once the run has finished
    the forget deletes it."""
    tc, _ = client
    key = uuid4().hex
    member_id = member.id
    run_id = _start(tc, member.id, key=key).json()["search"]["id"]
    running = _forget(tc, member.id, key)
    assert running.status_code == 409
    assert running.json()["detail"] == "Wait for this search to finish, then delete it."
    assert _start(tc, member.id, key=key).status_code == 410
    run = await db_session.get(WebSearchRun, UUID(run_id))
    run.status = "complete"
    await db_session.commit()
    assert _forget(tc, member.id, key).json() == {"status": "forgotten"}
    db_session.expire_all()
    assert await db_session.get(WebSearchRun, UUID(run_id)) is None
    assert tc.get(f"{ROOT}/users/{member_id}/keys/{key}").json() == {"search": None}
    assert _start(tc, member_id, key=key).status_code == 410


async def test_forgetting_a_key_is_the_members_own(client, member, db_session):
    tc, _ = client
    other = User(email=f"{uuid4()}@example.test", name="Other", is_active=True)
    db_session.add(other)
    await db_session.commit()
    key = uuid4().hex
    run_id = _start(tc, member.id, key=key).json()["search"]["id"]
    run = await db_session.get(WebSearchRun, UUID(run_id))
    run.status = "complete"
    await db_session.commit()
    # Another member's forget of the same key reaches only their own.
    assert _forget(tc, other.id, key).json() == {"status": "forgotten"}
    assert tc.get(f"{ROOT}/users/{member.id}/runs/{run_id}").status_code == 200
    assert _forget(tc, member.id, "short").status_code == 422
    assert _forget(tc, "not-a-uuid", key).status_code == 400


async def test_a_closing_or_gone_account_can_still_be_forgotten(
    client, member, db_session
):
    """Account deletion forgets its chats' searches too: a closing account
    (inactive) still has its run deleted, and an account already gone has
    nothing left, which is done, not a 404."""
    tc, _ = client
    key = uuid4().hex
    run_id = _start(tc, member.id, key=key).json()["search"]["id"]
    run = await db_session.get(WebSearchRun, UUID(run_id))
    run.status = "error"
    member.is_active = False
    await db_session.commit()
    assert _forget(tc, member.id, key).json() == {"status": "forgotten"}
    db_session.expire_all()
    assert await db_session.get(WebSearchRun, UUID(run_id)) is None
    gone = _forget(tc, uuid4(), uuid4().hex)
    assert gone.status_code == 200 and gone.json() == {"status": "forgotten"}


async def test_every_start_refuses_a_forgotten_key_where_runs_are_made(
    client, member, db_session
):
    """The dashboard's own start (and the search link's) make runs through
    the same creator as the site: a forgotten key refuses there too, so no
    path recreates a run the site forgot."""
    tc, _ = client
    key = uuid4().hex
    assert _forget(tc, member.id, key).json() == {"status": "forgotten"}
    with pytest.raises(dashboard_controller.SearchKeyForgotten):
        await dashboard_controller.create_search(
            db_session, member.id, "latest Python release notes", key
        )
    assert (await db_session.execute(WebSearchRun.__table__.select())).all() == []
    dashboard_controller.dispatch_one.assert_not_awaited()
    # Another key still starts.
    run = await dashboard_controller.create_search(
        db_session, member.id, "latest Python release notes", uuid4().hex
    )
    assert run.status == "queued"


async def test_deleting_a_site_run_forgets_its_key(client, member, db_session):
    """A run the site deletes by id takes its turn's key with it: a start
    retried with that key (its reply lost) gets 410, not the run again.
    Only site keys (64 hex, one per turn); a dashboard key can recur."""
    tc, _ = client
    site_key = hashlib.sha256(uuid4().bytes).hexdigest()
    dashboard_key = uuid4().hex
    ids = {}
    for key in (site_key, dashboard_key):
        ids[key] = _start(tc, member.id, key=key).json()["search"]["id"]
    # Still running: refused, and nothing forgotten.
    assert (
        tc.delete(f"{ROOT}/users/{member.id}/runs/{ids[site_key]}").status_code == 409
    )
    assert _start(tc, member.id, key=site_key).json()["idempotent"] is True
    for run_id in ids.values():
        run = await db_session.get(WebSearchRun, UUID(run_id))
        run.status = "complete"
    await db_session.commit()
    for run_id in ids.values():
        assert tc.delete(f"{ROOT}/users/{member.id}/runs/{run_id}").json() == {
            "status": "deleted"
        }
    assert _start(tc, member.id, key=site_key).status_code == 410
    assert _start(tc, member.id, key=dashboard_key).status_code == 201


# ── Your data: count and delete all ─────────────────────────────────


async def _run(db_session, user, status="complete", changed_ago=None):
    run = WebSearchRun(
        owner_user_id=user.id,
        submission_key=uuid4().hex,
        request="latest Python release notes",
        status=status,
        queries=[],
        results=[],
        ranked=status == "complete",
        usage={},
        version=1,
        attempt_count=1,
    )
    db_session.add(run)
    await db_session.flush()
    if changed_ago is not None:
        run.updated_at = datetime.now(UTC) - changed_ago
    db_session.add(
        WorkDispatch(
            job_type="web_search.run",
            aggregate_id=run.id,
            payload={"search_id": str(run.id)},
            status="complete",
        )
    )
    await db_session.commit()
    return run.id


async def _other_member(db_session):
    other = User(email=f"{uuid4()}@example.test", name="Other", is_active=True)
    db_session.add(other)
    await db_session.commit()
    return other


def _delete_all(tc, user_id, keep=None):
    return tc.post(
        f"{ROOT}/users/{user_id}/runs/delete-all",
        json={} if keep is None else {"keep": [str(rid) for rid in keep]},
    )


async def test_count_is_every_search_the_member_owns(client, member, db_session):
    tc, _ = client
    other = await _other_member(db_session)
    path = f"{ROOT}/users/{member.id}/runs/count"
    zero = tc.get(path)
    assert zero.status_code == 200
    assert zero.headers["Cache-Control"] == "private, no-store"
    assert zero.json() == {"searches": 0}
    await _run(db_session, member)
    await _run(db_session, member, status="error")
    await _run(db_session, member, status="searching")
    await _run(db_session, other)
    assert tc.get(path).json() == {"searches": 3}
    assert tc.get(f"{ROOT}/users/{other.id}/runs/count").json() == {"searches": 1}


async def test_count_and_delete_all_need_an_active_member(client, member, db_session):
    tc, _ = client
    await _run(db_session, member)
    member.is_active = False
    await db_session.commit()
    for response in (
        tc.get(f"{ROOT}/users/{member.id}/runs/count"),
        _delete_all(tc, member.id),
    ):
        assert response.status_code == 404
        assert response.json() == {"detail": "Account not found"}
        assert response.headers["Cache-Control"] == "private, no-store"
    assert await db_session.scalar(select(func.count(WebSearchRun.id))) == 1


async def test_delete_all_keeps_running_searches_and_the_ones_named(
    client, member, db_session
):
    tc, _ = client
    other = await _other_member(db_session)
    member_id, other_id = member.id, other.id
    finished = await _run(db_session, member)
    failed = await _run(db_session, member, status="error")
    stalled = await _run(
        db_session,
        member,
        status="searching",
        changed_ago=STALLED_AFTER + timedelta(minutes=1),
    )
    running = await _run(db_session, member, status="searching")
    cited = await _run(db_session, member)
    foreign = await _run(db_session, other)

    response = _delete_all(tc, member.id, keep=[cited, foreign])

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"
    body = response.json()
    assert (body["deleted"], body["running"]) == (3, 2)
    assert sorted(body["kept"]) == sorted([str(running), str(cited)])
    db_session.expire_all()
    left = set(await db_session.scalars(select(WebSearchRun.id)))
    assert left == {running, cited, foreign}
    dispatched = set(await db_session.scalars(select(WorkDispatch.aggregate_id)))
    assert dispatched == {running, cited, foreign}
    assert not {finished, failed, stalled} & dispatched
    again = _delete_all(tc, member_id, keep=[cited]).json()
    assert (again["deleted"], again["running"]) == (0, 2)
    assert tc.get(f"{ROOT}/users/{member_id}/runs/count").json() == {"searches": 2}
    assert tc.get(f"{ROOT}/users/{other_id}/runs/count").json() == {"searches": 1}


async def test_delete_all_without_keep_is_the_account_pages_rule(
    client, member, db_session
):
    tc, _ = client
    await _run(db_session, member)
    running = await _run(db_session, member, status="answering")
    assert _delete_all(tc, member.id).json() == {
        "deleted": 1,
        "running": 1,
        "kept": [str(running)],
    }
    assert _delete_all(tc, member.id).json() == {
        "deleted": 0,
        "running": 1,
        "kept": [str(running)],
    }


async def test_a_lost_start_deleted_by_delete_all_is_never_made_again(
    client, member, db_session
):
    """#326's P1 probe: a start whose reply never reached the site finishes,
    the site's Delete all (which can't name it: no id saved, and its reply no
    longer writing) deletes it, and the start retried with the same key gets
    410, not a new run."""
    tc, _ = client
    member_id = member.id
    key = hashlib.sha256(b"a reply whose start answer was lost").hexdigest()
    run_id = _start(tc, member_id, key=key).json()["search"]["id"]
    run = await db_session.get(WebSearchRun, UUID(run_id))
    run.status = "complete"
    await db_session.commit()

    deleted = tc.post(f"{ROOT}/users/{member_id}/runs/delete-all", json={"keep": []})

    assert deleted.json() == {"deleted": 1, "running": 0, "kept": []}
    retried = _start(tc, member_id, key=key)
    assert retried.status_code == 410
    assert retried.json()["detail"] == "This search was deleted."
    db_session.expire_all()
    assert (await db_session.execute(WebSearchRun.__table__.select())).all() == []


async def test_a_run_kept_by_its_key_is_not_marked_and_can_be_rejoined(
    client, member, db_session
):
    """The reply is still being written: its run is kept by key, its key is
    not marked, and the reply's retried start rejoins the same run."""
    tc, _ = client
    member_id = member.id
    key = hashlib.sha256(b"a reply still being written").hexdigest()
    run_id = _start(tc, member_id, key=key).json()["search"]["id"]
    run = await db_session.get(WebSearchRun, UUID(run_id))
    run.status = "complete"
    await db_session.commit()

    kept = tc.post(
        f"{ROOT}/users/{member_id}/runs/delete-all", json={"keep_keys": [key]}
    )

    assert kept.json() == {"deleted": 0, "running": 1, "kept": [run_id]}
    assert await db_session.get(WebSearchForgottenKey, (member_id, key)) is None
    rejoined = _start(tc, member_id, key=key)
    assert rejoined.status_code == 201
    assert rejoined.json()["search"]["id"] == run_id


async def test_delete_all_keeps_the_runs_of_replies_by_their_search_keys(
    client, member, db_session
):
    """A reply whose start's answer was lost has no run id on the site yet:
    it names the run by its search key, and the run is kept, finished or not."""
    tc, _ = client
    other = await _other_member(db_session)
    member_id = member.id
    rejoined, gone, foreign = [
        await db_session.get(WebSearchRun, await _run(db_session, owner))
        for owner in (member, member, other)
    ]
    key = hashlib.sha256(b"a reply still being written").hexdigest()
    rejoined.submission_key = key
    gone.submission_key = hashlib.sha256(b"a finished reply").hexdigest()
    foreign.submission_key = hashlib.sha256(b"another member's reply").hexdigest()
    ids = rejoined.id, foreign.id
    foreign_key = foreign.submission_key
    await db_session.flush()

    response = tc.post(
        f"{ROOT}/users/{member_id}/runs/delete-all",
        json={"keep_keys": [key, key, foreign_key]},
    )

    assert response.status_code == 200
    assert response.json() == {"deleted": 1, "running": 1, "kept": [str(ids[0])]}
    db_session.expire_all()
    left = set(await db_session.scalars(select(WebSearchRun.id)))
    assert left == set(ids)


@pytest.mark.parametrize(
    "body",
    [
        {"keep_keys": ["a" * 63]},
        {"keep_keys": ["A" * 64]},
        {"keep_keys": ["g" * 64]},
        {"keep_keys": [uuid4().hex]},
        {"keep_keys": ["a" * 64] * 201},
        {"keep_keys": "a" * 64},
        {"keep": ["not-a-run"]},
        {"keep": [str(uuid4()).upper().replace("-", "")]},
        {"keep": [str(uuid4()) for _ in range(201)]},
        {"keep": "all"},
        {"keep": [], "everything": True},
    ],
)
async def test_delete_all_refuses_a_bad_keep_list(client, member, db_session, body):
    tc, _ = client
    await _run(db_session, member)
    response = tc.post(f"{ROOT}/users/{member.id}/runs/delete-all", json=body)
    assert response.status_code == 422
    assert response.headers["Cache-Control"] == "private, no-store"
    assert await db_session.scalar(select(func.count(WebSearchRun.id))) == 1
