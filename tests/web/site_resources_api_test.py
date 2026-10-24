"""Site resources lookup: site principal isolation, exact-URL matches in request
order, sdanswer card fields and request limits."""

import json
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import yaml
from litestar import Litestar
from litestar.datastructures import State
from litestar.di import Provide
from litestar.middleware import DefineMiddleware
from litestar.plugins.pydantic import PydanticPlugin
from litestar.serialization import encode_json
from litestar.testing import AsyncTestClient
from skrift import config as skrift_config
from skrift.auth.services import UserPermissions
from skrift.config import interpolate_env_vars
from skrift.db.services import api_key_service
from skrift.middleware.rate_limit import RateLimitMiddleware
from skrift.ratelimit import RateLimiter

from smarter_dev.web.api_native import auth
from smarter_dev.web.api_native import rate_limiting
from smarter_dev.web.api_native import site_resources
from smarter_dev.web.api_native.site_memory import SiteMemoryController
from smarter_dev.web.api_native.site_resources import MAX_RESPONSE_BYTES
from smarter_dev.web.api_native.site_resources import SiteResourcesController
from smarter_dev.web.api_native.site_resources import _source
from smarter_dev.web.models import ResourceSource
from smarter_dev.web.sdanswer import ARTICLE_CARD_CAPS
from smarter_dev.web.sdanswer import _load_sources
from smarter_dev.web.sdanswer import _source_to_article_card
from smarter_dev.web.sdanswer import pick_sources

URL = "/api/site/resources/sources"


@pytest.fixture
async def site_client(db_session, monkeypatch):
    import skrift.auth.services

    @asynccontextmanager
    async def session_factory():
        yield db_session

    key = SimpleNamespace(
        id=uuid4(),
        user_id=uuid4(),
        principal_type="service",
        service_name="site",
        scoped_permission_list=["site-resources"],
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
                user_id=str(key.user_id), roles=set(), permissions={"site-resources"}
            )
        ),
    )
    monkeypatch.setattr(auth, "get_db_session_context", session_factory)
    app = Litestar(
        route_handlers=[SiteResourcesController],
        plugins=[PydanticPlugin()],
        dependencies={"db_session": Provide(lambda: db_session, sync_to_thread=False)},
        state=State({"session_maker_class": session_factory}),
    )
    async with AsyncTestClient(app=app) as client:
        client.headers["Authorization"] = "Bearer sk_test"
        yield client, key


async def seed_source(session, track_key, url, *, byline="Someone", blurb="About it"):
    source = ResourceSource(
        track_key=track_key,
        title=f"Title {track_key}",
        url=url,
        byline=byline,
        blurb=blurb,
        learning_type="article",
        first_indexed_at=date(2026, 10, 1),
    )
    session.add(source)
    await session.commit()
    return source


def wire(track_key, url, *, byline="Someone", blurb="About it"):
    return {
        "url": url,
        "title": f"Title {track_key}",
        "byline": byline,
        "blurb": blurb,
        "learning_type": "article",
        "track_key": track_key,
    }


async def test_known_urls_in_request_order_deduped(site_client, db_session):
    client, _ = site_client
    await seed_source(db_session, "a", "https://a.example/one")
    await seed_source(db_session, "b", "https://b.example/two", blurb=None)
    response = await client.post(
        URL,
        json={
            "urls": [
                "https://b.example/two",
                "https://unknown.example/",
                "https://a.example/one",
                "https://b.example/two",
                # Exact match only, as sdanswer: no normalisation.
                "https://a.example/one/",
            ]
        },
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "sources": [
            wire("b", "https://b.example/two", blurb=""),
            wire("a", "https://a.example/one"),
        ],
        "omitted": [],
    }
    assert response.headers["cache-control"] == "private, no-store"


async def test_never_consults_the_per_key_rate_limiter(site_client, monkeypatch):
    # The limiter's bucket is per API key and shared with the site memory
    # routes; card lookups must never spend it.
    resolve = AsyncMock(side_effect=AssertionError("rate limiter consulted"))
    check = AsyncMock(side_effect=AssertionError("rate limiter consulted"))
    monkeypatch.setattr(rate_limiting, "_resolve_rate_limited_key", resolve)
    monkeypatch.setattr(rate_limiting, "check_rate_limits", check)
    client, _ = site_client
    response = await client.post(URL, json={"urls": ["https://x.example/"]})
    assert response.status_code == 200, response.text
    assert not any(name.startswith("x-ratelimit") for name in response.headers)
    resolve.assert_not_awaited()
    check.assert_not_awaited()


