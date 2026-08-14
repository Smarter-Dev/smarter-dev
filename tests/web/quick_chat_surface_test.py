"""The Quick chat front door: its route, its rail entry, and its dividers.

Three things have to hold together here. ``/chat/quick`` is a fixed URL that
gets-or-creates one perpetual conversation per person, so visiting it twice must
not leave two. The rail pins that conversation instead of filing it by recency.
And the thread dividers are rendered from a payload the page template and the
reconcile API both read, so the two cannot draw different streams.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import yaml
from jinja2 import ChoiceLoader
from jinja2 import DictLoader
from jinja2 import Environment
from jinja2 import FileSystemLoader
from litestar import Litestar
from litestar._asgi.routing_trie.traversal import parse_path_to_route
from litestar.di import Provide
from litestar.exceptions import HTTPException
from skrift.auth.session_keys import SESSION_USER_ID
from skrift.db.models.user import User
from skrift.forms.core import CSRF_SESSION_KEY
from sqlalchemy import func
from sqlalchemy import select

from smarter_dev.web.chat import api as chat_api
from smarter_dev.web.chat import controller as chat_controller
from smarter_dev.web.chat.api import ChatApiController
from smarter_dev.web.chat.api import ConversationUpdateBody
from smarter_dev.web.chat.settings import ensure_settings
from smarter_dev.web.models import WebChatConversation
from smarter_dev.web.models import WebChatMessage
from smarter_dev.web.models import WebChatThread
from smarter_dev.web.models import WebChatTurn

_PROJECT_ROOT = Path(__file__).parents[2]
_THEME_TEMPLATES = _PROJECT_ROOT / "themes" / "smarterdev" / "templates"
_CHAT_CSS = _PROJECT_ROOT / "themes" / "smarterdev" / "static" / "css" / "pages"
_CHAT_JS = _PROJECT_ROOT / "themes" / "smarterdev" / "static" / "js" / "chat.js"

_QUICK_HANDLER = "smarter_dev.web.chat.controller:chat_quick"
_UUID_HANDLER = "smarter_dev.web.chat.controller:chat_conversation"


def _request(user_id, *, token: str = "csrf-token"):
    return SimpleNamespace(
        session={SESSION_USER_ID: str(user_id), CSRF_SESSION_KEY: token},
        headers={"X-CSRF-Token": token},
    )


async def _seed_user(db_session) -> User:
    """Skrift's User table is not in this project's metadata; create it here."""
    connection = await db_session.connection()
    await connection.run_sync(
        lambda sync_connection: User.metadata.create_all(sync_connection)
    )
    user = User(email=f"{uuid4().hex}@example.test", name="Chat User", is_active=True)
    db_session.add(user)
    await db_session.commit()
    await ensure_settings(db_session)
    return user


def _entitle(monkeypatch, *modules, roles=("sudo-rw",)):
    async def permissions(*_args, **_kwargs):
        return set(roles)

    for module in modules:
        monkeypatch.setattr(module, "get_user_permissions", permissions)


async def _seed_conversation(
    db_session, user, *, chat_mode: str = "standard", title: str = "New Chat"
) -> WebChatConversation:
    conversation = WebChatConversation(
        owner_user_id=user.id,
        intelligence_mode="efficient",
        selected_model_key="gpt-5-6-luna",
        reasoning_level="medium",
        title=title,
        status="idle",
        chat_mode=chat_mode,
    )
    db_session.add(conversation)
    await db_session.commit()
    return conversation


async def _seed_exchanges(db_session, conversation, count: int) -> None:
    """``count`` user/assistant pairs, so sequences run 1..2*count."""
    for index in range(count):
        turn = WebChatTurn(
            conversation_id=conversation.id,
            sequence=index * 2 + 1,
            submission_key=f"submission-{index}",
            response_version_group=uuid4(),
            response_sequence=index * 2 + 2,
            model_key=conversation.selected_model_key,
            reasoning_level=conversation.reasoning_level,
            status="succeeded",
        )
        db_session.add(turn)
        await db_session.flush()
        db_session.add(
            WebChatMessage(
                conversation_id=conversation.id,
                turn_id=turn.id,
                sequence=index * 2 + 1,
                role="user",
                content=f"question {index + 1}",
            )
        )
        db_session.add(
            WebChatMessage(
                conversation_id=conversation.id,
                turn_id=turn.id,
                sequence=index * 2 + 2,
                role="assistant",
                content=f"reply {index + 1}",
            )
        )
    await db_session.commit()


async def _seed_threads(db_session, conversation, starts: list[int]) -> None:
    """Boundaries as they are really written: one row per line actually drawn.

    Nothing writes a row for the thread a Quick chat opens in, so the first row
    a conversation has is a boundary with messages above it like any other.
    """
    for index, start in enumerate(starts):
        db_session.add(
            WebChatThread(
                conversation_id=conversation.id,
                sequence=index + 1,
                start_sequence=start,
                title=f"Subject {index + 1}",
                origin="agent",
                reason=None,
            )
        )
    await db_session.commit()


async def _quick_chat_count(db_session, user_id) -> int:
    return await db_session.scalar(
        select(func.count())
        .select_from(WebChatConversation)
        .where(
            WebChatConversation.owner_user_id == user_id,
            WebChatConversation.chat_mode == "quick",
        )
    )


def _render_chat_page(context: dict) -> str:
    """Render the real chat template over a stub ``base.html``."""
    stub_base = (
        "{% block page_type %}{% endblock %}"
        "{% block page_title %}{% endblock %}"
        "{% block page_css %}{% endblock %}"
        "{% block content %}{% endblock %}"
        "{% block page_scripts %}{% endblock %}"
    )
    environment = Environment(
        loader=ChoiceLoader(
            [
                DictLoader({"base.html": stub_base}),
                FileSystemLoader(_THEME_TEMPLATES),
            ]
        ),
        autoescape=True,
    )
    environment.globals["theme_url"] = lambda path: "/theme/" + path
    environment.globals["csrf_field"] = lambda: ""
    return environment.get_template("chat/index.html").render(**context)


async def _page_context(db_session, conversation, **extra) -> dict:
    context = {
        "conversation": conversation,
        "mode": "chat",
        "settings": await ensure_settings(db_session),
        "catalog_models": [],
        "model_reasoning_levels": [],
        "versions": {},
        "ultra_chat": False,
        **await chat_controller._chat_context(db_session, conversation),
    }
    context.update(extra)
    return context


class TestQuickChatRoute:
    @pytest.mark.asyncio
    async def test_the_first_visit_creates_one_quick_chat(
        self, db_session, monkeypatch
    ):
        user = await _seed_user(db_session)
        _entitle(monkeypatch, chat_controller)

        response = await chat_controller.chat_quick.fn(_request(user.id), db_session)

        conversation = response.context["conversation"]
        assert conversation.chat_mode == "quick"
        assert conversation.title == "Quick chat"
        assert conversation.title_is_custom is True
        assert conversation.status == "idle"
        assert conversation.next_sequence == 1
        assert response.context["quick_chat"] is True
        assert await _quick_chat_count(db_session, user.id) == 1

    @pytest.mark.asyncio
    async def test_a_second_visit_reuses_it(self, db_session, monkeypatch):
        user = await _seed_user(db_session)
        _entitle(monkeypatch, chat_controller)

        first = await chat_controller.chat_quick.fn(_request(user.id), db_session)
        second = await chat_controller.chat_quick.fn(_request(user.id), db_session)

        assert first.context["conversation"].id == second.context["conversation"].id
        assert await _quick_chat_count(db_session, user.id) == 1

    @pytest.mark.asyncio
    async def test_the_loser_of_a_create_race_adopts_the_winner(
        self, db_session, monkeypatch
    ):
        """Two tabs insert at once; the partial unique index picks one."""
        user = await _seed_user(db_session)
        user_id = user.id
        _entitle(monkeypatch, chat_controller)
        winner = await _seed_conversation(
            db_session, user, chat_mode="quick", title="Quick chat"
        )
        winner_id = winner.id
        original_lookup = chat_controller._live_quick_conversation
        calls = {"count": 0}

        async def blind_first_lookup(session, user_id):
            calls["count"] += 1
            if calls["count"] == 1:
                return None
            return await original_lookup(session, user_id)

        monkeypatch.setattr(
            chat_controller, "_live_quick_conversation", blind_first_lookup
        )

        response = await chat_controller.chat_quick.fn(_request(user_id), db_session)

        assert response.context["conversation"].id == winner_id
        assert await _quick_chat_count(db_session, user_id) == 1

    @pytest.mark.asyncio
    async def test_a_user_without_chat_is_refused(self, db_session, monkeypatch):
        user = await _seed_user(db_session)
        _entitle(monkeypatch, chat_controller, roles=("member",))

        with pytest.raises(HTTPException) as failure:
            await chat_controller.chat_quick.fn(_request(user.id), db_session)

        assert failure.value.status_code == 403
        assert await _quick_chat_count(db_session, user.id) == 0

    def test_the_route_is_registered_in_both_app_configs(self):
        for name in ("app.yaml", "app.development.yaml"):
            handlers = yaml.safe_load((_PROJECT_ROOT / name).read_text())["controllers"]
            assert _QUICK_HANDLER in handlers, name
            # Registered ahead of the UUID route, as the stage requires.
            assert handlers.index(_QUICK_HANDLER) < handlers.index(_UUID_HANDLER), name

    def test_the_quick_segment_does_not_shadow_the_uuid_route(self):
        """``/chat/quick`` reaches the new handler, not the UUID one."""

        async def provide_session() -> None:
            return None

        app = Litestar(
            route_handlers=[
                chat_controller.chat_index,
                chat_controller.chat_quick,
                chat_controller.chat_conversation,
            ],
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

        assert resolve("/chat/quick").handler_name.endswith("chat_quick")
        assert resolve("/chat/" + str(uuid4())).handler_name.endswith(
            "chat_conversation"
        )


class TestRailPinning:
    @pytest.mark.asyncio
    async def test_the_quick_chat_is_pinned_and_not_grouped(self, db_session):
        user = await _seed_user(db_session)
        standard = await _seed_conversation(db_session, user)
        quick = await _seed_conversation(
            db_session, user, chat_mode="quick", title="Quick chat"
        )

        rail = await chat_controller._rail_context(db_session, user.id)

        assert rail["quick_conversation"].id == quick.id
        assert [row.id for row in rail["conversations"]] == [standard.id]
        assert all(
            row.id != quick.id
            for group in rail["conversation_groups"]
            for row in group["items"]
        )

    @pytest.mark.asyncio
    async def test_without_a_quick_chat_the_pin_is_absent(self, db_session):
        user = await _seed_user(db_session)
        await _seed_conversation(db_session, user)

        rail = await chat_controller._rail_context(db_session, user.id)

        assert rail["quick_conversation"] is None

    @pytest.mark.asyncio
    async def test_the_pinned_row_is_rendered_without_a_menu(self, db_session):
        user = await _seed_user(db_session)
        quick = await _seed_conversation(
            db_session, user, chat_mode="quick", title="Quick chat"
        )
        context = await _page_context(
            db_session,
            quick,
            **await chat_controller._rail_context(db_session, user.id),
        )

        html = _render_chat_page(context)

        pinned = html.split("data-quick-chat-row", 1)[1].split("</div>", 1)[0]
        assert 'href="/chat/quick"' in pinned
        assert 'aria-current="page"' in pinned
        assert "data-history-menu" not in pinned

    @pytest.mark.asyncio
    async def test_the_empty_state_links_to_the_quick_chat(self, db_session):
        user = await _seed_user(db_session)
        context = {
            "conversation": None,
            "mode": "chat",
            "messages": [],
            "versions": {},
            "settings": await ensure_settings(db_session),
            "catalog_models": [],
            "ultra_chat": False,
            **await chat_controller._rail_context(db_session, user.id),
        }

        html = _render_chat_page(context)

        assert 'href="/chat/quick"' in html.split('class="chat-empty"', 1)[1]


class TestThreadSnapshots:
    @pytest.mark.asyncio
    async def test_both_snapshots_carry_the_same_threads(self, db_session, monkeypatch):
        user = await _seed_user(db_session)
        conversation = await _seed_conversation(
            db_session, user, chat_mode="quick", title="Quick chat"
        )
        await _seed_exchanges(db_session, conversation, 3)
        await _seed_threads(db_session, conversation, [1, 3, 5])

        async def entitled(*_args, **_kwargs):
            return {"sudo-rw"}

        monkeypatch.setattr(chat_api, "require_entitled", entitled)
        page = await chat_controller._chat_context(db_session, conversation)
        snapshot = await ChatApiController.get_conversation.fn(
            object.__new__(ChatApiController),
            conversation.id,
            _request(user.id),
            db_session,
        )

        assert page["threads"] == snapshot["threads"]
        assert [item["start_sequence"] for item in page["threads"]] == [1, 3, 5]
        assert [item["sequence"] for item in page["threads"]] == [1, 2, 3]
        assert page["threads"][0]["title"] == "Subject 1"
        assert page["threads"][0]["origin"] == "agent"

    @pytest.mark.asyncio
    async def test_a_standard_conversation_has_no_threads(
        self, db_session, monkeypatch
    ):
        user = await _seed_user(db_session)
        conversation = await _seed_conversation(db_session, user)
        await _seed_exchanges(db_session, conversation, 2)

        async def entitled(*_args, **_kwargs):
            return {"sudo-rw"}

        monkeypatch.setattr(chat_api, "require_entitled", entitled)
        page = await chat_controller._chat_context(db_session, conversation)
        snapshot = await ChatApiController.get_conversation.fn(
            object.__new__(ChatApiController),
            conversation.id,
            _request(user.id),
            db_session,
        )

        assert page["threads"] == []
        assert snapshot["threads"] == []
        assert page["thread_breaks"] == {}

    @pytest.mark.asyncio
    async def test_the_snapshot_messages_carry_their_sequence(
        self, db_session, monkeypatch
    ):
        """The reconcile needs a sequence per message to place a divider."""
        user = await _seed_user(db_session)
        conversation = await _seed_conversation(
            db_session, user, chat_mode="quick", title="Quick chat"
        )
        await _seed_exchanges(db_session, conversation, 2)

        async def entitled(*_args, **_kwargs):
            return {"sudo-rw"}

        monkeypatch.setattr(chat_api, "require_entitled", entitled)
        snapshot = await ChatApiController.get_conversation.fn(
            object.__new__(ChatApiController),
            conversation.id,
            _request(user.id),
            db_session,
        )

        assert [message["sequence"] for message in snapshot["messages"]] == [1, 2, 3, 4]


class TestDividerRendering:
    @pytest.mark.asyncio
    async def test_every_boundary_draws_its_own_divider(self, db_session):
        """Including the first one drawn: it is a line like any other.

        A Quick chat opens with no thread row at all, so the first row it ever
        gets is a boundary the agent or the evaluator drew mid-conversation.
        Skipping it would hide the line the person just watched appear.
        """
        user = await _seed_user(db_session)
        conversation = await _seed_conversation(
            db_session, user, chat_mode="quick", title="Quick chat"
        )
        await _seed_exchanges(db_session, conversation, 3)
        await _seed_threads(db_session, conversation, [3, 5])
        context = await _page_context(db_session, conversation)

        html = _render_chat_page(context)

        assert html.count("data-thread-break") == 2
        assert "Subject 1" in html
        assert "Subject 2" in html
        breaks = [
            fragment.split('data-start-sequence="', 1)[1].split('"', 1)[0]
            for fragment in html.split("data-thread-break")[1:]
        ]
        assert breaks == ["3", "5"]

    @pytest.mark.asyncio
    async def test_each_divider_sits_above_the_message_that_starts_the_thread(
        self, db_session
    ):
        user = await _seed_user(db_session)
        conversation = await _seed_conversation(
            db_session, user, chat_mode="quick", title="Quick chat"
        )
        await _seed_exchanges(db_session, conversation, 3)
        await _seed_threads(db_session, conversation, [3, 5])
        context = await _page_context(db_session, conversation)
        third = next(item for item in context["messages"] if item["sequence"] == 3)

        html = _render_chat_page(context)

        before = html.split('data-message-id="' + third["id"] + '"', 1)[0]
        assert before.count("data-thread-break") == 1
        assert "question 2" not in before

    @pytest.mark.asyncio
    async def test_a_standard_conversation_renders_no_divider(self, db_session):
        user = await _seed_user(db_session)
        conversation = await _seed_conversation(db_session, user)
        await _seed_exchanges(db_session, conversation, 2)
        context = await _page_context(db_session, conversation)

        html = _render_chat_page(context)

        assert "data-thread-break" not in html

    def test_the_divider_has_a_style_rule(self):
        css = (_CHAT_CSS / "chat.css").read_text()
        assert ".chat-thread-break {" in css
        assert ".chat-thread-break-label" in css


class TestReconcileKeepsDividers:
    """The client sweep is keyed separately, or reconciles pile up dividers."""

    @pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
    def test_the_reconcile_keeps_every_boundary_the_snapshot_carries(self, tmp_path):
        """Run the real function; a dropped boundary is a deleted divider."""
        source = _CHAT_JS.read_text()
        function = "function threadBreaks(" + source.split(
            "function threadBreaks(", 1
        )[1].split("\n  function ", 1)[0]
        harness = (
            Path(__file__).parent / "js" / "thread_breaks_harness.js"
        ).read_text()
        script = tmp_path / "thread_breaks.js"
        script.write_text(harness.replace("// <CHAT_JS_FUNCTIONS>", function))

        result = subprocess.run(
            ["node", str(script)], capture_output=True, text=True, check=False
        )

        assert result.returncode == 0, result.stdout + result.stderr

    def test_sync_thread_sweeps_dividers_by_their_own_keep_set(self):
        source = _CHAT_JS.read_text()
        body = source.split("function syncThread(", 1)[1].split("\n  function ", 1)[0]
        assert "[data-thread-break]" in body
        assert "keptBreaks" in body

    def test_the_divider_code_stays_es5(self):
        """chat.js is a hand-written IIFE with no build step behind it."""
        source = _CHAT_JS.read_text()
        start = source.index("function threadBreaks(")
        end = source.index("function syncThread(")
        region = (
            source[start:end]
            + source.split("function syncThread(", 1)[1].split("\n  function ", 1)[0]
        )
        code = "\n".join(
            line for line in region.splitlines() if not line.strip().startswith("//")
        )
        assert "=>" not in code
        assert "`" not in code
        assert "let " not in code
        assert "const " not in code


class TestQuickChatIsNotFiledAway:
    @pytest.mark.asyncio
    async def test_archiving_a_quick_chat_is_refused(self, db_session, monkeypatch):
        user = await _seed_user(db_session)
        conversation = await _seed_conversation(
            db_session, user, chat_mode="quick", title="Quick chat"
        )

        async def entitled(*_args, **_kwargs):
            return {"sudo-rw"}

        monkeypatch.setattr(chat_api, "require_entitled", entitled)

        with pytest.raises(HTTPException) as failure:
            await ChatApiController.update_conversation.fn(
                object.__new__(ChatApiController),
                conversation.id,
                ConversationUpdateBody(archived=True),
                _request(user.id),
                db_session,
            )

        assert failure.value.status_code == 409
        await db_session.refresh(conversation)
        assert conversation.archived_at is None

    @pytest.mark.asyncio
    async def test_deleting_a_quick_chat_is_refused(self, db_session, monkeypatch):
        user = await _seed_user(db_session)
        conversation = await _seed_conversation(
            db_session, user, chat_mode="quick", title="Quick chat"
        )

        async def entitled(*_args, **_kwargs):
            return {"sudo-rw"}

        monkeypatch.setattr(chat_api, "require_entitled", entitled)

        with pytest.raises(HTTPException) as failure:
            await ChatApiController.delete_conversation.fn(
                object.__new__(ChatApiController),
                conversation.id,
                _request(user.id),
                db_session,
            )

        assert failure.value.status_code == 409
        assert await db_session.get(WebChatConversation, conversation.id) is not None

    @pytest.mark.asyncio
    async def test_a_standard_conversation_still_archives(
        self, db_session, monkeypatch
    ):
        user = await _seed_user(db_session)
        conversation = await _seed_conversation(db_session, user)

        async def entitled(*_args, **_kwargs):
            return {"sudo-rw"}

        monkeypatch.setattr(chat_api, "require_entitled", entitled)

        result = await ChatApiController.update_conversation.fn(
            object.__new__(ChatApiController),
            conversation.id,
            ConversationUpdateBody(archived=True),
            _request(user.id),
            db_session,
        )

        assert result["archived"] is True
