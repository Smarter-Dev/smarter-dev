"""Site API for the agent's web-search previews (``/ai/search/<token>``):
the page's own views by status, distinct expired and missing answers, the
token checked before any lookup and never logged, limits and bounds."""

import logging
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis
import pytest
from litestar.di import Provide
from litestar.plugins.pydantic import PydanticPlugin
from litestar.testing import create_test_client
from skrift.db.base import Base as SkriftBase
from sqlalchemy import update

from smarter_dev.shared.database import async_sessionmaker
from smarter_dev.web.api_native import site_guards
from smarter_dev.web.api_native import site_search
from smarter_dev.web.api_native.site_search import SiteSearchController
from smarter_dev.web.models import SearchResultPreview
from smarter_dev.web.search_preview_controller import result_view
from smarter_dev.web.search_previews import complete_search_preview
from smarter_dev.web.search_previews import create_search_preview
from smarter_dev.web.search_previews import delete_expired_search_previews
from smarter_dev.web.search_previews import fail_search_preview

PATH = "/api/site/search/previews/resolve"
TOKEN = "Ab3_-" + "x" * 38
VISITOR = "visitor-key-0123456789"
RESULTS = [
    {
        "title": "<b>Task</b> Groups &amp; friends",
        "url": "https://www.docs.python.org/3/library/asyncio-task.html?x=1",
        "description": "Use <strong>TaskGroup</strong><script>x()</script>",
    },
    {"title": "", "url": "javascript:alert(1)", "description": ""},
    {
        "title": "Ünïcödé — 日本語",
        "url": "https://例え.jp/パス",
        "description": "<em>é</em>",
    },
]


@pytest.fixture
async def api(test_engine, db_session, monkeypatch):
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
    with create_test_client(
        route_handlers=[SiteSearchController],
        plugins=[PydanticPlugin()],
        dependencies={"db_session": Provide(session)},
    ) as tc:
        yield SimpleNamespace(tc=tc, key=key, redis=redis, session=db_session)


async def _preview(session, *, token=TOKEN, now=None, results=None, failed=False):
    reservation = await create_search_preview(
        session,
        "python async task groups",
        now=now,
        token=token,
        site_base_url="https://smarter.dev",
    )
    if results is not None:
        await complete_search_preview(session, reservation.id, results)
    if failed:
        await fail_search_preview(session, reservation.id)
    await session.commit()
    return reservation


def _resolve(tc, token=TOKEN, visitor=VISITOR):
    return tc.post(PATH, json={"token": token, "visitor": visitor})


@pytest.mark.parametrize(
    "principal,service,scopes",
    [
        ("user", None, ["site-search"]),
        ("service", "bot", ["site-search"]),
        ("service", "site", ["site-memory", "site-privacy", "site-billing"]),
        ("service", "site", []),
    ],
)
async def test_resolve_requires_the_site_search_scope(api, principal, service, scopes):
    await _preview(api.session, results=RESULTS)
    api.key.principal_type, api.key.service_name, api.key.scoped_permission_list = (
        principal,
        service,
        scopes,
    )
    response = _resolve(api.tc)
    assert response.status_code == 401
    assert "preview" not in response.text


async def test_pending_preview_answers_its_status_and_query(api):
    await _preview(api.session)
    response = _resolve(api.tc)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.json() == {
        "preview": {
            "status": "pending",
            "query": "python async task groups",
            "results": [],
        }
    }


async def test_ready_preview_carries_the_pages_own_result_views(api):
    await _preview(api.session, results=RESULTS)
    preview = _resolve(api.tc).json()["preview"]
    assert preview["status"] == "ready"
    # Exactly what search_preview_view hands its template.
    assert preview["results"] == [result_view(r) for r in RESULTS]
    first, unsafe, _ = preview["results"]
    assert first["title"] == "Task Groups & friends"
    assert first["domain"] == "docs.python.org"
    assert first["display_url"] == "/3/library/asyncio-task.html?x=1"
    assert first["description_html"] == "Use <strong>TaskGroup</strong>"
    assert unsafe["safe_url"] is None and unsafe["url"] == "javascript:alert(1)"


async def test_failed_preview_answers_failed_without_results(api):
    await _preview(api.session, failed=True)
    assert _resolve(api.tc).json()["preview"] == {
        "status": "failed",
        "query": "python async task groups",
        "results": [],
    }


async def test_expired_is_distinct_from_missing_until_swept(api):
    long_ago = datetime.now(UTC) - timedelta(days=1)
    await _preview(api.session, now=long_ago, results=RESULTS)
    expired = _resolve(api.tc)
    assert expired.status_code == 410
    assert expired.json() == {"detail": "This search preview has expired."}
    assert expired.headers["Cache-Control"] == "private, no-store"
    await delete_expired_search_previews(api.session)
    await api.session.commit()
    missing = _resolve(api.tc)
    assert missing.status_code == 404
    assert missing.json() == {"detail": "This search preview doesn't exist."}


