"""Admin controller for reviewing registered handlers.

A sidebar (guild dropdown + the channels in that guild that have member handlers,
plus an "Admin handlers" entry) and a content area showing the selected channel's
member handlers — or the guild's admin handlers — as collapsibles that reveal the
script. Admin-only; supports deleting either kind (cancelling any queued job).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from litestar import Controller, Request, get, post
from litestar.response import Redirect, Template as TemplateResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.admin.helpers import get_admin_context
from skrift.admin.navigation import ADMIN_NAV_TAG
from skrift.auth.guards import auth_guard, Permission
from skrift.flash import flash_error, flash_success, get_flash_messages

from smarter_dev.web.discord_admin_client import get_admin_discord_client
from smarter_dev.web.models import AdminHandler, ChannelHandler, HandlerRun

# How far back the per-handler error log reaches, and how many rows to keep per
# handler so a chronically-failing handler can't blow up the page.
ERROR_LOG_DAYS = 7
ERROR_LOG_PER_HANDLER = 50


def _group_error_runs(
    rows: list[HandlerRun], per_handler: int = ERROR_LOG_PER_HANDLER
) -> dict[str, list[dict]]:
    """Group non-ok runs (already newest-first) by handler id, keeping the most
    recent ``per_handler`` of each."""
    grouped: dict[str, list[dict]] = {}
    for run in rows:
        bucket = grouped.setdefault(str(run.handler_id), [])
        if len(bucket) < per_handler:
            bucket.append(
                {
                    "fired_at": run.fired_at,
                    "outcome": run.outcome,
                    "cap": run.cap,
                    "error": run.error,
                }
            )
    return grouped


async def _recent_error_log(
    db_session: AsyncSession, handler_ids: list[UUID]
) -> dict[str, list[dict]]:
    """Fetch each handler's non-ok runs from the last ``ERROR_LOG_DAYS`` days."""
    if not handler_ids:
        return {}
    since = datetime.now(timezone.utc) - timedelta(days=ERROR_LOG_DAYS)
    rows = list(
        (
            await db_session.execute(
                select(HandlerRun)
                .where(
                    HandlerRun.handler_id.in_(handler_ids),
                    HandlerRun.fired_at >= since,
                    HandlerRun.outcome != "ok",
                )
                .order_by(HandlerRun.fired_at.desc())
                .limit(1000)
            )
        ).scalars().all()
    )
    return _group_error_runs(rows)


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
        admin: str | None = None,
    ) -> TemplateResponse:
        ctx = await get_admin_context(request, db_session)
        view = "admin" if admin else "channels"

        # Guilds the bot is in, by name. Fall back to the guild ids that have
        # handlers (shown as ids) if the Discord API is unavailable.
        client = get_admin_discord_client()
        try:
            bot_guilds = await client.list_bot_guilds()
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
        admin_handlers: list[dict] = []

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

            # Admin handlers are per-guild (scoped to all channels or a subset).
            admin_rows = list(
                (
                    await db_session.execute(
                        select(AdminHandler)
                        .where(AdminHandler.guild_id == selected_guild_id)
                        .order_by(AdminHandler.trigger_type, AdminHandler.created_at)
                    )
                ).scalars().all()
            )

            def _scope_label(ids: list) -> str:
                if not ids:
                    return "All channels"
                return ", ".join("#" + name_map.get(c, c) for c in ids)

            admin_handlers = [
                {
                    "id": str(r.id),
                    "trigger_type": r.trigger_type,
                    "description": r.description,
                    "enabled": r.enabled,
                    "settings": r.settings or {},
                    "script": r.script,
                    "scope": _scope_label(list(r.channel_ids or [])),
                    "memory": r.memory or {},
                }
                for r in admin_rows
            ]

            # Channel (member-handler) selection only matters in the channels view.
            if view == "channels":
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

        # Last-week error log for every handler currently on screen (channel
        # member handlers + the guild's admin handlers).
        error_log = await _recent_error_log(
            db_session,
            [h.id for h in handlers] + [r.id for r in admin_rows]
            if selected_guild_id
            else [],
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
                "admin_handlers": admin_handlers,
                "admin_count": len(admin_handlers),
                "view": view,
                "error_log": error_log,
                "error_log_days": ERROR_LOG_DAYS,
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

    @post(
        "/handlers/admin/{handler_id:uuid}/delete",
        guards=[auth_guard, Permission("administrator")],
    )
    async def delete_admin_handler(
        self, request: Request, db_session: AsyncSession, handler_id: UUID
    ) -> Redirect:
        record = await db_session.get(AdminHandler, handler_id)
        if record is None:
            flash_error(request, "Admin handler not found.")
            return Redirect(path="/admin/handlers")

        guild_id = record.guild_id
        scheduled_job_id = record.scheduled_job_id
        await db_session.delete(record)
        await db_session.commit()

        if scheduled_job_id:
            try:
                from skrift.workers import get_handle

                await get_handle(scheduled_job_id).cancel()
            except Exception:  # noqa: BLE001 — best-effort; the chain self-stops
                pass

        flash_success(request, "Admin handler deleted.")
        return Redirect(path=f"/admin/handlers?guild_id={guild_id}&admin=1")

    @post(
        "/handlers/{handler_id:uuid}/clear-memory",
        guards=[auth_guard, Permission("administrator")],
    )
    async def clear_handler_memory(
        self, request: Request, db_session: AsyncSession, handler_id: UUID
    ) -> Redirect:
        record = await db_session.get(ChannelHandler, handler_id)
        if record is None:
            flash_error(request, "Handler not found.")
            return Redirect(path="/admin/handlers")
        record.memory = {}
        await db_session.commit()
        flash_success(request, "Handler memory cleared.")
        return Redirect(
            path=f"/admin/handlers?guild_id={record.guild_id}"
            f"&channel_id={record.channel_id}"
        )

    @post(
        "/handlers/admin/{handler_id:uuid}/clear-memory",
        guards=[auth_guard, Permission("administrator")],
    )
    async def clear_admin_handler_memory(
        self, request: Request, db_session: AsyncSession, handler_id: UUID
    ) -> Redirect:
        record = await db_session.get(AdminHandler, handler_id)
        if record is None:
            flash_error(request, "Admin handler not found.")
            return Redirect(path="/admin/handlers")
        record.memory = {}
        await db_session.commit()
        flash_success(request, "Admin handler memory cleared.")
        return Redirect(path=f"/admin/handlers?guild_id={record.guild_id}&admin=1")
