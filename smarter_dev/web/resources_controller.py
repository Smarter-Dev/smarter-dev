"""Resources index page at /resources.

Lightweight hub that lists the collections we publish under /resources/*.
Currently a single entry (agentic coding) plus a "more coming" placeholder,
so the route doubles as a stable parent for breadcrumbs and future SEO.
"""

from __future__ import annotations

from uuid import UUID

from litestar import Request, get
from litestar.response import Template
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.guards import Permission
from skrift.auth.services import get_user_permissions
from skrift.auth.session_keys import SESSION_USER_ID
from skrift.db.models.user import User

from smarter_dev.web.agent_api import resources_quota_state
from smarter_dev.web.models import AgentConversation


def _asker_display_name(user) -> str:
    """Return a human-readable label for the logged-in user (uppercased
    elsewhere). Mirrors the helper in ``agent_view_controller``."""
    if user is None:
        return "You"
    name = (getattr(user, "name", None) or "").strip()
    if name:
        return name
    email = (getattr(user, "email", None) or "").strip()
    if email:
        return email.split("@", 1)[0]
    return "You"

# Granted to every `sudo-*` tier in `smarter_dev/web/roles.py`; the Skrift
# `Permission` guard also bypasses for anyone holding `administrator`.
_HISTORY_PERMISSION = Permission("view-answer-history")


@get("/resources")
async def resources_index(
    request: Request, db_session: AsyncSession
) -> Template:
    user_id_raw = (
        request.session.get(SESSION_USER_ID) if request.session else None
    )
    is_authenticated = bool(user_id_raw)

    asker_name = "You"
    recent_answers: list[dict] = []
    quota_state: dict | None = None
    if user_id_raw:
        try:
            user_uuid = UUID(str(user_id_raw))
        except (ValueError, TypeError):
            user_uuid = None
        if user_uuid is not None:
            owner = await db_session.scalar(
                select(User).where(User.id == user_uuid)
            )
            asker_name = _asker_display_name(owner)
            quota_state = await resources_quota_state(db_session, user_uuid)
        perms = await get_user_permissions(db_session, user_id_raw)
        if user_uuid is not None and await _HISTORY_PERMISSION.check(perms):
            stmt = (
                select(AgentConversation.id, AgentConversation.title)
                .where(AgentConversation.owner_user_id == user_uuid)
                .where(AgentConversation.agent_type == "resources")
                .order_by(AgentConversation.created_at.desc())
            )
            result = await db_session.execute(stmt)
            recent_answers = [
                {"id": str(row.id), "title": row.title or "Untitled"}
                for row in result.all()
            ]

    return Template(
        "resources.html",
        context={
            "is_authenticated": is_authenticated,
            "asker_name": asker_name,
            "recent_answers": recent_answers,
            "quota_state": quota_state,
            "seo_meta": {
                "description": (
                    "Writing, courses, and tutorials from around the "
                    "web for engineers working with modern dev tooling "
                    "and AI-assisted software development."
                ),
                "canonical_url": "https://smarter.dev/resources",
                "robots": "index,follow",
            },
            "og_meta": {
                "title": "Resources: Smarter Dev",
                "description": (
                    "Writing, courses, and tutorials from around the "
                    "web for engineers working with modern dev tooling "
                    "and AI-assisted software development."
                ),
                "url": "https://smarter.dev/resources",
                "site_name": "Smarter Dev",
                "type": "website",
                "image": "",
            },
        },
    )
