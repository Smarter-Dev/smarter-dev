"""Production public and Python-owned links use their respective hosts."""

from pathlib import Path
from types import SimpleNamespace
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
        discord_redirect_uri=None,
        api_secret_key="test-secret",
    )


def test_production_url_bases_route_public_and_admin_links(monkeypatch):
    settings = _production_settings()
    assert settings.site_base_url == "https://smarter.dev"
    assert settings.admin_base_url == "https://admin.smarter.dev"
    assert settings.site_base_url != settings.admin_base_url

    monkeypatch.setattr(privacy_notice, "get_settings", lambda: settings)
    monkeypatch.setattr(config_links, "get_settings", lambda: settings)
    assert privacy_notice.privacy_url() == "https://smarter.dev/privacy"
    assert config_links.build_config_link_url(
        guild_id="1", channel_id="2", discord_user_id="3"
    ).startswith("https://admin.smarter.dev/admin/bot/configure/")
    assert settings.effective_discord_redirect_uri == (
        "https://admin.smarter.dev/bot-admin/auth/discord/callback"
    )


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
