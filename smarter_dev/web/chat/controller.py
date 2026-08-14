"""Unified `/chat` shell for private web Chat and shareable Resources mode."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from uuid import UUID

from litestar import Request
from litestar import get
from litestar.exceptions import HTTPException
from litestar.exceptions import NotFoundException
from litestar.response import Redirect
from litestar.response import Template
from skrift.auth.services import get_user_permissions
from skrift.auth.session_keys import SESSION_USER_ID
from skrift.db.models.user import User
from skrift.markdown import render_markdown
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from smarter_dev.shared.model_catalog import MODEL_CATALOG
from smarter_dev.shared.model_catalog import get_model
from smarter_dev.shared.model_catalog import model_vendor
from smarter_dev.web.agent_api import resources_quota_state
from smarter_dev.web.chat.api import require_user_id
from smarter_dev.web.chat.api import resolved_conversation_settings
from smarter_dev.web.chat.api import turn_meta
from smarter_dev.web.chat.documents import attachment_kind
from smarter_dev.web.chat.documents import conversation_artifacts
from smarter_dev.web.chat.entitlements import has_chat
from smarter_dev.web.chat.entitlements import has_ultra_chat
from smarter_dev.web.chat.settings import ensure_settings
from smarter_dev.web.chat.threads import QUICK_CHAT_MODE
from smarter_dev.web.chat.threads import thread_breaks
from smarter_dev.web.chat.threads import thread_snapshots
from smarter_dev.web.models import AgentConversation
from smarter_dev.web.models import ChatCatalogModel
from smarter_dev.web.models import ResourceAgentRun
from smarter_dev.web.models import WebChatAttachment
from smarter_dev.web.models import WebChatConversation
from smarter_dev.web.models import WebChatDocument
from smarter_dev.web.models import WebChatMessage
from smarter_dev.web.models import WebChatTurn
from smarter_dev.web.sdanswer import enrich_answer

# The Quick chat's fixed name. It is a surface rather than a subject, so this is
# a label, not a title anything is allowed to rewrite.
QUICK_CHAT_TITLE = "Quick chat"


def group_conversations(conversations: list, now: datetime) -> list[dict]:
    """Bucket the history rail by recency: Today, This week, then Older.

    Buckets are emitted in fixed order and empty ones are dropped, so the rail
    never shows a heading with nothing under it.
    """
    buckets: dict[str, list] = {"Today": [], "This week": [], "Older": []}
    today = now.date()
    for item in conversations:
        stamp = getattr(item, "updated_at", None) or getattr(item, "created_at", None)
        if stamp is None:
            buckets["Older"].append(item)
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        age = today - stamp.astimezone(UTC).date()
        if age.days <= 0:
            buckets["Today"].append(item)
        elif age.days <= 7:
            buckets["This week"].append(item)
        else:
            buckets["Older"].append(item)
    return [
        {"label": label, "items": items} for label, items in buckets.items() if items
    ]


async def _rail_context(session: AsyncSession, user_id: UUID) -> dict:
    """The history rail: live conversations grouped by recency, archived below.

    Archiving is filing, not deleting, so an archived conversation keeps its URL
    and stays reachable — it just moves out of the way into a shut drawer at the
    foot of the rail instead of competing with what the owner is working on.

    The Quick chat is deliberately not one of the grouped rows. It is a fixed
    surface rather than a filed conversation, so it is returned on its own and
    pinned above the recency bands instead of drifting down them.
    """
    conversations = list(
        (
            await session.execute(
                select(WebChatConversation)
                .where(
                    WebChatConversation.owner_user_id == user_id,
                    WebChatConversation.archived_at.is_(None),
                    WebChatConversation.chat_mode != QUICK_CHAT_MODE,
                )
                .order_by(WebChatConversation.updated_at.desc())
                .limit(50)
            )
        ).scalars()
    )
    archived = list(
        (
            await session.execute(
                select(WebChatConversation)
                .where(
                    WebChatConversation.owner_user_id == user_id,
                    WebChatConversation.archived_at.is_not(None),
                )
                .order_by(WebChatConversation.archived_at.desc())
                .limit(50)
            )
        ).scalars()
    )
    return {
        "conversations": conversations,
        "conversation_groups": group_conversations(conversations, datetime.now(UTC)),
        "archived_conversations": archived,
        "quick_conversation": await _live_quick_conversation(session, user_id),
    }


async def _live_quick_conversation(
    session: AsyncSession, user_id: UUID
) -> WebChatConversation | None:
    """The owner's one unarchived Quick chat, or None before they open one.

    ``uq_web_chat_one_live_quick`` is what makes "one" true, so this can select
    without an ordering and without worrying about picking the wrong row.
    """
    return await session.scalar(
        select(WebChatConversation).where(
            WebChatConversation.owner_user_id == user_id,
            WebChatConversation.chat_mode == QUICK_CHAT_MODE,
            WebChatConversation.archived_at.is_(None),
        )
    )


async def ensure_quick_conversation(
    session: AsyncSession, user_id: UUID, permissions
) -> WebChatConversation:
    """Get-or-create the caller's Quick chat.

    ``/chat`` is a fixed URL rather than a create button, so every visit
    runs this and all but the first are a plain select. Two tabs opening the URL
    at the same moment both reach the insert; the partial unique index fails the
    loser, which then adopts the winner's row rather than raising at a person
    who did nothing wrong.
    """
    existing = await _live_quick_conversation(session, user_id)
    if existing is not None:
        return existing
    intelligence_mode, model_key, reasoning_level = (
        await resolved_conversation_settings(session, permissions=permissions)
    )
    conversation = WebChatConversation(
        owner_user_id=user_id,
        intelligence_mode=intelligence_mode,
        selected_model_key=model_key,
        reasoning_level=reasoning_level,
        # Named up front and marked custom: the Quick chat is a place, not a
        # subject, so nothing — agent or evaluator — should rename it.
        title=QUICK_CHAT_TITLE,
        title_is_custom=True,
        chat_mode=QUICK_CHAT_MODE,
        status="idle",
        next_sequence=1,
    )
    session.add(conversation)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        winner = await _live_quick_conversation(session, user_id)
        if winner is None:
            # Not the race, then. Something else rejected the row; say so.
            raise
        return winner
    return conversation


async def _catalog_context(session: AsyncSession, settings) -> dict:
    """Enabled models rendered into the page so the selects work without JS.

    ``/v2/api/chat/catalog`` stays the source of truth — chat.js reconciles the
    same data after load — but the server-rendered options mean the model and
    reasoning controls are usable before the fetch resolves (and if it fails).
    """
    rows = {
        row.model_key: row
        for row in (
            await session.execute(
                select(ChatCatalogModel)
                .where(ChatCatalogModel.enabled.is_(True))
                .order_by(ChatCatalogModel.sort_order)
            )
        ).scalars()
    }
    models = [
        {
            "key": model.key,
            "label": model.label,
            "vendor": model_vendor(model),
            "cost_tier": rows[model.key].cost_tier,
            "reasoning_levels": [level.value for level in model.reasoning_levels],
        }
        for model in MODEL_CATALOG
        if model.key in rows
    ]
    default_model = get_model(settings.default_model_key)
    return {
        "catalog_models": models,
        "default_model_key": settings.default_model_key,
        "default_reasoning": settings.default_reasoning or "",
        "default_reasoning_levels": [
            level.value for level in default_model.reasoning_levels
        ]
        if default_model
        else [],
    }


async def _chat_context(
    session: AsyncSession, conversation: WebChatConversation
) -> dict:
    messages = list(
        (
            await session.execute(
                select(WebChatMessage)
                .where(
                    WebChatMessage.conversation_id == conversation.id,
                )
                .order_by(WebChatMessage.sequence, WebChatMessage.version_number)
            )
        ).scalars()
    )
    attachment_rows = list(
        (
            await session.execute(
                select(WebChatAttachment).where(
                    WebChatAttachment.conversation_id == conversation.id,
                    WebChatAttachment.turn_id.is_not(None),
                    WebChatAttachment.status == "ready",
                )
            )
        ).scalars()
    )
    attachments_by_turn: dict[UUID, list[dict]] = {}
    for attachment in attachment_rows:
        attachments_by_turn.setdefault(attachment.turn_id, []).append(
            {
                "id": str(attachment.id),
                "name": attachment.original_name,
                "media_type": attachment.media_type,
                "size_bytes": attachment.size_bytes,
                "kind": attachment_kind(attachment.media_type),
            }
        )
    document_rows = list(
        (
            await session.execute(
                select(WebChatDocument)
                .where(
                    WebChatDocument.conversation_id == conversation.id,
                    # An overwritten file is no longer in the conversation.
                    WebChatDocument.status != "superseded",
                )
                .order_by(WebChatDocument.created_at)
            )
        ).scalars()
    )
    documents_by_message: dict[UUID, list[dict]] = {}
    for document in document_rows:
        documents_by_message.setdefault(document.assistant_message_id, []).append(
            {
                "id": str(document.id),
                "title": document.title,
                "filename": document.filename,
                "size_bytes": document.size_bytes,
                "status": document.status,
                "assistant_message_id": str(document.assistant_message_id),
                "created_at": document.created_at,
            }
        )
    turns = {
        turn.id: turn
        for turn in (
            await session.execute(
                select(WebChatTurn).where(
                    WebChatTurn.conversation_id == conversation.id
                )
            )
        ).scalars()
    }
    versions: dict[str, list[dict]] = {}
    rendered = []
    for message in messages:
        item = {
            "id": str(message.id),
            "turn_id": str(message.turn_id),
            "role": message.role,
            **(
                turn_meta(turns.get(message.turn_id))
                if message.role == "assistant"
                else {"model_key": None, "elapsed": None}
            ),
            "content": message.content,
            "content_html": render_markdown(message.content or ""),
            "sequence": message.sequence,
            "version_group": str(message.version_group),
            "version_number": message.version_number,
            "is_active": message.is_active,
            "stopped": message.stopped,
            "attachments": attachments_by_turn.get(message.turn_id, [])
            if message.role == "user"
            else [],
            "documents": documents_by_message.get(message.id, [])
            if message.role == "assistant"
            else [],
        }
        if message.role == "assistant":
            versions.setdefault(str(message.version_group), []).append(item)
        if message.role != "assistant" or message.is_active:
            rendered.append(item)
    active_turn = await session.scalar(
        select(WebChatTurn)
        .where(
            WebChatTurn.conversation_id == conversation.id,
            WebChatTurn.status.in_(("submitted", "queued", "running", "stopping")),
        )
        .order_by(WebChatTurn.created_at.desc())
        .limit(1)
    )
    # A flat, oldest-first roll-up for the documents panel. Regenerating a turn
    # supersedes its assistant message, and the documents that message produced
    # go with it — the panel lists what the conversation *currently* holds, so
    # it is filtered to the messages actually on screen (chat.js applies the
    # same rule to the API snapshot in visibleDocuments()).
    visible_messages = {
        item["id"] for item in rendered if item["role"] == "assistant"
    }
    documents = [
        document
        for message_id in visible_messages
        for document in documents_by_message.get(UUID(message_id), [])
    ]
    documents.sort(key=lambda item: item["created_at"])
    # The panel holds uploads alongside them. An upload belongs to the user's
    # message rather than to a response, so nothing can supersede it — only
    # written documents are filtered by which reply is on screen.
    artifacts = [
        {**artifact.as_dict(), "created_at": artifact.created_at}
        for artifact in await conversation_artifacts(
            session, conversation_id=conversation.id
        )
        if artifact.is_upload
        or (
            artifact.assistant_message_id
            and str(artifact.assistant_message_id) in visible_messages
        )
    ]
    artifacts.sort(key=lambda item: (item["created_at"] is None, item["created_at"]))
    # The same list the reconcile API ships, plus a lookup the message loop can
    # index by sequence — scanning the thread list inside the Jinja loop would
    # be quadratic and would put the "thread 1 draws nothing" rule in a second
    # place.
    threads = await thread_snapshots(session, conversation_id=conversation.id)
    return {
        "messages": rendered,
        "versions": versions,
        "active_turn": active_turn,
        "documents": documents,
        "artifacts": artifacts,
        "threads": threads,
        "thread_breaks": thread_breaks(threads),
    }


async def _web_chat_page(
    session: AsyncSession,
    *,
    user_id: UUID,
    conversation: WebChatConversation,
    permissions,
) -> Template:
    """The one web-chat page render, shared by ``/chat`` and ``/chat/{id}``.

    A Quick chat is an ordinary conversation in every respect this page cares
    about — same composer, same selects, same documents dock — so it renders
    through exactly the same context rather than a parallel one that could drift.
    """
    context = await _chat_context(session, conversation)
    settings = await ensure_settings(session)
    selected_model = get_model(conversation.selected_model_key)
    return Template(
        "chat/index.html",
        context={
            "conversation": conversation,
            "mode": "chat",
            "quick_chat": conversation.chat_mode == QUICK_CHAT_MODE,
            **await _rail_context(session, user_id),
            "model": selected_model,
            "model_reasoning_levels": [
                level.value for level in selected_model.reasoning_levels
            ]
            if selected_model
            else [],
            "settings": settings,
            "ultra_chat": has_ultra_chat(permissions),
            "seo_meta": {
                "robots": "noindex,nofollow",
                "description": conversation.title or "Smarter Dev Chat",
            },
            **await _catalog_context(session, settings),
            **context,
        },
    )


@get("/chat/new")
async def chat_new(request: Request, db_session: AsyncSession) -> Template:
    """The empty shell an ordinary multi-turn conversation starts from.

    This is where ``/chat`` used to land, and it is load-bearing beyond
    convenience: ``intelligence_mode`` is fixed when a conversation is created
    and immutable afterwards, so the select on this page is the only place it
    can ever be chosen.
    """
    user_id = require_user_id(request)
    user = await db_session.get(User, user_id)
    permissions = await get_user_permissions(db_session, user_id)
    if user is None or not user.is_active or not has_chat(permissions):
        raise HTTPException(
            status_code=403, detail="Chat is not enabled for your account."
        )
    settings = await ensure_settings(db_session)
    return Template(
        "chat/index.html",
        context={
            "conversation": None,
            "messages": [],
            "versions": {},
            "mode": "chat",
            "settings": settings,
            **await _rail_context(db_session, user_id),
            "ultra_chat": has_ultra_chat(permissions),
            "seo_meta": {
                "robots": "noindex,nofollow",
                "description": "Smarter Dev Chat",
            },
            **await _catalog_context(db_session, settings),
        },
    )


@get("/chat")
async def chat_quick(request: Request, db_session: AsyncSession) -> Template:
    """The Quick chat: one perpetual conversation per person, and the default view.

    A ``GET`` that can write a row is a deliberate exception here: it is a
    get-or-create for the caller's own workspace, it is idempotent, and the
    route is behind auth and marked ``noindex,nofollow``.
    """
    user_id = require_user_id(request)
    user = await db_session.get(User, user_id)
    permissions = await get_user_permissions(db_session, user_id)
    if user is None or not user.is_active or not has_chat(permissions):
        raise HTTPException(
            status_code=403, detail="Chat is not enabled for your account."
        )
    conversation = await ensure_quick_conversation(db_session, user_id, permissions)
    return await _web_chat_page(
        db_session,
        user_id=user_id,
        conversation=conversation,
        permissions=permissions,
    )


@get("/chat/quick")
async def legacy_quick_chat_redirect() -> Redirect:
    """The Quick chat's first URL, kept alive for anything that bookmarked it."""
    return Redirect(path="/chat", status_code=308)


