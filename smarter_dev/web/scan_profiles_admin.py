"""Scan user profiles admin controller for the Skrift admin panel."""

from __future__ import annotations

from litestar import Controller, Request, get
from litestar.response import Template as TemplateResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.admin.helpers import get_admin_context
from skrift.admin.navigation import ADMIN_NAV_TAG
from skrift.auth.guards import auth_guard, Permission

from smarter_dev.web.models import ScanUserProfile


class ScanProfilesAdminController(Controller):
    """Scan user profiles in the Skrift admin panel."""

    path = "/admin"
    guards = [auth_guard]

    @get(
        "/scan-profiles",
        tags=[ADMIN_NAV_TAG],
        guards=[auth_guard, Permission("administrator")],
        opt={"label": "Scan Profiles", "icon": "users", "order": 55},
    )
    async def scan_profiles_list(
        self, request: Request, db_session: AsyncSession
    ) -> TemplateResponse:
        """List Scan user profiles."""
        ctx = await get_admin_context(request, db_session)

        result = await db_session.execute(
            select(ScanUserProfile).order_by(ScanUserProfile.updated_at.desc())
        )
        profiles = list(result.scalars().all())

        return TemplateResponse(
            "admin/scan_profiles.html",
            context={
                "profiles": profiles,
                "total": len(profiles),
                **ctx,
            },
        )