async def test_unknown_urls_only_return_empty(site_client):
    client, _ = site_client
    response = await client.post(URL, json={"urls": ["https://nope.example/"]})
    assert response.status_code == 200
    assert response.json() == {"sources": [], "omitted": []}


async def test_shared_url_matches_the_renderers_pick(site_client, db_session):
    client, _ = site_client
    url = "https://same.example/"
    # Inserted first, so a last-row-wins pick would choose z-last.
    await seed_source(db_session, "a-first", url, blurb="First blurb")
    await seed_source(db_session, "z-last", url, blurb="Last blurb")
    body = (await client.post(URL, json={"urls": [url]})).json()
    rendered = _source_to_article_card((await _load_sources(db_session, {url}))[url])
    rendered.pop("type")
    assert body == {"sources": [rendered], "omitted": []}
    assert body["sources"][0] == wire("a-first", url, blurb="First blurb")


@pytest.mark.parametrize("reverse", [False, True])
def test_pick_sources_keeps_lowest_track_key_in_any_order(reverse):
    rows = [
        SimpleNamespace(url="u", track_key="b"),
        SimpleNamespace(url="u", track_key="a"),
        SimpleNamespace(url="u", track_key="c"),
        SimpleNamespace(url="v", track_key="z"),
    ]
    picked = pick_sources(reversed(rows) if reverse else rows)
    assert {url: src.track_key for url, src in picked.items()} == {"u": "a", "v": "z"}


async def test_field_caps_match_the_renderers_card(site_client, db_session):
    client, _ = site_client
    url = "https://big.example/"
    await seed_source(db_session, "big", url, blurb="é" * (2 * 1024 * 1024))
    response = await client.post(URL, json={"urls": [url]})
    assert response.status_code == 200
    (source,) = response.json()["sources"]
    assert source["blurb"] == "é" * ARTICLE_CARD_CAPS["blurb"] == "é" * 2000
    rendered = _source_to_article_card((await _load_sources(db_session, {url}))[url])
    rendered.pop("type")
    assert source == rendered
    assert len(response.content) < 10_000


def test_card_caps_every_text_field_but_the_url():
    url = "https://x.example/" + "u" * 3000
    src = ResourceSource(
        track_key="k" * 300,
        title="t" * 600,
        url=url,
        byline="b" * 300,
        blurb="z" * 3000,
        learning_type="l" * 40,
    )
    card = _source_to_article_card(src)
    assert card["url"] == url
    assert {name: len(card[name]) for name in ARTICLE_CARD_CAPS} == ARTICLE_CARD_CAPS


async def test_body_stays_within_the_cap_and_lists_the_rest(site_client, db_session):
    client, _ = site_client
    # 99 large sources (about 11 KB each) plus one unknown URL.
    urls = [f"https://many.example/{n:03d}" for n in range(99)]
    for n, url in enumerate(urls):
        db_session.add(
            ResourceSource(
                track_key=f"many-{n:03d}",
                title="🦀" * 512,
                url=url,
                byline="🦀" * 256,
                blurb="🦀" * 2000,
                learning_type="article",
                first_indexed_at=date(2026, 10, 1),
            )
        )
    await db_session.commit()
    # Unknown URLs are neither answered nor omitted.
    response = await client.post(URL, json={"urls": [*urls, "https://nope.example/"]})
    assert response.status_code == 200
    assert len(response.content) <= MAX_RESPONSE_BYTES
    body = response.json()
    sources, omitted = body["sources"], body["omitted"]
    assert sources and omitted
    assert [s["url"] for s in sources] + omitted == urls
    assert all(s["blurb"] == "🦀" * 2000 for s in sources)
    # The next source would not have fit: answering it grows the body by its
    # encoded size less its omitted URL (every source here is the same size).
    grows = len(encode_json(sources[0])) - len(encode_json(sources[0]["url"]))
    assert len(response.content) + grows > MAX_RESPONSE_BYTES


