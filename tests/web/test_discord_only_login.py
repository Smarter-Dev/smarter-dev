"""Production ``app.yaml`` allows Discord sign-in and nothing else (task #56).

Loads the real ``app.yaml`` into Skrift and mounts Skrift's own
``AuthController``, so these tests exercise the same route guards production
runs: a method left out of ``auth`` must 404 on every entry point a browser
could hit directly, not just vanish from the login page.
"""

from __future__ import annotations

import re
import secrets
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import parse_qs
from urllib.parse import urlparse

import pytest
from litestar import Litestar
from litestar.contrib.jinja import JinjaTemplateEngine
from litestar.di import Provide
from litestar.middleware.session.client_side import CookieBackendConfig
from litestar.template.config import TemplateConfig
from litestar.testing import TestClient
from skrift import config as skrift_config
from skrift.controllers.auth import AuthController
from sqlalchemy.ext.asyncio import AsyncSession

APP_YAML = Path(__file__).resolve().parents[2] / "app.yaml"
DISABLED = ["github", "google", "passkey", "dummy"]


@pytest.fixture
def prod_settings(monkeypatch):
    for var in set(re.findall(r"\$([A-Z_][A-Z0-9_]*)", APP_YAML.read_text())):
        monkeypatch.setenv(var, f"test-{var.lower()}")
    monkeypatch.setenv("SKRIFT_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", secrets.token_hex(32))
    skrift_config.set_config_path(APP_YAML)
    yield skrift_config.get_settings()
    skrift_config._config_path_override = None
    skrift_config.get_settings.cache_clear()


@pytest.fixture
def client(prod_settings, tmp_path) -> TestClient:
    # Stand-in login template: renders just the provider keys the controller
    # hands the page, which is what the themed template loops over.
    (tmp_path / "login.html").write_text("{{ providers | join(',') }}")

    # No route under test reaches the database; the mock only satisfies the
    # handlers' AsyncSession parameter.
    async def no_db() -> AsyncSession:
        return AsyncMock(spec=AsyncSession)

    app = Litestar(
        route_handlers=[AuthController],
        middleware=[CookieBackendConfig(secret=secrets.token_bytes(16)).middleware],
        template_config=TemplateConfig(directory=tmp_path, engine=JinjaTemplateEngine),
        dependencies={"db_session": Provide(no_db)},
    )
    with TestClient(app) as c:
        yield c


def test_only_discord_is_configured(prod_settings):
    assert prod_settings.auth.get_method_keys() == ["discord"]
    assert list(prod_settings.auth.providers) == ["discord"]
    assert prod_settings.auth.second_factors.get_method_keys() == []


def test_login_page_lists_only_discord(client):
    resp = client.get("/auth/login", follow_redirects=False)
    assert resp.status_code == 200
    assert resp.text.strip() == "discord"


def test_discord_login_redirects_to_discord(client, prod_settings):
    resp = client.get("/auth/discord/login", follow_redirects=False)
    assert resp.status_code in (302, 303, 307)
    target = urlparse(resp.headers["location"])
    assert target.hostname == "discord.com"
    query = parse_qs(target.query)
    assert query["client_id"] == [prod_settings.auth.providers["discord"].client_id]
    assert query["redirect_uri"] == ["https://smarter.dev/auth/discord/callback"]


def test_discord_callback_is_live(client):
    # No state in the session, so the flow is rejected by the OAuth state
    # check — past the "is this method configured" gate the others fail at.
    resp = client.get("/auth/discord/callback?code=x&state=y", follow_redirects=False)
    assert resp.status_code == 400


@pytest.mark.parametrize("method", DISABLED)
def test_disabled_login_start_404s(client, method):
    assert (
        client.get(f"/auth/{method}/login", follow_redirects=False).status_code == 404
    )
    assert (
        client.get(
            f"/auth/{method}/login?next=/account", follow_redirects=False
        ).status_code
        == 404
    )


@pytest.mark.parametrize("method", DISABLED)
def test_disabled_callback_404s(client, method):
    assert (
        client.get(
            f"/auth/{method}/callback?code=x&state=y", follow_redirects=False
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/auth/{method}/callback?error=access_denied", follow_redirects=False
        ).status_code
        == 404
    )


@pytest.mark.parametrize(
    "path",
    [
        "/auth/passkey/options",
        "/auth/passkey/complete",
        "/auth/passkey/register/options",
        "/auth/passkey/register/complete",
        "/auth/github/complete",
        "/auth/google/complete",
        "/auth/dummy-login",
    ],
)
def test_disabled_interactive_endpoints_404(client, path):
    assert client.post(path, json={}).status_code == 404
