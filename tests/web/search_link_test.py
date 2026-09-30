"""Search links: the /s/<token> handler, anonymous searches that are never
saved, the redirect to web addresses, and the address parser.

The handlers run directly against the sqlite test session and fakeredis;
Luna, Brave, Jev and the worker queue are replaced."""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import fakeredis
import pytest
from litestar.exceptions import HTTPException
from litestar.response import Redirect
from litestar.response import Template
from skrift.auth.session_keys import SESSION_USER_ID
from skrift.db.models.user import User
from sqlalchemy import func
from sqlalchemy import select

from smarter_dev.web import dashboard_controller
from smarter_dev.web import search_link_controller as links
from smarter_dev.web.models import UsageCostRow
from smarter_dev.web.models import WebSearchLink
from smarter_dev.web.models import WebSearchRun
from smarter_dev.web.web_search import address
from smarter_dev.web.web_search import anonymous
from smarter_dev.web.web_search import pipeline
from tests.web.web_search_test import search_env  # noqa: F401 - fixture

run_link = links.SearchLinkController.run.fn


@pytest.fixture
def link_env(db_session, monkeypatch):
    redis = fakeredis.FakeAsyncRedis()
    monkeypatch.setattr(links, "get_redis_client", lambda: redis)
    submitted: list[dict] = []

    async def submit(job_type, payload, **options):
        submitted.append({"job_type": job_type, **payload})

    import skrift.workers

    monkeypatch.setattr(skrift.workers, "submit", submit)
    monkeypatch.setattr(
        "smarter_dev.web.chat.dispatch.ensure_submission_handler", lambda job_type: None
    )

    async def no_dispatch(dispatch_id):
        return True

    monkeypatch.setattr(dashboard_controller, "dispatch_one", no_dispatch)
    return {"session": db_session, "redis": redis, "submitted": submitted}


async def _user(session) -> User:
    # Skrift's User table is not in this project's metadata; create it here.
    connection = await session.connection()
    await connection.run_sync(lambda sync: User.metadata.create_all(sync))
    user = User(email=f"{uuid4().hex}@example.test", name="Link Owner", is_active=True)
    session.add(user)
    await session.commit()
    return user


async def _link(session, owner: User, *, open_addresses=False) -> WebSearchLink:
    link = WebSearchLink(
        owner_user_id=owner.id, token=uuid4().hex[:24], open_addresses=open_addresses
    )
    session.add(link)
    await session.commit()
    return link


def _request(*, user=None, ip="203.0.113.7", query="q=hello", headers=None, session=None):
    session = {} if session is None else session
    if user is not None:
        session[SESSION_USER_ID] = str(user.id)
    return SimpleNamespace(
        session=session,
        headers=headers or {},
        url=SimpleNamespace(path="/s/token", query=query),
        scope={"client": (ip, 5000)},
    )


async def _runs(session) -> int:
    return await session.scalar(select(func.count()).select_from(WebSearchRun))


async def test_prefetches_start_nothing(link_env):
    link = await _link(link_env["session"], await _user(link_env["session"]))
    request = _request(headers={"sec-purpose": "prefetch;prerender"})

    response = await run_link(None, request, link_env["session"], link.token, "hello")

    assert response.status_code == 204
    assert link_env["submitted"] == []
    assert await link_env["redis"].keys("*") == []


async def test_an_unknown_link_is_not_found(link_env):
    with pytest.raises(HTTPException) as error:
        await run_link(None, _request(), link_env["session"], "nope", "hello")
    assert error.value.status_code == 404


