"""Production ``app.yaml`` allows Discord, GitHub and Google sign-in, and no
passkeys (task #56, GitHub and Google back in task #71).

Loads the real ``app.yaml`` into Skrift and mounts Skrift's own
``AuthController``, so these tests exercise the same route guards production
runs: a method left out of ``auth`` must 404 on every entry point a browser
could hit directly, not just vanish from the login page. The account security
page must keep showing passkeys while passkey sign-in is off.
"""

from __future__ import annotations

import re
import secrets
from datetime import UTC
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from urllib.parse import parse_qs
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from jinja2 import ChoiceLoader
from jinja2 import DictLoader
from jinja2 import Environment
from jinja2 import FileSystemLoader
from litestar import Litestar
from litestar import Request
from litestar import get
from litestar.contrib.jinja import JinjaTemplateEngine
from litestar.di import Provide
from litestar.middleware.session.client_side import CookieBackendConfig
from litestar.template.config import TemplateConfig
from litestar.testing import TestClient
from skrift import config as skrift_config
from skrift.controllers.auth import AuthController
from skrift.forms.core import CSRF_FIELD_NAME
from skrift.forms.core import CSRF_SESSION_KEY
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.account_controller import AccountController

REPO = Path(__file__).resolve().parents[2]
APP_YAML = REPO / "app.yaml"
PROVIDERS = ["discord", "github", "google"]
DISABLED = ["passkey", "dummy"]
AUTHORIZE_HOSTS = {
    "discord": "discord.com",
    "github": "github.com",
    "google": "accounts.google.com",
}
SCOPES = {
    "discord": {"identify", "email"},
    "github": {"user:email"},
    "google": {"openid", "email", "profile"},
}


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

    # The only query any route under test makes is the signed-in user lookup.
    async def db() -> AsyncSession:
        session = AsyncMock(spec=AsyncSession)
        result = MagicMock()
        result.scalar_one_or_none.return_value = SimpleNamespace(id=uuid4())
        session.execute.return_value = result
        return session

    @get("/test/sign-in", sync_to_thread=False)
    def sign_in(request: Request) -> str:
        request.session["user_id"] = str(uuid4())
        request.session[CSRF_SESSION_KEY] = secrets.token_urlsafe(16)
        return request.session[CSRF_SESSION_KEY]

    app = Litestar(
        route_handlers=[AuthController, sign_in],
        middleware=[CookieBackendConfig(secret=secrets.token_bytes(16)).middleware],
        template_config=TemplateConfig(directory=tmp_path, engine=JinjaTemplateEngine),
        dependencies={"db_session": Provide(db)},
    )
    with TestClient(app) as c:
        yield c


def test_discord_github_and_google_are_configured(prod_settings):
    assert prod_settings.auth.get_method_keys() == PROVIDERS
    assert list(prod_settings.auth.providers) == PROVIDERS
    assert prod_settings.auth.second_factors.get_method_keys() == []


def test_login_page_lists_the_three_providers(client):
    resp = client.get("/auth/login", follow_redirects=False)
    assert resp.status_code == 200
    assert resp.text.strip() == ",".join(PROVIDERS)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_login_redirects_to_the_provider(client, prod_settings, provider):
    resp = client.get(f"/auth/{provider}/login", follow_redirects=False)
    assert resp.status_code in (302, 303, 307)
    target = urlparse(resp.headers["location"])
    assert target.hostname == AUTHORIZE_HOSTS[provider]
    query = parse_qs(target.query)
    assert query["client_id"] == [prod_settings.auth.providers[provider].client_id]
    assert query["redirect_uri"] == [f"https://smarter.dev/auth/{provider}/callback"]
    assert set(query["scope"][0].split()) == SCOPES[provider]


@pytest.mark.parametrize("provider", PROVIDERS)
def test_callback_is_live(client, provider):
    # No state in the session, so the flow is rejected by the OAuth state
    # check — past the "is this method configured" gate the others fail at.
    resp = client.get(f"/auth/{provider}/callback?code=x&state=y", follow_redirects=False)
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
        "/auth/dummy-login",
    ],
)
def test_disabled_interactive_endpoints_404(client, path):
    assert client.post(path, json={}).status_code == 404


@pytest.mark.parametrize("path", ["/auth/passkeys/options", "/auth/passkeys/complete"])
def test_signed_in_user_cannot_enroll_a_passkey(client, path):
    token = client.get("/test/sign-in").text
    resp = client.post(path, data={CSRF_FIELD_NAME: token})
    assert resp.status_code == 404
    assert resp.json() == {"error": "passkey_not_configured"}


async def _security_page_context(
    passkeys: list, linked: list
) -> tuple[dict, list[str]]:
    """Run the security page handler against a mocked session; return its
    context and the SQL of each query it made."""
    user = SimpleNamespace(id=uuid4())
    results = []
    for rows in ([user], linked, passkeys):
        result = MagicMock()
        result.scalar_one_or_none.return_value = rows[0] if rows else None
        result.scalars.return_value.all.return_value = rows
        results.append(result)
    # The campaign sign-ups lookup: no linked login holds a contact.
    no_contacts = MagicMock()
    no_contacts.all.return_value = []
    results.append(no_contacts)
    db = AsyncMock(spec=AsyncSession)
    db.execute.side_effect = results
    request = MagicMock()
    request.session = {"user_id": str(user.id)}
    response = await AccountController.security_page.fn(None, request, db)
    queries = [str(call.args[0]) for call in db.execute.call_args_list]
    return response.context, queries


async def test_security_page_keeps_passkeys_while_sign_in_is_off(prod_settings):
    passkey = SimpleNamespace(
        id=uuid4(),
        display_name="Laptop",
        enrolled_at=datetime(2026, 9, 1, tzinfo=UTC),
        last_used_at=None,
    )
    github = SimpleNamespace(
        provider="github", provider_email=None, created_at=datetime(2026, 1, 1)
    )
    context, queries = await _security_page_context([passkey], [github])

    assert context["passkeys"] == [passkey]
    assert "factor_type" in queries[2]
    assert context["passkey_sign_in"] is False
    assert context["passkey_available"] is False
    assert context["sign_in_methods"] == PROVIDERS


def _render_security_page(**context) -> str:
    env = Environment(
        loader=ChoiceLoader(
            [
                DictLoader(
                    {
                        "account/_layout.html": (
                            "{% block account_content %}{% endblock %}"
                        )
                    }
                ),
                FileSystemLoader(REPO / "themes/smarterdev/templates"),
            ]
        )
    )
    env.globals.update(csp_nonce=lambda: "n", csrf_field=lambda: "")
    return " ".join(env.get_template("account/security.html").render(**context).split())


def test_security_page_copy_with_three_providers():
    html = _render_security_page(
        sign_in_methods=PROVIDERS,
        passkey_sign_in=False,
        passkey_available=False,
        linked_accounts=[
            SimpleNamespace(provider="discord", provider_email=None, created_at=None),
            SimpleNamespace(provider="github", provider_email=None, created_at=None),
            SimpleNamespace(provider="google", provider_email=None, created_at=None),
        ],
        passkeys=[
            SimpleNamespace(
                id=uuid4(), display_name="Laptop", enrolled_at=None, last_used_at=None
            )
        ],
    )
    assert "Sign in with any of these" in html
    assert "only for now" not in html
    assert "sign-in off for now" not in html
    assert "Passkey sign-in is off for now." in html
    assert "Laptop" in html
    assert "/account/security/passkeys/" in html
    assert "Add passkey" not in html
