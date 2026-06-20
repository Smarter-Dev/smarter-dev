"""GET /ai/answer/{conversation_id} — render a persisted agent conversation.

Open to anyone with the link (private but not protected). The reply form
only renders when the current session's user owns the conversation; everyone
else gets a read-only view with a CTA back to /resources.
"""

from __future__ import annotations

import logging
from datetime import timezone
from uuid import UUID

from litestar import Request, get
from litestar.exceptions import NotFoundException
from litestar.response import Template
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from skrift.auth.guards import Permission
from skrift.auth.services import get_user_permissions
from skrift.auth.session_keys import SESSION_USER_ID
from skrift.db.models.user import User
from skrift.markdown import render_markdown

from smarter_dev.web.agent_api import resources_quota_state
from smarter_dev.web.models import AgentConversation
from smarter_dev.web.sdanswer import enrich_answer

_HISTORY_PERMISSION = Permission("view-answer-history")

logger = logging.getLogger(__name__)


def _asker_display_name(user) -> str:
    """Return a human-readable label for the conversation's asker.

    Falls back to the local-part of their email, then a generic "Guest" so
    the chat never renders a blank role label if the user row is missing or
    has nullable fields unset.
    """
    if user is None:
        return "Guest"
    name = (getattr(user, "name", None) or "").strip()
    if name:
        return name
    email = (getattr(user, "email", None) or "").strip()
    if email:
        return email.split("@", 1)[0]
    return "Guest"


def _to_utc(dt):
    """Ensure ``dt.isoformat()`` carries a UTC offset.

    Postgres TIMESTAMPTZ values come back tz-aware, but a naive datetime would
    serialize without an offset and the browser would mis-parse it as local
    time. This guards against that drift.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _format_clock(dt) -> str:
    """Render a UTC fallback timestamp as `H:MM AM/PM` (no leading zero on hour).

    The browser overrides this via `answer-time.js` so each reader sees their
    own local clock; this string only shows for the brief moment before the
    script runs (and for JS-disabled readers).
    """
    if dt is None:
        return ""
    hour = dt.hour % 12 or 12
    return f"{hour}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


def _current_user_id(request: Request) -> UUID | None:
    raw = request.session.get(SESSION_USER_ID) if request.session else None
    if not raw:
        return None
    try:
        return UUID(str(raw))
    except (ValueError, TypeError):
        return None


@get("/ai/answer/{conversation_id:uuid}")
async def answer_view(
    conversation_id: UUID, request: Request, db_session: AsyncSession
) -> Template:
    stmt = (
        select(AgentConversation)
        .where(AgentConversation.id == conversation_id)
        .options(selectinload(AgentConversation.messages))
    )
    result = await db_session.execute(stmt)
    conversation = result.scalar_one_or_none()
    if conversation is None:
        raise NotFoundException()

    current_id = _current_user_id(request)
    is_owner = current_id is not None and current_id == conversation.owner_user_id

    owner = await db_session.scalar(
        select(User).where(User.id == conversation.owner_user_id)
    )
    asker_name = _asker_display_name(owner)

    quota_state: dict | None = None
    if is_owner:
        quota_state = await resources_quota_state(
            db_session, current_id, conversation_id=conversation.id
        )

    # Sidebar of the viewer's own recent conversations of the SAME agent
    # type (so the resources answer page only links to other resources
    # answers, etc.). Gated on the same `view-answer-history` permission
    # used on /resources.
    recent_answers: list[dict] = []
    if current_id is not None:
        perms = await get_user_permissions(db_session, current_id)
        if await _HISTORY_PERMISSION.check(perms):
            stmt = (
                select(AgentConversation.id, AgentConversation.title)
                .where(AgentConversation.owner_user_id == current_id)
                .where(AgentConversation.agent_type == conversation.agent_type)
                .order_by(AgentConversation.created_at.desc())
            )
            result = await db_session.execute(stmt)
            recent_answers = [
                {"id": str(row.id), "title": row.title or "Untitled"}
                for row in result.all()
            ]

    messages = sorted(conversation.messages, key=lambda m: m.sequence)
    turns = []
    for m in messages:
        if m.role == "assistant":
            html, blocks = await enrich_answer(db_session, m.content or "")
        else:
            html = render_markdown(m.content or "")
            blocks = []
        turns.append(
            {
                "id": str(m.id),
                "sequence": m.sequence,
                "role": m.role,
                "content": m.content,
                "content_html": html,
                "citations": list(m.citations or []),
                "sdanswer_blocks": blocks,
                "created_at": _to_utc(m.created_at),
                "created_at_display": _format_clock(m.created_at),
            }
        )

    title = conversation.title or "Smarter Dev answer"
    answer_url = f"https://smarter.dev/ai/answer/{conversation.id}"

    return Template(
        "ai/answer.html",
        context={
            "conversation": {
                "id": str(conversation.id),
                "agent_type": conversation.agent_type,
                "title": title,
                "created_at": conversation.created_at,
                "updated_at": conversation.updated_at,
                "meta": dict(conversation.meta or {}),
            },
            "turns": turns,
            "is_owner": is_owner,
            "asker_name": asker_name,
            "answer_url": answer_url,
            "recent_answers": recent_answers,
            "quota_state": quota_state,
            "seo_meta": {
                "description": title,
                "canonical_url": answer_url,
                "robots": "noindex,nofollow",
            },
            "og_meta": {
                "title": title,
                "description": "An answer from Smarter Dev.",
                "url": answer_url,
                "site_name": "Smarter Dev",
                "type": "article",
                "image": "",
            },
        },
    )
