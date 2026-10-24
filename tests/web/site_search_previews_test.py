"""Site API for the agent's web-search previews (``/ai/search/<token>``):
the page's own views by status, distinct expired and missing answers, the
token checked before any lookup and never logged or echoed, the visitor
never raw in a Redis key, limits (its own and Skrift's app.yaml rule) and
bounds."""

import json
import logging
import traceback
from contextlib import asynccontextmanager
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import fakeredis
import pytest
import yaml
from litestar import Litestar
from litestar.datastructures import State
from litestar.di import Provide
from litestar.logging import LoggingConfig
from litestar.middleware import DefineMiddleware
from litestar.plugins.pydantic import PydanticPlugin
from litestar.testing import AsyncTestClient
from litestar.testing import create_test_client
from skrift import config as skrift_config
from skrift.auth.services import UserPermissions
from skrift.config import interpolate_env_vars
from skrift.db.base import Base as SkriftBase
from skrift.db.services import api_key_service
from skrift.middleware.rate_limit import RateLimitMiddleware
from skrift.ratelimit import RateLimiter
from sqlalchemy import update

from smarter_dev.shared.database import async_sessionmaker
from smarter_dev.web.api_native import auth
from smarter_dev.web.api_native import rate_limiting
from smarter_dev.web.api_native import site_guards
from smarter_dev.web.api_native import site_search
from smarter_dev.web.api_native.site_memory import SiteMemoryController
from smarter_dev.web.api_native.site_search import SiteSearchController
from smarter_dev.web.models import SearchResultPreview
from smarter_dev.web.search_preview_controller import result_view
from smarter_dev.web.search_previews import complete_search_preview
from smarter_dev.web.search_previews import create_search_preview
from smarter_dev.web.search_previews import delete_expired_search_previews
from smarter_dev.web.search_previews import fail_search_preview
from smarter_dev.web.web_search import anonymous

PATH = "/api/site/search/previews/resolve"
TOKEN = "Ab3_-" + "x" * 38
# The Worker's visitor key: an HMAC-SHA-256, base64url, 43 characters.
VISITOR = "Vk9_-" + "v" * 38
OTHER_VISITOR = "Ot8-_" + "o" * 38
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


def _site_key(scopes=("site-search",)):
    return SimpleNamespace(
        principal_type="service",
        service_name="site",
        scoped_permission_list=list(scopes),
    )


@pytest.fixture
async def api(test_engine, db_session, monkeypatch):
    async with test_engine.begin() as conn:
        await conn.run_sync(SkriftBase.metadata.create_all)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)

    async def session():
        async with maker() as db:
            yield db

    monkeypatch.setattr(site_guards, "bot_api_auth_guard", AsyncMock())
    key = _site_key()
    monkeypatch.setattr(
        site_guards, "resolve_request_api_key", AsyncMock(return_value=key)
    )
    redis = fakeredis.FakeAsyncRedis()
    monkeypatch.setattr(site_search, "get_redis_client", lambda: redis)

    def client(**options):
        return create_test_client(
            route_handlers=[SiteSearchController],
            plugins=[PydanticPlugin()],
            dependencies={"db_session": Provide(session)},
            **options,
        )

    with client() as tc:
        yield SimpleNamespace(
            tc=tc, key=key, redis=redis, session=db_session, client=client
        )


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
            "omitted": 0,
        }
    }


async def test_ready_preview_carries_the_pages_own_result_views(api):
    await _preview(api.session, results=RESULTS)
    preview = _resolve(api.tc).json()["preview"]
    assert preview["status"] == "ready" and preview["omitted"] == 0
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
        "omitted": 0,
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
    [
        "",
        "short",
        "x" * 42,
        "x" * 44,
        "x" * 129,
        "x" * 42 + "=",
        "x" * 42 + ".",
        "x" * 42 + "é",
    ],
)
async def test_malformed_token_is_missing_without_a_lookup(api, monkeypatch, token):
    lookup = AsyncMock()
    monkeypatch.setattr(site_search, "find_search_preview", lookup)
    response = _resolve(api.tc, token=token)
    assert response.status_code == 404
    assert response.json() == {"detail": "This search preview doesn't exist."}
    lookup.assert_not_awaited()


INVALID_BODIES = [
    {"token": TOKEN},
    {"token": TOKEN, "visitor": VISITOR, "extra": TOKEN},
    {"token": 12345, "visitor": VISITOR},
    {"token": [TOKEN], "visitor": VISITOR},
    {"token": TOKEN, "visitor": "short"},
    {"token": TOKEN, "visitor": "has.dot." + "a" * 35},
    {"token": TOKEN, "visitor": VISITOR + "a"},
    {"token": TOKEN * 20, "visitor": VISITOR},
    [TOKEN, VISITOR],
    f'{{"token": "{TOKEN}", "visitor": ',
    f"not json {TOKEN}",
]


