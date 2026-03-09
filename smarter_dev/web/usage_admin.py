"""Token usage tracking controller for the Skrift admin panel."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from litestar import Controller, Request, get
from litestar.response import Template as TemplateResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.admin.helpers import get_admin_context
from skrift.admin.navigation import ADMIN_NAV_TAG
from skrift.auth.guards import Permission, auth_guard

from smarter_dev.web.models import ResearchSession


class UsageAdminController(Controller):
    """Token usage tracking in the Skrift admin panel."""

    path = "/admin"
    guards = [auth_guard]

    @get(
        "/usage",
        tags=[ADMIN_NAV_TAG],
        guards=[auth_guard, Permission("administrator")],
        opt={"label": "Usage", "icon": "bar-chart-2", "order": 35},
    )
    async def usage_overview(
        self,
        request: Request,
        db_session: AsyncSession,
        days: Optional[int] = 30,
        pipeline_mode: Optional[str] = None,
    ) -> TemplateResponse:
        """Per-user token usage breakdown."""
        ctx = await get_admin_context(request, db_session)

        # Date cutoff
        cutoff = None
        if days:
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        # Build base filter
        filters = []
        if cutoff:
            filters.append(ResearchSession.created_at >= cutoff)
        if pipeline_mode:
            filters.append(ResearchSession.pipeline_mode == pipeline_mode)

        # Per-user aggregation
        stmt = (
            select(
                ResearchSession.user_id,
                ResearchSession.pipeline_mode,
                func.count().label("session_count"),
                func.sum(ResearchSession.input_tokens).label("total_input"),
                func.sum(ResearchSession.output_tokens).label("total_output"),
            )
            .where(*filters)
            .group_by(ResearchSession.user_id, ResearchSession.pipeline_mode)
            .order_by(
                (func.sum(ResearchSession.input_tokens) + func.sum(ResearchSession.output_tokens)).desc()
            )
        )
        result = await db_session.execute(stmt)
        rows = result.all()

        # Grand totals
        total_sessions = sum(r.session_count for r in rows)
        total_input = sum(r.total_input or 0 for r in rows)
        total_output = sum(r.total_output or 0 for r in rows)

        return TemplateResponse(
            "admin/usage.html",
            context={
                "rows": rows,
                "total_sessions": total_sessions,
                "total_input": total_input,
                "total_output": total_output,
                "days": days,
                "pipeline_mode": pipeline_mode or "",
                **ctx,
            },
        )
