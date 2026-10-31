"""Bot admin controller for the Skrift admin panel.

Provides the bot overview: the guilds with a moderation policy, each linked to
its Moderation page. The old per-guild AI moderation (triage) form and its
``moderation_configs`` table were dropped in #242: triage is the moderation
policy's ``triage`` section. The old form's URL redirects to the Moderation
page.
"""

from __future__ import annotations

from litestar import Controller, Request, get
from litestar.response import Redirect, Template as TemplateResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.admin.helpers import get_admin_context
from skrift.admin.navigation import ADMIN_NAV_TAG
from skrift.auth.guards import auth_guard, Permission

from smarter_dev.web.models import ModerationPolicy


class BotAdminController(Controller):
    """Bot admin panel — the guilds with a moderation policy."""

    path = "/admin"
    guards = [auth_guard]

    @get(
        "/bot",
        tags=[ADMIN_NAV_TAG],
        guards=[auth_guard, Permission("administrator")],
        opt={"label": "Bot", "icon": "cpu", "order": 60},
    )
    async def bot_overview(
        self, request: Request, db_session: AsyncSession
    ) -> TemplateResponse:
        """Bot admin landing page — lists the guilds with a moderation policy."""
        ctx = await get_admin_context(request, db_session)

        result = await db_session.execute(
            select(ModerationPolicy).order_by(ModerationPolicy.guild_id)
        )
        policies = list(result.scalars().all())

        return TemplateResponse(
            "admin/bot/overview.html",
            context={
                "policies": policies,
                "active_page": "overview",
                "guild_id": None,
                **ctx,
            },
        )

    @get(
        "/bot/moderation/{guild_id:str}",
        guards=[auth_guard, Permission("administrator")],
    )
    async def moderation_config(self, guild_id: str) -> Redirect:
        """The old AI moderation form: triage is on the Moderation page."""
        return Redirect(path=f"/admin/bot/guilds/{guild_id}/moderation")
