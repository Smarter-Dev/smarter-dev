"""Site corpus routes (#315): the site's chat searches the catalog and reads
its sources through ``resources_corpus``, as the Resources researcher does.

The guard, bounds and rate-rule tests run anywhere. Everything that touches
the catalog needs PostgreSQL: see ``tests/web/corpus_fixture.py``. The old
code against the new is ``resources_tools_drift_test.py``.
"""

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import yaml
from litestar.plugins.pydantic import PydanticPlugin
from litestar.testing import create_test_client
from skrift import config as skrift_config
from skrift.config import interpolate_env_vars

from smarter_dev.web import resources_agent
from smarter_dev.web import resources_corpus
from smarter_dev.web.api_native import site_guards
from smarter_dev.web.api_native import site_resources
from smarter_dev.web.api_native.site_resources import SiteResourcesController
from tests.web.corpus_fixture import COLD
from tests.web.corpus_fixture import LONG
from tests.web.corpus_fixture import LONG_BODY
from tests.web.corpus_fixture import STALE
from tests.web.corpus_fixture import TOOL
from tests.web.corpus_fixture import WARM
from tests.web.corpus_fixture import corpus  # noqa: F401 - fixture
from tests.web.corpus_fixture import needs_postgres
from tests.web.corpus_fixture import stored

SEARCH = "/api/site/resources/search"
READ = "/api/site/resources/read"
REPO = Path(__file__).parents[2]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(site_guards, "bot_api_auth_guard", AsyncMock())
    key = SimpleNamespace(
        principal_type="service",
        service_name="site",
        scoped_permission_list=["site-resources"],
    )
    monkeypatch.setattr(
        site_guards, "resolve_request_api_key", AsyncMock(return_value=key)
    )
    resources_corpus._READ_CACHE.clear()
    with create_test_client(
        route_handlers=[SiteResourcesController], plugins=[PydanticPlugin()]
    ) as tc:
        yield tc, key


@pytest.mark.parametrize(
    "principal,service,scopes",
    [
        ("user", None, ["site-resources"]),
        ("service", "bot", ["site-resources"]),
        (
            "service",
            "site",
            ["site-memory", "site-privacy", "site-billing", "site-search"],
        ),
        ("service", "site", []),
    ],
)
def test_both_routes_require_the_site_resources_scope(
    client, monkeypatch, principal, service, scopes
):
    tc, key = client
    key.principal_type, key.service_name, key.scoped_permission_list = (
        principal,
        service,
        scopes,
    )
    search = AsyncMock()
    read = AsyncMock()
    monkeypatch.setattr(site_resources, "search_corpus", search)
    monkeypatch.setattr(site_resources, "read_corpus", read)
    assert tc.post(SEARCH, json={"query": "postgres"}).status_code == 401
    assert tc.post(READ, json={"url": WARM}).status_code == 401
    search.assert_not_awaited()
    read.assert_not_awaited()


def test_requests_are_bounded(client, monkeypatch):
    tc, _ = client
    monkeypatch.setattr(site_resources, "search_corpus", AsyncMock(return_value=[]))
    for body in (
        {"query": ""},
        {"query": "   "},
        {"query": "x" * 501},
        {"query": "postgres", "limit": 21},
        {"query": "postgres", "limit": 0},
        {"query": "postgres", "extra": 1},
    ):
        assert tc.post(SEARCH, json=body).status_code == 422, body
    for body in ({"url": ""}, {"url": "https://x.test/" + "a" * 2048}, {}):
        assert tc.post(READ, json=body).status_code == 422, body
    # The lookup's own limit is unchanged: reads have their own.
    assert site_resources.MAX_URL_CHARS == 500
    assert site_resources.MAX_READ_URL_CHARS == 2048


def _rate_limits():
    raw = interpolate_env_vars(
        yaml.safe_load((REPO / "app.yaml").read_text()), strict=False
    )
    return skrift_config.RateLimitConfig(**raw.get("rate_limit", {}))


def test_app_yaml_gives_search_and_read_their_own_ip_rules():
    config = _rate_limits()
    search = config.resolve(SEARCH, "POST")
    read = config.resolve(READ, "POST")
    assert (search.name, search.key, search.limits) == (
        "site-corpus-search",
        "ip",
        [(240, 60.0)],
    )
    assert (read.name, read.key, read.limits) == (
        "site-corpus-read",
        "ip",
        [(120, 60.0), (3000, 3600.0)],
    )
    # The lookup keeps its own rule.
    assert config.resolve("/api/site/resources/sources", "POST").name == (
        "site-resources"
    )


