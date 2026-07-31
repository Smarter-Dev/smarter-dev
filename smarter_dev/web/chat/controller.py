"""Unified `/chat` shell for private web Chat and shareable Resources mode."""

from __future__ import annotations

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
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from smarter_dev.shared.model_catalog import get_model
from smarter_dev.web.agent_api import resources_quota_state
from smarter_dev.web.chat.api import require_user_id
from smarter_dev.web.chat.entitlements import has_chat
from smarter_dev.web.chat.entitlements import has_ultra_chat
from smarter_dev.web.chat.settings import ensure_settings
from smarter_dev.web.models import AgentConversation
from smarter_dev.web.models import ResourceAgentRun
from smarter_dev.web.models import WebChatAttachment
from smarter_dev.web.models import WebChatConversation
from smarter_dev.web.models import WebChatMessage
from smarter_dev.web.models import WebChatTurn
from smarter_dev.web.sdanswer import enrich_answer


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
            }
        )
    versions: dict[str, list[dict]] = {}
    rendered = []
    for message in messages:
        item = {
            "id": str(message.id),
            "turn_id": str(message.turn_id),
            "role": message.role,
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
    return {"messages": rendered, "versions": versions, "active_turn": active_turn}


@get("/chat")
async def chat_index(request: Request, db_session: AsyncSession) -> Template:
    user_id = require_user_id(request)
    user = await db_session.get(User, user_id)
    permissions = await get_user_permissions(db_session, user_id)
    if user is None or not user.is_active or not has_chat(permissions):
        raise HTTPException(
            status_code=403, detail="Chat is not enabled for your account."
        )
    settings = await ensure_settings(db_session)
    conversations = list(
        (
            await db_session.execute(
                select(WebChatConversation)
                .where(
                    WebChatConversation.owner_user_id == user_id,
                )
                .order_by(WebChatConversation.updated_at.desc())
                .limit(50)
            )
        ).scalars()
    )
    return Template(
        "chat/index.html",
        context={
            "conversation": None,
            "messages": [],
            "versions": {},
            "mode": "chat",
            "settings": settings,
            "conversations": conversations,
            "ultra_chat": has_ultra_chat(permissions),
            "seo_meta": {
                "robots": "noindex,nofollow",
                "description": "Smarter Dev Chat",
            },
        },
    )


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
        context = await _chat_context(db_session, web)
        settings = await ensure_settings(db_session)
        conversations = list(
            (
                await db_session.execute(
                    select(WebChatConversation)
                    .where(
                        WebChatConversation.owner_user_id == user_id,
                    )
                    .order_by(WebChatConversation.updated_at.desc())
                    .limit(50)
                )
            ).scalars()
        )
        return Template(
            "chat/index.html",
            context={
                "conversation": web,
                "mode": "chat",
                "conversations": conversations,
                "model": get_model(web.selected_model_key),
                "settings": settings,
                "ultra_chat": has_ultra_chat(permissions),
                "seo_meta": {
                    "robots": "noindex,nofollow",
                    "description": web.title or "Smarter Dev Chat",
                },
                **context,
            },
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
