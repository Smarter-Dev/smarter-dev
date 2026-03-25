"""Quests admin controller for the Skrift admin panel."""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
from typing import Annotated
from uuid import UUID

from litestar import Controller, Request, get, post
from litestar.enums import RequestEncodingType
from litestar.exceptions import NotFoundException
from litestar.params import Body
from litestar.response import Redirect, Template as TemplateResponse
from sqlalchemy import distinct, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.admin.helpers import get_admin_context
from skrift.admin.navigation import ADMIN_NAV_TAG
from skrift.auth.guards import Permission, auth_guard
from skrift.lib.flash import flash_error, flash_success, get_flash_messages

from smarter_dev.shared.config import get_settings
from smarter_dev.web.models import DailyQuest, Quest


class QuestsAdminController(Controller):
    """Quest management in the Skrift admin panel."""

    path = "/admin"
    guards = [auth_guard]

    # ── List ─────────────────────────────────────────────────

    @get(
        "/quests",
        tags=[ADMIN_NAV_TAG],
        guards=[auth_guard, Permission("manage-quests")],
        opt={"label": "Quests", "icon": "medal", "order": 58},
    )
    async def quests_list(
        self, request: Request, db_session: AsyncSession
    ) -> TemplateResponse:
        ctx = await get_admin_context(request, db_session)

        # Guild filter
        guild_id = request.query_params.get("guild_id")

        # Available guilds for filter dropdown
        guild_ids_result = await db_session.execute(
            select(distinct(Quest.guild_id)).order_by(Quest.guild_id)
        )
        guild_ids = [row[0] for row in guild_ids_result.all()]

        # Quests with optional daily quest join
        stmt = (
            select(Quest, DailyQuest)
            .outerjoin(
                DailyQuest,
                (DailyQuest.quest_id == Quest.id)
                & (DailyQuest.guild_id == Quest.guild_id),
            )
            .order_by(
                DailyQuest.active_date.desc().nullslast(),
                Quest.created_at.desc(),
            )
        )
        if guild_id:
            stmt = stmt.where(Quest.guild_id == guild_id)

        rows = (await db_session.execute(stmt)).all()
        quests = [
            {"quest": quest, "daily_quest": daily_quest}
            for quest, daily_quest in rows
        ]

        return TemplateResponse(
            "admin/quests/list.html",
            context={
                "quests": quests,
                "total": len(quests),
                "guild_ids": guild_ids,
                "selected_guild_id": guild_id or "",
                "today": date.today(),
                "flash_messages": get_flash_messages(request),
                **ctx,
            },
        )

    # ── Create ───────────────────────────────────────────────

    @get(
        "/quests/create",
        guards=[auth_guard, Permission("manage-quests")],
    )
    async def quest_create_form(
        self, request: Request, db_session: AsyncSession
    ) -> TemplateResponse:
        ctx = await get_admin_context(request, db_session)

        guild_ids_result = await db_session.execute(
            select(distinct(Quest.guild_id)).order_by(Quest.guild_id)
        )
        guild_ids = [row[0] for row in guild_ids_result.all()]

        return TemplateResponse(
            "admin/quests/create.html",
            context={
                "guild_ids": guild_ids,
                "flash_messages": get_flash_messages(request),
                **ctx,
            },
        )

    @post(
        "/quests/create",
        guards=[auth_guard, Permission("manage-quests")],
    )
    async def quest_create(
        self, request: Request, db_session: AsyncSession
    ) -> Redirect:
        form = await request.form()

        guild_id = (form.get("guild_id") or "").strip()
        title = (form.get("title") or "").strip()
        prompt = (form.get("prompt") or "").strip()
        quest_type = form.get("quest_type") or "daily"

        if not guild_id or not title or not prompt:
            flash_error(request, "Guild, title, and prompt are required.")
            return Redirect(path="/admin/quests/create")

        # Read optional script file uploads
        input_generator_script = await _read_upload(form.get("input_generator_script"))
        solution_validator_script = await _read_upload(form.get("solution_validator_script"))
        python_script = await _read_upload(form.get("python_script"))

        quest = Quest(
            guild_id=guild_id,
            title=title,
            prompt=prompt,
            quest_type=quest_type,
            input_generator_script=input_generator_script,
            solution_validator_script=solution_validator_script,
            python_script=python_script,
        )
        db_session.add(quest)
        await db_session.commit()

        flash_success(request, "Quest created.")
        return Redirect(path="/admin/quests")

    # ── Edit ─────────────────────────────────────────────────

    @get(
        "/quests/{quest_id:uuid}/edit",
        guards=[auth_guard, Permission("manage-quests")],
    )
    async def quest_edit_form(
        self, request: Request, db_session: AsyncSession, quest_id: UUID
    ) -> TemplateResponse:
        ctx = await get_admin_context(request, db_session)

        quest = await db_session.get(Quest, quest_id)
        if not quest:
            raise NotFoundException("Quest not found")

        # Latest daily quest for this quest
        result = await db_session.execute(
            select(DailyQuest)
            .where(
                DailyQuest.guild_id == quest.guild_id,
                DailyQuest.quest_id == quest.id,
            )
            .order_by(DailyQuest.active_date.desc())
            .limit(1)
        )
        daily_quest = result.scalar_one_or_none()

        return TemplateResponse(
            "admin/quests/edit.html",
            context={
                "quest": quest,
                "daily_quest": daily_quest,
                "flash_messages": get_flash_messages(request),
                **ctx,
            },
        )

    @post(
        "/quests/{quest_id:uuid}/edit",
        guards=[auth_guard, Permission("manage-quests")],
    )
    async def quest_edit(
        self,
        request: Request,
        db_session: AsyncSession,
        quest_id: UUID,
        data: Annotated[dict, Body(media_type=RequestEncodingType.URL_ENCODED)],
    ) -> Redirect:
        quest = await db_session.get(Quest, quest_id)
        if not quest:
            flash_error(request, "Quest not found.")
            return Redirect(path="/admin/quests")

        title = (data.get("title") or "").strip()
        prompt = (data.get("prompt") or "").strip()

        if title:
            quest.title = title
        if prompt:
            quest.prompt = prompt

        quest.quest_type = data.get("quest_type", quest.quest_type)

        # Toggle daily quest active state
        result = await db_session.execute(
            select(DailyQuest)
            .where(
                DailyQuest.guild_id == quest.guild_id,
                DailyQuest.quest_id == quest.id,
            )
            .order_by(DailyQuest.active_date.desc())
            .limit(1)
        )
        daily_quest = result.scalar_one_or_none()
        if daily_quest:
            daily_quest.is_active = data.get("is_active") == "1"

        await db_session.commit()

        flash_success(request, "Quest updated.")
        return Redirect(path="/admin/quests")

    # ── Delete ───────────────────────────────────────────────

    @post(
        "/quests/{quest_id:uuid}/delete",
        guards=[auth_guard, Permission("manage-quests")],
    )
    async def quest_delete(
        self, request: Request, db_session: AsyncSession, quest_id: UUID
    ) -> Redirect:
        quest = await db_session.get(Quest, quest_id)
        if not quest:
            flash_error(request, "Quest not found.")
            return Redirect(path="/admin/quests")

        await db_session.delete(quest)
        await db_session.commit()

        flash_success(request, "Quest deleted.")
        return Redirect(path="/admin/quests")

    # ── Schedule ─────────────────────────────────────────────

    @post(
        "/quests/{quest_id:uuid}/schedule",
        guards=[auth_guard, Permission("manage-quests")],
    )
    async def quest_schedule(
        self,
        request: Request,
        db_session: AsyncSession,
        quest_id: UUID,
        data: Annotated[dict, Body(media_type=RequestEncodingType.URL_ENCODED)],
    ) -> Redirect:
        quest = await db_session.get(Quest, quest_id)
        if not quest:
            flash_error(request, "Quest not found.")
            return Redirect(path="/admin/quests")

        raw_date = (data.get("active_date") or "").strip()
        if not raw_date:
            flash_error(request, "Active date is required.")
            return Redirect(path=f"/admin/quests/{quest_id}/edit")

        active_date = date.fromisoformat(raw_date)
        tz = ZoneInfo(get_settings().quest_timezone)
        expires_at = datetime.combine(
            active_date, datetime.max.time(), tzinfo=tz
        ).astimezone(timezone.utc)

        # Check for conflict with another quest on the same date
        conflict = await db_session.execute(
            select(DailyQuest)
            .where(
                DailyQuest.guild_id == quest.guild_id,
                DailyQuest.active_date == active_date,
                DailyQuest.quest_id != quest_id,
            )
            .limit(1)
        )
        if conflict.scalar_one_or_none():
            flash_error(request, "Another quest is already scheduled for this date.")
            return Redirect(path=f"/admin/quests/{quest_id}/edit")

        stmt = (
            pg_insert(DailyQuest)
            .values(
                guild_id=quest.guild_id,
                quest_id=quest_id,
                active_date=active_date,
                expires_at=expires_at,
                is_active=True,
            )
            .on_conflict_do_update(
                index_elements=["guild_id", "quest_id", "active_date"],
                set_={
                    "expires_at": expires_at,
                    "is_active": True,
                },
            )
        )

        await db_session.execute(stmt)
        await db_session.commit()

        flash_success(request, "Quest scheduled.")
        return Redirect(path="/admin/quests")


async def _read_upload(field) -> str | None:
    """Read an uploaded file field as UTF-8 text, or return None."""
    if field and hasattr(field, "read") and getattr(field, "filename", None):
        return (await field.read()).decode("utf-8")
    return None
