"""Production public and Python-owned links use their respective hosts."""

from pathlib import Path

import yaml

from smarter_dev.shared import config_links
from smarter_dev.shared import privacy_notice
from smarter_dev.shared.config import Settings


def test_production_url_bases_route_public_and_admin_links(monkeypatch):
    repo = Path(__file__).resolve().parents[2]
    configmap = yaml.safe_load((repo / "k8s/configmap.yaml").read_text())
    data = configmap["data"]
    settings = Settings(
        _env_file=None,
        environment="production",
        site_base_url=data["SITE_BASE_URL"],
        admin_base_url=data["ADMIN_BASE_URL"],
        discord_redirect_uri=None,
        api_secret_key="test-secret",
    )
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
