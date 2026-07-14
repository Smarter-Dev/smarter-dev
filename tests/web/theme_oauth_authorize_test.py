"""Theme override for the OAuth2 consent screen.

The hub consent page (/oauth/authorize) must render with the smarterdev
theme rather than falling back to skrift's built-in unstyled template.
The controller renders "oauth/authorize.html" with: client_id,
display_name, scopes, scope_descriptions (skrift/controllers/oauth2.py).
"""

from pathlib import Path

import pytest
from jinja2 import ChoiceLoader
from jinja2 import DictLoader
from jinja2 import Environment
from jinja2 import FileSystemLoader

THEME_TEMPLATES_DIR = Path(__file__).parents[2] / "themes" / "smarterdev" / "templates"
AUTHORIZE_TEMPLATE = THEME_TEMPLATES_DIR / "oauth" / "authorize.html"


@pytest.fixture(scope="module")
def template_source() -> str:
    assert AUTHORIZE_TEMPLATE.exists(), (
        "smarterdev theme must override oauth/authorize.html so the consent "
        "screen renders with site styling instead of skrift's built-in template"
    )
    return AUTHORIZE_TEMPLATE.read_text()


@pytest.fixture(scope="module")
def rendered_consent_page(template_source: str) -> str:
    """Render the override against a stub base to validate its own markup."""
    stub_base = "{% block page_css %}{% endblock %}{% block content %}{% endblock %}"
    environment = Environment(
        loader=ChoiceLoader([
            DictLoader({"base.html": stub_base}),
            FileSystemLoader(THEME_TEMPLATES_DIR),
        ]),
        autoescape=True,
    )
    environment.globals["csrf_field"] = lambda: '<input type="hidden" name="csrf_token" value="test-token">'
    environment.globals["csp_nonce"] = lambda: "test-nonce"
    return environment.get_template("oauth/authorize.html").render(
        client_id="zv-client",
        display_name="RunHacks.sh",
        scopes=["openid", "profile", "email"],
        scope_descriptions=[
            {"name": "openid", "description": "Verify your identity"},
            {"name": "profile", "description": "Access your name and picture"},
            {"name": "email", "description": "Access your email address"},
        ],
    )


def test_extends_theme_base(template_source: str):
    assert '{% extends "base.html" %}' in template_source


def test_consent_form_posts_back_with_csrf(template_source: str):
    assert 'method="post"' in template_source
    assert 'action="/oauth/authorize"' in template_source
    assert "csrf_field()" in template_source


def test_renders_client_display_name(rendered_consent_page: str):
    assert "RunHacks.sh" in rendered_consent_page


def test_renders_requested_scope_descriptions(rendered_consent_page: str):
    assert "Verify your identity" in rendered_consent_page
    assert "Access your name and picture" in rendered_consent_page
    assert "Access your email address" in rendered_consent_page


def test_renders_allow_and_deny_actions(rendered_consent_page: str):
    assert 'name="action" value="allow"' in rendered_consent_page
    assert 'name="action" value="deny"' in rendered_consent_page


def test_falls_back_to_plain_scopes_without_descriptions(template_source: str):
    stub_base = "{% block page_css %}{% endblock %}{% block content %}{% endblock %}"
    environment = Environment(
        loader=ChoiceLoader([
            DictLoader({"base.html": stub_base}),
            FileSystemLoader(THEME_TEMPLATES_DIR),
        ]),
        autoescape=True,
    )
    environment.globals["csrf_field"] = lambda: ""
    environment.globals["csp_nonce"] = lambda: "test-nonce"
    rendered = environment.get_template("oauth/authorize.html").render(
        client_id="zv-client",
        display_name="",
        scopes=["openid"],
        scope_descriptions=[],
    )
    assert "openid" in rendered
    assert "zv-client" in rendered
