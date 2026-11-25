"""Production public and Python-owned links use their respective hosts."""

from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
from unittest.mock import AsyncMock

import yaml

from smarter_dev.shared import config_links
from smarter_dev.shared import privacy_notice
from smarter_dev.shared.config import Settings
from smarter_dev.web import campaign_signups_api
from smarter_dev.web.billing import portal


def _production_settings():
    repo = Path(__file__).resolve().parents[2]
    configmap = yaml.safe_load((repo / "k8s/configmap.yaml").read_text())
    data = configmap["data"]
    return Settings(
        _env_file=None,
        environment="production",
        site_base_url=data["SITE_BASE_URL"],
        admin_base_url=data["ADMIN_BASE_URL"],
        api_secret_key="test-secret",
    )


def test_production_url_bases_route_public_and_admin_links(monkeypatch):
    settings = _production_settings()
    for base, host in (
        (settings.site_base_url, "smarter.dev"),
        (settings.admin_base_url, "admin.smarter.dev"),
    ):
        parsed = urlparse(base)
        assert parsed.scheme == "https"
        assert parsed.netloc == host
        assert parsed.path == ""

    monkeypatch.setattr(privacy_notice, "get_settings", lambda: settings)
    assert privacy_notice.privacy_url() == "https://smarter.dev/privacy"

    monkeypatch.setattr(config_links, "get_settings", lambda: settings)
    assert config_links.build_config_link_url(
        guild_id="1", channel_id="2", discord_user_id="3"
    ).startswith("https://admin.smarter.dev/admin/bot/configure/")


def test_login_callbacks_use_admin_host_and_return_to_either_host():
    from skrift.lib.redirects import is_safe_redirect_url

    repo = Path(__file__).resolve().parents[2]
    auth = yaml.safe_load((repo / "app.yaml").read_text())["auth"]
    assert auth["redirect_base_url"] == "https://admin.smarter.dev"
    domains = auth["allowed_redirect_domains"]
    for url in (
        "https://smarter.dev/account",
        "https://admin.smarter.dev/admin",
        "https://next.smarter.dev/",
    ):
        assert is_safe_redirect_url(url, domains), url
    assert not is_safe_redirect_url("https://evilsmarter.dev/", domains)


def test_dead_bot_admin_redirect_setting_is_gone():
    repo = Path(__file__).resolve().parents[2]
    # The configmap key stays one deploy so old pods can start; nothing reads it.
    for manifest in (repo / "k8s").glob("*.yaml"):
        if manifest.name == "configmap.yaml":
            continue
        assert "DISCORD_REDIRECT_URI" not in manifest.read_text(), manifest.name
    assert "discord_redirect_uri" not in Settings.model_fields
    assert not hasattr(Settings, "effective_discord_redirect_uri")


async def test_search_preview_url_stays_on_public_host(monkeypatch):
    from smarter_dev.web import search_previews

    settings = _production_settings()
    monkeypatch.setattr(search_previews, "get_settings", lambda: settings)
    session = SimpleNamespace(add=lambda row: None, flush=AsyncMock())
    reservation = await search_previews.create_search_preview(session, "q", token="t0k")
    assert reservation.url == "https://smarter.dev/ai/search/t0k"


async def test_production_confirmation_email_uses_public_host(monkeypatch):
    settings = _production_settings()
    send_email = AsyncMock()
    monkeypatch.setattr(campaign_signups_api, "get_settings", lambda: settings)
    monkeypatch.setattr(campaign_signups_api, "_build_confirmation_html", lambda url: url)
    monkeypatch.setattr(campaign_signups_api, "send_email", send_email)

    await campaign_signups_api._send_confirmation("user@example.test", "example-token")

    assert send_email.await_args.args[2] == (
        "https://smarter.dev/sudo/confirm?token=example-token"
    )


async def test_production_billing_portal_fallback_uses_public_host(monkeypatch):
    settings = _production_settings()
    create = AsyncMock(return_value=SimpleNamespace(customer_portal_url="https://polar.test"))

    class PolarContext:
        async def __aenter__(self):
            return SimpleNamespace(customer_sessions=SimpleNamespace(create_async=create))

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(portal, "get_settings", lambda: settings)
    monkeypatch.setattr(portal, "get_polar", PolarContext)

    assert await portal.create_portal_session("customer-id") == "https://polar.test"
    assert create.await_args.kwargs["request"]["return_url"] == (
        "https://smarter.dev/account/billing"
    )


def test_search_link_urls_stay_on_public_host(monkeypatch):
    from smarter_dev.web import dashboard_controller

    settings = _production_settings()
    monkeypatch.setattr(dashboard_controller, "get_settings", lambda: settings)
    link = SimpleNamespace(token="tok", open_addresses=False)
    state = dashboard_controller.link_state(link)
    assert state["url"] == "https://smarter.dev/s/tok?q=%s"


async def test_opensearch_description_stays_on_public_host(monkeypatch):
    from smarter_dev.web import search_link_controller as slc

    settings = _production_settings()
    monkeypatch.setattr(slc, "get_settings", lambda: settings)
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=SimpleNamespace(token="tok"))
    )
    response = await slc.SearchLinkController.opensearch.fn(
        object(), session, "tok"
    )
    body = response.content
    assert "https://smarter.dev/s/tok" in body
    assert "admin.smarter.dev" not in body


async def test_checkout_success_url_stays_on_public_host(monkeypatch):
    from smarter_dev.web import controllers

    settings = _production_settings()
    monkeypatch.setattr(controllers, "get_settings", lambda: settings)
    monkeypatch.setattr(
        controllers.flags_service, "is_enabled", AsyncMock(return_value=True)
    )
    create = AsyncMock(return_value="https://polar.test/checkout")
    monkeypatch.setattr(controllers, "create_checkout_session", create)

    class _Request:
        session = {"user_id": "u1"}

        async def form(self):
            return {"role": "hacker"}

    session = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(
                scalar_one_or_none=lambda: SimpleNamespace(id="u1")
            )
        )
    )
    await controllers.SudoController.checkout.fn(object(), _Request(), session)
    assert create.await_args.kwargs["success_url"] == (
        "https://smarter.dev/sudo/checkout/success"
    )
