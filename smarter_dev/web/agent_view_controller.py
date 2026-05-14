"""GET /ai/answer/{conversation_id} — render a persisted agent conversation.

Open to anyone with the link (private but not protected). The reply form
only renders when the current session's user owns the conversation; everyone
else gets a read-only view with a CTA back to /resources.
"""

from __future__ import annotations

import logging
from uuid import UUID

from litestar import Request, get
from litestar.exceptions import NotFoundException
from litestar.response import Template
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from skrift.auth.session_keys import SESSION_USER_ID
from skrift.lib.markdown import render_markdown

from smarter_dev.web.models import AgentConversation
from smarter_dev.web.sdanswer import enrich_answer

logger = logging.getLogger(__name__)


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
                "created_at": m.created_at,
            }
        )

    title = conversation.title or "Resource Agent answer"

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
            "seo_meta": {
                "description": title,
                "canonical_url": (
                    f"https://smarter.dev/ai/answer/{conversation.id}"
                ),
                "robots": "noindex,nofollow",
            },
            "og_meta": {
                "title": title,
                "description": (
                    "An answer from the Smarter Dev Resource Agent."
                ),
                "url": f"https://smarter.dev/ai/answer/{conversation.id}",
                "site_name": "Smarter Dev",
                "type": "article",
                "image": "",
            },
        },
    )
