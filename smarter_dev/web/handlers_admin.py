"""Admin controller for reviewing registered channel handlers.

A sidebar (guild dropdown + the channels in that guild that have handlers) and a
content area showing the selected channel's handlers as collapsibles that reveal
the script. Admin-only; supports deleting a handler (which also cancels any
queued scheduled job, mirroring the handler API).
"""

from __future__ import annotations

from uuid import UUID

from litestar import Controller, Request, get, post
from litestar.response import Redirect, Template as TemplateResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.admin.helpers import get_admin_context
from skrift.admin.navigation import ADMIN_NAV_TAG
from skrift.auth.guards import auth_guard, Permission
from skrift.flash import flash_error, flash_success, get_flash_messages

from smarter_dev.web.admin.discord import get_discord_client
from smarter_dev.web.models import ChannelHandler


class HandlersAdminController(Controller):
    """Review and delete registered channel handlers in the admin panel."""

    path = "/admin"
    guards = [auth_guard]

    @get(
        "/handlers",
        tags=[ADMIN_NAV_TAG],
        guards=[auth_guard, Permission("administrator")],
        opt={"label": "Handlers", "icon": "zap", "order": 65},
    )
    async def list_handlers(
        self,
        request: Request,
        db_session: AsyncSession,
        guild_id: str | None = None,
        channel_id: str | None = None,
    ) -> TemplateResponse:
        ctx = await get_admin_context(request, db_session)

        # Guilds the bot is in, by name. Fall back to the guild ids that have
        # handlers (shown as ids) if the Discord API is unavailable.
        client = get_discord_client()
        try:
            bot_guilds = await client.get_bot_guilds()
            guilds = [{"id": g.id, "name": g.name} for g in bot_guilds]
        except Exception:  # noqa: BLE001 — degrade gracefully without the API
            flash_error(request, "Couldn't reach Discord; showing guild ids only.")
            rows = (
                await db_session.execute(select(ChannelHandler.guild_id).distinct())
            ).scalars().all()
            guilds = [{"id": gid, "name": gid} for gid in rows]
        guilds.sort(key=lambda g: g["name"].lower())

        guild_ids = {g["id"] for g in guilds}
        selected_guild_id = (
            guild_id if guild_id in guild_ids else (guilds[0]["id"] if guilds else None)
        )

        channels: list[dict] = []
        handlers: list[ChannelHandler] = []
        selected_channel_id = None
        selected_channel_name = None

        if selected_guild_id:
            counts = dict(
                (
                    await db_session.execute(
                        select(ChannelHandler.channel_id, func.count())
                        .where(ChannelHandler.guild_id == selected_guild_id)
                        .group_by(ChannelHandler.channel_id)
                    )
                ).all()
            )
            name_map: dict[str, str] = {}
            try:
                for ch in await client.get_guild_channels(selected_guild_id):
                    name_map[ch.id] = ch.name
            except Exception:  # noqa: BLE001 — fall back to ids
                name_map = {}
            channels = sorted(
                (
                    {
                        "id": cid,
                        "name": name_map.get(cid, cid),
                        "count": count,
                    }
                    for cid, count in counts.items()
                ),
                key=lambda c: c["name"].lower(),
            )

            channel_ids = set(counts)
            if channel_id in channel_ids:
                selected_channel_id = channel_id
            elif channels:
                selected_channel_id = channels[0]["id"]

            if selected_channel_id:
                selected_channel_name = name_map.get(
                    selected_channel_id, selected_channel_id
                )
                handlers = list(
                    (
                        await db_session.execute(
                            select(ChannelHandler)
                            .where(
                                ChannelHandler.guild_id == selected_guild_id,
                                ChannelHandler.channel_id == selected_channel_id,
                            )
                            .order_by(
                                ChannelHandler.trigger_type,
                                ChannelHandler.created_at,
                            )
                        )
                    ).scalars().all()
                )

        return TemplateResponse(
            "admin/handlers/list.html",
            context={
                "guilds": guilds,
                "selected_guild_id": selected_guild_id,
                "channels": channels,
                "selected_channel_id": selected_channel_id,
                "selected_channel_name": selected_channel_name,
                "handlers": handlers,
                "flash_messages": get_flash_messages(request),
                **ctx,
            },
        )

    @post(
        "/handlers/{handler_id:uuid}/delete",
        guards=[auth_guard, Permission("administrator")],
    )
    async def delete_handler(
        self, request: Request, db_session: AsyncSession, handler_id: UUID
    ) -> Redirect:
        record = await db_session.get(ChannelHandler, handler_id)
        if record is None:
            flash_error(request, "Handler not found.")
            return Redirect(path="/admin/handlers")

        guild_id, channel_id = record.guild_id, record.channel_id
        scheduled_job_id = record.scheduled_job_id
        await db_session.delete(record)
        await db_session.commit()

        # Cancel any queued scheduled fire, mirroring the handler API's delete.
        if scheduled_job_id:
            try:
                from skrift.workers import get_handle

                await get_handle(scheduled_job_id).cancel()
            except Exception:  # noqa: BLE001 — best-effort; the chain self-stops
                pass

        flash_success(request, "Handler deleted.")
        return Redirect(
            path=f"/admin/handlers?guild_id={guild_id}&channel_id={channel_id}"
        )
