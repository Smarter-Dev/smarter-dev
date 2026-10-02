"""The site navigation in ``_partials/masthead.html``.

Chat is reached from the dashboard sidebar, not the site navigation, so a chat
page marks Dashboard as the section it sits in. A shareable Resources answer is
also served under ``/chat``, and it marks Resources.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import Environment
from jinja2 import FileSystemLoader

_THEME = Path(__file__).parents[2] / "themes" / "smarterdev"


def _render(path: str, *, signed_in: bool = True, **context) -> str:
    environment = Environment(
        loader=FileSystemLoader(_THEME / "templates"), autoescape=True
    )
    environment.globals["csp_nonce"] = lambda: ""
    request = SimpleNamespace(url=SimpleNamespace(path=path), session={})
    user = SimpleNamespace(id=1) if signed_in else None
    return environment.get_template("_partials/masthead.html").render(
        request=request, user=user, **context
    )


def _links(html: str) -> list[str]:
    block = re.search(r'<div class="nav-links">(.*?)</div>', html, re.S).group(1)
    return re.findall(r">([^<]+)</a>", block)


def _active(html: str) -> list[str]:
    return re.findall(r'class="active">([^<]+)</a>', html)


@pytest.mark.parametrize("signed_in", [True, False])
def test_chat_is_not_in_the_site_navigation(signed_in):
    html = _render("/", signed_in=signed_in)

    assert "Chat" not in _links(html)
    assert 'href="/chat"' not in html
    assert ("Dashboard" in _links(html)) is signed_in


@pytest.mark.parametrize(
    "path", ["/chat", "/chat/new", "/chat/0b8f2c1e-1111-4222-8333-444455556666"]
)
def test_a_chat_page_marks_dashboard(path):
    assert _active(_render(path, mode="chat")) == ["Dashboard"]


def test_a_resources_answer_marks_resources():
    html = _render("/chat/0b8f2c1e-1111-4222-8333-444455556666", mode="resources")

    assert _active(html) == ["Resources"]


@pytest.mark.parametrize(
    ("path", "section"),
    [
        ("/dashboard", "Dashboard"),
        ("/dashboard/search", "Dashboard"),
        ("/resources", "Resources"),
        ("/blog/a-post", "Blog"),
        ("/sudo", "sudo"),
    ],
)
def test_other_pages_mark_their_own_section(path, section):
    assert _active(_render(path)) == [section]


def test_a_path_that_only_starts_with_chat_marks_nothing():
    assert _active(_render("/chatter")) == []
