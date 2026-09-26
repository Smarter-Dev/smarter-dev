"""Retirements do not migrate chats.

A conversation whose model leaves the catalog keeps that selection and its
history. Its page says so and locks the composer, every server path that would
run a turn refuses it, and confirming an available model is the way back. None
of it ever reads a retired key as its successor.
"""

from __future__ import annotations

import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID
from uuid import uuid4

import pytest
from litestar.exceptions import HTTPException
from skrift.auth.session_keys import SESSION_USER_ID
from skrift.db.models.user import User
from skrift.forms.core import CSRF_SESSION_KEY
from sqlalchemy import select
from sqlalchemy import text

from smarter_dev.shared.model_catalog import RETIRED_SUCCESSORS
from smarter_dev.shared.model_catalog import get_model
from smarter_dev.web.chat import api as chat_api
from smarter_dev.web.chat import controller as chat_controller
from smarter_dev.web.chat import jobs as chat_jobs
from smarter_dev.web.chat.api import ChatApiController
from smarter_dev.web.chat.api import ModelChangeBody
from smarter_dev.web.chat.api import ReasoningBody
from smarter_dev.web.chat.api import TurnBody
from smarter_dev.web.chat.settings import ensure_settings
from smarter_dev.web.models import ChatCatalogModel
from smarter_dev.web.models import WebChatConversation
from smarter_dev.web.models import WebChatMessage
from smarter_dev.web.models import WebChatModelChange
from smarter_dev.web.models import WebChatTurn
from tests.web.quick_chat_surface_test import _render_chat_page

# Retired on 2026-09-24 and still mapped to a successor for admin settings.
RETIRED = "gpt-5-4"
AVAILABLE = "gpt-6-luna"  # ensure_settings enables it
OTHER = "gpt-6-sol"

_CHAT_JS = (
    Path(__file__).parents[2] / "themes" / "smarterdev" / "static" / "js" / "chat.js"
)


def test_the_retired_key_is_really_retired():
    assert get_model(RETIRED) is None
    assert RETIRED in RETIRED_SUCCESSORS


def _request(user_id, *, token: str = "csrf-token"):
    return SimpleNamespace(
        session={SESSION_USER_ID: str(user_id), CSRF_SESSION_KEY: token},
        headers={"X-CSRF-Token": token},
    )


def _controller():
    return object.__new__(ChatApiController)


@pytest.fixture
def entitled(monkeypatch):
    async def permissions(*_args, **_kwargs):
        return {"sudo-r"}

    async def do_not_dispatch(_dispatch_id):
        return None

    monkeypatch.setattr(chat_api, "require_entitled", permissions)
    monkeypatch.setattr(chat_api, "_try_dispatch", do_not_dispatch)
    monkeypatch.setattr(chat_controller, "get_user_permissions", permissions)


async def _seed(db_session, *, selected=RETIRED, reasoning="high"):
    """A conversation on ``selected`` with one finished exchange on it."""
    connection = await db_session.connection()
    await connection.run_sync(
        lambda sync_connection: User.metadata.create_all(sync_connection)
    )
    user = User(email=f"{uuid4().hex}@example.test", name="Chat User", is_active=True)
    db_session.add(user)
    await db_session.flush()
    await ensure_settings(db_session)
    (await db_session.get(ChatCatalogModel, OTHER)).enabled = True
    conversation = WebChatConversation(
        owner_user_id=user.id,
        intelligence_mode="efficient",
        selected_model_key=selected,
        reasoning_level=reasoning,
        title="Older chat",
        status="idle",
        next_sequence=2,
    )
    db_session.add(conversation)
    await db_session.flush()
    group = uuid4()
    turn = WebChatTurn(
        conversation_id=conversation.id,
        sequence=1,
        submission_key="first",
        response_version_group=group,
        response_sequence=2,
        model_key=selected,
        reasoning_level=reasoning,
        status="complete",
    )
    db_session.add(turn)
    await db_session.flush()
    for sequence, role, content, version_group in (
        (1, "user", "Hello", uuid4()),
        (2, "assistant", "Hi there", group),
    ):
        db_session.add(
            WebChatMessage(
                conversation_id=conversation.id,
                turn_id=turn.id,
                sequence=sequence,
                role=role,
                content=content,
                version_group=version_group,
                version_number=1,
                is_active=True,
            )
        )
    await db_session.commit()
    # Every read in the test goes back to the stored row, as a request does.
    db_session.expunge_all()
    return user, conversation, turn


