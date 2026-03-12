"""Research sessions admin controller for the Skrift admin panel."""

from __future__ import annotations

from uuid import UUID

from litestar import Controller, Request, get
from litestar.response import Template as TemplateResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.admin.helpers import get_admin_context
from skrift.admin.navigation import ADMIN_NAV_TAG
from skrift.auth.guards import auth_guard, Permission

from smarter_dev.web.models import ResearchSession


class ResearchSessionsAdminController(Controller):
    """Research sessions in the Skrift admin panel."""

    path = "/admin"
    guards = [auth_guard]

    @get(
        "/research-sessions",
        tags=[ADMIN_NAV_TAG],
        guards=[auth_guard, Permission("administrator")],
        opt={"label": "Research Sessions", "icon": "search", "order": 56},
    )
    async def research_sessions_list(
        self, request: Request, db_session: AsyncSession
    ) -> TemplateResponse:
        """List research sessions."""
        ctx = await get_admin_context(request, db_session)

        result = await db_session.execute(
            select(ResearchSession)
            .order_by(ResearchSession.created_at.desc())
            .limit(100)
        )
        sessions = list(result.scalars().all())

        total_result = await db_session.execute(
            select(func.count()).select_from(ResearchSession)
        )
        total = total_result.scalar() or 0

        return TemplateResponse(
            "admin/research_sessions.html",
            context={
                "sessions": sessions,
                "total": total,
                **ctx,
            },
        )

    @get(
        "/research-sessions/{session_id:uuid}",
        guards=[auth_guard, Permission("administrator")],
    )
    async def research_session_detail(
        self, request: Request, db_session: AsyncSession, session_id: UUID
    ) -> TemplateResponse:
        """Detail view for a single research session."""
        ctx = await get_admin_context(request, db_session)

        result = await db_session.execute(
            select(ResearchSession).where(ResearchSession.id == session_id)
        )
        session = result.scalar_one_or_none()

        if session is None:
            from litestar.exceptions import NotFoundException
            raise NotFoundException(f"Session {session_id} not found")

        # Extract planner reasoning and user profile snapshot from context
        context = session.context or {}
        planner_reasoning = context.get("planner_reasoning", "")
        user_profile_snapshot = context.get("user_profile_snapshot", "")

        return TemplateResponse(
            "admin/research_session_detail.html",
            context={
                "session": session,
                "planner_reasoning": planner_reasoning,
                "user_profile_snapshot": user_profile_snapshot,
                **ctx,
            },
        )
