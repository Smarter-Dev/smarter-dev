"""The site code route under Skrift's real global rate limiter, configured from
the real app.yaml as the app loads it (as tests/web/site_resources_api_test.py
does for the resources lookup). Runs go through the real job and real Monty."""

import asyncio
from contextlib import asynccontextmanager
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
from litestar.middleware import DefineMiddleware
from litestar.plugins.pydantic import PydanticPlugin
from litestar.testing import AsyncTestClient
from skrift import config as skrift_config
from skrift.config import interpolate_env_vars
from skrift.db.base import Base as SkriftBase
from skrift.db.models.user import User
from skrift.middleware.rate_limit import RateLimitMiddleware
from skrift.ratelimit import RateLimiter

from smarter_dev.web import site_code_jobs
from smarter_dev.web.api_native import site_code as site_code_api
from smarter_dev.web.api_native import site_guards
from smarter_dev.web.api_native.site_code import SiteCodeController

REPO = Path(__file__).parents[2]
IP_A, IP_B = "203.0.113.1", "198.51.100.7"


def app_rate_limit_config():
    """``rate_limit`` from app.yaml, built the way ``skrift.config.get_settings``
    builds each section (env interpolation, then the registered model)."""
    raw = interpolate_env_vars(
        yaml.safe_load((REPO / "app.yaml").read_text()), strict=False
    )
    model = skrift_config._CONFIG_SECTIONS["rate_limit"]
    assert model is skrift_config.RateLimitConfig
    return model(**raw.get("rate_limit", {}))


def code_url(user_id) -> str:
    return f"/api/site/code/users/{user_id}/runs"


class ClientIPStandIn:
    """Stands in for Skrift's ClientIPMiddleware, which runs before the rate
    limiter and records the resolved address in ``scope["state"]``."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            ip = dict(scope["headers"]).get(b"x-test-ip", IP_A.encode()).decode()
            scope.setdefault("state", {})["client_ip"] = ip
        await self.app(scope, receive, send)


@pytest.fixture
async def limited_app(test_engine, db_session, monkeypatch):
    async with test_engine.begin() as conn:
        await conn.run_sync(SkriftBase.metadata.create_all)
    member = User(email=f"{uuid4()}@example.test", name="Member", is_active=True)
    db_session.add(member)
    await db_session.commit()

    @asynccontextmanager
    async def session_factory():
        yield db_session

    monkeypatch.setattr(site_guards, "bot_api_auth_guard", AsyncMock())
    key = SimpleNamespace(
        principal_type="service",
        service_name="site",
        scoped_permission_list=["site-code"],
    )
    monkeypatch.setattr(
        site_guards, "resolve_request_api_key", AsyncMock(return_value=key)
    )
    redis = fakeredis.FakeAsyncRedis(decode_responses=True)
    monkeypatch.setattr(site_code_api, "get_redis_client", lambda: redis)
    monkeypatch.setattr(site_code_jobs, "get_redis_client", lambda: redis)

    async def submit(job_type, payload, **options):
        asyncio.ensure_future(
            site_code_jobs.run_site_code_job(site_code_jobs.SiteCodePayload(**payload))
        )

    import skrift.workers

    monkeypatch.setattr(skrift.workers, "submit", submit)
    monkeypatch.setattr(
        "smarter_dev.web.chat.dispatch.ensure_submission_handler",
        lambda job_type: None,
    )

    @asynccontextmanager
    async def build(config):
        app = Litestar(
            route_handlers=[SiteCodeController],
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

    return SimpleNamespace(build=build, member=member.id)


def _run(client, user_id, token="sk_site", ip=IP_A):
    return client.post(
        code_url(user_id),
        json={"reason": "adding", "code": "1 + 1"},
        headers={"Authorization": f"Bearer {token}", "X-Test-IP": ip},
    )


def test_app_yaml_gives_the_code_route_its_own_ip_rule():
    config = app_rate_limit_config()
    assert config.enabled
    rule = config.resolve(code_url(uuid4()), "POST")
    assert (rule.name, rule.key, rule.limits) == ("site-code", "ip", [(600, 60.0)])
    # POST only, and only under the code route: anything else under
    # /api/site/ falls to the site catch-all.
    assert config.resolve(code_url(uuid4()), "GET").name == "site"
    for other in (
        f"/api/site/search/users/{uuid4()}/runs",
        "/api/site/code",
        "/api/site/codex/users/x/runs",
    ):
        assert config.resolve(other, "POST").name != "site-code", other


async def test_code_runs_never_spend_another_budget(limited_app):
    # The /api/site/ catch-all and the default both cut to 3: code runs go
    # on, on their own rule.
    config = app_rate_limit_config()
    (catch_all,) = [rule for rule in config.rules if rule.name == "site"]
    catch_all.limits[0].limit = 3
    config.requests_per_minute = 3
    assert config.resolve("/api/site/not-a-family", "POST").limits == [(3, 60.0)]
    assert config.resolve("/chat", "GET").limits == [(3, 60.0)]
    async with limited_app.build(config) as client:
        for _ in range(10):
            response = await _run(client, limited_app.member)
            assert response.status_code == 200, response.text
        assert response.json() == {"result": "return value: 2"}


async def test_rule_limits_per_ip_whatever_the_key(limited_app):
    config = app_rate_limit_config()
    (rule,) = [rule for rule in config.rules if rule.name == "site-code"]
    rule.limits[0].limit = 5  # the app.yaml rule, with a lower limit for speed
    async with limited_app.build(config) as client:
        for _ in range(5):
            assert (await _run(client, limited_app.member)).status_code == 200
        limited = await _run(client, limited_app.member)
        assert limited.status_code == 429
        assert int(limited.headers["retry-after"]) > 0
        # The same address is limited whatever key or member it sends.
        for token in ("sk_site_b", "sk_invented_1"):
            assert (await _run(client, limited_app.member, token)).status_code == 429
        assert (await _run(client, uuid4())).status_code == 429
        # Another address has its own budget.
        assert (await _run(client, limited_app.member, ip=IP_B)).status_code == 200
