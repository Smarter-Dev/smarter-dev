"""Skrift's global rate limiter on the site API, configured from app.yaml.

The site Worker calls every /api/site/* route from a few Cloudflare egress
addresses, for all members at once. Each route family therefore has its own
IP-keyed namespace, and no site route spends the 60/min default. These tests
load app.yaml's ``rate_limit`` as Skrift does and run requests through the
real ``RateLimitMiddleware`` and ``RateLimiter`` on a Redis stand-in.
"""

import importlib
import re
from pathlib import Path

import fakeredis
import pytest
import yaml
from litestar import Litestar
from skrift import config as skrift_config
from skrift.config import RateLimitMatch
from skrift.config import RateLimitRule
from skrift.config import RateLimitWindow
from skrift.config import RedisConfig
from skrift.config import interpolate_env_vars
from skrift.middleware.rate_limit import RateLimitMiddleware
from skrift.ratelimit import RateLimiter

REPO = Path(__file__).parents[2]
SAMPLE_ID = "123456789012345678"

# Every /api/site route app.yaml mounts, and the rule it must resolve to.
# A new site route fails test_site_routes_match_this_table until it is placed.
SITE_ROUTES = {
    ("GET", "/api/site/billing/catalog"): "site-billing",
    ("GET", "/api/site/billing/users/{user_id:str}/membership"): "site-billing",
    ("POST", "/api/site/billing/users/{user_id:str}/checkout"): "site-billing",
    ("POST", "/api/site/billing/users/{user_id:str}/portal"): "site-billing",
    ("GET", "/api/site/guilds/{guild_id:str}/memory/blocks"): "site-memory",
    (
        "GET",
        "/api/site/guilds/{guild_id:str}/memory/members/{discord_user_id:str}",
    ): "site-memory",
    (
        "POST",
        "/api/site/guilds/{guild_id:str}/memory/members/{discord_user_id:str}/notes",
    ): "site-memory",
    ("GET", "/api/site/guilds/{guild_id:str}/memory/public"): "site-memory",
    ("GET", "/api/site/memory/public"): "site-memory",
    ("PUT", "/api/site/identities/{user_id:str}"): "site-identity",
    ("POST", "/api/site/privacy/delete-account/{user_id:str}"): "site-privacy",
    ("GET", "/api/site/privacy/opt-out/{discord_user_id:str}"): "site-privacy",
    ("POST", "/api/site/privacy/opt-out/{discord_user_id:str}"): "site-privacy",
    ("DELETE", "/api/site/privacy/opt-out/{discord_user_id:str}"): "site-privacy",
    ("POST", "/api/site/resources/read"): "site-corpus-read",
    ("POST", "/api/site/resources/search"): "site-corpus-search",
    ("POST", "/api/site/resources/sources"): "site-resources",
    ("GET", "/api/site/search/anonymous-runs/{search_id:str}"): "site-search",
    ("POST", "/api/site/search/previews/resolve"): "site-search-previews",
    ("GET", "/api/site/search/users/{owner_id:str}/link-owner"): "site-search",
    ("POST", "/api/site/search/users/{owner_id:str}/anonymous-runs"): "site-search",
    ("POST", "/api/site/search/users/{user_id:str}/runs"): "site-search",
    ("GET", "/api/site/search/users/{user_id:str}/runs/{run_id:str}"): "site-search",
    (
        "DELETE",
        "/api/site/search/users/{user_id:str}/runs/{run_id:str}",
    ): "site-search",
}

# Each family's (limit, window seconds) pairs, as app.yaml sets them.
FAMILY_LIMITS = {
    "site-resources": [(600, 60.0)],
    "site-corpus-search": [(240, 60.0)],
    "site-corpus-read": [(120, 60.0), (3000, 3600.0)],
    "site-search-previews": [(1200, 60.0)],
    "site-search": [(3000, 60.0)],
    "site-billing": [(1200, 60.0)],
    "site-memory": [(600, 60.0)],
    "site-identity": [(300, 60.0)],
    "site-privacy": [(300, 60.0)],
    "site": [(600, 60.0)],
}

