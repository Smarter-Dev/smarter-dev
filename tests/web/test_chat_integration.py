"""Database-backed integration tests for the unified Chat durability seams."""

from __future__ import annotations

from contextlib import asynccontextmanager
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
from smarter_dev.web.chat import dispatch as chat_dispatch
from smarter_dev.web.chat.api import ChatApiController
from smarter_dev.web.chat.api import ReasoningBody
from smarter_dev.web.chat.api import TurnBody
from smarter_dev.web.chat.csrf import require_api_csrf
from smarter_dev.web.chat.dispatch import ensure_submission_handler
from smarter_dev.web.chat.documents import append_document_body
from smarter_dev.web.chat.documents import begin_document
from smarter_dev.web.chat.documents import conversation_artifacts
from smarter_dev.web.chat.documents import find_artifact
from smarter_dev.web.chat.documents import find_readable_document
from smarter_dev.web.chat.documents import finish_document
from smarter_dev.web.chat.jobs import _upload_manifest
from smarter_dev.web.chat.limits import OperationAlreadyReserved
from smarter_dev.web.chat.limits import reserve_operation
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
        selected_model_key="gemini-3-1-flash-lite",
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
    model = get_model("gemini-3-1-flash-lite")

    first = await record_settled_chat_usage(
        db_session,
        operation_key=operation_key,
        operation_type="primary",
        model=model,
        tier="r",
        input_tokens=4_400_000,
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
    assert first.cost_usd == Decimal("1.1")
    assert first.overage_cost_usd == Decimal("0.1")
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
