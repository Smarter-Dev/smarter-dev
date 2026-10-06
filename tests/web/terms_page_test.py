"""The public Terms of Service at /terms (task #93).

Served through the real theme's ``base.html`` behind an empty cookie session
and with no database, so the page is proven to render for a visitor who is not
signed in. The terms text itself is pinned in ``tests/shared/terms_of_service_test.py``.
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
from smarter_dev.shared.terms_of_service import TERMS_PATH
from smarter_dev.web import terms_controller
from smarter_dev.web.terms_controller import terms_of_service

REPO = Path(__file__).resolve().parents[2]
THEME = REPO / "themes" / "smarterdev"
CONTROLLER = "smarter_dev.web.terms_controller:terms_of_service"


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
        route_handlers=[terms_of_service],
        middleware=[CookieBackendConfig(secret=b"0" * 16).middleware],
        template_config=TemplateConfig(
            instance=JinjaTemplateEngine.from_environment(_environment())
        ),
    )
    with TestClient(app) as c:
        yield c


def _text(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def test_the_terms_render_without_signing_in(client):
    response = client.get(TERMS_PATH)

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "<h1" in response.text and "Terms of Service" in response.text


def test_the_page_shows_the_whole_terms(client):
    html = client.get(TERMS_PATH).text
    text = _text(html)

    for heading in (
        "Who we are and what these terms cover",
        "Eligibility",
        "Your account",
        "Acceptable use",
        "Content you post",
        "The AI assistant",
        "Paid subscriptions",
        "Moderation and termination",
        "Disclaimer and limitation of liability",
        "Privacy",
        "Changes",
        "Governing law",
        "Contact",
    ):
        assert f">{heading}</h2>" in html
    assert "Smarter Dev LLC (\u201cwe\u201d)" in text
    assert "the State of &lt;STATE&gt;" in html
    assert 'href="/privacy"' in html
    assert 'href="mailto:admin@smarter.dev"' in html


def test_the_page_renders_the_markdown_file_not_a_copy(client, monkeypatch):
    # Negative control: swap the wording and the page must follow it.
    monkeypatch.setattr(
        terms_controller,
        "terms_markdown",
        lambda: "## Sentinel heading\n\nSentinel body.",
    )
    html = client.get(TERMS_PATH).text

    assert "Sentinel heading</h2>" in html
    assert "Acceptable use" not in html


def test_the_page_uses_the_site_layout_and_says_when_it_was_updated(client):
    html = client.get(TERMS_PATH).text

    assert 'name="viewport"' in html
    assert "/theme/css/pages/blog.css" in html
    assert 'class="post-body' in html
    assert "Last updated Oct 06, 2026" in html
    assert '<link rel="canonical" href="https://smarter.dev/terms">' in html


@pytest.mark.parametrize("config", ["app.yaml", "app.development.yaml"])
def test_the_route_is_registered(config):
    controllers = yaml.safe_load((REPO / config).read_text())["controllers"]
    assert CONTROLLER in controllers


def test_the_site_footer_links_to_the_terms_next_to_privacy():
    footer = (THEME / "templates" / "_partials" / "footer.html").read_text()
    assert (
        '<a href="/privacy">Privacy</a>\n        <a href="/terms">Terms</a>' in footer
    )