async def _stored(db_session, conversation_id):
    row = (
        await db_session.execute(
            text(
                "SELECT selected_model_key, reasoning_level"
                " FROM web_chat_conversations WHERE id = :id"
            ),
            {"id": conversation_id.hex},
        )
    ).one()
    return tuple(row)


async def _turn_count(db_session, conversation_id):
    return len(
        (
            await db_session.execute(
                select(WebChatTurn.id).where(
                    WebChatTurn.conversation_id == conversation_id
                )
            )
        ).all()
    )


# ── The selection is kept ─────────────────────────────────


async def test_a_retired_selection_reads_back_as_itself(db_session, entitled):
    user, conversation, _ = await _seed(db_session)
    db_session.expunge_all()

    loaded = await db_session.get(WebChatConversation, conversation.id)
    snapshot = await ChatApiController.get_conversation.fn(
        _controller(), conversation.id, _request(user.id), db_session
    )

    assert loaded.selected_model_key == RETIRED
    assert snapshot["model_key"] == RETIRED
    assert snapshot["model_available"] is False
    # History comes back untouched with it.
    assert [m["content"] for m in snapshot["messages"]] == ["Hello", "Hi there"]
    assert await _stored(db_session, conversation.id) == (RETIRED, "high")


async def test_an_available_selection_reports_available(db_session, entitled):
    user, conversation, _ = await _seed(db_session, selected=AVAILABLE)

    snapshot = await ChatApiController.get_conversation.fn(
        _controller(), conversation.id, _request(user.id), db_session
    )

    assert snapshot["model_available"] is True


async def test_an_admin_disabled_model_is_unavailable_too(db_session, entitled):
    user, conversation, _ = await _seed(db_session, selected=OTHER)
    (await db_session.get(ChatCatalogModel, OTHER)).enabled = False
    await db_session.commit()

    snapshot = await ChatApiController.get_conversation.fn(
        _controller(), conversation.id, _request(user.id), db_session
    )

    assert snapshot["model_available"] is False
    assert snapshot["model_key"] == OTHER


# ── Every path that would run a turn refuses ───────────────


async def test_sending_on_a_retired_model_is_refused(db_session, entitled):
    user, conversation, _ = await _seed(db_session)
    conversation_id = conversation.id

    with pytest.raises(HTTPException) as refused:
        await ChatApiController.submit_turn.fn(
            _controller(),
            conversation.id,
            TurnBody(content="Still there?", submission_key="second"),
            _request(user.id),
            db_session,
        )

    assert refused.value.status_code == 409
    assert "choose a new model" in refused.value.detail
    await db_session.rollback()
    assert await _turn_count(db_session, conversation_id) == 1
    assert await _stored(db_session, conversation_id) == (RETIRED, "high")


async def test_regenerating_on_a_retired_model_is_refused(db_session, entitled):
    user, conversation, turn = await _seed(db_session)
    conversation_id = conversation.id

    with pytest.raises(HTTPException) as refused:
        await ChatApiController.regenerate.fn(
            _controller(), conversation.id, turn.id, _request(user.id), db_session
        )

    assert refused.value.status_code == 409
    assert "choose a new model" in refused.value.detail
    await db_session.rollback()
    assert await _turn_count(db_session, conversation_id) == 1
    active = (
        await db_session.execute(
            select(WebChatMessage.content).where(
                WebChatMessage.role == "assistant",
                WebChatMessage.is_active.is_(True),
            )
        )
    ).scalars().all()
    assert active == ["Hi there"]