async def test_an_anonymous_search_is_never_saved(link_env):
    owner = await _user(link_env["session"])
    link = await _link(link_env["session"], owner)
    request = _request()

    response = await run_link(None, request, link_env["session"], link.token, "  why   is it slow ")

    assert isinstance(response, Redirect)
    search_id = response.url.rsplit("/", 1)[1]
    assert response.url == f"/s/r/{search_id}"
    assert link_env["submitted"] == [{"job_type": anonymous.JOB_TYPE, "search_id": search_id}]
    entry = await anonymous.load(link_env["redis"], search_id)
    assert entry["search"]["request"] == "why is it slow"
    assert entry["owner_user_id"] == str(owner.id)
    # Only the browser session that ran it can see it.
    assert entry["nid"] == request.session["_nid"]
    assert 0 < await link_env["redis"].ttl(f"web_search:anonymous:{search_id}") <= anonymous.TTL_SECONDS
    assert await links._visible(_request(session=dict(request.session)), search_id) is not None
    assert await links._visible(_request(), search_id) is None
    assert await _runs(link_env["session"]) == 0


async def test_a_third_anonymous_search_in_a_minute_asks_to_wait_or_log_in(link_env):
    link = await _link(link_env["session"], await _user(link_env["session"]))
    for _ in range(anonymous.PER_MINUTE_LIMIT):
        await run_link(None, _request(), link_env["session"], link.token, "hello")

    response = await run_link(None, _request(), link_env["session"], link.token, "hello")

    assert isinstance(response, Template)
    assert response.status_code == 429
    assert 0 < response.context["wait"] <= 60
    assert response.context["login_url"] == "/auth/login?next=%2Fs%2Ftoken%3Fq%3Dhello"
    assert len(link_env["submitted"]) == anonymous.PER_MINUTE_LIMIT
    # Another visitor has their own allowance.
    other = await run_link(None, _request(ip="198.51.100.9"), link_env["session"], link.token, "hello")
    assert isinstance(other, Redirect)


async def test_a_logged_in_search_is_saved_to_the_account(link_env):
    owner = await _user(link_env["session"])
    link = await _link(link_env["session"], owner)

    response = await run_link(None, _request(user=owner), link_env["session"], link.token, "hello")

    run = await link_env["session"].scalar(select(WebSearchRun))
    assert response.url == f"/dashboard/search/{run.id}"
    assert run.owner_user_id == owner.id and run.request == "hello"
    assert link_env["submitted"] == []


async def test_the_owner_goes_straight_to_a_web_address(link_env, search_env, monkeypatch):
    owner = await _user(link_env["session"])
    link = await _link(link_env["session"], owner, open_addresses=True)

    async def to_open(text):
        return "https://getbuild.ing", {"model": "jev-1.13.0", "input_tokens": 50, "cost_usd": 0.0000021}

    monkeypatch.setattr(address, "address_to_open", to_open)

    response = await run_link(None, _request(user=owner), link_env["session"], link.token, "getbuild.ing")

    assert response.status_code == 302 and response.url == "https://getbuild.ing"
    assert await _runs(link_env["session"]) == 0
    row = await link_env["session"].scalar(select(UsageCostRow))
    assert (row.operation_type, row.user_id, row.input_tokens) == ("web_search_address", owner.id, 50)


async def test_nobody_else_is_redirected_by_a_link(link_env, monkeypatch):
    owner = await _user(link_env["session"])
    link = await _link(link_env["session"], owner, open_addresses=True)
    stranger = await _user(link_env["session"])

    checked: list[str] = []

    async def to_open(text):
        # Recorded, not raised: the handler searches when the check fails.
        checked.append(text)
        return text, {}

    monkeypatch.setattr(address, "address_to_open", to_open)

    anonymous_response = await run_link(None, _request(), link_env["session"], link.token, "https://evil.example")
    stranger_response = await run_link(
        None, _request(user=stranger), link_env["session"], link.token, "https://evil.example"
    )

    assert anonymous_response.url.startswith("/s/r/")
    assert stranger_response.url.startswith("/dashboard/search/")
    assert checked == []