# One concrete request per namespace; site-memory has both of its shapes.
FAMILY_REQUESTS = {
    "site-resources": [("POST", "/api/site/resources/sources")],
    "site-corpus-search": [("POST", "/api/site/resources/search")],
    "site-corpus-read": [("POST", "/api/site/resources/read")],
    "site-search-previews": [("POST", "/api/site/search/previews/resolve")],
    "site-search": [("GET", f"/api/site/search/users/{SAMPLE_ID}/runs/r1")],
    "site-billing": [("GET", f"/api/site/billing/users/{SAMPLE_ID}/membership")],
    "site-memory": [
        ("GET", "/api/site/memory/public"),
        ("GET", f"/api/site/guilds/{SAMPLE_ID}/memory/blocks"),
    ],
    "site-identity": [("PUT", f"/api/site/identities/{SAMPLE_ID}")],
    "site-privacy": [("GET", f"/api/site/privacy/opt-out/{SAMPLE_ID}")],
    "site": [("POST", "/api/site/not-yet-a-family/x")],
}
DEFAULT_REQUEST = ("GET", "/chat")

# Tokens the requests carry; none of them may reach a Redis key name.
TOKENS = ("sk_site_live_8f3a1c", "sk_invented_a", "Zm9vYmFy-raw-token")


def app_rate_limit_config():
    """``rate_limit`` from app.yaml, built the way ``skrift.config.get_settings``
    builds each section (env interpolation, then the registered model)."""
    raw = interpolate_env_vars(
        yaml.safe_load((REPO / "app.yaml").read_text()), strict=False
    )
    model = skrift_config._CONFIG_SECTIONS["rate_limit"]
    assert model is skrift_config.RateLimitConfig
    return model(**raw.get("rate_limit", {}))


def app_redis_config():
    raw = interpolate_env_vars(
        yaml.safe_load((REPO / "app.yaml").read_text()), strict=False
    )
    return RedisConfig(**{**raw.get("redis", {}), "url": ""})


def mounted_routes():
    """(method, path template) for every route of app.yaml's controllers."""
    raw = yaml.safe_load((REPO / "app.yaml").read_text())
    handlers = []
    for spec in raw["controllers"]:
        module, name = spec.split(":")
        handlers.append(getattr(importlib.import_module(module), name))
    app = Litestar(route_handlers=handlers, openapi_config=None)
    return {
        (method, route.path)
        for route in app.routes
        for method in getattr(route, "methods", ())
        if method not in ("HEAD", "OPTIONS")
    }


def concrete(template):
    return re.sub(r"\{[^}]+\}", SAMPLE_ID, template)


def lowered(config, name, limit):
    """The app.yaml config with every rule in ``name`` limited to ``limit``
    in its first (per-minute) window."""
    for rule in config.rules:
        if rule.name == name:
            rule.limits[0].limit = limit
    return config


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


async def handled(scope, receive, send):
    """Whatever the route: the request got past the limiter."""
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


class Limited:
    """The middleware stack as skrift.asgi mounts it: client IP, then the
    global limiter, on Skrift's Redis-backed limiter."""

    def __init__(self, config):
        self.redis = fakeredis.aioredis.FakeRedis()
        self.app = ClientIPStandIn(
            RateLimitMiddleware(
                handled,
                config=config,
                limiter=RateLimiter(
                    redis_client=self.redis, redis_config=app_redis_config()
                ),
            )
        )
        self.sent = 0

    async def request(self, method, path, ip="203.0.113.1"):
        token = TOKENS[self.sent % len(TOKENS)]
        self.sent += 1
        scope = {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [
                (b"authorization", f"Bearer {token}".encode()),
                (b"x-api-key", token.encode()),
                (b"x-test-ip", ip.encode()),
            ],
        }
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        await self.app(scope, receive, send)
        start = sent[0]
        return start["status"], dict(start["headers"])

    async def keys(self):
        return sorted(key.decode() for key in await self.redis.keys("*"))


def test_site_routes_match_this_table():
    site = {route for route in mounted_routes() if route[1].startswith("/api/site")}
    assert site == set(SITE_ROUTES)


@pytest.mark.parametrize(
    ("route", "name"),
    sorted(SITE_ROUTES.items()),
    ids=[f"{method} {path}" for (method, path), _ in sorted(SITE_ROUTES.items())],
)
def test_every_site_route_resolves_to_its_family(route, name):
    method, template = route
    rule = app_rate_limit_config().resolve(concrete(template), method)
    assert (rule.name, rule.key, rule.limits) == (
        name,
        "ip",
        FAMILY_LIMITS[name],
    )


