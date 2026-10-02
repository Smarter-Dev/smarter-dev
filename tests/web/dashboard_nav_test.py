"""The dashboard sidebar Web search and Chat share.

``/dashboard`` opens the Quick chat for anyone with Chat and the new-search view
for everyone else; ``/dashboard/search`` is the new-search view for all. Both
kinds of page draw ``dashboard/_nav.html``: the section for what is on screen
open, the other shut, and the selected history row marked current, so a deep
link lands on its item rather than on an empty view.
"""

from __future__ import annotations

import re
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from jinja2 import ChoiceLoader
from jinja2 import DictLoader
from jinja2 import Environment
from jinja2 import FileSystemLoader
from litestar import Litestar
from litestar._asgi.routing_trie.traversal import parse_path_to_route
from litestar.di import Provide
from litestar.response import Redirect
from litestar.response import Template

from smarter_dev.web import dashboard_controller
from smarter_dev.web import dashboard_nav
from smarter_dev.web import search_link_controller as links
from smarter_dev.web.chat import controller as chat_controller
from smarter_dev.web.models import WebSearchRun
from tests.web.quick_chat_surface_test import _entitle
from tests.web.quick_chat_surface_test import _page_context
from tests.web.quick_chat_surface_test import _render_chat_page
from tests.web.quick_chat_surface_test import _request
from tests.web.quick_chat_surface_test import _seed_conversation
from tests.web.quick_chat_surface_test import _seed_user

_THEME = Path(__file__).parents[2] / "themes" / "smarterdev"
_CONTROLLER = object.__new__(dashboard_controller.DashboardController)


def _page_request(user_id, path: str = "/dashboard"):
    request = _request(user_id)
    request.url = SimpleNamespace(path=path)
    return request


def _no_chat(monkeypatch):
    _entitle(monkeypatch, dashboard_controller, chat_controller, roles=())


def _chat(monkeypatch):
    _entitle(monkeypatch, dashboard_controller, chat_controller)


async def _seed_search(db_session, user, request="why is my build slow") -> WebSearchRun:
    run = WebSearchRun(
        owner_user_id=user.id,
        submission_key=uuid4().hex,
        request=request,
        status="done",
        queries=[],
        results=[{"url": "https://example.test", "relevant": True}],
        ranked=True,
        usage={},
        version=1,
        attempt_count=1,
    )
    db_session.add(run)
    await db_session.commit()
    return run


def _render_dashboard(context: dict) -> str:
    stub_base = (
        "{% block page_type %}{% endblock %}"
        "{% block page_title %}{% endblock %}"
        "{% block page_css %}{% endblock %}"
        "{% block head %}{% endblock %}"
        "{% block content %}{% endblock %}"
        "{% block page_scripts %}{% endblock %}"
    )
    environment = Environment(
        loader=ChoiceLoader(
            [DictLoader({"base.html": stub_base}), FileSystemLoader(_THEME / "templates")]
        ),
        autoescape=True,
    )
    environment.globals["theme_url"] = lambda path: "/theme/" + path
    environment.globals["csrf_field"] = lambda: ""
    return environment.get_template("dashboard/index.html").render(**context)


def _section(html: str, name: str) -> str:
    match = re.search(
        rf'<section class="ud-nav-section" data-ud-section="{name}".*?</section>', html, re.S
    )
    assert match, f"no {name} section"
    return match.group(0)


def _is_open(section: str) -> bool:
    head = section.split(">", 1)[0]
    toggle = re.search(r'data-ud-toggle aria-expanded="(true|false)"', section).group(1)
    body = re.search(r'<div class="ud-nav-body"[^>]*>', section).group(0)
    opened = "data-open" in head
    assert opened == (toggle == "true") == ("hidden" not in body), section[:400]
    return opened


