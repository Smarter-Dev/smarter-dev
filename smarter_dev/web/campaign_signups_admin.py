"""Campaign signups admin controller for the Skrift admin panel."""

from __future__ import annotations

from litestar import Controller, Request, get
from litestar.response import Template as TemplateResponse
from sqlalchemy import select, distinct
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.admin.helpers import get_admin_context
from skrift.admin.navigation import ADMIN_NAV_TAG
from skrift.auth.guards import auth_guard, Permission
from skrift.lib.flash import get_flash_messages

from smarter_dev.web.models import CampaignSignup


class CampaignSignupsAdminController(Controller):
    """Campaign signups management in the Skrift admin panel."""

    path = "/admin"
    guards = [auth_guard]

    @get(
        "/campaign-signups",
        tags=[ADMIN_NAV_TAG],
        guards=[auth_guard, Permission("administrator")],
        opt={"label": "Campaign Signups", "icon": "mail", "order": 50},
    )
    async def campaign_signups_list(
        self, request: Request, db_session: AsyncSession
    ) -> TemplateResponse:
        """List campaign signups with filtering."""
        ctx = await get_admin_context(request, db_session)

        campaign = request.query_params.get("campaign", "")
        status = request.query_params.get("status", "")

        query = select(CampaignSignup)

        if campaign:
            query = query.where(CampaignSignup.campaign_slug == campaign)

        if status == "confirmed":
            query = query.where(CampaignSignup.email_confirmed.is_(True))
        elif status == "unconfirmed":
            query = query.where(CampaignSignup.email_confirmed.is_(False))

        query = query.order_by(CampaignSignup.created_at.desc())

        result = await db_session.execute(query)
        signups = list(result.scalars().all())

        slugs_result = await db_session.execute(
            select(distinct(CampaignSignup.campaign_slug)).order_by(
                CampaignSignup.campaign_slug
            )
        )
        campaign_slugs = list(slugs_result.scalars().all())

        total = len(signups)
        confirmed_count = sum(1 for s in signups if s.email_confirmed)

        flash_messages = get_flash_messages(request)
        return TemplateResponse(
            "admin/campaign_signups.html",
            context={
                "flash_messages": flash_messages,
                "signups": signups,
                "total": total,
                "confirmed_count": confirmed_count,
                "campaign_slugs": campaign_slugs,
                "filters": {
                    "campaign": campaign,
                    "status": status,
                },
                **ctx,
            },
        )
