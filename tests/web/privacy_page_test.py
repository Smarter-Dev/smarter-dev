"""The public privacy notice at /privacy (task #71).

Served through the real theme's ``base.html`` behind an empty cookie session
and with no database, so the page is proven to render for a visitor who is not
signed in. The notice text itself is pinned in ``tests/shared/privacy_notice_test.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment
from jinja2 import FileSystemLoader
from litestar import Litestar
from litestar.contrib.jinja import JinjaTemplateEngine
from litestar.middleware.session.client_side import CookieBackendConfig
from litestar.template.config import TemplateConfig
from litestar.testing import TestClient
from skrift.markdown import render_markdown

from smarter_dev.shared.config import get_settings
from smarter_dev.shared.privacy_notice import PRIVACY_PATH
from smarter_dev.shared.privacy_notice import short_version
from smarter_dev.web.privacy_controller import privacy_notice

REPO = Path(__file__).resolve().parents[2]
THEME = REPO / "themes" / "smarterdev"
CONTROLLER = "smarter_dev.web.privacy_controller:privacy_notice"


def _environment() -> Environment:
    environment = Environment(
        loader=FileSystemLoader(THEME / "templates"), autoescape=True
    )
    environment.globals.update(
        theme_url=lambda path: f"/theme/{path}",
        static_url=lambda path: f"/static/{path}",
        csp_nonce=lambda: "",
    )
    environment.filters["markdown"] = render_markdown
    return environment


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(get_settings(), "site_base_url", "https://smarter.dev")
    app = Litestar(
        route_handlers=[privacy_notice],
        middleware=[CookieBackendConfig(secret=b"0" * 16).middleware],
        template_config=TemplateConfig(
            instance=JinjaTemplateEngine.from_environment(_environment())
        ),
    )
    with TestClient(app) as c:
        yield c


def _text(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def test_the_notice_renders_without_signing_in(client):
    response = client.get(PRIVACY_PATH)

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "<h1" in response.text and "Privacy notice" in response.text


def test_the_page_shows_the_whole_notice(client):
    text = _text(client.get(PRIVACY_PATH).text)

    for heading in (
        "The short version",
        "What the Discord bot stores",
        "What the website stores",
        "Who else handles your data",
        "Deleting your data",
    ):
        assert heading in text
    assert "permanent" in text
    assert "Moderation history is kept so the server can stay safe" in text
    assert "anonymous usage and cost records" in text
    assert "DM an @admin on the Smarter Dev Discord server" in text
    assert 'href="mailto:admin@smarter.dev"' in client.get(PRIVACY_PATH).text


def test_the_short_version_on_the_page_is_the_one_the_command_quotes(client):
    text = _text(client.get(PRIVACY_PATH).text)
    for point in short_version():
        assert _text(render_markdown(point)) in text


def test_the_page_uses_the_site_layout_and_says_when_it_was_updated(client):
    html = client.get(PRIVACY_PATH).text

    assert 'name="viewport"' in html
    assert "/theme/css/pages/blog.css" in html
    assert 'class="post-body' in html
    assert "Last updated Oct 04, 2026" in html
    assert '<link rel="canonical" href="https://smarter.dev/privacy">' in html


@pytest.mark.parametrize("config", ["app.yaml", "app.development.yaml"])
def test_the_route_is_registered(config):
    controllers = yaml.safe_load((REPO / config).read_text())["controllers"]
    assert CONTROLLER in controllers


def test_the_site_footer_links_to_the_notice():
    footer = (THEME / "templates" / "_partials" / "footer.html").read_text()
    assert f'href="{PRIVACY_PATH}"' in footer
