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
from smarter_dev.web.chat.documents import store_markdown_document
from smarter_dev.web.chat.limits import OperationAlreadyReserved
from smarter_dev.web.chat.limits import reserve_operation
from smarter_dev.web.chat.settings import ensure_settings
from smarter_dev.web.chat.usage import record_settled_chat_usage
from smarter_dev.web.models import AgentConversation
from smarter_dev.web.models import ChatSpendLimit
from smarter_dev.web.models import ChatSpendReservation
from smarter_dev.web.models import ResourceAgentRun
from smarter_dev.web.models import UsageCostRow
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
async def test_markdown_document_is_idempotent_private_and_downloadable(
    db_session, monkeypatch
):
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

    first, first_created = await store_markdown_document(
        db_session,
        conversation_id=conversation.id,
        turn_id=turn.id,
        assistant_message_id=assistant.id,
        worker_lease_token="lease-token",
        tool_call_id="tool-call-1",
        title="Durable queues",
        filename="durable-queues",
        markdown="# Durable queues\n\nUse an outbox.",
    )
    await db_session.commit()
    second, second_created = await store_markdown_document(
        db_session,
        conversation_id=conversation.id,
        turn_id=turn.id,
        assistant_message_id=assistant.id,
        worker_lease_token="lease-token",
        tool_call_id="tool-call-1",
        title="Ignored retry title",
        filename="ignored.md",
        markdown="Ignored retry body",
    )
    await db_session.commit()

    assert first.id == second.id
    assert first_created is True and second_created is False
    assert await db_session.scalar(select(func.count(WebChatDocument.id))) == 1

    async def entitled(*_args, **_kwargs):
        return {"sudo-r"}

    monkeypatch.setattr(chat_api, "require_entitled", entitled)
    controller = object.__new__(ChatApiController)
    preview_response = await ChatApiController.get_document.fn(
        controller, conversation.id, first.id, _request(user.id), db_session
    )
    preview = preview_response.content
    assert preview["title"] == "Durable queues"
    assert "<h1>Durable queues</h1>" in preview["content_html"]
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
    assert "data-root-activity" in template
    assert "data-chat-agent-panel" in template
    assert "data-activity-timer" in template
    assert "data-chat-new-intelligence" in template
    assert "data-version-group" in template
    assert "data-chat-error" in template
    assert "data-chat-csrf" in template
    assert "sdanswer.js" in template and "data-resource-running" in template
    assert "chat_document_created" in javascript
    assert "data-chat-document-dialog" in template
    assert "data-document-id" in template
    assert "/documents/" in javascript and "/download" in javascript
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
