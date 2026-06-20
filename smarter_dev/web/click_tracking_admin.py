"""Admin panel for tracked-link click counters."""

from __future__ import annotations

from litestar import Controller, Request, get
from litestar.response import Template as TemplateResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.admin.helpers import get_admin_context
from skrift.admin.navigation import ADMIN_NAV_TAG
from skrift.auth.guards import auth_guard, Permission
from skrift.flash import get_flash_messages

from smarter_dev.web.models import TrackedLinkCounter


class ClickTrackingAdminController(Controller):
    """Tracked-link counters in the Skrift admin panel."""

    path = "/admin"
    guards = [auth_guard]

    @get(
        "/click-tracking",
        tags=[ADMIN_NAV_TAG],
        guards=[auth_guard, Permission("administrator")],
        opt={"label": "Click Tracking", "icon": "mouse-pointer", "order": 60},
    )
    async def click_tracking_list(
        self, request: Request, db_session: AsyncSession
    ) -> TemplateResponse:
        ctx = await get_admin_context(request, db_session)

        prefix = request.query_params.get("prefix", "").strip()

        query = select(TrackedLinkCounter)
        if prefix:
            query = query.where(TrackedLinkCounter.key.like(f"{prefix}%"))
        query = query.order_by(TrackedLinkCounter.count.desc())

        result = await db_session.execute(query)
        counters = list(result.scalars().all())

        total = len(counters)
        total_clicks = sum(c.count for c in counters)

        flash_messages = get_flash_messages(request)
        return TemplateResponse(
            "admin/click_tracking.html",
            context={
                "flash_messages": flash_messages,
                "counters": counters,
                "total": total,
                "total_clicks": total_clicks,
                "filters": {"prefix": prefix},
                **ctx,
            },
        )