async def test_preview_expires_on_its_clock(api):
    reservation = await _preview(api.session, results=RESULTS)
    assert _resolve(api.tc).status_code == 200
    await api.session.execute(
        update(SearchResultPreview)
        .where(SearchResultPreview.id == reservation.id)
        .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await api.session.commit()
    assert _resolve(api.tc).status_code == 410


async def test_unknown_token_is_missing(api):
    await _preview(api.session, results=RESULTS)
    response = _resolve(api.tc, token="Z" * 43)
    assert response.status_code == 404
    assert response.json() == {"detail": "This search preview doesn't exist."}


@pytest.mark.parametrize(
    "token",
    ["", "short", "x" * 42, "x" * 44, "x" * 42 + "=", "x" * 42 + ".", "x" * 42 + "é"],
)
async def test_malformed_token_is_missing_without_a_lookup(api, monkeypatch, token):
    lookup = AsyncMock()
    monkeypatch.setattr(site_search, "find_search_preview", lookup)
    response = _resolve(api.tc, token=token)
    assert response.status_code == 404
    assert response.json() == {"detail": "This search preview doesn't exist."}
    lookup.assert_not_awaited()


async def test_oversized_token_and_bad_visitor_are_refused(api, monkeypatch):
    lookup = AsyncMock()
    monkeypatch.setattr(site_search, "find_search_preview", lookup)
    oversized = _resolve(api.tc, token="x" * 129)
    assert oversized.status_code == 422
    assert "x" * 129 not in oversized.text
    assert _resolve(api.tc, visitor="short").status_code == 422
    assert _resolve(api.tc, visitor="has.dot.0123456789").status_code == 422
    assert api.tc.post(PATH, json={"token": TOKEN}).status_code == 422
    lookup.assert_not_awaited()


async def test_reads_are_limited_per_visitor(api):
    await _preview(api.session, results=RESULTS)
    for _ in range(site_search.PREVIEW_READS_PER_MINUTE):
        assert _resolve(api.tc).status_code == 200
    limited = _resolve(api.tc)
    assert limited.status_code == 429
    assert limited.json()["detail"] == site_search.PREVIEW_LIMITED
    assert 1 <= int(limited.headers["Retry-After"]) <= 60
    assert limited.headers["Cache-Control"] == "private, no-store"
    # Malformed tokens count too: scanning costs the same minute.
    assert _resolve(api.tc, token="nope").status_code == 429
    # Another visitor has its own minute.
    assert _resolve(api.tc, visitor="another-visitor-0123").status_code == 200


async def test_limiter_outage_admits_the_read(api, monkeypatch):
    await _preview(api.session, results=RESULTS)

    def down():
        raise ConnectionError("redis down")

    monkeypatch.setattr(site_search, "get_redis_client", down)
    assert _resolve(api.tc).status_code == 200


async def test_answer_is_bounded(api):
    huge = "y" * 50_000
    results = [
        {"title": f"r{i}", "url": f"https://e.test/{i}", "description": ""}
        for i in range(30)
    ]
    results[0] = {
        "title": huge,
        "url": "https://e.test/" + huge,
        "description": "<em>" + huge + "</em>",
    }
    reservation = await _preview(api.session, results=results)
    await api.session.execute(
        update(SearchResultPreview)
        .where(SearchResultPreview.id == reservation.id)
        .values(query=huge)
    )
    await api.session.commit()
    response = _resolve(api.tc)
    preview = response.json()["preview"]
    assert len(preview["query"]) == site_search.MAX_PREVIEW_QUERY
    assert len(preview["results"]) == site_search.MAX_PREVIEW_RESULTS
    first = preview["results"][0]
    assert len(first["title"]) == site_search.MAX_PREVIEW_TITLE
    assert len(first["url"]) == site_search.MAX_PREVIEW_URL
    # A URL too long to carry whole is shown, never linked, as the page
    # shows a URL it can't link.
    assert first["safe_url"] is None and first["domain"] == ""
    # Cut before sanitizing; escaping grows a character at most to "&quot;".
    assert first["description_html"].startswith("<em>y")
    assert len(first["description_html"]) <= 6 * site_search.MAX_PREVIEW_DESCRIPTION
    assert len(response.content) < 100_000


async def test_no_log_line_holds_the_token(api, caplog, monkeypatch):
    await _preview(api.session, results=RESULTS)
    caplog.set_level(logging.DEBUG)
    _resolve(api.tc)
    _resolve(api.tc, token="Q" * 43)
    _resolve(api.tc, token="x" * 129)

    def down():
        raise ConnectionError(f"redis down {TOKEN}")

    monkeypatch.setattr(site_search, "get_redis_client", down)
    _resolve(api.tc)
    logged = "\n".join(f"{r.getMessage()} {r.exc_text or ''}" for r in caplog.records)
    assert TOKEN not in logged and "Q" * 43 not in logged
    assert "x" * 129 not in logged
