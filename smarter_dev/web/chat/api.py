"""Owner-checked HTTP API for Smarter Dev web Chat."""

from __future__ import annotations

import re
from contextlib import suppress
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from urllib.parse import quote
from uuid import UUID
from uuid import uuid4

from litestar import Controller
from litestar import Request
from litestar import delete
from litestar import get
from litestar import patch
from litestar import post
from litestar.datastructures import UploadFile
from litestar.exceptions import HTTPException
from litestar.response import Response
from litestar.status_codes import HTTP_200_OK
from litestar.status_codes import HTTP_201_CREATED
from msgspec import Struct
from msgspec import field
from skrift.auth.services import get_user_permissions
from skrift.auth.session_keys import SESSION_USER_ID
from skrift.markdown import render_markdown
from skrift.workers import get_handle
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.model_catalog import MODEL_CATALOG
from smarter_dev.shared.model_catalog import get_model
from smarter_dev.shared.model_catalog import model_vendor
from smarter_dev.shared.model_catalog import parse_reasoning_level
from smarter_dev.shared.model_catalog import resolve_reasoning_level
from smarter_dev.web.chat.attachments import AttachmentError
from smarter_dev.web.chat.attachments import extract_text_bounded
from smarter_dev.web.chat.attachments import require_attachment_count
from smarter_dev.web.chat.attachments import validate_attachment
from smarter_dev.web.chat.cancellation import publish_cancellation
from smarter_dev.web.chat.conversations import ConversationTitleError
from smarter_dev.web.chat.conversations import derive_title
from smarter_dev.web.chat.conversations import normalize_title
from smarter_dev.web.chat.csrf import require_api_csrf
from smarter_dev.web.chat.dispatch import cancel_dispatch
from smarter_dev.web.chat.dispatch import create_dispatch
from smarter_dev.web.chat.dispatch import dispatch_one
from smarter_dev.web.chat.documents import attachment_kind
from smarter_dev.web.chat.documents import conversation_artifacts
from smarter_dev.web.chat.entitlements import has_chat
from smarter_dev.web.chat.entitlements import has_ultra_chat
from smarter_dev.web.chat.entitlements import resolve_spend_tier
from smarter_dev.web.chat.limits import current_spend_decision
from smarter_dev.web.chat.limits import get_or_start_four_hour_window
from smarter_dev.web.chat.policy import IntelligenceMode
from smarter_dev.web.chat.policy import parse_intelligence_mode
from smarter_dev.web.chat.settings import ensure_settings
from smarter_dev.web.chat.spend import as_utc
from smarter_dev.web.chat.usage import usage_metrics
from smarter_dev.web.llm_pricing import model_change_warning
from smarter_dev.web.models import ChatCatalogModel
from smarter_dev.web.models import ChatSpendLimit
from smarter_dev.web.models import WebChatAttachment
from smarter_dev.web.models import WebChatConversation
from smarter_dev.web.models import WebChatDocument
from smarter_dev.web.models import WebChatMessage
from smarter_dev.web.models import WebChatModelChange
from smarter_dev.web.models import WebChatRuntimeEvent
from smarter_dev.web.models import WebChatSubagent
from smarter_dev.web.models import WebChatTurn
from smarter_dev.web.models import WorkDispatch

MAX_INPUT_CHARS = 5_000
ACTIVE_TURN_STATUSES = ("submitted", "queued", "running", "stopping")


class ConversationBody(Struct):
    intelligence_mode: str | None = None
    model_key: str | None = None
    reasoning_level: str | None = None


class TurnBody(Struct):
    content: str
    submission_key: str
    attachment_ids: list[str] = field(default_factory=list)


class ConversationUpdateBody(Struct):
    """Rename and archive share one endpoint; both fields are optional."""

    title: str | None = None
    archived: bool | None = None


class ReasoningBody(Struct):
    reasoning_level: str | None = None


class ModelChangeBody(Struct):
    model_key: str


class VersionBody(Struct):
    message_id: str


def require_user_id(request: Request) -> UUID:
    raw = request.session.get(SESSION_USER_ID) if request.session else None
    try:
        return UUID(str(raw))
    except (ValueError, TypeError):
        raise HTTPException(status_code=401, detail="Sign in to use Chat.") from None


async def require_entitled(
    session: AsyncSession, user_id: UUID, *, ultra: bool = False
):
    from skrift.db.models.user import User

    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="This account is inactive.")
    permissions = await get_user_permissions(session, user_id)
    if not has_chat(permissions):
        raise HTTPException(
            status_code=403, detail="Chat is not enabled for your account."
        )
    if ultra and not has_ultra_chat(permissions):
        raise HTTPException(
            status_code=403, detail="Ultra Intelligence requires Ultra Chat."
        )
    return permissions


async def require_between_turns(session: AsyncSession, conversation_id: UUID) -> None:
    active = await session.scalar(
        select(WebChatTurn.id).where(
            WebChatTurn.conversation_id == conversation_id,
            WebChatTurn.status.in_(ACTIVE_TURN_STATUSES),
        )
    )
    if active is not None:
        raise HTTPException(
            status_code=409,
            detail="Model and reasoning can only change between turns.",
        )


async def owned_conversation(
    session: AsyncSession, conversation_id: UUID, user_id: UUID, *, lock: bool = False
) -> WebChatConversation:
    stmt = select(WebChatConversation).where(
        WebChatConversation.id == conversation_id,
        WebChatConversation.owner_user_id == user_id,
    )
    if lock:
        stmt = stmt.with_for_update()
    conversation = await session.scalar(stmt)
    if conversation is None:
        # Deliberately do not reveal whether another owner has this UUID.
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return conversation


def validate_content(value: str) -> str:
    content = (value or "").strip()
    if not content:
        raise HTTPException(status_code=422, detail="Message is required.")
    if len(content) > MAX_INPUT_CHARS:
        raise HTTPException(
            status_code=422, detail="Message is too long (max 5000 characters)."
        )
    return content


async def _try_dispatch(dispatch_id: UUID) -> None:
    try:
        await dispatch_one(dispatch_id)
    except Exception:
        # The durable APP_STARTUP reconciler owns retries.
        return