# ---------------------------------------------------------------------------
# Against PostgreSQL
# ---------------------------------------------------------------------------


def _jina(monkeypatch, result=None, *, delay=0.0):
    calls = []

    async def fake(client, url, *, log_host=True):
        calls.append(url)
        if delay:
            await asyncio.sleep(delay)
        return result or {"title": "Fetched", "content": f"Body of {url}"}

    monkeypatch.setattr(resources_corpus, "jina_read", fake)
    return calls


@needs_postgres
async def test_search_ranks_and_labels_like_the_researcher(client, corpus):  # noqa: F811
    tc, _ = client
    response = tc.post(SEARCH, json={"query": "postgres queue"})
    assert response.headers["Cache-Control"] == "private, no-store"
    hits = response.json()["hits"]
    assert hits[0]["url"] == WARM
    assert hits[0]["kind"] == "source"
    assert hits[0]["directory"] == "production-operations"
    assert hits[0]["category"] == "databases"
    tools = tc.post(SEARCH, json={"query": "pgbouncer"}).json()["hits"]
    assert {"kind": "tool", "url": TOOL, "byline": ""}.items() <= tools[-1].items()
    ctx = SimpleNamespace(deps=resources_agent.RunDeps("", ""))
    assert hits == await resources_agent.search_resources(ctx, "postgres queue")


@needs_postgres
async def test_a_cold_read_fetches_once_and_writes_back(client, corpus, monkeypatch):  # noqa: F811
    tc, _ = client
    calls = _jina(monkeypatch)
    first = tc.post(READ, json={"url": COLD})
    assert first.status_code == 200
    assert first.json() == {
        "url": COLD,
        "body": f"Fetched\n\nBody of {COLD}",
        "cached": False,
    }
    body, fetched = await stored(corpus, COLD)
    assert body == f"Fetched\n\nBody of {COLD}" and fetched is not None
    assert tc.post(READ, json={"url": COLD}).json()["cached"] is True
    assert calls == [COLD]


@needs_postgres
async def test_a_stale_body_is_refetched(client, corpus, monkeypatch):  # noqa: F811
    tc, _ = client
    calls = _jina(monkeypatch)
    assert tc.post(READ, json={"url": STALE}).json()["cached"] is False
    assert calls == [STALE]


@needs_postgres
async def test_only_curated_urls_are_read(client, corpus, monkeypatch):  # noqa: F811
    tc, _ = client
    calls = _jina(monkeypatch)
    for url in (
        "https://example.test/not-in-the-catalog",
        "http://169.254.169.254/latest/meta-data/",
        TOOL,  # a tool's home page is a hit, not a source with a body
    ):
        response = tc.post(READ, json={"url": url})
        assert response.status_code == 404
        assert response.json()["detail"] == "This isn't one of the curated sources."
    assert calls == []
    assert resources_corpus._READ_CACHE == {}


@needs_postgres
async def test_the_response_is_capped_and_the_stored_body_kept(
    client,
    corpus,  # noqa: F811
    monkeypatch,
):
    tc, _ = client
    _jina(monkeypatch, result={"title": "Huge", "content": "word " * 50_000})
    fresh = tc.post(READ, json={"url": COLD}).json()["body"]
    assert len(fresh) == resources_corpus.READ_MAX_CHARS
    # A body stored longer (as earlier reads did) is capped in the response
    # only: the researcher still reads all of it.
    long = tc.post(READ, json={"url": LONG}).json()["body"]
    assert long == LONG_BODY[: resources_corpus.READ_MAX_CHARS]
    assert (await stored(corpus, LONG))[0] == LONG_BODY
    ctx = SimpleNamespace(deps=resources_agent.RunDeps("", ""))
    ctx.deps.seen_urls.add(LONG)
    assert await resources_agent.read_source(ctx, LONG) == LONG_BODY


@needs_postgres
async def test_simultaneous_cold_reads_share_one_fetch(corpus, monkeypatch):  # noqa: F811
    calls = _jina(monkeypatch, delay=0.3)
    reads = await asyncio.gather(
        *(
            resources_corpus.read_corpus(COLD, curated_only=True, log_hosts=False)
            for _ in range(8)
        )
    )
    assert calls == [COLD]
    assert {read.body for read in reads} == {f"Fetched\n\nBody of {COLD}"}


