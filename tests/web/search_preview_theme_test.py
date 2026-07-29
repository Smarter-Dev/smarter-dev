"""Search previews render through the active smarterdev Skrift theme."""

from pathlib import Path

from jinja2 import ChoiceLoader
from jinja2 import DictLoader
from jinja2 import Environment
from jinja2 import FileSystemLoader

_PROJECT_ROOT = Path(__file__).parents[2]
_THEME_ROOT = _PROJECT_ROOT / "themes" / "smarterdev"
_TEMPLATE = _THEME_ROOT / "templates" / "ai" / "search_preview.html"
_CSS = _THEME_ROOT / "static" / "css" / "pages" / "search-preview.css"
_LEGACY_TEMPLATE = _PROJECT_ROOT / "templates" / "ai" / "search_preview.html"


def test_search_preview_is_an_active_theme_template():
    source = _TEMPLATE.read_text()
    assert '{% extends "base.html" %}' in source
    assert "theme_url('css/pages/search-preview.css')" in source
    assert not _LEGACY_TEMPLATE.exists()


def test_results_are_plain_rows_not_cards():
    source = _TEMPLATE.read_text()
    css = _CSS.read_text()
    assert 'class="sp-result"' in source
    assert "card" not in source.lower()
    result_rule = css.split(".sp-result {", 1)[1].split("}", 1)[0]
    assert "border" not in result_rule
    assert "background" not in result_rule


def test_theme_template_renders_sanitized_snippet_html():
    stub_base = (
        "{% block head %}{% endblock %}"
        "{% block page_css %}{% endblock %}"
        "{% block content %}{% endblock %}"
    )
    environment = Environment(
        loader=ChoiceLoader(
            [
                DictLoader({"base.html": stub_base}),
                FileSystemLoader(_THEME_ROOT / "templates"),
            ]
        ),
        autoescape=True,
    )
    environment.globals["theme_url"] = lambda path: f"/theme/{path}"
    rendered = environment.get_template("ai/search_preview.html").render(
        query="latest models",
        status="ready",
        results=[
            {
                "safe_url": "https://example.com/models",
                "url": "https://example.com/models",
                "domain": "example.com",
                "display_url": "/models",
                "title": "Model release notes",
                "description_html": "New <strong>model</strong>.",
            }
        ],
    )

    assert "latest models" in rendered
    assert "example.com" in rendered
    assert "<strong>model</strong>" in rendered
    assert "snapshot" not in rendered.lower()