async def _finalize_cancelled_turn(turn_id: UUID, db_session: AsyncSession) -> None:
    turn = await db_session.scalar(
        select(WebChatTurn).where(WebChatTurn.id == turn_id).with_for_update()
    )
    if turn is None or turn.status not in ACTIVE_TURN_STATUSES:
        return
    turn.status = "stopped"
    turn.finished_at = datetime.now(UTC)
    conversation = await db_session.get(WebChatConversation, turn.conversation_id)
    if conversation is not None:
        conversation.status = "idle"
    placeholder = await db_session.scalar(
        select(WebChatMessage).where(
            WebChatMessage.turn_id == turn.id,
            WebChatMessage.role == "assistant",
        )
    )
    if placeholder is not None:
        placeholder.stopped = True
        if not placeholder.content:
            placeholder.content = "Stopped before the response started."
    sequence = (
        int(
            await db_session.scalar(
                select(func.coalesce(func.max(WebChatRuntimeEvent.sequence), 0)).where(
                    WebChatRuntimeEvent.turn_id == turn.id
                )
            )
            or 0
        )
        + 1
    )
    db_session.add(
        WebChatRuntimeEvent(
            conversation_id=turn.conversation_id,
            turn_id=turn.id,
            sequence=sequence,
            event_type="chat_turn_stopped",
            payload={
                "content": placeholder.content if placeholder else "",
                "stopped": True,
            },
        )
    )
    await db_session.commit()


def turn_meta(turn: WebChatTurn | None) -> dict:
    """Which model answered and how long it took, for the byline on a response.

    ``elapsed`` is ``None`` while a turn is still running — the browser owns the
    live timer in that case and only adopts this once the turn has finished.
    """
    if turn is None:
        return {"model_key": None, "elapsed": None}
    elapsed = None
    if turn.finished_at and turn.created_at:
        elapsed = round((turn.finished_at - turn.created_at).total_seconds(), 1)
    return {"model_key": turn.model_key, "elapsed": elapsed}


def message_dict(
    message: WebChatMessage,
    attachments: list[dict] | None = None,
    turn: WebChatTurn | None = None,
) -> dict:
    return {
        "id": str(message.id),
        "turn_id": str(message.turn_id),
        # The response byline (which model, how long). Only assistant turns show
        # one, and only once the turn has finished — the browser owns the timer
        # while it is still running.
        **(turn_meta(turn) if message.role == "assistant" else {}),
        "sequence": message.sequence,
        "role": message.role,
        "content": message.content,
        # The browser reconciles terminal state without a reload, so it needs the
        # same server-rendered markdown the page template emits. Rendering here
        # keeps a single markdown implementation instead of shipping one to JS.
        "content_html": render_markdown(message.content or ""),
        "version_group": str(message.version_group),
        "version_number": message.version_number,
        "is_active": message.is_active,
        "stopped": message.stopped,
        "attachments": attachments or [],
        "created_at": message.created_at.isoformat() if message.created_at else None,
    }


def document_dict(document: WebChatDocument) -> dict:
    return {
        "id": str(document.id),
        "turn_id": str(document.turn_id),
        "assistant_message_id": str(document.assistant_message_id),
        "title": document.title,
        "filename": document.filename,
        "size_bytes": document.size_bytes,
        # A document is visible while it is still being written, so the client
        # needs to know whether it is looking at a file or at a file in progress.
        "status": document.status,
    }