@needs_postgres
async def test_a_reader_giving_up_leaves_the_fetch_for_the_rest(corpus, monkeypatch):  # noqa: F811
    calls = _jina(monkeypatch, delay=0.3)
    waiting = asyncio.ensure_future(resources_corpus.read_corpus(COLD, log_hosts=False))
    quitter = asyncio.ensure_future(resources_corpus.read_corpus(COLD, log_hosts=False))
    await asyncio.sleep(0.1)
    quitter.cancel()
    read = await waiting
    assert read.body == f"Fetched\n\nBody of {COLD}"
    assert calls == [COLD]
    # The last reader to give up stops the fetch.
    _jina(monkeypatch, delay=5)
    alone = asyncio.ensure_future(resources_corpus.read_corpus(STALE, log_hosts=False))
    await asyncio.sleep(0.1)
    alone.cancel()
    with pytest.raises(asyncio.CancelledError):
        await alone
    await asyncio.sleep(0)
    assert resources_corpus._IN_FLIGHT == {}
    assert (await stored(corpus, STALE))[0] == "old body"


@needs_postgres
async def test_the_timeout_covers_the_whole_read(client, corpus, monkeypatch):  # noqa: F811
    tc, _ = client
    monkeypatch.setattr(site_resources, "READ_TIMEOUT_S", 0.3)
    calls = _jina(monkeypatch)
    # A slow cache lookup alone runs it out, before any fetch.
    real = resources_corpus._stored

    async def slow_lookup(url):
        await asyncio.sleep(2)
        return await real(url)

    monkeypatch.setattr(resources_corpus, "_stored", slow_lookup)
    started = asyncio.get_running_loop().time()
    response = tc.post(READ, json={"url": COLD})
    assert asyncio.get_running_loop().time() - started < 1.5
    assert response.status_code == 504
    assert response.json()["detail"] == "This source took too long to read."
    assert calls == []
    # And a slow fetch, which stores nothing.
    monkeypatch.setattr(resources_corpus, "_stored", real)
    _jina(monkeypatch, delay=5)
    assert tc.post(READ, json={"url": COLD}).status_code == 504
    assert await stored(corpus, COLD) == (None, None)


@needs_postgres
async def test_a_failed_fetch_says_so_plainly(client, corpus, monkeypatch):  # noqa: F811
    tc, _ = client
    _jina(
        monkeypatch, result={"error": f"Failed to read URL (ConnectError): {COLD}?t=1"}
    )
    response = tc.post(READ, json={"url": COLD})
    assert response.status_code == 502
    assert response.json() == {"detail": "This source couldn't be read just now."}
    assert await stored(corpus, COLD) == (None, None)


def _jina_transport(monkeypatch, handler):
    """The real jina_read, over a stubbed transport."""
    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient
    monkeypatch.setattr(
        resources_corpus.httpx,
        "AsyncClient",
        lambda *a, **k: real(*a, transport=transport, **k),
    )


def _logged(caplog):
    return "\n".join(
        f"{r.name} {r.getMessage()} {r.exc_text or ''}" for r in caplog.records
    )


@needs_postgres
@pytest.mark.parametrize(
    "answer",
    [
        lambda request: httpx.Response(
            200, json={"data": {"title": "T", "content": "C"}}
        ),
        lambda request: httpx.Response(502, text="bad gateway"),
        lambda request: (_ for _ in ()).throw(
            httpx.ConnectError("refused", request=request)
        ),
    ],
    ids=["200", "502", "connect-error"],
)
async def test_corpus_routes_log_the_route_never_the_query_or_url(
    client,
    corpus,  # noqa: F811
    monkeypatch,
    caplog,
    answer,
):
    tc, _ = client
    caplog.set_level(logging.DEBUG)
    query = "zebra-query-marker"
    tc.post(SEARCH, json={"query": query})
    tc.post(SEARCH, json={"query": query * 50})
    tc.post(READ, json={"url": COLD, "extra": query})
    _jina_transport(monkeypatch, answer)
    tc.post(READ, json={"url": COLD})
    text = _logged(caplog)
    for leak in (query, COLD, "example.test", "feature-flags", "r.jina.ai"):
        assert leak not in text


@needs_postgres
async def test_the_researcher_logs_as_main_does(corpus, monkeypatch, caplog):  # noqa: F811
    caplog.set_level(logging.INFO)
    _jina_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, json={"data": {"title": "T", "content": "C"}}
        ),
    )
    ctx = SimpleNamespace(deps=resources_agent.RunDeps("", ""))
    ctx.deps.seen_urls.add(COLD)
    await resources_agent.read_source(ctx, COLD)
    # httpx's request line, as on main: only the site routes hold it back.
    assert f"https://r.jina.ai/{COLD}" in _logged(caplog)