async def test_smaller_cap_omits_from_the_first_that_does_not_fit(
    site_client, db_session, monkeypatch
):
    client, _ = site_client
    urls = [f"https://few.example/{n}" for n in range(4)]
    for n, url in enumerate(urls):
        await seed_source(db_session, f"few-{n}", url, blurb="x" * 1000)
    monkeypatch.setattr(site_resources, "MAX_RESPONSE_BYTES", 2500)
    response = await client.post(URL, json={"urls": urls})
    assert len(response.content) <= 2500
    assert [s["url"] for s in response.json()["sources"]] == urls[:2]
    assert response.json()["omitted"] == urls[2:]
    monkeypatch.setattr(site_resources, "MAX_RESPONSE_BYTES", 10)
    response = await client.post(URL, json={"urls": urls})
    assert response.json() == {"sources": [], "omitted": urls}


def test_fields_match_the_sdanswer_article_card():
    src = ResourceSource(
        track_key="k",
        title="T",
        url="https://x.example/",
        byline=None,
        blurb=None,
        learning_type="video",
    )
    card = _source_to_article_card(src)
    assert card.pop("type") == "article"
    assert _source(src) == card
    assert card["byline"] == "" and card["blurb"] == ""


@pytest.mark.parametrize(
    "body",
    [
        {"urls": []},
        {"urls": [f"https://x.example/{n}" for n in range(101)]},
        {"urls": ["https://x.example/" + "a" * 483]},  # 501 characters
        {"urls": [""]},
        {"urls": [42]},
        {"urls": "https://x.example/"},
        {"urls": ["https://x.example/"], "extra": True},
        {},
    ],
)
async def test_invalid_bodies_are_422(site_client, body):
    client, _ = site_client
    response = await client.post(URL, json=body)
    assert response.status_code == 422, response.text
    assert response.json()["type"] == "validation_error"


async def test_limits_are_inclusive(site_client):
    client, _ = site_client
    assert len("https://x.example/" + "a" * 482) == 500
    for body in (
        {"urls": [f"https://x.example/{n}" for n in range(100)]},
        {"urls": ["https://x.example/" + "a" * 482]},
    ):
        assert (await client.post(URL, json=body)).status_code == 200


@pytest.mark.parametrize("kind", ["missing", "bot", "user", "search-only", "unscoped"])
async def test_rejects_non_site_resources_credentials(site_client, kind):
    client, key = site_client
    headers = {}
    if kind == "missing":
        headers["Authorization"] = ""
    elif kind == "bot":
        key.service_name = "discord-bot"
    elif kind == "user":
        key.principal_type = "user"
    elif kind == "search-only":
        # The existing site key before the scope edit.
        key.scoped_permission_list = [
            "site-memory",
            "site-privacy",
            "site-billing",
            "site-identity",
            "site-search",
        ]
    elif kind == "unscoped":
        key.scoped_permission_list = []
    response = await client.post(
        URL, json={"urls": ["https://x.example/"]}, headers=headers
    )
    assert response.status_code == 401


async def test_real_skrift_site_key_authorization(test_engine, monkeypatch):
    """Real role sync, key storage/verification and permission resolution."""
    from skrift.auth.services import assign_role_to_user
    from skrift.auth.services import invalidate_user_permissions_cache
    from skrift.auth.services import sync_roles_to_database
    from skrift.db.base import Base as SkriftBase
    from skrift.db.models.user import User

    import smarter_dev.web.roles  # noqa: F401 — sync the actual site-service role
    from smarter_dev.shared.database import async_sessionmaker

    async with test_engine.begin() as connection:
        await connection.run_sync(SkriftBase.metadata.create_all)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as session:
        owner = User(email="site-resources@example.test", name="Site", is_active=True)
        denied_owner = User(email="no-role@example.test", name="No", is_active=True)
        session.add_all([owner, denied_owner])
        await session.commit()
        await sync_roles_to_database(session)
        assert await assign_role_to_user(session, owner.id, "site-service")
        await seed_source(session, "a", "https://a.example/")
        keys = {}
        variants = {
            "site": {},
            "bot": {"service_name": "discord-bot"},
            "user": {"principal_type": "user"},
            "missing-scope": {"scoped_permissions": ["site-search"]},
            "unscoped": {"scoped_permissions": None},
            "role-only": {"scoped_permissions": None, "scoped_roles": ["site-service"]},
            "owner-denied": {"user_id": denied_owner.id},
        }
        for name, overrides in variants.items():
            options = {
                "user_id": owner.id,
                "display_name": name,
                "principal_type": "service",
                "service_name": "site",
                "scoped_permissions": ["site-resources"],
            }
            options.update(overrides)
            _, keys[name], _ = await api_key_service.create_api_key(session, **options)

    async def provide_db_session():
        async with maker() as session:
            yield session

    monkeypatch.setattr(auth, "get_db_session_context", maker)
    app = Litestar(
        route_handlers=[SiteResourcesController],
        plugins=[PydanticPlugin()],
        dependencies={"db_session": Provide(provide_db_session)},
        state=State({"session_maker_class": maker}),
    )
    invalidate_user_permissions_cache()
    try:
        async with AsyncTestClient(app=app) as client:
            no_key = await client.post(URL, json={"urls": ["https://a.example/"]})
            assert no_key.status_code == 401
            for name, key in keys.items():
                response = await client.post(
                    URL,
                    json={"urls": ["https://a.example/"]},
                    headers={"Authorization": f"Bearer {key}"},
                )
                assert response.status_code == (200 if name == "site" else 401), (
                    name,
                    response.text,
                )
                if name == "site":
                    assert response.json() == {
                        "sources": [wire("a", "https://a.example/")],
                        "omitted": [],
                    }
    finally:
        invalidate_user_permissions_cache()


