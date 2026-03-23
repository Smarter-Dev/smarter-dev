"""Moderation actions admin controller for the Skrift admin panel.

Provides a view of all moderation actions per guild and per user.
"""

from __future__ import annotations

from litestar import Controller, Request, get
from litestar.response import Template as TemplateResponse
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.admin.helpers import get_admin_context
from skrift.auth.guards import auth_guard, Permission

from smarter_dev.web.crud import ModerationActionOperations

mod_action_ops = ModerationActionOperations()


class ModActionsAdminController(Controller):
    """Moderation action log in the Skrift admin panel."""

    path = "/admin"
    guards = [auth_guard]

    @get(
        "/bot/mod-actions/{guild_id:str}",
        guards=[auth_guard, Permission("administrator")],
    )
    async def mod_actions_list(
        self, request: Request, db_session: AsyncSession, guild_id: str
    ) -> TemplateResponse:
        """View all moderation actions for a guild with filtering."""
        ctx = await get_admin_context(request, db_session)

        filter_type = request.query_params.get("action_type", "")
        filter_user = request.query_params.get("user_id", "").strip()

        if filter_user:
            actions = await mod_action_ops.get_actions_for_user(
                db_session, guild_id, filter_user, limit=100
            )
        else:
            actions = await mod_action_ops.get_actions_for_guild(
                db_session, guild_id, action_type=filter_type or None, limit=100
            )

        return TemplateResponse(
            "admin/bot/mod_actions.html",
            context={
                "actions": actions,
                "guild_id": guild_id,
                "filter_type": filter_type,
                "filter_user": filter_user,
                "active_page": "mod_actions",
                **ctx,
            },
        )

    @get(
        "/bot/mod-actions/{guild_id:str}/user/{user_id:str}",
        guards=[auth_guard, Permission("administrator")],
    )
    async def user_mod_history(
        self, request: Request, db_session: AsyncSession, guild_id: str, user_id: str
    ) -> TemplateResponse:
        """View moderation history for a specific user."""
        ctx = await get_admin_context(request, db_session)

        actions = await mod_action_ops.get_actions_for_user(
            db_session, guild_id, user_id, limit=100
        )

        return TemplateResponse(
            "admin/bot/user_history.html",
            context={
                "actions": actions,
                "guild_id": guild_id,
                "user_id": user_id,
                "active_page": "mod_actions",
                **ctx,
            },
        )