@get("/chat/{conversation_id:uuid}")
async def chat_conversation(
    conversation_id: UUID, request: Request, db_session: AsyncSession
) -> Template:
    # Web Chat is always owner-only and intentionally checked first.
    web = await db_session.get(WebChatConversation, conversation_id)
    if web is not None:
        user_id = require_user_id(request)
        user = await db_session.get(User, user_id)
        permissions = await get_user_permissions(db_session, user_id)
        if (
            user is None
            or not user.is_active
            or web.owner_user_id != user_id
            or not has_chat(permissions)
        ):
            raise NotFoundException()
        return await _web_chat_page(
            db_session, user_id=user_id, conversation=web, permissions=permissions
        )

    # Resources remain link-shareable and retain their quota mechanics, but use
    # the unified shell. Only the owner gets a follow-up composer.
    resource = await db_session.scalar(
        select(AgentConversation)
        .where(
            AgentConversation.id == conversation_id,
            AgentConversation.agent_type == "resources",
        )
        .options(selectinload(AgentConversation.messages))
    )
    if resource is None:
        raise NotFoundException()
    raw_user = request.session.get(SESSION_USER_ID) if request.session else None
    try:
        user_id = UUID(str(raw_user)) if raw_user else None
    except (TypeError, ValueError):
        user_id = None
    is_owner = user_id == resource.owner_user_id
    messages = []
    for message in sorted(resource.messages, key=lambda row: row.sequence):
        html, blocks = (
            await enrich_answer(db_session, message.content or "")
            if message.role == "assistant"
            else (render_markdown(message.content or ""), [])
        )
        messages.append(
            {
                "id": str(message.id),
                "role": message.role,
                "content": message.content,
                "content_html": html,
                "sequence": message.sequence,
                "citations": list(message.citations or []),
                "sdanswer_blocks": blocks,
            }
        )
    quota = (
        await resources_quota_state(db_session, user_id, conversation_id=resource.id)
        if is_owner
        else None
    )
    active_resource_run = await db_session.scalar(
        select(ResourceAgentRun).where(
            ResourceAgentRun.conversation_id == resource.id,
            ResourceAgentRun.status.in_(("submitted", "running")),
        )
    )
    resource_conversations = (
        list(
            (
                await db_session.execute(
                    select(AgentConversation)
                    .where(
                        AgentConversation.owner_user_id == user_id,
                        AgentConversation.agent_type == "resources",
                    )
                    .order_by(AgentConversation.updated_at.desc())
                    .limit(50)
                )
            ).scalars()
        )
        if is_owner
        else []
    )
    return Template(
        "chat/index.html",
        context={
            "conversation": resource,
            "mode": "resources",
            "messages": messages,
            "versions": {},
            "conversations": resource_conversations,
            "conversation_groups": group_conversations(
                resource_conversations, datetime.now(UTC)
            ),
            "is_owner": is_owner,
            "quota_state": quota,
            "resource_running": active_resource_run is not None,
            "ultra_chat": False,
            "seo_meta": {
                "robots": "noindex,nofollow",
                "description": resource.title or "Smarter Dev answer",
            },
        },
    )


@get("/ai/answer/{conversation_id:uuid}")
async def legacy_answer_redirect(conversation_id: UUID) -> Redirect:
    return Redirect(path=f"/chat/{conversation_id}", status_code=308)