@pytest.mark.parametrize("body", INVALID_BODIES)
async def test_an_invalid_request_is_refused_quoting_nothing(api, monkeypatch, body):
    lookup = AsyncMock()
    monkeypatch.setattr(site_search, "find_search_preview", lookup)
    raw = body if isinstance(body, str) else json.dumps(body)
    response = api.tc.post(
        PATH, content=raw, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid search preview request."}
    assert response.headers["Cache-Control"] == "private, no-store"
    assert TOKEN not in response.text and VISITOR not in response.text
    lookup.assert_not_awaited()


def _everything_logged(records) -> str:
    """Each record's message, its formatted exception and traceback."""
    parts = []
    for record in records:
        parts.append(record.getMessage())
        parts.append(record.exc_text or "")
        if record.exc_info:
            parts.append("".join(traceback.format_exception(*record.exc_info)))
            exc = record.exc_info[1]
            while exc is not None:
                parts.append(repr(exc))
                parts.append(repr(getattr(exc, "args", "")))
                exc = exc.__cause__ or exc.__context__
    return "\n".join(parts)


async def test_no_log_record_or_traceback_holds_the_token(api, caplog, monkeypatch):
    """With Litestar logging every exception, as a misconfigured or debug
    deployment would: no record, traceback or chained exception quotes the
    token or the visitor key, whatever the caller sent."""
    await _preview(api.session, results=RESULTS)
    caplog.set_level(logging.DEBUG)
    bodies = [
        *(b if isinstance(b, str) else json.dumps(b) for b in INVALID_BODIES),
        json.dumps({"token": TOKEN, "visitor": VISITOR}),
        json.dumps({"token": "Q" * 43, "visitor": VISITOR}),
        json.dumps({"token": TOKEN + "!", "visitor": VISITOR}),
    ]
    with api.client(
        logging_config=LoggingConfig(log_exceptions="always"), debug=True
    ) as tc:
        # Litestar's own logger may not propagate to the root caplog reads.
        for name in ("litestar", ""):
            logging.getLogger(name).addHandler(caplog.handler)
        for raw in bodies:
            tc.post(PATH, content=raw, headers={"Content-Type": "application/json"})

        def down():
            raise ConnectionError(f"redis down {TOKEN} {VISITOR}")

        monkeypatch.setattr(site_search, "get_redis_client", down)
        tc.post(PATH, json={"token": TOKEN, "visitor": VISITOR})
    for name in ("litestar", ""):
        logging.getLogger(name).removeHandler(caplog.handler)
    logged = _everything_logged(caplog.records)
    # The run did log exceptions, so the check saw that path.
    assert any(r.exc_info for r in caplog.records)
    for secret in (TOKEN, VISITOR, "Q" * 43):
        assert secret not in logged


async def test_no_redis_key_holds_the_visitor_key_or_token(api):
    """A visitor key spelled exactly like the preview's token (the same
    shape) still never reaches a key name: keys hold its HMAC."""
    await _preview(api.session, results=RESULTS)
    assert _resolve(api.tc, visitor=TOKEN).status_code == 200
    assert _resolve(api.tc).status_code == 200
    keys = [k.decode() for k in await api.redis.keys("*")]
    assert len(keys) == 2
    for key in keys:
        assert key.startswith("search_preview:visitor:")
        assert len(key.rsplit(":", 1)[1]) == 64
        assert TOKEN not in key and VISITOR not in key


async def test_the_anonymous_run_limiter_keys_by_hmac_too():
    redis = fakeredis.FakeAsyncRedis()
    for visitor in (TOKEN, "203.0.113.9"):
        assert (
            await anonymous.admit(redis, visitor=visitor, owner_user_id=uuid4()) is None
        )
    keys = [k.decode() for k in await redis.keys("web_search:anonymous:visitor:*")]
    assert len(keys) == 2
    for key in keys:
        assert TOKEN not in key and "203.0.113.9" not in key
        assert len(key.rsplit(":", 1)[1]) == 64
    # The same visitor keeps one minute: the HMAC is stable.
    key = anonymous.visitor_limit_key("web_search:anonymous:visitor", TOKEN)
    assert key == anonymous.visitor_limit_key("web_search:anonymous:visitor", TOKEN)
    assert key in keys


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
    assert _resolve(api.tc, visitor=OTHER_VISITOR).status_code == 200


async def test_this_limiters_outage_admits_the_read(api, monkeypatch):
    await _preview(api.session, results=RESULTS)

    def down():
        raise ConnectionError("redis down")

    monkeypatch.setattr(site_search, "get_redis_client", down)
    assert _resolve(api.tc).status_code == 200


async def test_the_page_sized_cuts(api):
    huge = "y" * 50_000
    results = [
        {"title": huge, "url": "https://e.test/" + huge, "description": huge},
        {"title": "ok", "url": "https://e.test/ok", "description": "<em>" + huge},
    ]
    reservation = await _preview(api.session, results=results)
    await api.session.execute(
        update(SearchResultPreview)
        .where(SearchResultPreview.id == reservation.id)
        .values(query=huge)
    )
    await api.session.commit()
    preview = _resolve(api.tc).json()["preview"]
    assert len(preview["query"]) == site_search.MAX_PREVIEW_QUERY
    first, second = preview["results"]
    assert len(first["title"]) == site_search.MAX_PREVIEW_TITLE
    assert len(first["url"]) == site_search.MAX_PREVIEW_URL
    # A URL too long to carry whole is shown, never linked, as the page
    # shows a URL it can't link.
    assert first["safe_url"] is None and first["domain"] == ""
    assert len(first["description_html"]) == site_search.MAX_PREVIEW_DESCRIPTION
    assert second["description_html"].startswith("<em>y")
    assert second["safe_url"] == "https://e.test/ok"


async def test_the_worst_case_answer_stays_under_its_cap(api):
    """Every field at its cut, of characters JSON escapes to six bytes and
    markup the sanitizer escapes: the answer stays under MAX_PREVIEW_BYTES,
    keeps at least one result, and counts the rest as omitted."""
    control = "\x01"
    worst = {
        "title": control * site_search.MAX_PREVIEW_TITLE,
        "url": "https://e.test/" + control * site_search.MAX_PREVIEW_URL,
        "description": "<" * site_search.MAX_PREVIEW_DESCRIPTION,
    }
    linked = {
        "title": control * site_search.MAX_PREVIEW_TITLE,
        "url": ("https://e.test/" + "\x7f" * 3_000)[: site_search.MAX_PREVIEW_URL],
        "description": ('"' + control) * (site_search.MAX_PREVIEW_DESCRIPTION // 2),
    }
    results = [worst, linked] * 15
    reservation = await _preview(api.session, results=results)
    await api.session.execute(
        update(SearchResultPreview)
        .where(SearchResultPreview.id == reservation.id)
        .values(query=control * 50_000)
    )
    await api.session.commit()
    response = _resolve(api.tc)
    assert response.status_code == 200
    assert len(response.content) <= site_search.MAX_PREVIEW_BYTES
    preview = response.json()["preview"]
    assert 1 <= len(preview["results"]) < site_search.MAX_PREVIEW_RESULTS
    assert preview["omitted"] == len(results) - len(preview["results"])


async def test_more_results_than_the_count_are_omitted(api):
    results = [
        {"title": f"r{i}", "url": f"https://e.test/{i}", "description": ""}
        for i in range(25)
    ]
    await _preview(api.session, results=results)
    preview = _resolve(api.tc).json()["preview"]
    assert [r["title"] for r in preview["results"]] == [f"r{i}" for i in range(20)]
    assert preview["omitted"] == 5


# Skrift's global rate limiter, configured from app.yaml as the app loads it
# (the pattern of tests/web/site_resources_api_test.py, PR 220).

REPO = Path(__file__).parents[2]
GUILD = "123456789012345678"
MEMORY_URL = f"/api/site/guilds/{GUILD}/memory/blocks"


def app_rate_limit_config():
    """``rate_limit`` from app.yaml, built the way ``skrift.config.get_settings``
    builds each section (env interpolation, then the registered model)."""
    raw = interpolate_env_vars(
        yaml.safe_load((REPO / "app.yaml").read_text()), strict=False
    )
    model = skrift_config._CONFIG_SECTIONS["rate_limit"]
    assert model is skrift_config.RateLimitConfig
    return model(**raw.get("rate_limit", {}))


class ClientIPStandIn:
    """Stands in for Skrift's ClientIPMiddleware, which runs before the rate
    limiter and records the resolved address in ``scope["state"]``."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            ip = dict(scope["headers"]).get(b"x-test-ip", b"203.0.113.1").decode()
            scope.setdefault("state", {})["client_ip"] = ip
        await self.app(scope, receive, send)


@pytest.fixture
async def limited_app(test_engine, db_session, monkeypatch):
    import skrift.auth.services

    async with test_engine.begin() as conn:
        await conn.run_sync(SkriftBase.metadata.create_all)

    @asynccontextmanager
    async def session_factory():
        yield db_session

    scopes = ["site-search", "site-memory"]
    key = SimpleNamespace(
        id=uuid4(),
        user_id=uuid4(),
        principal_type="service",
        service_name="site",
        scoped_permission_list=scopes,
        scoped_role_list=[],
        key_prefix="sk_test",
        display_name="site",
    )
    monkeypatch.setattr(api_key_service, "verify_api_key", AsyncMock(return_value=key))
    monkeypatch.setattr(
        skrift.auth.services,
        "get_user_permissions",
        AsyncMock(
            return_value=UserPermissions(
                user_id=str(key.user_id), roles=set(), permissions=set(scopes)
            )
        ),
    )
    monkeypatch.setattr(auth, "get_db_session_context", session_factory)
    # The site-memory routes' own per-key limiter is not under test here.
    monkeypatch.setattr(rate_limiting, "get_db_session_context", session_factory)
    monkeypatch.setattr(
        rate_limiting,
        "check_rate_limits",
        AsyncMock(
            return_value=rate_limiting.RateLimitDecision(allowed=True, headers={})
        ),
    )
    redis = fakeredis.FakeAsyncRedis()
    monkeypatch.setattr(site_search, "get_redis_client", lambda: redis)
    await _preview(db_session, results=RESULTS)

    @asynccontextmanager
    async def build(config):
        app = Litestar(
            route_handlers=[SiteSearchController, SiteMemoryController],
            plugins=[PydanticPlugin()],
            dependencies={
                "db_session": Provide(lambda: db_session, sync_to_thread=False)
            },
            state=State({"session_maker_class": session_factory}),
            # As skrift.asgi mounts them: client IP, then the global limiter.
            middleware=[
                ClientIPStandIn,
                DefineMiddleware(
                    RateLimitMiddleware,
                    config=config,
                    limiter=RateLimiter(redis_client=None),
                ),
            ],
        )
        async with AsyncTestClient(app=app) as client:
            yield client

    return build


def _limited_resolve(client, ip="203.0.113.1", visitor=VISITOR):
    return client.post(
        PATH,
        json={"token": TOKEN, "visitor": visitor},
        headers={"Authorization": "Bearer sk_site", "X-Test-IP": ip},
    )


def test_app_yaml_gives_the_resolve_its_own_ip_rule():
    config = app_rate_limit_config()
    assert config.enabled
    rule = config.resolve(PATH, "POST")
    assert (rule.name, rule.key, rule.limits) == (
        "site-search-previews",
        "ip",
        [(1200, 60.0)],
    )
    assert config.resolve(MEMORY_URL, "GET").name == "default"


async def test_resolves_never_spend_the_other_site_routes_budget(limited_app):
    """Past the default's 60 a minute from one address, every resolve is
    answered by the route (not Skrift's plain 429), and another site route
    from that address still answers 200."""
    config = app_rate_limit_config()
    default_limit = config.resolve(MEMORY_URL, "GET").limits[0][0]
    async with limited_app(config) as client:
        for n in range(default_limit + 5):
            # Visitors behind one Worker address, each well inside its minute.
            visitor = f"{n:04d}" + "w" * 39
            response = await _limited_resolve(client, visitor=visitor)
            assert response.status_code == 200, response.text
            assert response.headers["cache-control"] == "private, no-store"
        memory = await client.get(
            MEMORY_URL,
            headers={"Authorization": "Bearer sk_site", "X-Test-IP": "203.0.113.1"},
        )
        assert memory.status_code == 200, memory.text


async def test_the_resolve_rule_limits_per_ip(limited_app):
    config = app_rate_limit_config()
    (rule,) = [r for r in config.rules if r.name == "site-search-previews"]
    rule.limits[0].limit = 5  # the app.yaml rule, with a lower limit for speed
    async with limited_app(config) as client:
        for n in range(5):
            visitor = f"{n:04d}" + "w" * 39
            assert (await _limited_resolve(client, visitor=visitor)).status_code == 200
        limited = await _limited_resolve(client, visitor="9999" + "w" * 39)
        assert limited.status_code == 429
        assert int(limited.headers["retry-after"]) > 0
        # Another address has its own budget.
        assert (await _limited_resolve(client, ip="198.51.100.7")).status_code == 200
