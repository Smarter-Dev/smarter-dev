"""Database-backed integration tests for the unified Chat durability seams."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID
from uuid import uuid4

import pytest
from litestar.exceptions import HTTPException
from skrift.auth.session_keys import SESSION_USER_ID
from skrift.db.models.user import User
from skrift.forms.core import CSRF_SESSION_KEY
from skrift.workers import registry as worker_registry
from sqlalchemy import func
from sqlalchemy import select

from smarter_dev.shared.model_catalog import get_model
from smarter_dev.web import agent_api
from smarter_dev.web.agent_api import AskBody
from smarter_dev.web.agent_api import ResourcesAgentApiController
from smarter_dev.web.chat import api as chat_api
from smarter_dev.web.chat import controller as chat_controller
from smarter_dev.web.chat import dispatch as chat_dispatch
from smarter_dev.web.chat.api import ChatApiController
from smarter_dev.web.chat.api import ConversationUpdateBody
from smarter_dev.web.chat.api import ReasoningBody
from smarter_dev.web.chat.api import TurnBody
from smarter_dev.web.chat.conversations import name_if_unnamed
from smarter_dev.web.chat.csrf import require_api_csrf
from smarter_dev.web.chat.dispatch import ensure_submission_handler
from smarter_dev.web.chat.documents import append_document_body
from smarter_dev.web.chat.documents import apply_document_patches
from smarter_dev.web.chat.documents import begin_document
from smarter_dev.web.chat.documents import conversation_artifacts
from smarter_dev.web.chat.documents import edit_document_body
from smarter_dev.web.chat.documents import find_artifact
from smarter_dev.web.chat.documents import find_name_clash
from smarter_dev.web.chat.documents import find_readable_document
from smarter_dev.web.chat.documents import finish_document
from smarter_dev.web.chat.documents import retire_failed_overwrite
from smarter_dev.web.chat.documents import supersede_replaced_documents
from smarter_dev.web.chat.jobs import _upload_manifest
from smarter_dev.web.chat.limits import OperationAlreadyReserved
from smarter_dev.web.chat.limits import reserve_operation
from smarter_dev.web.chat.runtime import RunSuperseded
from smarter_dev.web.chat.settings import ensure_settings
from smarter_dev.web.chat.usage import record_settled_chat_usage
from smarter_dev.web.models import AgentConversation
from smarter_dev.web.models import ChatSpendLimit
from smarter_dev.web.models import ChatSpendReservation
from smarter_dev.web.models import ResourceAgentRun
from smarter_dev.web.models import UsageCostRow
from smarter_dev.web.models import WebChatAttachment
from smarter_dev.web.models import WebChatConversation
from smarter_dev.web.models import WebChatDocument
from smarter_dev.web.models import WebChatMessage
from smarter_dev.web.models import WebChatTurn
from smarter_dev.web.models import WorkDispatch


@pytest.mark.parametrize(
    "job_type",
    (
        "chat.turn.run",
        "chat.subagent.run",
        "chat.account.delete",
        "resources.agent.run",
    ),
)
def test_web_submitter_can_resolve_worker_descriptors(job_type):
    ensure_submission_handler(job_type)
    assert worker_registry.get(job_type).job_type == job_type


@pytest.mark.asyncio
async def test_outbox_registers_descriptor_before_web_submission(
    db_session, monkeypatch
):
    row = WorkDispatch(
        job_type="chat.turn.run",
        aggregate_id=uuid4(),
        payload={"turn_id": str(uuid4())},
        queue="agents",
        status="pending",
    )
    db_session.add(row)
    await db_session.commit()
    submitted = {}

    async def fake_submit(job_type, payload, **kwargs):
        submitted.update(job_type=job_type, payload=payload, kwargs=kwargs)

    @asynccontextmanager
    async def fake_session_context():
        yield db_session

    monkeypatch.setattr(chat_dispatch, "get_db_session_context", fake_session_context)
    monkeypatch.setattr(chat_dispatch, "worker_submit", fake_submit)
    assert await chat_dispatch.dispatch_one(row.id) is True
    await db_session.refresh(row)
    assert row.status == "dispatched"
    assert submitted["job_type"] == "chat.turn.run"
    assert submitted["payload"] == row.payload


def _request(user_id, *, token: str = "csrf-token"):
    return SimpleNamespace(
        session={SESSION_USER_ID: str(user_id), CSRF_SESSION_KEY: token},
        headers={"X-CSRF-Token": token},
    )


async def _seed_chat(db_session):
    connection = await db_session.connection()
    await connection.run_sync(
        lambda sync_connection: User.metadata.create_all(sync_connection)
    )
    user = User(email=f"{uuid4().hex}@example.test", name="Chat User", is_active=True)
    db_session.add(user)
    await db_session.flush()
    await ensure_settings(db_session)
    conversation = WebChatConversation(
        owner_user_id=user.id,
        intelligence_mode="efficient",
        selected_model_key="gpt-5-6-luna",
        reasoning_level="medium",
        title="New Chat",
        status="idle",
    )
    db_session.add(conversation)
    await db_session.commit()
    return user, conversation


@pytest.mark.asyncio
async def test_submit_is_atomic_idempotent_and_creates_durable_placeholder(
    db_session, monkeypatch
):
    user, conversation = await _seed_chat(db_session)

    async def entitled(*_args, **_kwargs):
        return {"sudo-r"}

    async def do_not_dispatch(_dispatch_id):
        return None

    monkeypatch.setattr(chat_api, "require_entitled", entitled)
    monkeypatch.setattr(chat_api, "_try_dispatch", do_not_dispatch)
    controller = object.__new__(ChatApiController)
    body = TurnBody(content="Explain durable queues", submission_key="submission-1")

    first = await ChatApiController.submit_turn.fn(
        controller, conversation.id, body, _request(user.id), db_session
    )
    second = await ChatApiController.submit_turn.fn(
        controller, conversation.id, body, _request(user.id), db_session
    )

    assert first["idempotent"] is False
    assert second == {
        "turn_id": first["turn_id"],
        "status": "submitted",
        "idempotent": True,
    }
    turn_id = first["turn_id"]
    messages = list(
        (
            await db_session.execute(
                select(WebChatMessage).order_by(WebChatMessage.sequence)
            )
        ).scalars()
    )
    assert [(row.role, row.content) for row in messages] == [
        ("user", "Explain durable queues"),
        ("assistant", ""),
    ]
    assert str(messages[1].turn_id) == turn_id
    assert await db_session.scalar(select(func.count(WebChatTurn.id))) == 1
    dispatch = await db_session.scalar(select(WorkDispatch))
    assert dispatch.status == "pending" and str(dispatch.aggregate_id) == turn_id


@pytest.mark.asyncio
async def test_stop_cancels_pending_outbox_and_reaches_terminal_state(
    db_session, monkeypatch
):
    user, conversation = await _seed_chat(db_session)

    async def entitled(*_args, **_kwargs):
        return {"sudo-r"}

    async def do_not_dispatch(_dispatch_id):
        return None

    monkeypatch.setattr(chat_api, "require_entitled", entitled)
    monkeypatch.setattr(chat_api, "_try_dispatch", do_not_dispatch)
    controller = object.__new__(ChatApiController)
    created = await ChatApiController.submit_turn.fn(
        controller,
        conversation.id,
        TurnBody(content="Stop this", submission_key="submission-stop"),
        _request(user.id),
        db_session,
    )
    turn_id = UUID(created["turn_id"])

    result = await ChatApiController.stop_turn.fn(
        controller, conversation.id, turn_id, _request(user.id), db_session
    )

    turn = await db_session.get(WebChatTurn, turn_id)
    placeholder = await db_session.scalar(
        select(WebChatMessage).where(WebChatMessage.role == "assistant")
    )
    dispatch = await db_session.scalar(select(WorkDispatch))
    assert result["status"] == "stopped"
    assert turn.status == "stopped" and turn.finished_at is not None
    assert placeholder.stopped and "Stopped" in placeholder.content
    assert dispatch.status == "cancelled"


@pytest.mark.asyncio
async def test_repeated_regeneration_uses_one_version_group(db_session, monkeypatch):
    user, conversation = await _seed_chat(db_session)

    async def entitled(*_args, **_kwargs):
        return {"sudo-r"}

    async def do_not_dispatch(_dispatch_id):
        return None

    monkeypatch.setattr(chat_api, "require_entitled", entitled)
    monkeypatch.setattr(chat_api, "_try_dispatch", do_not_dispatch)
    controller = object.__new__(ChatApiController)
    created = await ChatApiController.submit_turn.fn(
        controller,
        conversation.id,
        TurnBody(content="Version me", submission_key="submission-version"),
        _request(user.id),
        db_session,
    )
    original = await db_session.get(WebChatTurn, UUID(created["turn_id"]))
    original.status = "complete"
    conversation.status = "idle"
    original_message = await db_session.scalar(
        select(WebChatMessage).where(WebChatMessage.role == "assistant")
    )
    original_message.content = "Version one"
    await db_session.commit()

    second = await ChatApiController.regenerate.fn(
        controller, conversation.id, original.id, _request(user.id), db_session
    )
    second_turn = await db_session.get(WebChatTurn, UUID(second["turn_id"]))
    second_turn.status = "complete"
    conversation.status = "idle"
    second_message = await db_session.scalar(
        select(WebChatMessage).where(WebChatMessage.turn_id == second_turn.id)
    )
    second_message.content = "Version two"
    await db_session.commit()

    third = await ChatApiController.regenerate.fn(
        controller, conversation.id, second_turn.id, _request(user.id), db_session
    )
    versions = list(
        (
            await db_session.execute(
                select(WebChatMessage)
                .where(WebChatMessage.role == "assistant")
                .order_by(WebChatMessage.version_number)
            )
        ).scalars()
    )
    assert second["version_group"] == third["version_group"]
    assert [row.version_number for row in versions] == [1, 2, 3]
    assert [row.is_active for row in versions] == [False, False, True]
    assert versions[2].sequence == versions[0].sequence


@pytest.mark.asyncio
async def test_reservation_settlement_is_idempotent_and_attributes_actual_overage(
    db_session,
):
    user, conversation = await _seed_chat(db_session)
    limit = await db_session.get(ChatSpendLimit, "r")
    limit.four_hour_usd = Decimal("1")
    limit.daily_usd = Decimal("1")
    limit.weekly_usd = Decimal("1")
    await db_session.commit()
    operation_key = "chat:test:request:1"
    reservation, decision = await reserve_operation(
        db_session,
        operation_key=operation_key,
        user_id=user.id,
        tier="r",
        intelligence_mode="efficient",
        estimate_usd=Decimal("1.10"),
        conversation_id=conversation.id,
    )
    assert reservation is not None and decision.allowed and decision.in_overage
    model = get_model("gemini-3-5-flash-lite")

    first = await record_settled_chat_usage(
        db_session,
        operation_key=operation_key,
        operation_type="primary",
        model=model,
        tier="r",
        # 4M input at 3.5 Flash Lite's $0.30/M settles at $1.20 — deliberately
        # NOT the $1.10 reserved above, so the assertions below can only pass if
        # settlement uses the ACTUAL cost rather than the estimate.
        input_tokens=4_000_000,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
        user_id=user.id,
        conversation_id=conversation.id,
        root_turn_id=uuid4(),
        intelligence_mode="efficient",
    )
    second = await record_settled_chat_usage(
        db_session,
        operation_key=operation_key,
        operation_type="primary",
        model=model,
        tier="r",
        input_tokens=999,
        output_tokens=999,
        cache_read_tokens=0,
        cache_write_tokens=0,
        user_id=user.id,
        conversation_id=conversation.id,
        root_turn_id=uuid4(),
        intelligence_mode="efficient",
    )
    await db_session.commit()

    stored_reservation = await db_session.scalar(
        select(ChatSpendReservation).where(
            ChatSpendReservation.operation_key == operation_key
        )
    )
    assert first.id == second.id
    assert first.cost_usd == Decimal("1.2")
    # Everything past the $1 limit is overage.
    assert first.overage_cost_usd == Decimal("0.2")
    assert stored_reservation.status == "settled"
    assert await db_session.scalar(select(func.count(UsageCostRow.id))) == 1


@pytest.mark.asyncio
async def test_reservations_fail_closed_past_shared_overage(db_session):
    user, conversation = await _seed_chat(db_session)
    limit = await db_session.get(ChatSpendLimit, "r")
    limit.four_hour_usd = Decimal("1")
    limit.daily_usd = Decimal("1")
    limit.weekly_usd = Decimal("1")
    await db_session.commit()
    first, _ = await reserve_operation(
        db_session,
        operation_key="first",
        user_id=user.id,
        tier="r",
        intelligence_mode="efficient",
        estimate_usd=Decimal("0.75"),
        conversation_id=conversation.id,
    )
    second, decision = await reserve_operation(
        db_session,
        operation_key="second",
        user_id=user.id,
        tier="r",
        intelligence_mode="efficient",
        estimate_usd=Decimal("0.50"),
        conversation_id=conversation.id,
    )
    assert first is not None
    assert second is None and decision.hard_cutoff and not decision.allowed


@pytest.mark.asyncio
async def test_cross_owner_conversation_lookup_is_not_found(db_session, monkeypatch):
    owner, conversation = await _seed_chat(db_session)
    stranger = User(
        email=f"{uuid4().hex}@example.test", name="Stranger", is_active=True
    )
    db_session.add(stranger)
    await db_session.commit()

    async def entitled(*_args, **_kwargs):
        return {"sudo-r"}

    monkeypatch.setattr(chat_api, "require_entitled", entitled)
    controller = object.__new__(ChatApiController)
    with pytest.raises(HTTPException) as exc:
        await ChatApiController.get_conversation.fn(
            controller, conversation.id, _request(stranger.id), db_session
        )
    assert exc.value.status_code == 404
    assert owner.id != stranger.id


@pytest.mark.asyncio
async def test_streamed_document_lifecycle_is_private_and_downloadable_when_done(
    db_session, monkeypatch
):
    """A document is created empty, filled by a stream, then settled.

    The reader can see it the whole way through, so the checks here follow that
    order: visible and unfinished, appended to, and only downloadable once the
    write has actually finished.
    """
    user, conversation = await _seed_chat(db_session)
    turn = WebChatTurn(
        conversation_id=conversation.id,
        sequence=1,
        submission_key="document-turn",
        response_version_group=uuid4(),
        response_sequence=2,
        model_key=conversation.selected_model_key,
        reasoning_level=conversation.reasoning_level,
        status="running",
        worker_lease_token="lease-token",
    )
    db_session.add(turn)
    await db_session.flush()
    assistant = WebChatMessage(
        conversation_id=conversation.id,
        turn_id=turn.id,
        sequence=2,
        role="assistant",
        content="",
        version_group=turn.response_version_group,
    )
    db_session.add(assistant)
    await db_session.commit()

    opened, needs_body = await begin_document(
        db_session,
        conversation_id=conversation.id,
        turn_id=turn.id,
        assistant_message_id=assistant.id,
        worker_lease_token="lease-token",
        tool_call_id="tool-call-1",
        title="Durable queues",
        filename="durable-queues",
    )
    await db_session.commit()
    assert needs_body is True
    assert (opened.status, opened.markdown_content, opened.size_bytes) == (
        "streaming",
        "",
        0,
    )
    assert opened.filename == "durable-queues.md"

    for chunk in ("# Durable queues\n\n", "Use an outbox."):
        await append_document_body(
            db_session,
            document_id=opened.id,
            turn_id=turn.id,
            worker_lease_token="lease-token",
            chunk=chunk,
            size_bytes=1,
        )
    await db_session.commit()
    await db_session.refresh(opened)
    assert opened.markdown_content == "# Durable queues\n\nUse an outbox."

    async def entitled(*_args, **_kwargs):
        return {"sudo-r"}

    monkeypatch.setattr(chat_api, "require_entitled", entitled)
    controller = object.__new__(ChatApiController)

    # Half a file is not a download, and the preview says so instead.
    with pytest.raises(HTTPException) as exc:
        await ChatApiController.download_document.fn(
            controller, conversation.id, opened.id, _request(user.id), db_session
        )
    assert exc.value.status_code == 409
    live = await ChatApiController.get_document.fn(
        controller, conversation.id, opened.id, _request(user.id), db_session
    )
    assert live.content["status"] == "streaming"
    assert live.content["content_text"] == "# Durable queues\n\nUse an outbox."

    first = await finish_document(
        db_session,
        document_id=opened.id,
        turn_id=turn.id,
        worker_lease_token="lease-token",
        markdown="# Durable queues\n\nUse an outbox.",
        status="complete",
    )
    await db_session.commit()
    assert first.status == "complete"
    assert first.size_bytes == len(b"# Durable queues\n\nUse an outbox.")

    # Tool redelivery for a finished document must not pay to write it twice.
    second, second_needs_body = await begin_document(
        db_session,
        conversation_id=conversation.id,
        turn_id=turn.id,
        assistant_message_id=assistant.id,
        worker_lease_token="lease-token",
        tool_call_id="tool-call-1",
        title="Ignored retry title",
        filename="ignored.md",
    )
    await db_session.commit()
    assert second.id == first.id and second_needs_body is False
    assert second.title == "Durable queues"
    assert await db_session.scalar(select(func.count(WebChatDocument.id))) == 1

    # A redelivery that arrives while a write is unfinished starts the file over
    # rather than resuming from half a body.
    opened.status = "streaming"
    opened.markdown_content = "# Durable queues\n\nUse an "
    opened.size_bytes = len(opened.markdown_content.encode("utf-8"))
    await db_session.commit()
    restarted, restarted_needs_body = await begin_document(
        db_session,
        conversation_id=conversation.id,
        turn_id=turn.id,
        assistant_message_id=assistant.id,
        worker_lease_token="lease-token",
        tool_call_id="tool-call-1",
        title="Durable queues",
        filename="durable-queues.md",
    )
    await db_session.commit()
    assert restarted_needs_body is True
    assert (restarted.markdown_content, restarted.size_bytes) == ("", 0)
    first = await finish_document(
        db_session,
        document_id=opened.id,
        turn_id=turn.id,
        worker_lease_token="lease-token",
        markdown="# Durable queues\n\nUse an outbox.",
        status="complete",
    )
    await db_session.commit()

    preview_response = await ChatApiController.get_document.fn(
        controller, conversation.id, first.id, _request(user.id), db_session
    )
    preview = preview_response.content
    assert preview["title"] == "Durable queues"
    assert "<h1>Durable queues</h1>" in preview["content_html"]
    assert preview["content_text"] is None
    assert "markdown" not in preview
    assert preview_response.headers["Cache-Control"] == "no-store"

    download = await ChatApiController.download_document.fn(
        controller, conversation.id, first.id, _request(user.id), db_session
    )
    assert download.content == b"# Durable queues\n\nUse an outbox."
    assert download.headers["Content-Disposition"].startswith(
        'attachment; filename="durable-queues.md"'
    )
    assert download.headers["Cache-Control"] == "no-store"

    # Rereading is what lets the receipt in the conversation stay short.
    reread = await find_readable_document(
        db_session, conversation_id=conversation.id, filename="durable-queues"
    )
    assert reread is not None and reread.id == first.id
    assert await find_readable_document(
        db_session, conversation_id=conversation.id, filename="nope.md"
    ) is None

    snapshot = await ChatApiController.get_conversation.fn(
        controller, conversation.id, _request(user.id), db_session
    )
    assert snapshot["documents"] == [
        {
            "id": str(first.id),
            "turn_id": str(turn.id),
            "assistant_message_id": str(assistant.id),
            "title": "Durable queues",
            "filename": "durable-queues.md",
            "size_bytes": first.size_bytes,
            "status": "complete",
        }
    ]
    assert "markdown_content" not in str(snapshot)

    stranger = User(
        email=f"{uuid4().hex}@example.test", name="Stranger", is_active=True
    )
    db_session.add(stranger)
    await db_session.commit()
    with pytest.raises(HTTPException) as exc:
        await ChatApiController.get_document.fn(
            controller, conversation.id, first.id, _request(stranger.id), db_session
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_submit_rolls_back_all_rows_when_outbox_creation_fails(
    db_session, monkeypatch
):
    user, conversation = await _seed_chat(db_session)

    async def entitled(*_args, **_kwargs):
        return {"sudo-r"}

    async def fail_dispatch(*_args, **_kwargs):
        raise RuntimeError("scripted outbox failure")

    monkeypatch.setattr(chat_api, "require_entitled", entitled)
    monkeypatch.setattr(chat_api, "create_dispatch", fail_dispatch)
    controller = object.__new__(ChatApiController)
    with pytest.raises(RuntimeError):
        await ChatApiController.submit_turn.fn(
            controller,
            conversation.id,
            TurnBody(content="Atomic please", submission_key="rollback-key"),
            _request(user.id),
            db_session,
        )
    await db_session.rollback()
    assert await db_session.scalar(select(func.count(WebChatTurn.id))) == 0
    assert await db_session.scalar(select(func.count(WebChatMessage.id))) == 0
    assert await db_session.scalar(select(func.count(WorkDispatch.id))) == 0


def test_csrf_is_mandatory_for_cookie_authenticated_mutations():
    request = SimpleNamespace(
        session={CSRF_SESSION_KEY: "expected"},
        headers={"X-CSRF-Token": "wrong"},
    )
    with pytest.raises(HTTPException) as exc:
        require_api_csrf(request)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_active_turn_blocks_reasoning_changes(db_session, monkeypatch):
    user, conversation = await _seed_chat(db_session)

    async def entitled(*_args, **_kwargs):
        return {"sudo-r"}

    monkeypatch.setattr(chat_api, "require_entitled", entitled)
    turn = WebChatTurn(
        conversation_id=conversation.id,
        sequence=1,
        submission_key="active-selection",
        response_version_group=uuid4(),
        response_sequence=2,
        model_key=conversation.selected_model_key,
        reasoning_level=conversation.reasoning_level,
        status="running",
    )
    db_session.add(turn)
    await db_session.commit()

    controller = object.__new__(ChatApiController)
    with pytest.raises(HTTPException) as exc:
        await ChatApiController.reasoning.fn(
            controller,
            conversation.id,
            ReasoningBody(reasoning_level="high"),
            _request(user.id),
            db_session,
        )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_conversation_snapshot_carries_rendered_markdown_and_all_versions(
    db_session, monkeypatch
):
    """The browser reconciles terminal turns from this payload without a reload."""
    user, conversation = await _seed_chat(db_session)

    async def entitled(*_args, **_kwargs):
        return {"sudo-r"}

    monkeypatch.setattr(chat_api, "require_entitled", entitled)
    version_group = uuid4()
    turn = WebChatTurn(
        conversation_id=conversation.id,
        sequence=1,
        submission_key="reconcile-turn",
        response_version_group=version_group,
        response_sequence=2,
        model_key=conversation.selected_model_key,
        reasoning_level=conversation.reasoning_level,
        status="complete",
    )
    db_session.add(turn)
    await db_session.flush()
    db_session.add_all(
        [
            WebChatMessage(
                conversation_id=conversation.id,
                turn_id=turn.id,
                sequence=1,
                role="user",
                content="Explain **durable** queues",
                version_group=uuid4(),
                version_number=1,
                is_active=True,
            ),
            WebChatMessage(
                conversation_id=conversation.id,
                turn_id=turn.id,
                sequence=2,
                role="assistant",
                content="# Superseded",
                version_group=version_group,
                version_number=1,
                is_active=False,
            ),
            WebChatMessage(
                conversation_id=conversation.id,
                turn_id=turn.id,
                sequence=2,
                role="assistant",
                content="# Durable queues",
                version_group=version_group,
                version_number=2,
                is_active=True,
            ),
        ]
    )
    await db_session.commit()

    snapshot = await ChatApiController.get_conversation.fn(
        object.__new__(ChatApiController),
        conversation.id,
        _request(user.id),
        db_session,
    )

    assert snapshot["active_turn"] is None
    messages = snapshot["messages"]
    user_message = next(item for item in messages if item["role"] == "user")
    assert "<strong>durable</strong>" in user_message["content_html"]
    assert user_message["attachments"] == []
    assistants = [item for item in messages if item["role"] == "assistant"]
    # Every version ships so the client can rebuild the version <select>.
    assert [item["version_number"] for item in assistants] == [1, 2]
    assert [item["is_active"] for item in assistants] == [False, True]
    assert all(item["version_group"] == str(version_group) for item in assistants)
    active = next(item for item in assistants if item["is_active"])
    assert "<h1>Durable queues</h1>" in active["content_html"]


@pytest.mark.asyncio
async def test_active_turn_snapshot_carries_rendered_partial_markdown(
    db_session, monkeypatch
):
    user, conversation = await _seed_chat(db_session)

    async def entitled(*_args, **_kwargs):
        return {"sudo-r"}

    monkeypatch.setattr(chat_api, "require_entitled", entitled)
    turn = WebChatTurn(
        conversation_id=conversation.id,
        sequence=1,
        submission_key="streaming-markdown-turn",
        response_version_group=uuid4(),
        response_sequence=2,
        model_key=conversation.selected_model_key,
        reasoning_level=conversation.reasoning_level,
        status="running",
    )
    db_session.add(turn)
    await db_session.flush()
    db_session.add(
        WebChatMessage(
            conversation_id=conversation.id,
            turn_id=turn.id,
            sequence=2,
            role="assistant",
            content="# Streaming\n\n- first\n- second",
            version_group=turn.response_version_group,
            version_number=1,
            is_active=True,
        )
    )
    await db_session.commit()

    snapshot = await ChatApiController.get_conversation.fn(
        object.__new__(ChatApiController),
        conversation.id,
        _request(user.id),
        db_session,
    )

    assert snapshot["active_turn"]["partial"] == "# Streaming\n\n- first\n- second"
    assert "<h1>Streaming</h1>" in snapshot["active_turn"]["partial_html"]
    assert "<li>first</li>" in snapshot["active_turn"]["partial_html"]


@pytest.mark.asyncio
async def test_active_ambiguous_reservation_cannot_be_reused(db_session):
    user, conversation = await _seed_chat(db_session)
    kwargs = {
        "operation_key": "ambiguous-provider-request",
        "user_id": user.id,
        "tier": "r",
        "intelligence_mode": "efficient",
        "estimate_usd": Decimal("0.01"),
        "conversation_id": conversation.id,
    }
    reservation, _ = await reserve_operation(db_session, **kwargs)
    assert reservation is not None
    with pytest.raises(OperationAlreadyReserved):
        await reserve_operation(db_session, **kwargs)


@pytest.mark.asyncio
async def test_resources_ask_is_idempotent_and_rejects_inactive_user(
    db_session, monkeypatch
):
    user, _conversation = await _seed_chat(db_session)

    async def no_quota(*_args, **_kwargs):
        return None

    async def do_not_dispatch(_dispatch_id):
        return None

    monkeypatch.setattr(agent_api, "_enforce_weekly_question_quota", no_quota)
    monkeypatch.setattr(agent_api, "_enforce_rate", lambda *_args: None)
    monkeypatch.setattr(agent_api, "_try_dispatch", do_not_dispatch)
    controller = object.__new__(ResourcesAgentApiController)
    body = AskBody(
        question="How do durable queues work?",
        submission_key="resource-submission-1",
    )
    first = await ResourcesAgentApiController.ask.fn(
        controller, body, _request(user.id), db_session
    )
    second = await ResourcesAgentApiController.ask.fn(
        controller, body, _request(user.id), db_session
    )
    assert first["id"] == second["id"]
    assert second["idempotent"] is True
    assert await db_session.scalar(select(func.count(ResourceAgentRun.id))) == 1
    assert await db_session.scalar(select(func.count(AgentConversation.id))) == 1

    user.is_active = False
    await db_session.commit()
    with pytest.raises(HTTPException) as exc:
        await ResourcesAgentApiController.ask.fn(
            controller,
            AskBody(question="Another", submission_key="resource-submission-2"),
            _request(user.id),
            db_session,
        )
    assert exc.value.status_code == 401


def test_browser_contract_includes_csrf_recovery_and_live_updates():
    javascript = Path("themes/smarterdev/static/js/chat.js").read_text()
    worker = Path("smarter_dev/web/chat/jobs.py").read_text()
    template = Path("themes/smarterdev/templates/chat/index.html").read_text()
    assert "X-CSRF-Token" in javascript
    assert "sk:notification" in javascript
    assert "sk:notification-status" in javascript
    assert "setInterval(reconcile" not in javascript
    assert "setInterval(refreshActivityTimers" in javascript
    # Terminal chat turns reconcile the DOM from the conversation snapshot
    # instead of reloading the page, so scroll/composer state survives.
    terminal = javascript.split("function handleTerminal")[1].split(
        "function notification"
    )[0]
    assert "reconcile();" in terminal
    chat_branch = terminal.split("if (mode === 'chat')")[1].split(
        "if (type === 'agent_run_error')"
    )[0]
    assert "location.reload" not in chat_branch
    assert "chat_turn_complete" in javascript and "chat_turn_stopped" in javascript
    assert "function syncThread" in javascript
    assert "content_html" in javascript
    assert "data-version-group" in javascript and "data-regenerate" in javascript
    assert "captureScroll" in javascript and "restoreScroll" in javascript
    output_delta = javascript.split("if (type === 'chat_output_delta')")[1].split(
        "if (type === 'chat_title_changed')"
    )[0]
    assert "pending.innerHTML = data.content_html" in output_delta
    assert "pending.textContent = data.content" not in output_delta
    assert "target.innerHTML = active.partial_html" in javascript
    assert "content_html=render_markdown(draft)" in worker
    assert "data-root-activity" in template
    assert "data-chat-agent-panel" in template
    assert "data-activity-timer" in template
    assert "data-chat-new-intelligence" in template
    assert "data-version-group" in template
    assert "data-chat-error" in template
    assert "data-chat-csrf" in template
    # Model, reasoning, usage, and sub-agents live behind disclosures now. The
    # controls are still server-rendered inside them so the page works without
    # scripting, and every hook chat.js drives is still present in the DOM.
    assert "data-chat-model" in template and "data-chat-reasoning" in template
    assert "data-chat-model-label" in template
    assert "data-media-instruction-row" in template and "data-media-instruction" in template
    assert "data-chat-subagents" in template and "data-chat-subagent-summary" in template
    for hook in (
        "data-context-tokens",
        "data-subagent-tokens",
        "data-total-tokens",
        "data-conversation-percent",
        "data-all-percent",
    ):
        assert hook in template
    assert 'aria-describedby="chat-composer-hint"' in template
    assert "syncModelLabel" in javascript and "syncMediaInstruction" in javascript
    assert "aria-expanded" in javascript
    assert "sdanswer.js" in template and "data-resource-running" in template
    assert "chat_document_created" in javascript
    # Documents are read in the dock — a column of the shell — rather than a
    # modal, so a document can sit beside the turn discussing it. The inline
    # cards stay in the thread and their Preview opens that same panel.
    assert "data-chat-dock" in template and "data-dock-toggle" in template
    assert "data-dock-list" in template and "data-dock-preview" in template
    assert "data-preview-content" in template and "data-dock-back" in template
    assert "data-open-document" in template and "openDocument" in javascript
    assert "data-chat-document-dialog" not in template
    assert "data-document-id" in template
    assert "/documents/" in javascript and "/download" in javascript
    # Rail and dock are collapsible and their state survives a reload, so the
    # workshop layout a reader sets up is still there next visit.
    assert "data-rail-toggle" in template
    assert 'data-rail="open"' in template and 'data-dock="closed"' in template
    assert "chat.rail" in javascript and "chat.dock" in javascript
    # Zero-width columns stay in the DOM, so both must be inert when folded or
    # their contents keep taking tab focus.
    assert "inert" in javascript
    # Selecting a passage in a document raises a composer that sends the
    # question with that passage quoted, so the agent answers about the
    # paragraph rather than the document.
    assert "data-chat-quote" in template and "data-quote-input" in template
    assert "data-quote-text" in template and "data-quote-source" in template
    assert "sendMessage" in javascript and "quotedPassage" in javascript
    # The dock is draggable, and the width is remembered per conversation with
    # the store capped so it cannot grow across a long history.
    assert "data-dock-resize" in template
    assert 'role="separator"' in template and "aria-orientation" in template
    assert "chat.dockWidths" in javascript and "DOCK_WIDTH_CAP" in javascript
    main = Path("main.py").read_text()
    assert "/storage/default/chat-attachments/" in main
    deploy = Path(".github/workflows/deploy.yaml").read_text()
    assert "deploy-chat-child-worker.yaml" in deploy
    jobs = Path("smarter_dev/web/chat/jobs.py").read_text()
    runtime = Path("smarter_dev/web/chat/runtime.py").read_text()
    assert '"chat_tool_event"' in jobs and "subagent_id" in jobs
    assert "model operation exceeded its hard timeout" not in runtime
    assert "timed out and was cancelled" not in jobs
    assert "cancellation_subscription" in runtime
    assert "await asyncio.sleep(interval)" not in runtime


@pytest.mark.asyncio
async def test_uploads_are_artifacts_listed_for_the_model_and_shelved_in_the_panel(
    db_session, monkeypatch
):
    """An upload is a file in the conversation, not a paste into the prompt.

    It shares the panel and the read tool with documents the agent wrote, it is
    announced rather than inlined, and its own preview knows the difference
    between markdown, a source file, and an image.
    """
    user, conversation = await _seed_chat(db_session)
    turn = WebChatTurn(
        conversation_id=conversation.id,
        sequence=1,
        submission_key="upload-turn",
        response_version_group=uuid4(),
        response_sequence=2,
        model_key=conversation.selected_model_key,
        reasoning_level=conversation.reasoning_level,
        status="complete",
        worker_lease_token=None,
    )
    db_session.add(turn)
    await db_session.flush()
    user_message = WebChatMessage(
        conversation_id=conversation.id,
        turn_id=turn.id,
        sequence=1,
        role="user",
        content="Have a look at these",
        version_group=uuid4(),
    )
    assistant = WebChatMessage(
        conversation_id=conversation.id,
        turn_id=turn.id,
        sequence=2,
        role="assistant",
        content="Looked.",
        version_group=turn.response_version_group,
    )
    db_session.add_all([user_message, assistant])
    await db_session.flush()
    written = WebChatDocument(
        conversation_id=conversation.id,
        turn_id=turn.id,
        assistant_message_id=assistant.id,
        tool_call_id="call-written",
        title="Review",
        filename="review.md",
        markdown_content="# Review\n\nLooks fine.",
        size_bytes=24,
        status="complete",
    )
    notes = WebChatAttachment(
        conversation_id=conversation.id,
        owner_user_id=user.id,
        turn_id=turn.id,
        storage_key=uuid4().hex,
        original_name="notes.md",
        media_type="text/markdown",
        size_bytes=42,
        sha256=uuid4().hex,
        extracted_text="# Uploaded notes\n\nThe secret is 12345.",
        status="ready",
    )
    script = WebChatAttachment(
        conversation_id=conversation.id,
        owner_user_id=user.id,
        turn_id=turn.id,
        storage_key=uuid4().hex,
        original_name="run.py",
        media_type="text/x-python",
        size_bytes=18,
        sha256=uuid4().hex,
        extracted_text="print('```')",
        status="ready",
    )
    chart = WebChatAttachment(
        conversation_id=conversation.id,
        owner_user_id=user.id,
        turn_id=turn.id,
        storage_key=uuid4().hex,
        original_name="chart.png",
        media_type="image/png",
        size_bytes=2048,
        sha256=uuid4().hex,
        summarization_instruction="Read the axis labels",
        status="ready",
    )
    staged = WebChatAttachment(
        conversation_id=conversation.id,
        owner_user_id=user.id,
        turn_id=None,
        storage_key=uuid4().hex,
        original_name="draft.txt",
        media_type="text/plain",
        size_bytes=9,
        sha256=uuid4().hex,
        extracted_text="not sent",
        status="ready",
    )
    db_session.add_all([written, notes, script, chart, staged])
    await db_session.commit()

    artifacts = await conversation_artifacts(
        db_session, conversation_id=conversation.id
    )
    by_name = {artifact.filename: artifact for artifact in artifacts}
    # A file still in the composer is not yet part of the conversation.
    assert "draft.txt" not in by_name
    assert by_name["review.md"].origin == "created"
    assert by_name["review.md"].kind == "markdown"
    assert by_name["notes.md"].origin == "upload"
    assert by_name["chart.png"].kind == "image"
    assert by_name["run.py"].kind == "text"

    # One namespace for the model: it names a file, not a table.
    found = await find_artifact(
        db_session, conversation_id=conversation.id, filename="CHART.PNG"
    )
    assert found is not None and found.is_upload and found.kind == "image"
    assert (
        await find_artifact(
            db_session, conversation_id=conversation.id, filename="review"
        )
    ).origin == "created"

    # The prompt gets an announcement, never the contents.
    manifest = _upload_manifest([notes, chart])
    assert "notes.md" in manifest and "chart.png" in manifest
    assert "The secret is 12345." not in manifest
    assert "summarization instruction" in manifest
    assert 'read_document("' in manifest

    async def entitled(*_args, **_kwargs):
        return {"sudo-r"}

    monkeypatch.setattr(chat_api, "require_entitled", entitled)
    controller = object.__new__(ChatApiController)

    markdown_preview = await ChatApiController.preview_attachment.fn(
        controller, conversation.id, notes.id, _request(user.id), db_session
    )
    assert "<h1>Uploaded notes</h1>" in markdown_preview.content["content_html"]
    assert markdown_preview.content["origin"] == "upload"
    assert markdown_preview.content["content_url"] is None

    # A source file rendered as prose is unreadable, so it is fenced — with a
    # fence long enough to survive the backticks inside the file itself.
    code_preview = await ChatApiController.preview_attachment.fn(
        controller, conversation.id, script.id, _request(user.id), db_session
    )
    assert "<code" in code_preview.content["content_html"]
    assert "print(" in code_preview.content["content_html"]

    image_preview = await ChatApiController.preview_attachment.fn(
        controller, conversation.id, chart.id, _request(user.id), db_session
    )
    assert image_preview.content["kind"] == "image"
    assert image_preview.content["content_html"] == ""
    assert "inline=true" in image_preview.content["content_url"]
    assert image_preview.content["summarization_instruction"] == "Read the axis labels"

    snapshot = await ChatApiController.get_conversation.fn(
        controller, conversation.id, _request(user.id), db_session
    )
    shelf = {item["filename"]: item for item in snapshot["artifacts"]}
    assert set(shelf) == {"review.md", "notes.md", "run.py", "chart.png"}
    assert shelf["notes.md"]["origin"] == "upload"
    assert shelf["review.md"]["origin"] == "created"
    # The inline cards under a reply stay written documents only.
    assert [item["filename"] for item in snapshot["documents"]] == ["review.md"]

    stranger = User(
        email=f"{uuid4().hex}@example.test", name="Stranger", is_active=True
    )
    db_session.add(stranger)
    await db_session.commit()
    with pytest.raises(HTTPException) as exc:
        await ChatApiController.preview_attachment.fn(
            controller, conversation.id, notes.id, _request(stranger.id), db_session
        )
    assert exc.value.status_code == 404


class _StubBackend:
    """A storage backend that records deletions, and can be told to fail one."""

    def __init__(self, fail: set[str] | None = None):
        self.deleted: list[str] = []
        self.fail = fail or set()

    async def delete(self, key: str) -> None:
        if key in self.fail:
            raise RuntimeError("store unavailable")
        self.deleted.append(key)


def _request_with_storage(user_id, backend, *, token: str = "csrf-token"):
    class _Manager:
        async def get(self, _name):
            return backend

    return SimpleNamespace(
        session={SESSION_USER_ID: str(user_id), CSRF_SESSION_KEY: token},
        headers={"X-CSRF-Token": token},
        app=SimpleNamespace(state=SimpleNamespace(storage_manager=_Manager())),
    )


@pytest.mark.asyncio
async def test_a_named_conversation_is_never_renamed_behind_the_owners_back(
    db_session, monkeypatch
):
    """The stand-in title only stands in while nothing has actually named it."""
    user, conversation = await _seed_chat(db_session)

    async def entitled(*_args, **_kwargs):
        return {"sudo-r"}

    async def do_not_dispatch(_dispatch_id):
        return None

    monkeypatch.setattr(chat_api, "require_entitled", entitled)
    monkeypatch.setattr(chat_api, "_try_dispatch", do_not_dispatch)
    controller = object.__new__(ChatApiController)

    renamed = await ChatApiController.update_conversation.fn(
        controller,
        conversation.id,
        ConversationUpdateBody(title="  Outbox\n  Design  "),
        _request(user.id),
        db_session,
    )
    assert renamed["title"] == "Outbox Design"
    assert renamed["archived"] is False

    await ChatApiController.submit_turn.fn(
        controller,
        conversation.id,
        TurnBody(content="Explain durable queues", submission_key="first"),
        _request(user.id),
        db_session,
    )

    await db_session.refresh(conversation)
    assert conversation.title == "Outbox Design"
    assert conversation.title_is_custom is True

    with pytest.raises(HTTPException) as exc:
        await ChatApiController.update_conversation.fn(
            controller,
            conversation.id,
            ConversationUpdateBody(title="   "),
            _request(user.id),
            db_session,
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_an_unnamed_conversation_still_takes_the_first_message_as_a_title(
    db_session, monkeypatch
):
    user, conversation = await _seed_chat(db_session)

    async def entitled(*_args, **_kwargs):
        return {"sudo-r"}

    async def do_not_dispatch(_dispatch_id):
        return None

    monkeypatch.setattr(chat_api, "require_entitled", entitled)
    monkeypatch.setattr(chat_api, "_try_dispatch", do_not_dispatch)
    controller = object.__new__(ChatApiController)

    await ChatApiController.submit_turn.fn(
        controller,
        conversation.id,
        TurnBody(content="Explain durable queues", submission_key="first"),
        _request(user.id),
        db_session,
    )
    await db_session.refresh(conversation)
    assert conversation.title == "Explain durable queues"
    # Still nobody's choice, so the agent's set_chat_title tool stays available.
    assert conversation.title_is_custom is False


@pytest.mark.asyncio
async def test_archiving_is_reversible_and_keeps_the_conversation(
    db_session, monkeypatch
):
    user, conversation = await _seed_chat(db_session)

    async def entitled(*_args, **_kwargs):
        return {"sudo-r"}

    monkeypatch.setattr(chat_api, "require_entitled", entitled)
    controller = object.__new__(ChatApiController)

    archived = await ChatApiController.update_conversation.fn(
        controller,
        conversation.id,
        ConversationUpdateBody(archived=True),
        _request(user.id),
        db_session,
    )
    await db_session.refresh(conversation)
    assert archived["archived"] is True and conversation.archived_at is not None

    restored = await ChatApiController.update_conversation.fn(
        controller,
        conversation.id,
        ConversationUpdateBody(archived=False),
        _request(user.id),
        db_session,
    )
    await db_session.refresh(conversation)
    assert restored["archived"] is False and conversation.archived_at is None
    assert await db_session.get(WebChatConversation, conversation.id) is not None


@pytest.mark.asyncio
async def test_deleting_a_conversation_destroys_its_transcript_and_its_uploads(
    db_session, monkeypatch
):
    user, conversation = await _seed_chat(db_session)

    async def entitled(*_args, **_kwargs):
        return {"sudo-r"}

    async def do_not_dispatch(_dispatch_id):
        return None

    monkeypatch.setattr(chat_api, "require_entitled", entitled)
    monkeypatch.setattr(chat_api, "_try_dispatch", do_not_dispatch)
    controller = object.__new__(ChatApiController)
    created = await ChatApiController.submit_turn.fn(
        controller,
        conversation.id,
        TurnBody(content="Delete me", submission_key="doomed"),
        _request(user.id),
        db_session,
    )
    turn_id = UUID(created["turn_id"])
    turn = await db_session.get(WebChatTurn, turn_id)
    turn.status = "complete"
    upload = WebChatAttachment(
        conversation_id=conversation.id,
        owner_user_id=user.id,
        turn_id=turn_id,
        storage_key=uuid4().hex,
        original_name="notes.md",
        media_type="text/markdown",
        size_bytes=42,
        sha256=uuid4().hex,
        extracted_text="private",
        status="ready",
    )
    db_session.add(upload)
    await db_session.commit()
    backend = _StubBackend()

    result = await ChatApiController.delete_conversation.fn(
        controller,
        conversation.id,
        _request_with_storage(user.id, backend),
        db_session,
    )

    assert result == {"status": "deleted"}
    assert backend.deleted == [upload.storage_key]
    assert await db_session.get(WebChatConversation, conversation.id) is None
    assert await db_session.scalar(select(func.count(WebChatTurn.id))) == 0
    assert await db_session.scalar(select(func.count(WebChatMessage.id))) == 0
    assert await db_session.scalar(select(func.count(WebChatAttachment.id))) == 0


@pytest.mark.asyncio
async def test_a_stranded_upload_leaves_the_conversation_deletable_again(
    db_session, monkeypatch
):
    """A store that will not delete must not leave private files unreferenced.

    The conversation survives so the owner can retry, and the row stays behind
    marked ``deleting`` — which is exactly what the orphan reconciler collects.
    """
    user, conversation = await _seed_chat(db_session)

    async def entitled(*_args, **_kwargs):
        return {"sudo-r"}

    monkeypatch.setattr(chat_api, "require_entitled", entitled)
    controller = object.__new__(ChatApiController)
    upload = WebChatAttachment(
        conversation_id=conversation.id,
        owner_user_id=user.id,
        turn_id=None,
        storage_key=uuid4().hex,
        original_name="notes.md",
        media_type="text/markdown",
        size_bytes=42,
        sha256=uuid4().hex,
        extracted_text="private",
        status="ready",
    )
    db_session.add(upload)
    await db_session.commit()
    backend = _StubBackend(fail={upload.storage_key})

    with pytest.raises(HTTPException) as exc:
        await ChatApiController.delete_conversation.fn(
            controller,
            conversation.id,
            _request_with_storage(user.id, backend),
            db_session,
        )

    assert exc.value.status_code == 502
    await db_session.refresh(upload)
    assert upload.status == "deleting"
    assert await db_session.get(WebChatConversation, conversation.id) is not None


@pytest.mark.asyncio
async def test_deleting_is_refused_while_a_turn_is_still_running(
    db_session, monkeypatch
):
    user, conversation = await _seed_chat(db_session)

    async def entitled(*_args, **_kwargs):
        return {"sudo-r"}

    async def do_not_dispatch(_dispatch_id):
        return None

    monkeypatch.setattr(chat_api, "require_entitled", entitled)
    monkeypatch.setattr(chat_api, "_try_dispatch", do_not_dispatch)
    controller = object.__new__(ChatApiController)
    await ChatApiController.submit_turn.fn(
        controller,
        conversation.id,
        TurnBody(content="Still going", submission_key="busy"),
        _request(user.id),
        db_session,
    )

    with pytest.raises(HTTPException) as exc:
        await ChatApiController.delete_conversation.fn(
            controller,
            conversation.id,
            _request_with_storage(user.id, _StubBackend()),
            db_session,
        )

    assert exc.value.status_code == 409
    assert await db_session.get(WebChatConversation, conversation.id) is not None


@pytest.mark.asyncio
async def test_the_rail_hides_archived_chats_in_their_own_drawer(
    db_session, monkeypatch
):
    user, live = await _seed_chat(db_session)
    filed = WebChatConversation(
        owner_user_id=user.id,
        intelligence_mode="efficient",
        selected_model_key=live.selected_model_key,
        reasoning_level=live.reasoning_level,
        title="Last month's spike",
        status="idle",
        archived_at=datetime.now(UTC),
    )
    db_session.add(filed)
    await db_session.commit()

    rail = await chat_controller._rail_context(db_session, user.id)

    assert [row.id for row in rail["conversations"]] == [live.id]
    assert [row.id for row in rail["archived_conversations"]] == [filed.id]
    assert all(
        row.id != filed.id
        for group in rail["conversation_groups"]
        for row in group["items"]
    )


@pytest.mark.asyncio
async def test_the_agents_name_never_overwrites_one_the_owner_chose(db_session):
    """The tool writes only while the conversation is still unnamed.

    The owner can rename from the rail while a turn is running, so the agent's
    name has to lose that race rather than quietly win it.
    """
    _user, conversation = await _seed_chat(db_session)

    assert await name_if_unnamed(
        db_session, conversation_id=conversation.id, title="Durable Queue Design"
    )
    await db_session.refresh(conversation)
    assert conversation.title == "Durable Queue Design"
    assert conversation.title_is_custom is True

    assert not await name_if_unnamed(
        db_session, conversation_id=conversation.id, title="Something Else"
    )
    await db_session.refresh(conversation)
    assert conversation.title == "Durable Queue Design"


async def _seed_document(
    db_session, user, conversation, *, filename, body, key, status="complete"
):
    """A settled document plus the turn and assistant message that wrote it."""
    turn = WebChatTurn(
        conversation_id=conversation.id,
        sequence=await db_session.scalar(
            select(func.count(WebChatTurn.id)).where(
                WebChatTurn.conversation_id == conversation.id
            )
        )
        + 1,
        submission_key=key,
        response_version_group=uuid4(),
        response_sequence=2,
        model_key=conversation.selected_model_key,
        reasoning_level=conversation.reasoning_level,
        # Settled: only one turn of a conversation may be active at a time, and
        # the write that produced this document is long finished.
        status="complete",
        worker_lease_token=f"lease-{key}",
    )
    db_session.add(turn)
    await db_session.flush()
    assistant = WebChatMessage(
        conversation_id=conversation.id,
        turn_id=turn.id,
        sequence=turn.sequence * 2,
        role="assistant",
        content="",
        version_group=turn.response_version_group,
    )
    db_session.add(assistant)
    await db_session.flush()
    document = WebChatDocument(
        conversation_id=conversation.id,
        turn_id=turn.id,
        assistant_message_id=assistant.id,
        tool_call_id=f"call-{key}",
        title=filename.removesuffix(".md").title(),
        filename=filename,
        markdown_content=body,
        size_bytes=len(body.encode()),
        status=status,
    )
    db_session.add(document)
    await db_session.commit()
    return turn, document


@pytest.mark.asyncio
async def test_a_second_write_of_one_name_is_a_clash_but_a_retry_is_not(db_session):
    """The clash check has to tell a new write apart from a redelivered one."""
    user, conversation = await _seed_chat(db_session)
    turn, document = await _seed_document(
        db_session,
        user,
        conversation,
        filename="report.md",
        body="# Report\n\nOriginal.\n",
        key="first-write",
    )

    # A different call writing the same name: a real clash.
    assert (
        await find_name_clash(
            db_session,
            conversation_id=conversation.id,
            filename="report.md",
            turn_id=turn.id,
            tool_call_id="a-different-call",
        )
    ).id == document.id

    # The same call delivered twice must not collide with its own document.
    assert (
        await find_name_clash(
            db_session,
            conversation_id=conversation.id,
            filename="report.md",
            turn_id=turn.id,
            tool_call_id="call-first-write",
        )
        is None
    )

    # A name nothing has taken is free.
    assert (
        await find_name_clash(
            db_session,
            conversation_id=conversation.id,
            filename="notes.md",
            turn_id=turn.id,
            tool_call_id="another-call",
        )
        is None
    )


@pytest.mark.asyncio
async def test_a_successful_overwrite_retires_the_file_it_replaced(db_session):
    user, conversation = await _seed_chat(db_session)
    _first_turn, original = await _seed_document(
        db_session,
        user,
        conversation,
        filename="report.md",
        body="# Report\n\nVersion one.\n",
        key="first-write",
    )
    _second_turn, replacement = await _seed_document(
        db_session,
        user,
        conversation,
        filename="report.md",
        body="# Report\n\nVersion two.\n",
        key="second-write",
    )

    retired = await supersede_replaced_documents(
        db_session,
        conversation_id=conversation.id,
        filename="report.md",
        keep_id=replacement.id,
    )
    await db_session.commit()

    assert retired == [original.id]
    await db_session.refresh(original)
    assert original.status == "superseded"
    # The row survives — this is a retirement, not a deletion.
    assert original.markdown_content == "# Report\n\nVersion one.\n"

    # One name, one file, from every direction that resolves one.
    resolved = await find_readable_document(
        db_session, conversation_id=conversation.id, filename="report.md"
    )
    assert resolved.id == replacement.id
    shelf = await conversation_artifacts(db_session, conversation_id=conversation.id)
    assert [item.id for item in shelf] == [replacement.id]


@pytest.mark.asyncio
async def test_an_abandoned_overwrite_leaves_the_original_standing(db_session):
    """The whole point of writing into a new row rather than resetting the old."""
    user, conversation = await _seed_chat(db_session)
    _first_turn, original = await _seed_document(
        db_session,
        user,
        conversation,
        filename="report.md",
        body="# Report\n\nThe good version.\n",
        key="first-write",
    )
    _second_turn, wreck = await _seed_document(
        db_session,
        user,
        conversation,
        filename="report.md",
        body="# Rep",
        key="crashed-write",
        status="stopped",
    )

    await retire_failed_overwrite(db_session, document_id=wreck.id)
    await db_session.commit()

    await db_session.refresh(original)
    await db_session.refresh(wreck)
    assert original.status == "complete"
    assert wreck.status == "superseded"
    resolved = await find_readable_document(
        db_session, conversation_id=conversation.id, filename="report.md"
    )
    assert resolved.id == original.id
    assert resolved.markdown_content == "# Report\n\nThe good version.\n"


@pytest.mark.asyncio
async def test_editing_rewrites_the_body_under_the_editing_turns_lease(db_session):
    """An edit happens turns after the write, so it carries its own lease."""
    user, conversation = await _seed_chat(db_session)
    write_turn, document = await _seed_document(
        db_session,
        user,
        conversation,
        filename="report.md",
        body="# Report\n\nThe queue is slow.\n",
        key="first-write",
        status="truncated",
    )
    edit_turn = WebChatTurn(
        conversation_id=conversation.id,
        sequence=9,
        submission_key="edit-turn",
        response_version_group=uuid4(),
        response_sequence=2,
        model_key=conversation.selected_model_key,
        status="running",
        worker_lease_token="edit-lease",
    )
    db_session.add(edit_turn)
    await db_session.commit()

    patched = apply_document_patches(
        document.markdown_content,
        [("The queue is slow.", "The queue is slow under burst load.")],
    )
    settled = await edit_document_body(
        db_session,
        document_id=document.id,
        turn_id=edit_turn.id,
        worker_lease_token="edit-lease",
        markdown=patched,
    )
    await db_session.commit()

    assert settled.markdown_content.endswith("under burst load.\n")
    assert settled.size_bytes == len(patched.encode())
    # Patching a truncated file does not make it whole.
    assert settled.status == "truncated"
    # The card stays under the reply that wrote it, not the one that edited it.
    assert settled.turn_id == write_turn.id

    # A worker whose lease has moved on cannot write.
    with pytest.raises(RunSuperseded):
        await edit_document_body(
            db_session,
            document_id=document.id,
            turn_id=edit_turn.id,
            worker_lease_token="a-stale-lease",
            markdown="anything",
        )


@pytest.mark.asyncio
async def test_a_superseded_document_is_neither_served_nor_downloadable(
    db_session, monkeypatch
):
    user, conversation = await _seed_chat(db_session)
    _turn, document = await _seed_document(
        db_session,
        user,
        conversation,
        filename="report.md",
        body="# Report\n\nOld.\n",
        key="first-write",
    )
    document.status = "superseded"
    await db_session.commit()

    async def entitled(*_args, **_kwargs):
        return {"sudo-r"}

    monkeypatch.setattr(chat_api, "require_entitled", entitled)
    controller = object.__new__(ChatApiController)

    for handler in (
        ChatApiController.get_document,
        ChatApiController.download_document,
    ):
        with pytest.raises(HTTPException) as exc:
            await handler.fn(
                controller, conversation.id, document.id, _request(user.id), db_session
            )
        assert exc.value.status_code == 410

    snapshot = await ChatApiController.get_conversation.fn(
        controller, conversation.id, _request(user.id), db_session
    )
    assert snapshot["artifacts"] == []
    assert snapshot["documents"] == []