def test_every_site_rule_is_ip_keyed_and_has_a_known_limit():
    config = app_rate_limit_config()
    assert config.enabled
    assert config.rules
    for rule in config.rules:
        assert rule.key == "ip", rule.name
        assert rule.match.path.startswith("/api/site/"), rule.name
        assert [w.pair for w in rule.limits] == FAMILY_LIMITS[rule.name]


def test_non_site_routes_keep_default_and_auth():
    config = app_rate_limit_config()
    others = [
        route for route in mounted_routes() if not route[1].startswith("/api/site")
    ]
    assert len(others) > 200
    near_misses = [
        ("GET", "/api/site"),
        ("GET", "/api/sites/search/x"),
        ("GET", "/api/site-search/x"),
        ("POST", "/api/search/users/x/runs"),
        ("GET", "/site/api/search/x"),
        ("GET", "/api/v1/site/memory/public"),
    ]
    for method, template in [*others, *near_misses]:
        rule = config.resolve(concrete(template), method)
        if template.startswith("/auth"):
            assert (rule.name, rule.limits) == ("auth", [(10, 60.0)]), template
        else:
            assert (rule.name, rule.key, rule.limits) == (
                "default",
                "ip",
                [(60, 60.0)],
            ), template
    for path in ("/auth/login", "/auth/discord/callback"):
        assert (
            config.resolve(path, "GET").name,
            config.resolve(path, "GET").limits,
        ) == (
            "auth",
            [(10, 60.0)],
        )
    assert (config.default, config.auth, config.paths) == (None, None, {})


def test_an_exact_route_rule_still_beats_its_family_prefix():
    # Later site rules (an exact path and method) win over the family prefix
    # wherever app.yaml lists them.
    config = app_rate_limit_config()
    for path in (
        "/api/site/search/previews/resolve",
        "/api/site/resources/search",
    ):
        config.rules.insert(
            0,
            RateLimitRule(
                name="exact",
                match=RateLimitMatch(path=path, method="POST"),
                limits=[RateLimitWindow(limit=1, per="minute")],
            ),
        )
        assert config.resolve(path, "POST").name == "exact"
        assert config.resolve(path, "GET").name != "exact"


async def test_site_traffic_never_spends_the_default():
    limited = Limited(app_rate_limit_config())
    for family, requests in FAMILY_REQUESTS.items():
        for n in range(61):
            method, path = requests[n % len(requests)]
            status, _ = await limited.request(method, path)
            assert status == 200, (family, n)
    # The default's whole 60 is still there, and it still limits.
    for _ in range(60):
        assert (await limited.request(*DEFAULT_REQUEST))[0] == 200
    assert (await limited.request(*DEFAULT_REQUEST))[0] == 429


@pytest.mark.parametrize("family", sorted(FAMILY_REQUESTS))
async def test_a_saturated_family_429s_alone(family):
    limit = 4
    limited = Limited(lowered(app_rate_limit_config(), family, limit))
    requests = FAMILY_REQUESTS[family]
    for n in range(limit):
        assert (await limited.request(*requests[n % len(requests)]))[0] == 200
    # At the edge, every shape of the family is limited, with Retry-After.
    for method, path in requests:
        status, headers = await limited.request(method, path)
        assert status == 429
        assert 0 < int(headers[b"retry-after"]) <= 60
    # Another address has its own budget.
    assert (await limited.request(*requests[0], ip="198.51.100.7"))[0] == 200
    # Every other family, and the default, still answer.
    for other, others in FAMILY_REQUESTS.items():
        if other != family:
            for method, path in others:
                assert (await limited.request(method, path))[0] == 200, other
    assert (await limited.request(*DEFAULT_REQUEST))[0] == 200


async def test_redis_keys_name_the_family_and_address_only():
    limited = Limited(app_rate_limit_config())
    for requests in FAMILY_REQUESTS.values():
        for method, path in requests:
            for ip in ("203.0.113.1", "198.51.100.7"):
                assert (await limited.request(method, path, ip=ip))[0] == 200
    await limited.request(*DEFAULT_REQUEST)
    keys = await limited.keys()
    for key in keys:
        for secret in (*TOKENS, "apikey:", "Bearer", "bearer", "sk_"):
            assert secret not in key
    assert keys == sorted(
        f"skrift:ratelimit:{name}:{ip}"
        for name in [*FAMILY_REQUESTS, "default"]
        for ip in ("203.0.113.1", "198.51.100.7")
        if name != "default" or ip == "203.0.113.1"
    )
