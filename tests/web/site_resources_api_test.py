"""Site resources lookup: site principal isolation, exact-URL matches in request
order, sdanswer card fields and request limits."""

from contextlib import asynccontextmanager
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from litestar import Litestar
from litestar.datastructures import State
from litestar.di import Provide
from litestar.plugins.pydantic import PydanticPlugin
from litestar.testing import AsyncTestClient
from skrift.auth.services import UserPermissions
from skrift.db.services import api_key_service

from smarter_dev.web.api_native import auth
from smarter_dev.web.api_native import rate_limiting
from smarter_dev.web.api_native.site_resources import SiteResourcesController
from smarter_dev.web.api_native.site_resources import _source
from smarter_dev.web.models import ResourceSource
from smarter_dev.web.sdanswer import _source_to_article_card

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
        ]
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
    assert response.json() == {"sources": []}


async def test_shared_url_returns_lowest_track_key(site_client, db_session):
    client, _ = site_client
    await seed_source(db_session, "z-later", "https://same.example/")
    await seed_source(db_session, "a-first", "https://same.example/")
    body = (await client.post(URL, json={"urls": ["https://same.example/"]})).json()
    assert body == {"sources": [wire("a-first", "https://same.example/")]}


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
                        "sources": [wire("a", "https://a.example/")]
                    }
    finally:
        invalidate_user_permissions_cache()