async def test_an_anonymous_search_runs_without_a_row(search_env, monkeypatch):
    redis = fakeredis.FakeAsyncRedis()
    owner_id = uuid4()
    state = await anonymous.start(redis, owner_user_id=owner_id, nid="nid-1", request="why is it slow")
    sent: list[tuple[str, dict]] = []

    async def notify_session(nid, event_type, **payload):
        sent.append((nid, payload["search"]))

    import skrift.notifications

    monkeypatch.setattr(skrift.notifications, "notify_session", notify_session)
    entry = await anonymous.load(redis, state["id"])

    assert await pipeline.search(anonymous.UnsavedRun(redis, entry)) == "complete"

    saved = json.loads(await redis.get(f"web_search:anonymous:{state['id']}"))["search"]
    assert saved["status"] == "complete" and saved["ranked"] is True
    assert [nid for nid, _ in sent] == ["nid-1"] * len(sent)
    assert sent[-1][1] == saved
    session = search_env["session"]
    assert await _runs(session) == 0
    rows = list(await session.scalars(select(UsageCostRow)))
    assert {row.operation_type for row in rows} == {
        "web_search_queries",
        "web_search_brave",
        "web_search_ranking",
    }
    assert all(row.user_id == owner_id and row.details["anonymous"] for row in rows)


async def test_a_refused_search_is_not_counted():
    redis = fakeredis.FakeAsyncRedis()
    assert await anonymous.wait_seconds(redis, "k", 1, 60) == 0
    assert await anonymous.wait_seconds(redis, "k", 1, 60) > 0
    assert int(await redis.get("k")) == 1


async def test_a_new_link_replaces_the_old_one(link_env, monkeypatch):
    monkeypatch.setattr(dashboard_controller, "require_api_csrf", lambda request: None)
    owner = await _user(link_env["session"])
    save = dashboard_controller.DashboardController.save_link.fn
    request = _request(user=owner)

    first = (await save(None, dashboard_controller.LinkBody(), request, link_env["session"]))["link"]
    toggled = (
        await save(None, dashboard_controller.LinkBody(open_addresses=True), request, link_env["session"])
    )["link"]
    rotated = (
        await save(None, dashboard_controller.LinkBody(rotate=True), request, link_env["session"])
    )["link"]

    assert first["open_addresses"] is False and toggled["open_addresses"] is True
    assert toggled["url"] == first["url"] and rotated["url"] != first["url"]
    assert rotated["url"].endswith("?q=%s")
    old_token = first["url"].split("/s/")[1].split("?")[0]
    with pytest.raises(HTTPException):
        await run_link(None, _request(), link_env["session"], old_token, "hello")


@pytest.mark.parametrize(
    ("typed", "url", "explicit"),
    [
        ("smarter.dev", "https://smarter.dev", False),
        ("getbuild.ing", "https://getbuild.ing", False),
        ("GetBuild.ING/pricing", "https://getbuild.ing/pricing", False),
        ("astro.build", "https://astro.build", False),
        ("münchen.de", "https://xn--mnchen-3ya.de", False),
        ("setup.py", "https://setup.py", False),
        ("https://example.com/a?b=1", "https://example.com/a?b=1", True),
        ("localhost:3000", "http://localhost:3000", True),
        ("192.168.1.1/admin", "http://192.168.1.1/admin", True),
    ],
)
def test_addresses_on_any_registered_top_level_domain_parse(typed, url, explicit):
    found = address.parse(typed)
    assert (found.url, found.explicit) == (url, explicit)


@pytest.mark.parametrize(
    "typed",
    ["node.js", "next.js", "3.14", "python3.12", "a.b.c", "undo a pushed commit", "user@example.com", "ftp://x.com", ""],
)
def test_other_text_is_not_an_address(typed):
    assert address.parse(typed) is None


def test_file_names_and_code_veto_an_open():
    assert address.decide({"open": 0.8, "file": 0.1, "code": 0.1})
    assert not address.decide({"open": 0.4, "file": 0.1, "code": 0.1})
    assert not address.decide({"open": 0.8, "file": 0.7, "code": 0.1})
    assert not address.decide({"open": 0.8, "file": 0.1, "code": 0.7})


async def test_an_explicit_address_opens_without_asking_jev(monkeypatch):
    async def ask(found, client=None):
        raise AssertionError("no Jev call for a scheme")

    monkeypatch.setattr(address, "ask_jev", ask)
    assert await address.address_to_open("https://example.com") == ("https://example.com", {})
    assert await address.address_to_open("why is my build slow") == (None, {})