async def test_changing_reasoning_on_a_retired_model_leaves_it_alone(
    db_session, entitled
):
    user, conversation, _ = await _seed(db_session)
    conversation_id = conversation.id

    with pytest.raises(HTTPException) as refused:
        await ChatApiController.reasoning.fn(
            _controller(),
            conversation.id,
            ReasoningBody(reasoning_level="low"),
            _request(user.id),
            db_session,
        )

    assert refused.value.status_code == 409
    await db_session.rollback()
    assert await _stored(db_session, conversation_id) == (RETIRED, "high")


@pytest.fixture
def worker_db(db_session, monkeypatch):
    @asynccontextmanager
    async def fake_session_context():
        yield db_session

    async def permissions(*_args, **_kwargs):
        return {"sudo-r"}

    monkeypatch.setattr(chat_jobs, "get_db_session_context", fake_session_context)
    monkeypatch.setattr(chat_jobs, "get_user_permissions", permissions)
    return db_session


async def test_a_turn_queued_on_a_retired_key_is_refused_not_run_on_a_successor(
    worker_db,
):
    # Queued before the retirement deployed; the worker picks it up after.
    user, conversation, _ = await _seed(worker_db)
    queued = WebChatTurn(
        conversation_id=conversation.id,
        sequence=2,
        submission_key="queued",
        response_version_group=uuid4(),
        response_sequence=4,
        model_key=RETIRED,
        status="submitted",
    )
    worker_db.add(queued)
    await worker_db.commit()

    with pytest.raises(LookupError, match="unavailable"):
        await chat_jobs._preflight(queued.id, user.id)


async def test_a_queued_turn_on_an_available_key_still_runs(worker_db):
    user, conversation, _ = await _seed(worker_db, selected=AVAILABLE)
    queued = WebChatTurn(
        conversation_id=conversation.id,
        sequence=2,
        submission_key="queued",
        response_version_group=uuid4(),
        response_sequence=4,
        model_key=AVAILABLE,
        status="submitted",
    )
    worker_db.add(queued)
    await worker_db.commit()

    _, _, model, _, _ = await chat_jobs._preflight(queued.id, user.id)

    assert model.key == AVAILABLE


# ── Choosing an available model recovers ──────────────────


async def test_choosing_an_available_model_recovers_the_conversation(
    db_session, entitled
):
    user, conversation, _ = await _seed(db_session)
    controller = _controller()

    proposed = await ChatApiController.propose_model.fn(
        controller,
        conversation.id,
        ModelChangeBody(model_key=OTHER),
        _request(user.id),
        db_session,
    )
    assert RETIRED in proposed["warning"]
    assert proposed["requires_confirmation"] is True
    # Proposing changes nothing yet.
    assert await _stored(db_session, conversation.id) == (RETIRED, "high")

    confirmed = await ChatApiController.confirm_model.fn(
        controller,
        conversation.id,
        UUID(proposed["id"]),
        _request(user.id),
        db_session,
    )
    assert confirmed["model_key"] == OTHER
    change = await db_session.get(WebChatModelChange, UUID(proposed["id"]))
    assert (change.from_model_key, change.to_model_key) == (RETIRED, OTHER)

    snapshot = await ChatApiController.get_conversation.fn(
        controller, conversation.id, _request(user.id), db_session
    )
    assert snapshot["model_available"] is True
    created = await ChatApiController.submit_turn.fn(
        controller,
        conversation.id,
        TurnBody(content="Back again", submission_key="second"),
        _request(user.id),
        db_session,
    )
    turn = await db_session.get(WebChatTurn, UUID(created["turn_id"]))
    assert turn.model_key == OTHER
    # The earlier exchange keeps the model it ran on.
    first = await db_session.scalar(
        select(WebChatTurn).where(WebChatTurn.submission_key == "first")
    )
    assert first.model_key == RETIRED


