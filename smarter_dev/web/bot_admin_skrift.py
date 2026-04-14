"""Bot admin controller for the Skrift admin panel.

Provides guild overview and moderation configuration pages.
"""

from __future__ import annotations

from typing import Annotated

from litestar import Controller, Request, get, post
from litestar.datastructures import State
from litestar.enums import RequestEncodingType
from litestar.params import Body
from litestar.response import Redirect, Template as TemplateResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.admin.helpers import get_admin_context
from skrift.admin.navigation import ADMIN_NAV_TAG
from skrift.auth.guards import auth_guard, Permission

from smarter_dev.web.crud import ModerationConfigOperations
from smarter_dev.web.models import ModerationConfig

mod_config_ops = ModerationConfigOperations()


class BotAdminController(Controller):
    """Bot admin panel — guild overview and moderation configuration."""

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
        """Bot admin landing page — lists guilds with config status."""
        ctx = await get_admin_context(request, db_session)

        result = await db_session.execute(
            select(ModerationConfig).order_by(ModerationConfig.created_at.desc())
        )
        guilds = list(result.scalars().all())

        return TemplateResponse(
            "admin/bot/overview.html",
            context={
                "guilds": guilds,
                "active_page": "overview",
                "guild_id": None,
                **ctx,
            },
        )

    @post(
        "/bot/add-guild",
        guards=[auth_guard, Permission("administrator")],
    )
    async def add_guild(
        self,
        request: Request,
        db_session: AsyncSession,
        data: Annotated[dict, Body(media_type=RequestEncodingType.URL_ENCODED)],
    ) -> Redirect:
        """Add a guild to the moderation config."""
        guild_id = data.get("guild_id", "").strip()
        if not guild_id:
            return Redirect(path="/admin/bot")

        await mod_config_ops.get_or_create_config(db_session, guild_id)
        await db_session.commit()

        return Redirect(path=f"/admin/bot/moderation/{guild_id}")

    @get(
        "/bot/moderation/{guild_id:str}",
        guards=[auth_guard, Permission("administrator")],
    )
    async def moderation_config(
        self, request: Request, db_session: AsyncSession, guild_id: str
    ) -> TemplateResponse:
        """Moderation config page for a specific guild."""
        ctx = await get_admin_context(request, db_session)

        config = await mod_config_ops.get_config(db_session, guild_id)

        return TemplateResponse(
            "admin/bot/moderation_config.html",
            context={
                "config": config,
                "guild_id": guild_id,
                "active_page": "moderation",
                **ctx,
            },
        )

    @post(
        "/bot/moderation/{guild_id:str}",
        guards=[auth_guard, Permission("administrator")],
    )
    async def save_moderation_config(
        self,
        request: Request,
        db_session: AsyncSession,
        guild_id: str,
        data: Annotated[dict, Body(media_type=RequestEncodingType.URL_ENCODED)],
    ) -> Redirect:
        """Save moderation config for a guild."""
        # Parse form data
        is_active = data.get("is_active") == "true"
        instructions = data.get("instructions", "").strip()
        response_channel_id = data.get("response_channel_id", "").strip() or None

        # Parse context message limit
        try:
            context_message_limit = int(data.get("context_message_limit", 25))
            context_message_limit = max(5, min(50, context_message_limit))
        except (ValueError, TypeError):
            context_message_limit = 25

        # Parse monitored role IDs (newline-separated)
        role_ids_raw = data.get("monitored_role_ids", "")
        monitored_role_ids = [
            rid.strip()
            for rid in role_ids_raw.split("\n")
            if rid.strip()
        ]

        # Parse enabled tools (multiple checkboxes)
        # URL-encoded forms send multiple values as comma-separated or repeated keys
        enabled_tools_raw = data.get("enabled_tools", "")
        if isinstance(enabled_tools_raw, list):
            enabled_tools = enabled_tools_raw
        elif enabled_tools_raw:
            enabled_tools = [t.strip() for t in enabled_tools_raw.split(",") if t.strip()]
        else:
            enabled_tools = []

        # Filter to valid tool names
        valid_tools = {"timeout", "purge", "delete"}
        enabled_tools = [t for t in enabled_tools if t in valid_tools]

        await mod_config_ops.update_config(
            db_session,
            guild_id,
            is_active=is_active,
            instructions=instructions,
            monitored_role_ids=monitored_role_ids,
            enabled_tools=enabled_tools,
            response_channel_id=response_channel_id,
            context_message_limit=context_message_limit,
        )
        await db_session.commit()

        # Refresh the bot's in-memory cache
        try:
            from smarter_dev.bot.plugins.mod_monitor import refresh_config
            import asyncio
            asyncio.create_task(refresh_config(guild_id))
        except ImportError:
            pass  # Bot not running in this process

        return Redirect(path=f"/admin/bot/moderation/{guild_id}")