def test_parity_script_prints_the_pick_for_duplicated_urls_only(tmp_path, capsys):
    import importlib.util

    path = Path(__file__).parents[2] / "scripts" / "parity" / "pick_sources.py"
    spec = importlib.util.spec_from_file_location("pick_sources_script", path)
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    rows = [
        {"url": "https://dup.example/", "track_key": "z-last", "title": "Z"},
        {"url": "https://one.example/", "track_key": "only"},
        {"url": "https://dup.example/", "track_key": "a-first", "title": "A"},
    ]
    catalog = tmp_path / "resources-catalog.json"
    for data in (rows, {"resource_sources": rows}):
        catalog.write_text(json.dumps(data))
        assert script.main(["pick_sources.py", str(catalog)]) == 0
        assert json.loads(capsys.readouterr().out) == {
            "https://dup.example/": "a-first"
        }


# Skrift's global rate limiter, configured from app.yaml as the app loads it.

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
async def limited_app(db_session, monkeypatch):
    import skrift.auth.services

    @asynccontextmanager
    async def session_factory():
        yield db_session

    scopes = ["site-resources", "site-memory"]
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

    @asynccontextmanager
    async def build(config):
        app = Litestar(
            route_handlers=[SiteResourcesController, SiteMemoryController],
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


def _post(client, token, ip="203.0.113.1"):
    return client.post(
        URL,
        json={"urls": ["https://x.example/"]},
        headers={"Authorization": f"Bearer {token}", "X-Test-IP": ip},
    )


def test_app_yaml_gives_the_lookup_its_own_ip_rule():
    config = app_rate_limit_config()
    assert config.enabled
    rule = config.resolve(URL, "POST")
    assert (rule.name, rule.key, rule.limits) == (
        "site-resources",
        "ip",
        [(600, 60.0)],
    )
    assert config.resolve(MEMORY_URL, "GET").name == "default"


async def test_lookups_never_spend_the_site_memory_budget(limited_app):
    config = app_rate_limit_config()
    default_limit = config.resolve(MEMORY_URL, "GET").limits[0][0]
    async with limited_app(config) as client:
        for _ in range(default_limit + 1):
            assert (await _post(client, "sk_site")).status_code == 200
        response = await client.get(
            MEMORY_URL,
            headers={"Authorization": "Bearer sk_site", "X-Test-IP": "203.0.113.1"},
        )
        assert response.status_code == 200, response.text


async def test_lookup_rule_limits_per_ip_whatever_the_key(limited_app):
    config = app_rate_limit_config()
    (rule,) = [rule for rule in config.rules if rule.name == "site-resources"]
    rule.limits[0].limit = 5  # the app.yaml rule, with a lower limit for speed
    ip_a, ip_b = "203.0.113.1", "198.51.100.7"
    async with limited_app(config) as client:
        for _ in range(5):
            assert (await _post(client, "sk_site_a", ip=ip_a)).status_code == 200
        limited = await _post(client, "sk_site_a", ip=ip_a)
        assert limited.status_code == 429
        assert int(limited.headers["retry-after"]) > 0
        # The same address is limited whatever key it sends, invented or not.
        for token in ("sk_site_b", "sk_invented_1", "sk_invented_2"):
            assert (await _post(client, token, ip=ip_a)).status_code == 429
        # Another address has its own budget.
        assert (await _post(client, "sk_site_a", ip=ip_b)).status_code == 200