async def test_recovery_cannot_pick_another_unavailable_model(db_session, entitled):
    user, conversation, _ = await _seed(db_session)

    for target in ("gpt-5-4-nano", "no-such-model"):
        with pytest.raises(HTTPException) as refused:
            await ChatApiController.propose_model.fn(
                _controller(),
                conversation.id,
                ModelChangeBody(model_key=target),
                _request(user.id),
                db_session,
            )
        assert refused.value.status_code == 422


async def test_a_change_whose_target_retired_since_is_not_confirmed_onto_a_successor(
    db_session, entitled
):
    # Proposed while gpt-5-4-mini was live; it has been retired since.
    user, conversation, _ = await _seed(db_session, selected=AVAILABLE)
    conversation_id = conversation.id
    from datetime import UTC
    from datetime import datetime
    from datetime import timedelta

    change = WebChatModelChange(
        conversation_id=conversation.id,
        owner_user_id=user.id,
        from_model_key=AVAILABLE,
        to_model_key="gpt-5-4-mini",
        warning="",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    db_session.add(change)
    await db_session.commit()

    with pytest.raises(HTTPException) as refused:
        await ChatApiController.confirm_model.fn(
            _controller(), conversation.id, change.id, _request(user.id), db_session
        )

    assert refused.value.status_code == 409
    await db_session.rollback()
    assert (await _stored(db_session, conversation_id))[0] == AVAILABLE


# ── The page ──────────────────────────────────────────────


async def _page_html(db_session, user, conversation):
    response = await chat_controller.chat_conversation.fn(
        conversation.id, _request(user.id), db_session
    )
    return response.context, _render_chat_page(response.context)


def _tag(html, marker):
    start = html.rindex("<", 0, html.index(marker))
    return html[start : html.index(">", start) + 1]


async def test_the_page_shows_the_notice_and_renders_the_composer_locked(
    db_session, entitled
):
    user, conversation, _ = await _seed(db_session)

    context, html = await _page_html(db_session, user, conversation)

    assert context["model_unavailable"] is True
    notice = _tag(html, "data-model-unavailable ")
    assert "hidden" not in notice
    assert f"<b data-model-unavailable-key>{RETIRED}</b> is no longer available" in html
    assert "data-model-unavailable-choose" in html
    assert "disabled" in _tag(html, "data-chat-input")
    assert "disabled" in _tag(html, 'type="submit" class="p-btn is-primary"')
    assert "disabled" in _tag(html, "data-chat-reasoning")
    assert "disabled" in _tag(html, "data-regenerate ")
    assert 'data-model-available="false"' in html
    # The history is still on the page.
    assert "Hi there" in html


async def test_an_available_conversation_renders_no_notice(db_session, entitled):
    user, conversation, _ = await _seed(db_session, selected=AVAILABLE)

    context, html = await _page_html(db_session, user, conversation)

    assert context["model_unavailable"] is False
    assert "hidden" in _tag(html, "data-model-unavailable ")
    assert "disabled" not in _tag(html, "data-chat-input")
    assert "disabled" not in _tag(html, "data-regenerate ")
    assert 'data-model-available="true"' in html


def _function(source: str, name: str) -> str:
    return "function " + name + "(" + source.split(
        "function " + name + "(", 1
    )[1].split("\n  function ", 1)[0]


def test_the_composer_lock_follows_the_notice(tmp_path):
    source = _CHAT_JS.read_text()
    functions = _function(source, "setBusy") + "\n" + _function(
        source, "setModelAvailability"
    )
    harness = (Path(__file__).parent / "js" / "model_unavailable_harness.js").read_text()
    script = tmp_path / "model_unavailable.js"
    script.write_text(harness.replace("// <CHAT_JS_FUNCTIONS>", functions))

    result = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_chat_js_reads_availability_from_the_catalog_and_the_snapshot():
    source = _CHAT_JS.read_text()
    activate = _function(source, "activateConversationControls")
    assert "setModelAvailability(" in activate
    assert "snapshot.model_available === false" in _function(source, "reconcile")
    assert "if (modelUnavailable)" in _function(source, "sendMessage")


# ── A turn caught by a retirement ─────────────────────────


async def test_a_turn_queued_before_a_retirement_ends_cleanly_and_recovery_follows(
    db_session, entitled, monkeypatch
):
    """Queued on a live model, which is retired before the worker gets to it.

    The worker ends the turn as ``selection_required`` without a model call,
    leaves nothing active, and the conversation goes on once its owner
    confirms an available model.
    """
    from smarter_dev.web.chat import runtime as chat_runtime
    from smarter_dev.web.chat.jobs import ChatTurnPayload

    @asynccontextmanager
    async def fake_session_context():
        yield db_session

    async def permissions(*_args, **_kwargs):
        return {"sudo-r"}

    events: list = []

    async def record_event(turn_id, kind, data):
        events.append((kind, data))

    async def ignore(*_args, **_kwargs):
        return None

    async def no_heartbeat(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chat_jobs, "get_db_session_context", fake_session_context)
    monkeypatch.setattr(chat_runtime, "get_db_session_context", fake_session_context)
    monkeypatch.setattr(chat_jobs, "get_user_permissions", permissions)
    monkeypatch.setattr(chat_jobs, "_event", record_event)
    monkeypatch.setattr(chat_jobs, "_notify_safe", ignore)
    monkeypatch.setattr(chat_jobs, "_heartbeat_turn", no_heartbeat)

    user, conversation, _ = await _seed(db_session, selected=AVAILABLE)
    conversation_id = conversation.id
    controller = _controller()
    created = await ChatApiController.submit_turn.fn(
        controller,
        conversation_id,
        TurnBody(content="Queued just before the deploy", submission_key="queued"),
        _request(user.id),
        db_session,
    )
    turn_id = UUID(created["turn_id"])
    # The deploy retires the model: its key leaves the catalog. Stand in for
    # that by moving the stored keys onto one the catalog no longer has.
    await db_session.execute(
        text("UPDATE web_chat_turns SET model_key = :k WHERE id = :id"),
        {"k": RETIRED, "id": turn_id.hex},
    )
    await db_session.execute(
        text("UPDATE web_chat_conversations SET selected_model_key = :k WHERE id = :id"),
        {"k": RETIRED, "id": conversation_id.hex},
    )
    await db_session.commit()
    db_session.expunge_all()

    outcome = await chat_jobs.run_chat_turn(ChatTurnPayload(turn_id=str(turn_id)))

    assert outcome == {"status": "selection_required"}
    db_session.expunge_all()
    turn = await db_session.get(WebChatTurn, turn_id)
    assert turn.status == "selection_required"
    assert turn.model_key == RETIRED
    assert turn.worker_lease_token is None
    assert turn.finished_at is not None
    placeholder = await db_session.scalar(
        select(WebChatMessage).where(
            WebChatMessage.turn_id == turn_id, WebChatMessage.role == "assistant"
        )
    )
    assert placeholder.content == "The selected model is unavailable. Choose another model."
    assert [kind for kind, _ in events] == ["chat_turn_error"]
    assert events[0][1]["status"] == "selection_required"

    snapshot = await ChatApiController.get_conversation.fn(
        controller, conversation_id, _request(user.id), db_session
    )
    assert snapshot["active_turn"] is None
    assert snapshot["model_available"] is False
    assert snapshot["model_key"] == RETIRED

    proposed = await ChatApiController.propose_model.fn(
        controller,
        conversation_id,
        ModelChangeBody(model_key=OTHER),
        _request(user.id),
        db_session,
    )
    await ChatApiController.confirm_model.fn(
        controller, conversation_id, UUID(proposed["id"]), _request(user.id), db_session
    )
    again = await ChatApiController.submit_turn.fn(
        controller,
        conversation_id,
        TurnBody(content="Trying again", submission_key="again"),
        _request(user.id),
        db_session,
    )
    assert again["status"] == "submitted"
    assert (await db_session.get(WebChatTurn, UUID(again["turn_id"]))).model_key == OTHER
