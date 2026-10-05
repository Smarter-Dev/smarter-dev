"""A member can delete what they made on the website, and every copy of it goes.

The routes run as the site runs them, against the test database; only object
storage, the worker hand-off and Skrift's settings are stood in for.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from litestar.exceptions import HTTPException
from skrift.auth.session_keys import SESSION_USER_ID
from skrift.db.models.notification import StoredNotification
from skrift.db.models.push_subscription import PushSubscription
from skrift.db.models.user import User
from sqlalchemy import func
from sqlalchemy import select

from smarter_dev.web import agent_api
from smarter_dev.web import dashboard_controller
from smarter_dev.web.agent_api import AgentConversationApiController
from smarter_dev.web.chat import api as chat_api
from smarter_dev.web.chat import jobs as chat_jobs
from smarter_dev.web.chat.api import ChatApiController
from smarter_dev.web.chat.csrf import CSRF_SESSION_KEY
from smarter_dev.web.chat.settings import ensure_settings
from smarter_dev.web.chat.threads import QUICK_CHAT_MODE
from smarter_dev.web.chat.usage import forget_stale_reply_copies
from smarter_dev.web.dashboard_controller import DashboardController
from smarter_dev.web.models import AccountDeletionRequest
from smarter_dev.web.models import AgentConversation
from smarter_dev.web.models import AgentMessage
from smarter_dev.web.models import ResourceAgentRun
from smarter_dev.web.models import UsageCostRow
from smarter_dev.web.models import WebChatAttachment
from smarter_dev.web.models import WebChatConversation
from smarter_dev.web.models import WebChatTurn
from smarter_dev.web.models import WebSearchLink
from smarter_dev.web.models import WebSearchRun
from smarter_dev.web.models import WorkDispatch

_WORDS = "how do I size a connection pool for a burst of webhooks"


class _Storage:
    """Object storage that records what it was asked to delete."""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def delete(self, key: str) -> None:
        self.deleted.append(key)

    async def list_keys(self, prefix: str = ""):
        for key in ():
            yield key


def _request(user_id, storage: _Storage | None = None):
    class _Manager:
        async def get(self, _name):
            return storage

    return SimpleNamespace(
        session={SESSION_USER_ID: str(user_id), CSRF_SESSION_KEY: "csrf"},
        headers={"X-CSRF-Token": "csrf"},
        app=SimpleNamespace(state=SimpleNamespace(storage_manager=_Manager())),
    )


async def _user(db_session) -> User:
    connection = await db_session.connection()
    await connection.run_sync(lambda sync: User.metadata.create_all(sync))
    user = User(email=f"{uuid4().hex}@example.test", name="Member", is_active=True)
    db_session.add(user)
    await db_session.flush()
    await ensure_settings(db_session)
    await db_session.commit()
    return user


async def _count(db_session, model, *conditions) -> int:
    db_session.expire_all()
    return await db_session.scalar(
        select(func.count()).select_from(model).where(*conditions)
    )


def _cost(user_id, *, mode, operation, **ids) -> UsageCostRow:
    return UsageCostRow(
        operation_key=uuid4().hex,
        product_mode=mode,
        operation_type=operation,
        user_id=user_id,
        provider_key="openai",
        catalog_model_key="gpt-6-luna",
        model_id="gpt-6-luna",
        input_tokens=10,
        output_tokens=10,
        cost_usd=Decimal("0.001"),
        metered_at=datetime.now(UTC),
        **ids,
    )


# ── Chat ─────────────────────────────────────────────────────────────


async def _chat(
    db_session, user, *, mode="standard", turn_status="complete", archived=False
):
    conversation = WebChatConversation(
        owner_user_id=user.id,
        intelligence_mode="efficient",
        selected_model_key="gpt-6-luna",
        reasoning_level="medium",
        title="Pool sizing",
        status="idle",
        chat_mode=mode,
        archived_at=datetime.now(UTC) if archived else None,
    )
    db_session.add(conversation)
    await db_session.flush()
    turn = WebChatTurn(
        conversation_id=conversation.id,
        sequence=1,
        submission_key=uuid4().hex[:16],
        response_version_group=uuid4(),
        response_sequence=2,
        model_key="gpt-6-luna",
        reasoning_level="medium",
        status=turn_status,
    )
    db_session.add(turn)
    await db_session.flush()
    upload = WebChatAttachment(
        conversation_id=conversation.id,
        owner_user_id=user.id,
        turn_id=turn.id,
        storage_key=uuid4().hex,
        original_name="notes.md",
        media_type="text/markdown",
        size_bytes=len(_WORDS),
        sha256=uuid4().hex,
        extracted_text=_WORDS,
        status="ready",
    )
    reply = _cost(
        user.id,
        mode="chat",
        operation="chat_turn",
        conversation_id=conversation.id,
        root_turn_id=turn.id,
    )
    reply.details = {
        "model_response": {"parts": [{"content": _WORDS}]},
        "durable_delta": [{"parts": [{"content": _WORDS}]}],
        "request_ordinal": 1,
        "response_complete": True,
    }
    db_session.add_all(
        [
            upload,
            reply,
            WorkDispatch(
                job_type="chat.turn.run",
                aggregate_id=turn.id,
                payload={"turn_id": str(turn.id)},
                status="complete",
            ),
        ]
    )
    await db_session.commit()
    return conversation, turn, upload, reply


@pytest.mark.asyncio
async def test_deleting_a_chat_takes_the_copies_of_its_replies_too(
    db_session, monkeypatch
):
    user = await _user(db_session)
    conversation, turn, upload, reply = await _chat(db_session, user)
    conversation_id, turn_id, upload_key, reply_id = (
        conversation.id,
        turn.id,
        upload.storage_key,
        reply.id,
    )

    async def lapsed(*_args, **_kwargs):
        raise AssertionError("deleting needs no Chat plan")

    monkeypatch.setattr(chat_api, "require_entitled", lapsed)
    storage = _Storage()

    result = await ChatApiController.delete_conversation.fn(
        object.__new__(ChatApiController),
        conversation_id,
        _request(user.id, storage),
        db_session,
    )

    assert result == {"status": "deleted"}
    assert storage.deleted == [upload_key]
    assert await _count(db_session, WebChatConversation) == 0
    assert await _count(db_session, WebChatTurn) == 0
    assert (
        await _count(db_session, WorkDispatch, WorkDispatch.aggregate_id == turn_id)
        == 0
    )
    # The cost row stays, for spend and limits, without the reply it carried.
    cost = await db_session.get(UsageCostRow, reply_id)
    assert cost is not None and _WORDS not in json.dumps(cost.details)
    assert cost.details["response_complete"] is True


@pytest.mark.asyncio
async def test_deleting_all_chats_includes_the_quick_chat_and_skips_a_running_one(
    db_session,
):
    user = await _user(db_session)
    await _chat(db_session, user)
    await _chat(db_session, user, mode=QUICK_CHAT_MODE)
    await _chat(db_session, user, archived=True)
    running, *_ = await _chat(db_session, user, turn_status="running")
    running_id = running.id
    storage = _Storage()

    result = await ChatApiController.delete_all_conversations.fn(
        object.__new__(ChatApiController), _request(user.id, storage), db_session
    )

    assert result == {"deleted": 3, "running": 1, "stranded": 0}
    assert len(storage.deleted) == 3
    db_session.expire_all()
    assert (await db_session.scalars(select(WebChatConversation.id))).all() == [
        running_id
    ]


@pytest.mark.asyncio
async def test_a_chat_whose_uploads_cannot_be_removed_is_kept_and_counted(db_session):
    user = await _user(db_session)
    _, _, stuck, _ = await _chat(db_session, user)
    await _chat(db_session, user)
    stuck_key = stuck.storage_key

    class _Refusing(_Storage):
        async def delete(self, key: str) -> None:
            if key == stuck_key:
                raise RuntimeError("store unavailable")
            await super().delete(key)

    result = await ChatApiController.delete_all_conversations.fn(
        object.__new__(ChatApiController), _request(user.id, _Refusing()), db_session
    )

    assert result == {"deleted": 1, "running": 0, "stranded": 1}
    assert await _count(db_session, WebChatConversation) == 1


@pytest.mark.asyncio
async def test_a_single_quick_chat_delete_is_still_refused(db_session):
    user = await _user(db_session)
    quick, *_ = await _chat(db_session, user, mode=QUICK_CHAT_MODE)

    with pytest.raises(HTTPException) as refused:
        await ChatApiController.delete_conversation.fn(
            object.__new__(ChatApiController),
            quick.id,
            _request(user.id, _Storage()),
            db_session,
        )

    assert refused.value.status_code == 409


# ── Questions about our resources ────────────────────────────────────


async def _question(db_session, user, *, status="complete", stalled=False):
    conversation = AgentConversation(
        owner_user_id=user.id, agent_type="resources", title="Pool sizing"
    )
    db_session.add(conversation)
    await db_session.flush()
    run = ResourceAgentRun(
        conversation_id=conversation.id,
        owner_user_id=user.id,
        user_sequence=1,
        submission_key=uuid4().hex[:16],
        question=_WORDS,
        status=status,
    )
    db_session.add_all(
        [
            AgentMessage(
                conversation_id=conversation.id, sequence=1, role="user", content=_WORDS
            ),
            AgentMessage(
                conversation_id=conversation.id,
                sequence=2,
                role="assistant",
                content="Queue it.",
            ),
            run,
        ]
    )
    await db_session.flush()
    if stalled:
        run.updated_at = datetime.now(UTC) - timedelta(hours=1)
    db_session.add_all(
        [
            WorkDispatch(
                job_type="resources.agent.run",
                aggregate_id=run.id,
                # Written before Resources jobs stopped carrying the question.
                payload={"run_id": str(run.id), "question": _WORDS},
                status="complete",
            ),
            StoredNotification(
                scope="source",
                scope_id=f"user:{user.id}",
                source_key=f"user:{user.id}",
                type="agent_reframe_ready",
                payload_json=json.dumps(
                    {"conversation_id": str(conversation.id), "message": _WORDS}
                ),
            ),
            _cost(
                user.id,
                mode="resources",
                operation="resource_author",
                conversation_id=conversation.id,
                root_turn_id=run.id,
            ),
        ]
    )
    await db_session.commit()
    return conversation


@pytest.mark.asyncio
async def test_deleting_a_question_takes_its_answers_and_every_copy(db_session):
    user = await _user(db_session)
    question = await _question(db_session, user)
    other = await _question(db_session, user)
    question_id, other_id = question.id, other.id

    result = await AgentConversationApiController.delete_conversation.fn(
        object.__new__(AgentConversationApiController),
        question_id,
        _request(user.id),
        db_session,
    )

    assert result == {"status": "deleted"}
    assert (
        await _count(db_session, AgentConversation, AgentConversation.id == question_id)
        == 0
    )
    assert (
        await _count(
            db_session, AgentMessage, AgentMessage.conversation_id == question_id
        )
        == 0
    )
    assert (
        await _count(
            db_session,
            ResourceAgentRun,
            ResourceAgentRun.conversation_id == question_id,
        )
        == 0
    )
    db_session.expire_all()
    remaining = (await db_session.scalars(select(WorkDispatch.payload))).all()
    assert [payload["run_id"] for payload in remaining] != [] and len(remaining) == 1
    notes = (await db_session.scalars(select(StoredNotification.payload_json))).all()
    assert notes == [json.dumps({"conversation_id": str(other_id), "message": _WORDS})]


@pytest.mark.asyncio
async def test_a_question_is_deleted_only_by_its_asker_and_not_while_it_is_answered(
    db_session,
):
    asker, stranger = await _user(db_session), await _user(db_session)
    answering = await _question(db_session, asker, status="running")
    stalled = await _question(db_session, asker, status="running", stalled=True)
    answering_id, stalled_id = answering.id, stalled.id
    controller = object.__new__(AgentConversationApiController)

    with pytest.raises(HTTPException) as not_theirs:
        await AgentConversationApiController.delete_conversation.fn(
            controller, answering_id, _request(stranger.id), db_session
        )
    with pytest.raises(HTTPException) as busy:
        await AgentConversationApiController.delete_conversation.fn(
            controller, answering_id, _request(asker.id), db_session
        )
    # A run nothing has touched for a while was abandoned by its worker.
    await AgentConversationApiController.delete_conversation.fn(
        controller, stalled_id, _request(asker.id), db_session
    )

    assert (not_theirs.value.status_code, busy.value.status_code) == (404, 409)
    db_session.expire_all()
    assert (await db_session.scalars(select(AgentConversation.id))).all() == [
        answering_id
    ]


@pytest.mark.asyncio
async def test_deleting_questions_does_not_give_back_the_weekly_quota(db_session):
    user = await _user(db_session)
    await _question(db_session, user)
    await _question(db_session, user)
    before = await agent_api._count_questions_last_week(
        db_session, user.id, "resources"
    )

    result = await AgentConversationApiController.delete_all_conversations.fn(
        object.__new__(AgentConversationApiController), _request(user.id), db_session
    )

    assert result == {"deleted": 2, "running": 0}
    assert before == 2
    assert (
        await agent_api._count_questions_last_week(db_session, user.id, "resources")
        == 2
    )


@pytest.mark.asyncio
async def test_a_deleted_old_question_followed_up_this_week_is_not_a_new_question(
    db_session,
):
    user = await _user(db_session)
    old = await _question(db_session, user)
    old_id = old.id
    # Asked last month; this week's cost row is a follow-up.
    first = _cost(
        user.id, mode="resources", operation="resource_author", conversation_id=old_id
    )
    first.metered_at = datetime.now(UTC) - timedelta(days=30)
    db_session.add(first)
    await db_session.commit()

    await AgentConversationApiController.delete_conversation.fn(
        object.__new__(AgentConversationApiController),
        old_id,
        _request(user.id),
        db_session,
    )

    assert (
        await agent_api._count_questions_last_week(db_session, user.id, "resources")
        == 0
    )


@pytest.mark.asyncio
async def test_a_resources_run_a_worker_holds_is_not_deleted(db_session):
    user = await _user(db_session)
    question = await _question(db_session, user, status="running", stalled=True)
    question_id = question.id
    run = await db_session.scalar(select(ResourceAgentRun))
    # Its row has not changed for an hour, but a worker's lease on it is live.
    run.worker_lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
    run.updated_at = datetime.now(UTC) - timedelta(hours=1)
    await db_session.commit()

    with pytest.raises(HTTPException) as busy:
        await AgentConversationApiController.delete_conversation.fn(
            object.__new__(AgentConversationApiController),
            question_id,
            _request(user.id),
            db_session,
        )

    assert busy.value.status_code == 409


# ── Dashboard searches ───────────────────────────────────────────────


async def _search(
    db_session, user, *, status="complete", stalled=False, anonymous=False
):
    run = WebSearchRun(
        owner_user_id=user.id,
        submission_key=uuid4().hex[:16],
        request=_WORDS,
        status=status,
        queries=[_WORDS],
        results=[{"title": "Pools", "snippet": _WORDS}],
        ranked=True,
        usage={},
        version=1,
        attempt_count=1,
    )
    db_session.add(run)
    await db_session.flush()
    if stalled:
        run.updated_at = datetime.now(UTC) - timedelta(hours=1)
    cost = _cost(
        user.id, mode="search", operation="web_search_queries", root_turn_id=run.id
    )
    cost.details = {
        "web_search_id": str(run.id),
        **({"anonymous": True} if anonymous else {}),
    }
    db_session.add_all(
        [
            cost,
            WorkDispatch(
                job_type="web_search.run",
                aggregate_id=run.id,
                payload={"search_id": str(run.id)},
                status="complete",
            ),
        ]
    )
    await db_session.commit()
    return run


def _dashboard_request(user_id):
    request = _request(user_id)
    request.session["user_id"] = str(user_id)
    return request


@pytest.mark.asyncio
async def test_deleting_a_search_takes_its_results_and_answer(db_session, monkeypatch):
    user = await _user(db_session)
    search = await _search(db_session, user)
    running = await _search(db_session, user, status="searching")
    search_id, running_id = search.id, running.id
    monkeypatch.setattr(
        dashboard_controller, "_session_user_id", lambda _request: user.id
    )
    controller = object.__new__(DashboardController)

    result = await DashboardController.delete_one_search.fn(
        controller, _dashboard_request(user.id), db_session, search_id
    )
    with pytest.raises(HTTPException) as busy:
        await DashboardController.delete_one_search.fn(
            controller, _dashboard_request(user.id), db_session, running_id
        )

    assert [row["id"] for row in result["searches"]] == [str(running_id)]
    assert busy.value.status_code == 409
    assert await _count(db_session, WebSearchRun, WebSearchRun.id == search_id) == 0
    assert (
        await _count(db_session, WorkDispatch, WorkDispatch.aggregate_id == search_id)
        == 0
    )


@pytest.mark.asyncio
async def test_deleting_searches_does_not_reset_the_daily_limit(
    db_session, monkeypatch
):
    user = await _user(db_session)
    await _search(db_session, user)
    await _search(db_session, user, status="planning", stalled=True)
    # Run through the member's link while signed out: charged to them, not counted.
    await _search(db_session, user, anonymous=True)
    await db_session.execute(
        WebSearchRun.__table__.delete()
        .where(WebSearchRun.request == _WORDS)
        .where(
            WebSearchRun.id.in_(
                select(UsageCostRow.root_turn_id).where(
                    UsageCostRow.details["anonymous"].as_boolean().is_(True)
                )
            )
        )
    )
    await db_session.commit()
    monkeypatch.setattr(
        dashboard_controller, "_session_user_id", lambda _request: user.id
    )
    assert await dashboard_controller.searches_in_last_day(db_session, user.id) == 2

    result = await DashboardController.delete_every_search.fn(
        object.__new__(DashboardController), _dashboard_request(user.id), db_session
    )

    assert (result["deleted"], result["running"], result["searches"]) == (2, 0, [])
    assert await dashboard_controller.searches_in_last_day(db_session, user.id) == 2


# ── Account deletion ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deleting_the_account_leaves_none_of_it_behind(db_session, monkeypatch):
    user = await _user(db_session)
    user_id = user.id
    await _chat(db_session, user)
    await _question(db_session, user)
    await _search(db_session, user)
    db_session.add(
        WebSearchLink(owner_user_id=user_id, token=uuid4().hex, open_addresses=False)
    )
    db_session.add(
        PushSubscription(
            user_id=str(user_id),
            endpoint="https://push.example/1",
            key_p256dh="k",
            key_auth="a",
        )
    )
    deletion = AccountDeletionRequest(user_id=user_id, status="pending")
    db_session.add(deletion)
    await db_session.commit()
    request_id = deletion.id

    @asynccontextmanager
    async def session_context():
        yield db_session

    class _Manager:
        def __init__(self, _settings):
            pass

        async def get(self, _name):
            return _Storage()

        async def close(self):
            pass

    monkeypatch.setattr(chat_jobs, "get_db_session_context", session_context)
    monkeypatch.setattr("skrift.storage.StorageManager", _Manager)
    monkeypatch.setattr(
        "skrift.config.get_settings", lambda: SimpleNamespace(storage=None)
    )

    assert await chat_jobs.delete_chat_account(
        chat_jobs.ChatAccountDeletionPayload(request_id=str(request_id))
    ) == {"status": "deleted"}

    # Chats and questions go with the user row by foreign-key cascade, which
    # Postgres enforces; this checks what has no foreign key to cascade.
    for model, condition in (
        (WebSearchRun, WebSearchRun.owner_user_id == user_id),
        (WebSearchLink, WebSearchLink.owner_user_id == user_id),
        (PushSubscription, PushSubscription.user_id == str(user_id)),
        (StoredNotification, StoredNotification.source_key == f"user:{user_id}"),
        (WorkDispatch, WorkDispatch.job_type != "chat.account.delete"),
    ):
        assert await _count(db_session, model, condition) == 0, model.__tablename__
    db_session.expire_all()
    for details in (await db_session.scalars(select(UsageCostRow.details))).all():
        assert _WORDS not in json.dumps(details)


# ── Replies kept in the usage ledger ─────────────────────────────────


def _holds_reply(row) -> bool:
    return _WORDS in json.dumps(row.details)


async def _run_turn_that(db_session, monkeypatch, turn, preflight_error):
    """Run the turn worker until its preflight fails with ``preflight_error``."""

    @asynccontextmanager
    async def session_context():
        yield db_session

    async def preflight(*_args, **_kwargs):
        raise preflight_error

    async def quiet(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chat_jobs, "get_db_session_context", session_context)
    monkeypatch.setattr(chat_jobs, "_preflight", preflight)
    monkeypatch.setattr(chat_jobs, "_event", quiet)
    monkeypatch.setattr(chat_jobs, "_notify_safe", quiet)
    return await chat_jobs.run_chat_turn(
        chat_jobs.ChatTurnPayload(turn_id=str(turn.id))
    )


@pytest.mark.asyncio
async def test_a_finished_turn_leaves_no_reply_in_its_usage_rows(
    db_session, monkeypatch
):
    user = await _user(db_session)
    _conversation, turn, _upload, reply = await _chat(
        db_session, user, turn_status="queued"
    )
    reply_id = reply.id

    result = await _run_turn_that(
        db_session, monkeypatch, turn, PermissionError("No Chat plan")
    )

    assert result == {"status": "error"}
    db_session.expire_all()
    cost = await db_session.get(UsageCostRow, reply_id)
    assert not _holds_reply(cost)
    assert cost.details["response_complete"] is True


@pytest.mark.asyncio
async def test_a_turn_going_back_for_another_attempt_keeps_its_replies(
    db_session, monkeypatch
):
    user = await _user(db_session)
    _conversation, turn, _upload, reply = await _chat(
        db_session, user, turn_status="queued"
    )
    reply_id, turn_id = reply.id, turn.id

    with pytest.raises(RuntimeError):
        await _run_turn_that(db_session, monkeypatch, turn, RuntimeError("blip"))

    db_session.expire_all()
    assert (await db_session.get(WebChatTurn, turn_id)).status == "queued"
    # The next attempt recovers its last settled reply from here.
    assert _holds_reply(await db_session.get(UsageCostRow, reply_id))


@pytest.mark.asyncio
async def test_the_sweep_clears_replies_of_turns_not_running(db_session):
    user = await _user(db_session)
    *_, finished = await _chat(db_session, user)
    *_, running = await _chat(db_session, user, turn_status="running")
    # A chat deleted before replies were cleared at delete time left its rows
    # pointing at a turn that is gone.
    orphan = _cost(user.id, mode="chat", operation="primary", root_turn_id=uuid4())
    orphan.details = dict(finished.details)
    db_session.add(orphan)
    await db_session.commit()
    finished_id, running_id, orphan_id = finished.id, running.id, orphan.id

    assert await forget_stale_reply_copies(db_session, batch=1) == 2

    db_session.expire_all()
    assert not _holds_reply(await db_session.get(UsageCostRow, finished_id))
    assert not _holds_reply(await db_session.get(UsageCostRow, orphan_id))
    assert _holds_reply(await db_session.get(UsageCostRow, running_id))
    assert await forget_stale_reply_copies(db_session) == 0


# ── Where the member finds the deletes ───────────────────────────────


def test_the_security_page_offers_to_delete_all_of_each_kind():
    from tests.web.test_sign_in_methods import _render_security_page

    html = _render_security_page(
        sign_in_methods=["discord"],
        passkey_sign_in=False,
        passkey_available=False,
        linked_accounts=[],
        passkeys=[],
        your_data={"chats": 3, "questions": 1, "searches": 0},
    )

    assert "Your data" in html
    assert 'action="/account/data/chats/delete"' in html
    assert "Delete all 3 conversations?" in html
    assert 'action="/account/data/questions/delete"' in html
    assert "Delete all 1 question?" in html
    # Nothing to delete, nothing offered.
    assert 'action="/account/data/searches/delete"' not in html
    assert "0 searches" in html


def test_a_resources_asker_gets_a_delete_on_each_question_and_nothing_else():
    from tests.web.quick_chat_surface_test import _render_chat_page

    question = SimpleNamespace(id=uuid4(), title="Pool sizing", archived_at=None)
    context = {
        "conversation": question,
        "mode": "resources",
        "messages": [],
        "versions": {},
        "is_owner": True,
        "quota_state": None,
        "resource_running": False,
        "ultra_chat": False,
        "conversation_groups": [{"label": "Today", "items": [question]}],
    }

    owner = " ".join(_render_chat_page(context).split())
    visitor = " ".join(_render_chat_page({**context, "is_owner": False}).split())

    assert "data-history-menu" in owner
    assert 'data-row-action="delete"' in owner
    assert 'data-row-action="rename"' not in owner
    assert "Delete this question?" in owner
    assert "data-row-menu" not in visitor


@pytest.mark.asyncio
async def test_the_security_page_deletes_all_of_a_kind_and_says_what_it_kept(
    db_session, monkeypatch
):
    from smarter_dev.web import account_controller
    from smarter_dev.web.account_controller import AccountController

    user = await _user(db_session)
    await _search(db_session, user)
    await _search(db_session, user)
    await _search(db_session, user, status="answering")
    flashed = []

    async def csrf_ok(_request):
        return True

    monkeypatch.setattr(account_controller, "verify_csrf", csrf_ok)
    monkeypatch.setattr(
        account_controller, "flash_success", lambda _r, text: flashed.append(text)
    )
    request = _request(user.id)
    request.session["user_id"] = user.id

    response = await AccountController.delete_your_data.fn(
        object.__new__(AccountController), request, db_session, "searches"
    )

    assert response.url == "/account/security"
    assert flashed == [
        "Deleted 2 searches. 1 still being answered was kept; delete it once it finishes."
    ]
    assert await _count(db_session, WebSearchRun) == 1