class TestDashboardRoutes:
    @pytest.mark.asyncio
    async def test_the_dashboard_opens_the_quick_chat_for_chat_users(
        self, db_session, monkeypatch
    ):
        _chat(monkeypatch)
        user = await _seed_user(db_session)

        response = await dashboard_controller.DashboardController.home.fn(
            _CONTROLLER, _page_request(user.id), db_session
        )

        assert isinstance(response, Template)
        assert response.template_name == "chat/index.html"
        assert response.context["conversation"].chat_mode == "quick"
        nav = response.context["dashboard_nav"]
        assert nav["open"] == "chat" and nav["chat"] and nav["chat_actions"]

    @pytest.mark.asyncio
    async def test_without_chat_the_dashboard_is_the_new_search(self, db_session, monkeypatch):
        _no_chat(monkeypatch)
        user = await _seed_user(db_session)

        response = await dashboard_controller.DashboardController.home.fn(
            _CONTROLLER, _page_request(user.id), db_session
        )

        assert response.template_name == "dashboard/index.html"
        assert response.context["dashboard_state"]["view"] == "home"
        assert response.context["dashboard_state"]["chat_home"] is False
        nav = response.context["dashboard_nav"]
        assert nav["open"] == "search" and nav["chat"] is False

    @pytest.mark.asyncio
    async def test_the_new_search_url_works_with_chat(self, db_session, monkeypatch):
        _chat(monkeypatch)
        user = await _seed_user(db_session)
        await _seed_conversation(db_session, user, title="Planning")

        response = await dashboard_controller.DashboardController.new_search_page.fn(
            _CONTROLLER, _page_request(user.id, "/dashboard/search"), db_session
        )

        assert response.template_name == "dashboard/index.html"
        state = response.context["dashboard_state"]
        assert state["view"] == "home" and state["chat_home"] is True
        nav = response.context["dashboard_nav"]
        assert nav["open"] == "search" and nav["tool"] == "search" and nav["chat"]
        # The shut Chat section still lists the chats, without row menus.
        assert not nav["chat_actions"]
        assert [c.title for c in response.context["conversations"]] == ["Planning"]

    @pytest.mark.asyncio
    async def test_a_search_deep_link_marks_its_row(self, db_session, monkeypatch):
        _chat(monkeypatch)
        user = await _seed_user(db_session)
        run = await _seed_search(db_session, user)

        response = await dashboard_controller.DashboardController.search_page.fn(
            _CONTROLLER, _page_request(user.id), db_session, run.id
        )

        state = response.context["dashboard_state"]
        assert state["view"] == "search" and state["search"]["id"] == str(run.id)
        nav = response.context["dashboard_nav"]
        assert nav["search_id"] == str(run.id) and nav["tool"] is None
        html = _render_dashboard(response.context)
        assert _is_open(_section(html, "search"))
        assert not _is_open(_section(html, "chat"))
        assert re.search(
            rf'href="/dashboard/search/{run.id}" data-dashboard-link aria-current="page"', html
        )

    @pytest.mark.asyncio
    async def test_signed_out_visitors_are_sent_to_log_in(self, db_session):
        request = SimpleNamespace(session={}, url=SimpleNamespace(path="/dashboard/search"))

        response = await dashboard_controller.DashboardController.new_search_page.fn(
            _CONTROLLER, request, db_session
        )

        assert isinstance(response, Redirect)
        assert response.url == "/auth/login?next=%2Fdashboard%2Fsearch"

    def test_the_new_search_route_does_not_shadow_a_search(self):
        async def provide_session() -> None:
            return None

        app = Litestar(
            route_handlers=[dashboard_controller.DashboardController],
            dependencies={"db_session": Provide(provide_session)},
            openapi_config=None,
        )
        router = app.asgi_router

        def resolve(path: str):
            return parse_path_to_route(
                method="GET",
                mount_paths_regex=router._mount_paths_regex,
                mount_routes=router._mount_routes,
                path=path,
                plain_routes=router._plain_routes,
                root_node=router.root_route_map_node,
            )[1]

        assert resolve("/dashboard").handler_name.endswith("home")
        assert resolve("/dashboard/search").handler_name.endswith("new_search_page")
        assert resolve(f"/dashboard/search/{uuid4()}").handler_name.endswith("search_page")