class ChatApiController(Controller):
    path = "/v2/api/chat"

    @get("/catalog")
    async def catalog(self, request: Request, db_session: AsyncSession) -> dict:
        user_id = require_user_id(request)
        permissions = await require_entitled(db_session, user_id)
        settings = await ensure_settings(db_session)
        rows = {
            row.model_key: row
            for row in (
                await db_session.execute(
                    select(ChatCatalogModel)
                    .where(ChatCatalogModel.enabled.is_(True))
                    .order_by(ChatCatalogModel.sort_order)
                )
            ).scalars()
        }
        default_intelligence = settings.default_intelligence_mode
        if (
            default_intelligence == IntelligenceMode.ULTRA_INTELLIGENCE.value
            and not has_ultra_chat(permissions)
        ):
            default_intelligence = IntelligenceMode.MAXIMIZE_INTELLIGENCE.value
        return {
            "defaults": {
                "model_key": settings.default_model_key,
                "reasoning_level": settings.default_reasoning,
                "intelligence_mode": default_intelligence,
            },
            "ultra_chat": has_ultra_chat(permissions),
            "models": [
                {
                    "key": model.key,
                    "label": model.label,
                    "vendor": model_vendor(model),
                    "cost_tier": rows[model.key].cost_tier,
                    "reasoning_levels": [
                        level.value for level in model.reasoning_levels
                    ],
                    "default_reasoning": model.default_reasoning.value
                    if model.default_reasoning
                    else None,
                    "vision": model.supports_vision,
                    "context_window": model.context_window,
                }
                for model in MODEL_CATALOG
                if model.key in rows
            ],
            "intelligence_modes": [mode.value for mode in IntelligenceMode],
        }

    @post("/conversations", status_code=HTTP_201_CREATED)
    async def create_conversation(
        self, data: ConversationBody, request: Request, db_session: AsyncSession
    ) -> dict:
        require_api_csrf(request)
        user_id = require_user_id(request)
        settings = await ensure_settings(db_session)
        requested_mode = data.intelligence_mode or settings.default_intelligence_mode
        permissions = await require_entitled(db_session, user_id)
        if (
            data.intelligence_mode is None
            and requested_mode == IntelligenceMode.ULTRA_INTELLIGENCE.value
            and not has_ultra_chat(permissions)
        ):
            requested_mode = IntelligenceMode.MAXIMIZE_INTELLIGENCE.value
        mode = parse_intelligence_mode(requested_mode)
        if mode is IntelligenceMode.ULTRA_INTELLIGENCE and not has_ultra_chat(
            permissions
        ):
            raise HTTPException(
                status_code=403, detail="Ultra Intelligence requires Ultra Chat."
            )
        model_key = data.model_key or settings.default_model_key
        enabled = await db_session.get(ChatCatalogModel, model_key)
        model = get_model(model_key)
        if model is None or enabled is None or not enabled.enabled:
            raise HTTPException(
                status_code=422, detail="Select an available Chat model."
            )
        requested = parse_reasoning_level(
            data.reasoning_level or settings.default_reasoning
        )
        effective = resolve_reasoning_level(model, requested)
        conversation = WebChatConversation(
            owner_user_id=user_id,
            intelligence_mode=mode.value,
            selected_model_key=model.key,
            reasoning_level=effective.value if effective else None,
            title="New Chat",
            status="idle",
            next_sequence=1,
        )
        db_session.add(conversation)
        await db_session.commit()
        return {
            "id": str(conversation.id),
            "url": f"/chat/{conversation.id}",
            "mode": "chat",
        }

    @get("/conversations/{conversation_id:uuid}")
    async def get_conversation(
        self, conversation_id: UUID, request: Request, db_session: AsyncSession
    ) -> dict:
        user_id = require_user_id(request)
        await require_entitled(db_session, user_id)
        conversation = await owned_conversation(db_session, conversation_id, user_id)
        messages = list(
            (
                await db_session.execute(
                    select(WebChatMessage)
                    .where(
                        WebChatMessage.conversation_id == conversation_id,
                    )
                    .order_by(WebChatMessage.sequence, WebChatMessage.version_number)
                )
            ).scalars()
        )
        events = list(
            (
                await db_session.execute(
                    select(WebChatRuntimeEvent)
                    .where(
                        WebChatRuntimeEvent.conversation_id == conversation_id,
                    )
                    .order_by(WebChatRuntimeEvent.created_at.desc())
                    .limit(200)
                )
            ).scalars()
        )
        events.reverse()
        turns = {
            turn.id: turn
            for turn in (
                await db_session.execute(
                    select(WebChatTurn).where(
                        WebChatTurn.conversation_id == conversation_id
                    )
                )
            ).scalars()
        }
        activity_events = list(
            (
                await db_session.execute(
                    select(WebChatRuntimeEvent)
                    .where(
                        WebChatRuntimeEvent.conversation_id == conversation_id,
                        WebChatRuntimeEvent.event_type.in_(
                            ("chat_tool_event", "chat_run_state", "chat_subagent_state")
                        ),
                    )
                    .order_by(WebChatRuntimeEvent.created_at.desc())
                    .limit(1_000)
                )
            ).scalars()
        )
        activity_events.reverse()
        active_turn = await db_session.scalar(
            select(WebChatTurn)
            .where(
                WebChatTurn.conversation_id == conversation_id,
                WebChatTurn.status.in_(ACTIVE_TURN_STATUSES),
            )
            .order_by(WebChatTurn.created_at.desc())
            .limit(1)
        )
        pending_attachments = list(
            (
                await db_session.execute(
                    select(WebChatAttachment).where(
                        WebChatAttachment.conversation_id == conversation_id,
                        WebChatAttachment.owner_user_id == user_id,
                        WebChatAttachment.turn_id.is_(None),
                        WebChatAttachment.status == "ready",
                    )
                )
            ).scalars()
        )
        submitted_attachments = list(
            (
                await db_session.execute(
                    select(WebChatAttachment).where(
                        WebChatAttachment.conversation_id == conversation_id,
                        WebChatAttachment.turn_id.is_not(None),
                        WebChatAttachment.status == "ready",
                    )
                )
            ).scalars()
        )
        attachments_by_turn: dict[UUID, list[dict]] = {}
        for attachment in submitted_attachments:
            attachments_by_turn.setdefault(attachment.turn_id, []).append(
                {
                    "id": str(attachment.id),
                    "name": attachment.original_name,
                    "media_type": attachment.media_type,
                    "size_bytes": attachment.size_bytes,
                    # The chip opens the same previewer the panel uses.
                    "kind": attachment_kind(attachment.media_type),
                }
            )
        subagents = list(
            (
                await db_session.execute(
                    select(WebChatSubagent)
                    .join(WebChatTurn, WebChatTurn.id == WebChatSubagent.root_turn_id)
                    .where(WebChatTurn.conversation_id == conversation_id)
                    .order_by(WebChatSubagent.queued_at)
                )
            ).scalars()
        )
        documents = list(
            (
                await db_session.execute(
                    select(WebChatDocument)
                    .where(
                        WebChatDocument.conversation_id == conversation_id,
                        # An overwritten file keeps its row so a failed
                        # overwrite can be undone; it is not a file any more.
                        WebChatDocument.status != "superseded",
                    )
                    .order_by(WebChatDocument.created_at)
                )
            ).scalars()
        )
        return {
            "id": str(conversation.id),
            "mode": "chat",
            "title": conversation.title,
            "archived": conversation.archived_at is not None,
            "intelligence_mode": conversation.intelligence_mode,
            "model_key": conversation.selected_model_key,
            "reasoning_level": conversation.reasoning_level,
            "status": conversation.status,
            "active_turn": (
                {
                    "id": str(active_turn.id),
                    "status": active_turn.status,
                    "partial": next(
                        (
                            message.content
                            for message in messages
                            if message.turn_id == active_turn.id
                            and message.role == "assistant"
                        ),
                        "",
                    ),
                    "started_at": active_turn.created_at.isoformat(),
                }
                if active_turn
                else None
            ),
            "messages": [
                message_dict(
                    message,
                    attachments_by_turn.get(message.turn_id, [])
                    if message.role == "user"
                    else [],
                    turns.get(message.turn_id),
                )
                for message in messages
            ],
            # Two lists on purpose. `documents` drives the cards under the reply
            # that produced them; `artifacts` is the panel's shelf, where a file
            # the agent wrote and a file the user uploaded sit side by side.
            "documents": [document_dict(document) for document in documents],
            "artifacts": [
                artifact.as_dict()
                for artifact in await conversation_artifacts(
                    db_session, conversation_id=conversation_id
                )
            ],
            "pending_attachments": [
                {
                    "id": str(attachment.id),
                    "name": attachment.original_name,
                    "media_type": attachment.media_type,
                    "size_bytes": attachment.size_bytes,
                    "kind": attachment_kind(attachment.media_type),
                }
                for attachment in pending_attachments
            ],
            "subagents": [
                {
                    "id": str(child.id),
                    "root_turn_id": str(child.root_turn_id),
                    "parent_subagent_id": str(child.parent_subagent_id)
                    if child.parent_subagent_id
                    else None,
                    "name": child.name,
                    "lineage": child.lineage,
                    "spawn_ordinal": child.spawn_ordinal,
                    "status": child.status,
                    "input_tokens": child.input_tokens,
                    "output_tokens": child.output_tokens,
                    "queued_at": child.queued_at.isoformat(),
                    "started_at": child.started_at.isoformat()
                    if child.started_at
                    else None,
                    "finished_at": child.finished_at.isoformat()
                    if child.finished_at
                    else None,
                }
                for child in subagents
            ],
            "events": [
                {
                    "type": event.event_type,
                    "payload": event.payload,
                    "sequence": event.sequence,
                    "turn_id": str(event.turn_id),
                    "created_at": event.created_at.isoformat(),
                }
                for event in events
            ],
            "activity_events": [
                {
                    "type": event.event_type,
                    "payload": event.payload,
                    "sequence": event.sequence,
                    "turn_id": str(event.turn_id),
                    "created_at": event.created_at.isoformat(),
                }
                for event in activity_events
            ],
        }

    @patch("/conversations/{conversation_id:uuid}")
    async def update_conversation(
        self,
        conversation_id: UUID,
        data: ConversationUpdateBody,
        request: Request,
        db_session: AsyncSession,
    ) -> dict:
        """Rename and archive, the two edits that do not touch the transcript.

        Both are safe mid-turn: neither changes what the running agent sees, and
        an owner who archives a conversation while it is still answering is
        making a filing decision, not a cancellation.
        """
        require_api_csrf(request)
        user_id = require_user_id(request)
        await require_entitled(db_session, user_id)
        conversation = await owned_conversation(
            db_session, conversation_id, user_id, lock=True
        )
        if data.title is not None:
            try:
                conversation.title = normalize_title(data.title)
            except ConversationTitleError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from None
            conversation.title_is_custom = True
        if data.archived is not None:
            conversation.archived_at = datetime.now(UTC) if data.archived else None
        await db_session.commit()
        return {
            "id": str(conversation.id),
            "title": conversation.title,
            "archived": conversation.archived_at is not None,
        }

    @delete("/conversations/{conversation_id:uuid}", status_code=HTTP_200_OK)
    async def delete_conversation(
        self,
        conversation_id: UUID,
        request: Request,
        db_session: AsyncSession,
    ) -> dict:
        """Destroy a conversation and everything hanging off it.

        Turns, messages, documents and attachment rows go with the conversation
        by cascade, but the uploaded objects are outside the database and have to
        be removed by hand. They are marked ``deleting`` first, so a failure part
        way through leaves them to the periodic orphan reconciler instead of
        stranding private files with no row pointing at them — and the
        conversation itself survives, so the owner can simply try again.
        """
        require_api_csrf(request)
        user_id = require_user_id(request)
        await require_entitled(db_session, user_id)
        conversation = await owned_conversation(
            db_session, conversation_id, user_id, lock=True
        )
        active = await db_session.scalar(
            select(WebChatTurn.id).where(
                WebChatTurn.conversation_id == conversation.id,
                WebChatTurn.status.in_(ACTIVE_TURN_STATUSES),
            )
        )
        if active is not None:
            # A worker still holds this turn's rows. Stop it first, then delete.
            raise HTTPException(
                status_code=409,
                detail="Stop the current response before deleting this chat.",
            )
        attachments = list(
            (
                await db_session.execute(
                    select(WebChatAttachment).where(
                        WebChatAttachment.conversation_id == conversation_id
                    )
                )
            ).scalars()
        )
        if attachments:
            for attachment in attachments:
                attachment.status = "deleting"
            await db_session.commit()
            backend = await request.app.state.storage_manager.get("chat_attachments")
            stranded = 0
            for attachment in attachments:
                try:
                    await backend.delete(attachment.storage_key)
                except Exception:
                    stranded += 1
                    continue
                await db_session.delete(attachment)
            await db_session.commit()
            if stranded:
                raise HTTPException(
                    status_code=502,
                    detail="Some uploads could not be removed. Try again shortly.",
                )
        await db_session.delete(conversation)
        await db_session.commit()
        return {"status": "deleted"}

    @patch("/conversations/{conversation_id:uuid}/reasoning")
    async def reasoning(
        self,
        conversation_id: UUID,
        data: ReasoningBody,
        request: Request,
        db_session: AsyncSession,
    ) -> dict:
        require_api_csrf(request)
        user_id = require_user_id(request)
        await require_entitled(db_session, user_id)
        conversation = await owned_conversation(
            db_session, conversation_id, user_id, lock=True
        )
        await require_between_turns(db_session, conversation.id)
        model = get_model(conversation.selected_model_key)
        effective = (
            resolve_reasoning_level(model, parse_reasoning_level(data.reasoning_level))
            if model
            else None
        )
        conversation.reasoning_level = effective.value if effective else None
        await db_session.commit()
        return {"reasoning_level": conversation.reasoning_level}

    @post(
        "/conversations/{conversation_id:uuid}/model-changes",
        status_code=HTTP_201_CREATED,
    )
    async def propose_model(
        self,
        conversation_id: UUID,
        data: ModelChangeBody,
        request: Request,
        db_session: AsyncSession,
    ) -> dict:
        require_api_csrf(request)
        user_id = require_user_id(request)
        await require_entitled(db_session, user_id)
        conversation = await owned_conversation(db_session, conversation_id, user_id)
        await require_between_turns(db_session, conversation.id)
        target_setting = await db_session.get(ChatCatalogModel, data.model_key)
        old, new = get_model(conversation.selected_model_key), get_model(data.model_key)
        if (
            old is None
            or new is None
            or target_setting is None
            or not target_setting.enabled
        ):
            raise HTTPException(
                status_code=422, detail="Select an available Chat model."
            )
        warning = model_change_warning(old, new)
        change = WebChatModelChange(
            conversation_id=conversation.id,
            owner_user_id=user_id,
            from_model_key=old.key,
            to_model_key=new.key,
            warning=warning,
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )
        db_session.add(change)
        await db_session.commit()
        return {"id": str(change.id), "warning": warning, "requires_confirmation": True}

    @post(
        "/conversations/{conversation_id:uuid}/model-changes/{change_id:uuid}/confirm"
    )
    async def confirm_model(
        self,
        conversation_id: UUID,
        change_id: UUID,
        request: Request,
        db_session: AsyncSession,
    ) -> dict:
        require_api_csrf(request)
        user_id = require_user_id(request)
        await require_entitled(db_session, user_id)
        conversation = await owned_conversation(
            db_session, conversation_id, user_id, lock=True
        )
        await require_between_turns(db_session, conversation.id)
        change = await db_session.scalar(
            select(WebChatModelChange)
            .where(
                WebChatModelChange.id == change_id,
                WebChatModelChange.conversation_id == conversation_id,
                WebChatModelChange.owner_user_id == user_id,
            )
            .with_for_update()
        )
        now = datetime.now(UTC)
        if (
            change is None
            or change.confirmed_at is not None
            or as_utc(now) >= as_utc(change.expires_at)
        ):
            raise HTTPException(
                status_code=409, detail="Model change confirmation expired."
            )
        target = await db_session.get(ChatCatalogModel, change.to_model_key)
        if (
            conversation.selected_model_key != change.from_model_key
            or target is None
            or not target.enabled
        ):
            raise HTTPException(
                status_code=409,
                detail="Model selection changed or is no longer available.",
            )
        conversation.selected_model_key = change.to_model_key
        model = get_model(change.to_model_key)
        effective = (
            resolve_reasoning_level(
                model, parse_reasoning_level(conversation.reasoning_level)
            )
            if model
            else None
        )
        conversation.reasoning_level = effective.value if effective else None
        change.confirmed_at = now
        await db_session.commit()
        return {
            "model_key": conversation.selected_model_key,
            "reasoning_level": conversation.reasoning_level,
        }

    @post(
        "/conversations/{conversation_id:uuid}/attachments",
        status_code=HTTP_201_CREATED,
    )
    async def upload_attachment(
        self, conversation_id: UUID, request: Request, db_session: AsyncSession
    ) -> dict:
        require_api_csrf(request)
        user_id = require_user_id(request)
        await require_entitled(db_session, user_id)
        await owned_conversation(db_session, conversation_id, user_id, lock=True)
        form = await request.form()
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise HTTPException(status_code=422, detail="A file is required.")
        instruction = str(form.get("summarization_instruction") or "").strip() or None
        if instruction and len(instruction) > 2000:
            raise HTTPException(
                status_code=422,
                detail="Attachment summarization instruction is too long.",
            )
        pending_count = int(
            await db_session.scalar(
                select(func.count(WebChatAttachment.id)).where(
                    WebChatAttachment.owner_user_id == user_id,
                    WebChatAttachment.conversation_id == conversation_id,
                    WebChatAttachment.turn_id.is_(None),
                )
            )
            or 0
        )
        if pending_count >= 5:
            raise HTTPException(
                status_code=422,
                detail="Submit or remove an attachment before uploading another.",
            )
        data = await upload.read(10 * 1024 * 1024 + 1)
        try:
            validated = validate_attachment(
                upload.filename or "attachment", upload.content_type or "", data
            )
            extracted = await extract_text_bounded(
                data, validated.media_type, timeout=15
            )
        except (AttachmentError, TimeoutError) as exc:
            detail = (
                str(exc)
                if isinstance(exc, AttachmentError)
                else "Attachment extraction timed out."
            )
            raise HTTPException(status_code=422, detail=detail) from exc
        # Serialize the final object write with account deletion. The deletion
        # endpoint locks the same user row before taking its attachment snapshot,
        # so an authorized upload either commits first and is deleted, or observes
        # the inactive account and never creates an object.
        from skrift.db.models.user import User

        user = await db_session.scalar(
            select(User).where(User.id == user_id).with_for_update()
        )
        if user is None or not user.is_active:
            raise HTTPException(status_code=401, detail="This account is inactive.")
        # Opaque keys work with both hash-fanned local storage and configured S3
        # prefixes and disclose no owner/conversation identifiers.
        # An opaque fixed-width owner UUID prefix lets account deletion
        # discover a blob left by a process crash before the DB commit.
        key = f"{user_id.hex}{uuid4().hex}"
        backend = await request.app.state.storage_manager.get("chat_attachments")
        attachment = WebChatAttachment(
            conversation_id=conversation_id,
            owner_user_id=user_id,
            storage_key=key,
            original_name=validated.filename,
            media_type=validated.media_type,
            size_bytes=validated.size_bytes,
            sha256=validated.sha256,
            extracted_text=extracted,
            summarization_instruction=instruction,
            status="uploading",
        )
        db_session.add(attachment)
        await db_session.flush()
        try:
            # Keep the user-row lock through storage and the ready-state commit.
            # Account deletion cannot snapshot/delete this user's rows midway.
            await backend.put(key, data, validated.media_type)
            attachment.status = "ready"
            await db_session.commit()
        except Exception:
            await db_session.rollback()
            # The store response may be ambiguous. An idempotent delete closes
            # the crash/error path even though the DB insert was rolled back.
            with suppress(Exception):
                await backend.delete(key)
            raise
        return {
            "id": str(attachment.id),
            "name": attachment.original_name,
            "media_type": attachment.media_type,
            "size_bytes": attachment.size_bytes,
        }

    @delete(
        "/conversations/{conversation_id:uuid}/attachments/{attachment_id:uuid}",
        status_code=HTTP_200_OK,
    )
    async def delete_attachment(
        self,
        conversation_id: UUID,
        attachment_id: UUID,
        request: Request,
        db_session: AsyncSession,
    ) -> dict:
        require_api_csrf(request)
        user_id = require_user_id(request)
        await require_entitled(db_session, user_id)
        await owned_conversation(db_session, conversation_id, user_id)
        attachment = await db_session.scalar(
            select(WebChatAttachment)
            .where(
                WebChatAttachment.id == attachment_id,
                WebChatAttachment.conversation_id == conversation_id,
                WebChatAttachment.owner_user_id == user_id,
                WebChatAttachment.turn_id.is_(None),
                WebChatAttachment.status == "ready",
            )
            .with_for_update()
        )
        if attachment is None:
            raise HTTPException(status_code=404, detail="Attachment not found.")
        backend = await request.app.state.storage_manager.get("chat_attachments")
        # Persist intent first. If object deletion or the final DB commit fails,
        # the periodic orphan reconciler can safely finish this idempotently.
        attachment.status = "deleting"
        await db_session.commit()
        try:
            await backend.delete(attachment.storage_key)
        except Exception:
            attachment.status = "ready"
            await db_session.commit()
            raise
        await db_session.delete(attachment)
        await db_session.commit()
        return {"status": "deleted"}

    @get("/conversations/{conversation_id:uuid}/attachments/{attachment_id:uuid}")
    async def download_attachment(
        self,
        conversation_id: UUID,
        attachment_id: UUID,
        request: Request,
        db_session: AsyncSession,
        inline: bool = False,
    ) -> Response:
        user_id = require_user_id(request)
        await require_entitled(db_session, user_id)
        await owned_conversation(db_session, conversation_id, user_id)
        attachment = await db_session.scalar(
            select(WebChatAttachment).where(
                WebChatAttachment.id == attachment_id,
                WebChatAttachment.conversation_id == conversation_id,
                WebChatAttachment.owner_user_id == user_id,
            )
        )
        if attachment is None:
            raise HTTPException(status_code=404, detail="Attachment not found.")
        backend = await request.app.state.storage_manager.get("chat_attachments")
        content = await backend.get(attachment.storage_key)
        safe_name = attachment.original_name.replace('"', "")
        encoded_name = quote(attachment.original_name, safe="")
        # The panel shows an uploaded image where it shows any other file, which
        # needs the bytes served for display rather than for saving. Still
        # nosniff, still no-store — only the disposition changes.
        disposition = "inline" if inline else "attachment"
        return Response(
            content=content,
            media_type=attachment.media_type,
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": (
                    f'{disposition}; filename="{safe_name}"; '
                    f"filename*=UTF-8''{encoded_name}"
                ),
            },
        )

    @get("/conversations/{conversation_id:uuid}/attachments/{attachment_id:uuid}/preview")
    async def preview_attachment(
        self,
        conversation_id: UUID,
        attachment_id: UUID,
        request: Request,
        db_session: AsyncSession,
    ) -> Response:
        """Render an upload for the documents panel.

        Same shape as a document preview so one previewer can show either. Text
        and PDFs are rendered from their extracted text — as markdown when the
        upload is markdown, and as a code block otherwise, since a .py or .csv
        rendered as prose is unreadable. An image reports a URL instead of HTML.
        """
        user_id = require_user_id(request)
        await require_entitled(db_session, user_id)
        await owned_conversation(db_session, conversation_id, user_id)
        attachment = await db_session.scalar(
            select(WebChatAttachment).where(
                WebChatAttachment.id == attachment_id,
                WebChatAttachment.conversation_id == conversation_id,
                WebChatAttachment.owner_user_id == user_id,
                WebChatAttachment.status == "ready",
            )
        )
        if attachment is None:
            raise HTTPException(status_code=404, detail="Attachment not found.")
        kind = attachment_kind(attachment.media_type)
        content_html = ""
        if kind != "image":
            text = attachment.extracted_text or ""
            if not text:
                content_html = render_markdown(
                    "_This file has no extractable text._"
                )
            elif attachment.media_type == "text/markdown":
                content_html = render_markdown(text)
            else:
                # Long enough to survive any run of backticks in the file itself.
                longest = max(
                    (len(run) for run in re.findall(r"`+", text)), default=0
                )
                fence = "`" * max(3, longest + 1)
                content_html = render_markdown(f"{fence}\n{text}\n{fence}")
        return Response(
            content={
                "id": str(attachment.id),
                "origin": "upload",
                "kind": kind,
                "title": attachment.original_name,
                "filename": attachment.original_name,
                "size_bytes": attachment.size_bytes,
                "status": attachment.status,
                "media_type": attachment.media_type,
                "content_html": content_html,
                "content_url": (
                    f"/v2/api/chat/conversations/{conversation_id}"
                    f"/attachments/{attachment.id}?inline=true"
                    if kind == "image"
                    else None
                ),
                "summarization_instruction": attachment.summarization_instruction,
            },
            media_type="application/json",
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @get("/conversations/{conversation_id:uuid}/documents/{document_id:uuid}")
    async def get_document(
        self,
        conversation_id: UUID,
        document_id: UUID,
        request: Request,
        db_session: AsyncSession,
    ) -> Response:
        user_id = require_user_id(request)
        await require_entitled(db_session, user_id)
        await owned_conversation(db_session, conversation_id, user_id)
        document = await db_session.scalar(
            select(WebChatDocument).where(
                WebChatDocument.id == document_id,
                WebChatDocument.conversation_id == conversation_id,
            )
        )
        if document is None:
            raise HTTPException(status_code=404, detail="Document not found.")
        if document.status == "superseded":
            # The row survives an overwrite so a failed one can be undone; the
            # file it held is not part of the conversation any more.
            raise HTTPException(
                status_code=410, detail="This document was replaced by a newer one."
            )
        return Response(
            content={
                **document_dict(document),
                "content_html": render_markdown(document.markdown_content),
                # A file still being written is shown as the raw text it is so
                # far, so a reader who opens it late sees the same stream as one
                # who was already watching. Finished files render as markdown.
                "content_text": (
                    document.markdown_content
                    if document.status == "streaming"
                    else None
                ),
            },
            media_type="application/json",
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @get(
        "/conversations/{conversation_id:uuid}/documents/{document_id:uuid}/download"
    )
    async def download_document(
        self,
        conversation_id: UUID,
        document_id: UUID,
        request: Request,
        db_session: AsyncSession,
    ) -> Response:
        user_id = require_user_id(request)
        await require_entitled(db_session, user_id)
        await owned_conversation(db_session, conversation_id, user_id)
        document = await db_session.scalar(
            select(WebChatDocument).where(
                WebChatDocument.id == document_id,
                WebChatDocument.conversation_id == conversation_id,
            )
        )
        if document is None:
            raise HTTPException(status_code=404, detail="Document not found.")
        if document.status == "streaming":
            # Half a file is not a download. The preview shows the live text.
            raise HTTPException(
                status_code=409, detail="Document is still being written."
            )
        if document.status == "superseded":
            raise HTTPException(
                status_code=410, detail="This document was replaced by a newer one."
            )
        safe_name = document.filename.replace('"', "")
        encoded_name = quote(document.filename, safe="")
        return Response(
            content=document.markdown_content.encode("utf-8"),
            media_type="text/markdown; charset=utf-8",
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": (
                    f'attachment; filename="{safe_name}"; '
                    f"filename*=UTF-8''{encoded_name}"
                ),
            },
        )

    @get("/conversations/{conversation_id:uuid}/usage")
    async def conversation_usage(
        self, conversation_id: UUID, request: Request, db_session: AsyncSession
    ) -> dict:
        user_id = require_user_id(request)
        permissions = await require_entitled(db_session, user_id)
        await owned_conversation(db_session, conversation_id, user_id)
        from smarter_dev.web.models import ChatSpendWindow

        window = await db_session.scalar(
            select(ChatSpendWindow)
            .where(
                ChatSpendWindow.user_id == user_id,
            )
            .order_by(ChatSpendWindow.starts_at.desc())
            .limit(1)
        )
        tier = resolve_spend_tier(permissions)
        limit = await db_session.get(ChatSpendLimit, tier) if tier else None
        now = datetime.now(UTC)
        start = window.starts_at if window else now
        end = window.ends_at if window else now
        return await usage_metrics(
            db_session,
            user_id=user_id,
            conversation_id=conversation_id,
            window_start=start,
            window_end=end,
            four_hour_limit=limit.four_hour_usd if limit else Decimal("0"),
            window_id=window.id if window else None,
        )

    @post("/conversations/{conversation_id:uuid}/turns", status_code=HTTP_201_CREATED)
    async def submit_turn(
        self,
        conversation_id: UUID,
        data: TurnBody,
        request: Request,
        db_session: AsyncSession,
    ) -> dict:
        require_api_csrf(request)
        user_id = require_user_id(request)
        permissions = await require_entitled(db_session, user_id)
        content = validate_content(data.content)
        try:
            attachment_ids = [UUID(value) for value in data.attachment_ids]
            require_attachment_count(attachment_ids)
        except (ValueError, AttachmentError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not data.submission_key or len(data.submission_key) > 128:
            raise HTTPException(
                status_code=422, detail="A valid submission_key is required."
            )
        conversation = await owned_conversation(
            db_session, conversation_id, user_id, lock=True
        )
        duplicate = await db_session.scalar(
            select(WebChatTurn).where(
                WebChatTurn.conversation_id == conversation_id,
                WebChatTurn.submission_key == data.submission_key,
            )
        )
        if duplicate is not None:
            dispatch = await db_session.scalar(
                select(WorkDispatch).where(
                    WorkDispatch.job_type == "chat.turn.run",
                    WorkDispatch.aggregate_id == duplicate.id,
                )
            )
            await db_session.commit()
            if dispatch is not None and duplicate.status in ACTIVE_TURN_STATUSES:
                await _try_dispatch(dispatch.id)
            return {
                "turn_id": str(duplicate.id),
                "status": duplicate.status,
                "idempotent": True,
            }
        if await db_session.scalar(
            select(func.count(WebChatTurn.id)).where(
                WebChatTurn.conversation_id == conversation_id,
                WebChatTurn.status.in_(ACTIVE_TURN_STATUSES),
            )
        ):
            raise HTTPException(
                status_code=409, detail="Wait for the active turn to finish or stop it."
            )
        enabled = await db_session.get(
            ChatCatalogModel, conversation.selected_model_key
        )
        if enabled is None or not enabled.enabled:
            raise HTTPException(
                status_code=409,
                detail="The selected model is unavailable; choose a new model.",
            )
        if (
            conversation.intelligence_mode == IntelligenceMode.ULTRA_INTELLIGENCE.value
            and not has_ultra_chat(permissions)
        ):
            raise HTTPException(
                status_code=403, detail="Ultra Chat permission is required."
            )
        attachments = []
        if attachment_ids:
            attachments = list(
                (
                    await db_session.execute(
                        select(WebChatAttachment)
                        .where(
                            WebChatAttachment.id.in_(attachment_ids),
                            WebChatAttachment.conversation_id == conversation_id,
                            WebChatAttachment.owner_user_id == user_id,
                            WebChatAttachment.turn_id.is_(None),
                            WebChatAttachment.status == "ready",
                        )
                        .with_for_update()
                    )
                ).scalars()
            )
            if len(attachments) != len(attachment_ids):
                raise HTTPException(
                    status_code=422,
                    detail="One or more attachments are invalid or already used.",
                )
        tier = resolve_spend_tier(permissions)
        if tier is None:
            raise HTTPException(
                status_code=403, detail="Chat is not enabled for your account."
            )
        admission = await current_spend_decision(
            db_session,
            user_id=user_id,
            tier=tier,
            intelligence_mode=conversation.intelligence_mode,
        )
        if admission.hard_cutoff:
            raise HTTPException(
                status_code=429,
                detail="Chat usage limit reached for the current spend window.",
            )
        window = await get_or_start_four_hour_window(db_session, user_id)
        version_group = uuid4()
        response_sequence = conversation.next_sequence * 2
        turn = WebChatTurn(
            conversation_id=conversation_id,
            sequence=conversation.next_sequence,
            submission_key=data.submission_key,
            kind="message",
            response_version_group=version_group,
            response_sequence=response_sequence,
            model_key=conversation.selected_model_key,
            reasoning_level=conversation.reasoning_level,
            status="submitted",
            four_hour_window_id=window.id,
        )
        db_session.add(turn)
        await db_session.flush()
        db_session.add(
            WebChatMessage(
                conversation_id=conversation_id,
                turn_id=turn.id,
                sequence=response_sequence - 1,
                role="user",
                content=content,
                version_group=uuid4(),
                version_number=1,
                is_active=True,
            )
        )
        # The durable placeholder is the browser/reload target and accumulates
        # partial streamed output. It also establishes the single version group.
        db_session.add(
            WebChatMessage(
                conversation_id=conversation_id,
                turn_id=turn.id,
                sequence=response_sequence,
                role="assistant",
                content="",
                version_group=version_group,
                version_number=1,
                is_active=True,
            )
        )
        for attachment in attachments:
            attachment.turn_id = turn.id
        conversation.next_sequence += 1
        conversation.status = "submitted"
        # A stand-in until the agent names the conversation on this first turn.
        # Once anything has actually named it, it is left alone.
        if not conversation.title_is_custom and conversation.next_sequence == 2:
            conversation.title = derive_title(content)
        dispatch = await create_dispatch(
            db_session,
            job_type="chat.turn.run",
            aggregate_id=turn.id,
            payload={"turn_id": str(turn.id)},
        )
        await db_session.commit()
        await _try_dispatch(dispatch.id)
        return {"turn_id": str(turn.id), "status": "submitted", "idempotent": False}

    @post("/conversations/{conversation_id:uuid}/turns/{turn_id:uuid}/stop")
    async def stop_turn(
        self,
        conversation_id: UUID,
        turn_id: UUID,
        request: Request,
        db_session: AsyncSession,
    ) -> dict:
        require_api_csrf(request)
        user_id = require_user_id(request)
        await require_entitled(db_session, user_id)
        conversation = await owned_conversation(
            db_session, conversation_id, user_id, lock=True
        )
        turn = await db_session.scalar(
            select(WebChatTurn)
            .where(
                WebChatTurn.id == turn_id,
                WebChatTurn.conversation_id == conversation_id,
            )
            .with_for_update()
        )
        if turn is None:
            raise HTTPException(status_code=404, detail="Turn not found.")
        if turn.status not in ACTIVE_TURN_STATUSES:
            return {"status": turn.status}
        was_not_running = turn.status in {"submitted", "queued"}
        now = datetime.now(UTC)
        turn.stop_requested_at = turn.stop_requested_at or now
        turn.status = "stopping"
        conversation.status = "stopping"
        children = list(
            (
                await db_session.execute(
                    select(WebChatSubagent)
                    .where(
                        WebChatSubagent.root_turn_id == turn.id,
                        WebChatSubagent.status.in_(("queued", "running")),
                    )
                    .with_for_update()
                )
            ).scalars()
        )
        worker_job_ids: list[str] = []
        root_job_id, root_pending_cancelled = await cancel_dispatch(
            db_session, job_type="chat.turn.run", aggregate_id=turn.id
        )
        if root_job_id:
            worker_job_ids.append(root_job_id)
        for child in children:
            child.cancel_requested_at = now
            if child.status == "queued":
                child.status = "cancelled"
                child.finished_at = now
            child_job_id, _ = await cancel_dispatch(
                db_session, job_type="chat.subagent.run", aggregate_id=child.id
            )
            if child_job_id:
                worker_job_ids.append(child_job_id)
        await db_session.commit()
        with suppress(Exception):
            await publish_cancellation(turn.id, reason="stop")
        root_queue_cancelled = False
        for worker_job_id in worker_job_ids:
            try:
                cancelled = await get_handle(worker_job_id).cancel()
                if worker_job_id == root_job_id:
                    root_queue_cancelled = bool(cancelled)
            except Exception:
                continue
        can_finalize = (
            root_queue_cancelled
            or root_pending_cancelled
            or (root_job_id is None and was_not_running)
        ) and not any(child.status == "running" for child in children)
        if can_finalize:
            await _finalize_cancelled_turn(turn.id, db_session)
        return {"status": "stopped" if can_finalize else turn.status}

    @post(
        "/conversations/{conversation_id:uuid}/turns/{turn_id:uuid}/regenerate",
        status_code=HTTP_201_CREATED,
    )
    async def regenerate(
        self,
        conversation_id: UUID,
        turn_id: UUID,
        request: Request,
        db_session: AsyncSession,
    ) -> dict:
        require_api_csrf(request)
        user_id = require_user_id(request)
        permissions = await require_entitled(db_session, user_id)
        conversation = await owned_conversation(
            db_session, conversation_id, user_id, lock=True
        )
        original = await db_session.scalar(
            select(WebChatTurn).where(
                WebChatTurn.id == turn_id,
                WebChatTurn.conversation_id == conversation_id,
            )
        )
        if original is None or original.status not in {"complete", "stopped", "error"}:
            raise HTTPException(
                status_code=409, detail="Only a finished turn can be regenerated."
            )
        if await db_session.scalar(
            select(func.count(WebChatTurn.id)).where(
                WebChatTurn.conversation_id == conversation_id,
                WebChatTurn.status.in_(ACTIVE_TURN_STATUSES),
            )
        ):
            raise HTTPException(status_code=409, detail="Another turn is active.")
        root_turn_id = original.regenerates_turn_id or original.id
        root_turn = await db_session.get(WebChatTurn, root_turn_id)
        if root_turn is None:
            raise HTTPException(
                status_code=409, detail="Response family no longer exists."
            )
        active = await db_session.scalar(
            select(WebChatMessage)
            .where(
                WebChatMessage.version_group == root_turn.response_version_group,
                WebChatMessage.role == "assistant",
                WebChatMessage.is_active.is_(True),
            )
            .with_for_update()
        )
        if active is None:
            raise HTTPException(
                status_code=409, detail="Active response version is missing."
            )
        # Regenerating an older branch while later user turns exist would make
        # those turns depend on an answer that is being replaced. Branching is
        # intentionally explicit rather than silently corrupting context.
        later_user = await db_session.scalar(
            select(func.count(WebChatMessage.id)).where(
                WebChatMessage.conversation_id == conversation_id,
                WebChatMessage.role == "user",
                WebChatMessage.is_active.is_(True),
                WebChatMessage.sequence > root_turn.response_sequence,
            )
        )
        if later_user:
            raise HTTPException(
                status_code=409,
                detail="Only the latest response can be regenerated.",
            )
        next_version = (
            int(
                await db_session.scalar(
                    select(func.max(WebChatMessage.version_number)).where(
                        WebChatMessage.version_group == root_turn.response_version_group
                    )
                )
                or 0
            )
            + 1
        )
        tier = resolve_spend_tier(permissions)
        if tier is None:
            raise HTTPException(status_code=403, detail="Chat is not enabled.")
        admission = await current_spend_decision(
            db_session,
            user_id=user_id,
            tier=tier,
            intelligence_mode=conversation.intelligence_mode,
        )
        if admission.hard_cutoff:
            raise HTTPException(
                status_code=429,
                detail="Chat usage limit reached for the current spend window.",
            )
        window = await get_or_start_four_hour_window(db_session, user_id)
        regenerated = WebChatTurn(
            conversation_id=conversation_id,
            sequence=conversation.next_sequence,
            submission_key=f"regenerate:{root_turn_id}:{uuid4().hex}",
            kind="regenerate",
            regenerates_turn_id=root_turn_id,
            response_version_group=root_turn.response_version_group,
            response_sequence=root_turn.response_sequence,
            model_key=conversation.selected_model_key,
            reasoning_level=conversation.reasoning_level,
            status="submitted",
            four_hour_window_id=window.id,
        )
        db_session.add(regenerated)
        await db_session.flush()
        active.is_active = False
        db_session.add(
            WebChatMessage(
                conversation_id=conversation.id,
                turn_id=regenerated.id,
                sequence=root_turn.response_sequence,
                role="assistant",
                content="",
                version_group=root_turn.response_version_group,
                version_number=next_version,
                is_active=True,
            )
        )
        conversation.next_sequence += 1
        conversation.status = "submitted"
        dispatch = await create_dispatch(
            db_session,
            job_type="chat.turn.run",
            aggregate_id=regenerated.id,
            payload={"turn_id": str(regenerated.id)},
        )
        await db_session.commit()
        await _try_dispatch(dispatch.id)
        return {
            "turn_id": str(regenerated.id),
            "status": "submitted",
            "version_group": str(root_turn.response_version_group),
            "version_number": next_version,
        }

    @patch(
        "/conversations/{conversation_id:uuid}/turns/{turn_id:uuid}/selected-version"
    )
    async def select_version(
        self,
        conversation_id: UUID,
        turn_id: UUID,
        data: VersionBody,
        request: Request,
        db_session: AsyncSession,
    ) -> dict:
        require_api_csrf(request)
        user_id = require_user_id(request)
        await require_entitled(db_session, user_id)
        await owned_conversation(db_session, conversation_id, user_id, lock=True)
        routed_turn = await db_session.scalar(
            select(WebChatTurn).where(
                WebChatTurn.id == turn_id,
                WebChatTurn.conversation_id == conversation_id,
            )
        )
        if routed_turn is None:
            raise HTTPException(status_code=404, detail="Turn not found.")
        if await db_session.scalar(
            select(func.count(WebChatTurn.id)).where(
                WebChatTurn.conversation_id == conversation_id,
                WebChatTurn.status.in_(ACTIVE_TURN_STATUSES),
            )
        ):
            raise HTTPException(
                status_code=409,
                detail="Wait for the active turn to finish before switching versions.",
            )
        if await db_session.scalar(
            select(func.count(WebChatMessage.id)).where(
                WebChatMessage.conversation_id == conversation_id,
                WebChatMessage.role == "user",
                WebChatMessage.is_active.is_(True),
                WebChatMessage.sequence > routed_turn.response_sequence,
            )
        ):
            raise HTTPException(
                status_code=409,
                detail="Only the latest response version can be switched.",
            )
        try:
            message_id = UUID(data.message_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid message ID.") from None
        target = await db_session.scalar(
            select(WebChatMessage).where(
                WebChatMessage.id == message_id,
                WebChatMessage.conversation_id == conversation_id,
                WebChatMessage.role == "assistant",
                WebChatMessage.version_group == routed_turn.response_version_group,
            )
        )
        if target is None:
            raise HTTPException(status_code=404, detail="Response version not found.")
        await db_session.execute(
            update(WebChatMessage)
            .where(WebChatMessage.version_group == target.version_group)
            .values(is_active=False)
        )
        target.is_active = True
        conversation = await db_session.get(WebChatConversation, conversation_id)
        if conversation is not None:
            conversation.context_revision += 1
            conversation.context_state = []
        await db_session.commit()
        return {"message_id": str(target.id), "version_number": target.version_number}