class TestSidebarMarkup:
    @pytest.mark.asyncio
    async def test_a_chat_page_opens_chat_and_shuts_search(self, db_session):
        user = await _seed_user(db_session)
        quick = await _seed_conversation(db_session, user, chat_mode="quick", title="Quick chat")
        await _seed_conversation(db_session, user, title="Planning")
        run = await _seed_search(db_session, user, request="postgres count slow")
        context = await _page_context(
            db_session,
            quick,
            **await chat_controller.rail_context(db_session, user.id),
            **await chat_controller._nav_context(db_session, user.id),
        )

        html = _render_chat_page(context)

        search, chat = _section(html, "search"), _section(html, "chat")
        assert html.index('data-ud-section="search"') < html.index('data-ud-section="chat"')
        assert _is_open(chat) and not _is_open(search)
        # The open chat section keeps the rail's actions and the pinned Quick chat.
        assert 'class="p-btn is-accent chat-new"' in chat
        assert "data-history-menu" in chat
        assert re.search(r'<a href="/chat" aria-current="page" data-history-title', chat)
        # Searches are listed, and leave the chat page with a full load.
        assert f'href="/dashboard/search/{run.id}" data-sk-no-spa' in search
        assert "postgres count slow" in search and "1 relevant result" in search
        assert 'aria-controls="ud-nav-search"' in search and 'id="ud-nav-search"' in search

    @pytest.mark.asyncio
    async def test_a_conversation_deep_link_marks_its_row(self, db_session):
        user = await _seed_user(db_session)
        planning = await _seed_conversation(db_session, user, title="Planning")
        context = await _page_context(
            db_session,
            planning,
            **await chat_controller.rail_context(db_session, user.id),
            **await chat_controller._nav_context(db_session, user.id),
        )

        html = _render_chat_page(context)

        chat = _section(html, "chat")
        assert _is_open(chat)
        assert re.search(rf'<a href="/chat/{planning.id}" aria-current="page"', chat)

    @pytest.mark.asyncio
    async def test_an_archived_deep_link_opens_its_drawer(self, db_session):
        user = await _seed_user(db_session)
        old = await _seed_conversation(db_session, user, title="Filed")
        old.archived_at = datetime.now(UTC)
        await db_session.commit()
        context = await _page_context(
            db_session,
            old,
            **await chat_controller.rail_context(db_session, user.id),
            **await chat_controller._nav_context(db_session, user.id),
        )

        html = _render_chat_page(context)

        assert '<details class="chat-history-archive" data-history-archive open>' in html

    @pytest.mark.asyncio
    async def test_the_search_page_lists_chats_without_their_menus(
        self, db_session, monkeypatch
    ):
        _chat(monkeypatch)
        user = await _seed_user(db_session)
        await _seed_conversation(db_session, user, chat_mode="quick", title="Quick chat")
        planning = await _seed_conversation(db_session, user, title="Planning")

        response = await dashboard_controller.DashboardController.new_search_page.fn(
            _CONTROLLER, _page_request(user.id, "/dashboard/search"), db_session
        )
        html = _render_dashboard(response.context)

        search, chat = _section(html, "search"), _section(html, "chat")
        assert _is_open(search) and not _is_open(chat)
        assert re.search(r'href="/dashboard/search" data-ud-entry data-tool="search" data-dashboard-link aria-current="page"', search)
        assert f'href="/chat/{planning.id}"' in chat and "data-history-menu" not in chat
        # Chat is another page: its links skip spa-nav's fetch.
        assert 'href="/dashboard" data-ud-entry data-tool="chat" data-sk-no-spa' in chat
        assert 'href="/chat/new" class="p-btn is-accent chat-new" data-sk-no-spa' in chat

    @pytest.mark.asyncio
    async def test_without_chat_there_is_no_chat_section(self, db_session, monkeypatch):
        _no_chat(monkeypatch)
        user = await _seed_user(db_session)

        response = await dashboard_controller.DashboardController.home.fn(
            _CONTROLLER, _page_request(user.id), db_session
        )
        html = _render_dashboard(response.context)

        assert _is_open(_section(html, "search"))
        assert 'data-ud-section="chat"' not in html
        assert "Searches you run show up here." in html

    def test_the_new_search_links_point_at_the_new_search_url(self):
        template = (_THEME / "templates" / "dashboard" / "index.html").read_text()
        assert 'href="/dashboard"' not in template
        assert template.count('class="ud-back" href="/dashboard/search"') == 2

    def test_the_sidebar_files_are_in_the_client_build(self):
        for name in dashboard_controller.CLIENT_FILES:
            assert (_THEME / name).is_file(), name
        assert "templates/dashboard/_nav.html" in dashboard_controller.CLIENT_FILES
        assert "static/js/dashboard-nav.js" in dashboard_controller.CLIENT_FILES

    def test_a_shut_body_cannot_be_reshown_by_its_display_rule(self):
        # .ud-nav-body sets display: flex, which beats the hidden attribute.
        css = (_THEME / "static" / "css" / "pages" / "dashboard-nav.css").read_text()
        assert ".ud-nav [hidden] { display: none !important }" in css


class TestRecentMeta:
    def test_it_reads_like_the_search_page(self):
        now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
        row = {"status": "done", "active": False, "relevant": 3}

        def at(delta):
            return {**row, "created_at": (now - delta).isoformat()}

        assert dashboard_nav.recent_meta(at(timedelta(seconds=5)), now) == "3 relevant results · just now"
        assert dashboard_nav.recent_meta(at(timedelta(minutes=5)), now) == "3 relevant results · 5 min ago"
        assert dashboard_nav.recent_meta(at(timedelta(hours=2)), now) == "3 relevant results · 2 h ago"
        assert dashboard_nav.recent_meta(at(timedelta(days=3)), now) == "3 relevant results · Sep 28"
        assert dashboard_nav.recent_meta({**at(timedelta(0)), "relevant": 1}, now).startswith("1 relevant result ·")
        assert dashboard_nav.recent_meta({**at(timedelta(0)), "status": "error"}, now) == "Failed · just now"
        assert dashboard_nav.recent_meta(
            {**at(timedelta(0)), "status": "searching", "active": True}, now
        ) == "Searching… · just now"
        assert dashboard_nav.recent_meta(
            {**at(timedelta(0)), "status": "answering", "active": True}, now
        ) == "Writing an answer… · just now"


async def test_an_empty_search_link_opens_the_new_search(db_session):
    request = SimpleNamespace(session={}, headers={}, scope={"client": ("203.0.113.7", 5000)})
    link = SimpleNamespace(token="t", open_addresses=False, owner_user_id=None)

    class _Session:
        async def scalar(self, _query):
            return link

    response = await links.SearchLinkController.run.fn(None, request, _Session(), "t", "  ")

    assert isinstance(response, Redirect)
    assert response.url == "/dashboard/search"
