"""Skrift admin: blogging-agent candidate-topics dashboard.

Stage 1 of the blogging-agent pipeline. Surfaces the candidate blog topics
that the Discord chat agent has filed during conversations, lets an operator
walk the queue and mark each topic ``kept`` / ``drafted`` / ``discarded``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from litestar import Controller, Request, get, post
from litestar.enums import RequestEncodingType
from litestar.exceptions import NotFoundException
from litestar.params import Body, Parameter
from litestar.response import Redirect
from litestar.response import Template as TemplateResponse
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.admin.helpers import get_admin_context
from skrift.admin.navigation import ADMIN_NAV_TAG
from skrift.auth.guards import Permission, auth_guard
from skrift.db.models.user import User
from skrift.lib.flash import flash_error, flash_success, get_flash_messages

from smarter_dev.web.models import (
    CandidateBlogTopic,
    ChatAgentEngagement,
)

logger = logging.getLogger(__name__)

VALID_STATUSES: frozenset[str] = frozenset(
    {"new", "kept", "drafted", "discarded"}
)
VALID_CATEGORIES: frozenset[str] = frozenset(
    {"concept", "misconception", "news"}
)


class BloggingAgentAdminController(Controller):
    """Operator dashboard for blog topic candidates surfaced by agents."""

    path = "/admin/blogging-agent"
    guards = [auth_guard]

    @get(
        "/topics",
        tags=[ADMIN_NAV_TAG],
        guards=[auth_guard, Permission("manage-bot")],
        opt={
            "label": "Blogging Agent",
            "icon": "edit-circle",
            "order": 66,
        },
    )
    async def list_topics(
        self,
        request: Request,
        db_session: AsyncSession,
        status: Annotated[str | None, Parameter(query="status")] = None,
        category: Annotated[str | None, Parameter(query="category")] = None,
        page: Annotated[int, Parameter(query="page")] = 1,
    ) -> TemplateResponse:
        ctx = await get_admin_context(request, db_session)
        page_size = 25
        page = max(1, page)

        summary_stmt = (
            select(CandidateBlogTopic.status, func.count(CandidateBlogTopic.id))
            .group_by(CandidateBlogTopic.status)
        )
        status_counts_rows = (await db_session.execute(summary_stmt)).all()
        status_counts: dict[str, int] = {
            s: 0 for s in ("new", "kept", "drafted", "discarded")
        }
        for s, c in status_counts_rows:
            status_counts[s] = int(c)

        stmt = (
            select(CandidateBlogTopic, ChatAgentEngagement)
            .outerjoin(
                ChatAgentEngagement,
                ChatAgentEngagement.id == CandidateBlogTopic.engagement_id,
            )
            .order_by(desc(CandidateBlogTopic.surfaced_at))
        )
        if status and status in VALID_STATUSES:
            stmt = stmt.where(CandidateBlogTopic.status == status)
        if category and category in VALID_CATEGORIES:
            stmt = stmt.where(CandidateBlogTopic.category == category)

        offset = (page - 1) * page_size
        stmt = stmt.limit(page_size).offset(offset)
        rows = (await db_session.execute(stmt)).all()
        topics = [
            {"topic": topic, "engagement": engagement}
            for topic, engagement in rows
        ]

        count_stmt = select(func.count(CandidateBlogTopic.id))
        if status and status in VALID_STATUSES:
            count_stmt = count_stmt.where(CandidateBlogTopic.status == status)
        if category and category in VALID_CATEGORIES:
            count_stmt = count_stmt.where(CandidateBlogTopic.category == category)
        total = (await db_session.execute(count_stmt)).scalar() or 0
        total_pages = max(1, (total + page_size - 1) // page_size)

        return TemplateResponse(
            "admin/blogging-agent/list.html",
            context={
                "topics": topics,
                "selected_status": status or "",
                "selected_category": category or "",
                "status_counts": status_counts,
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": total_pages,
                "valid_statuses": sorted(VALID_STATUSES),
                "valid_categories": sorted(VALID_CATEGORIES),
                "flash_messages": get_flash_messages(request),
                **ctx,
            },
        )

    @get(
        "/topics/{topic_id:uuid}",
        guards=[auth_guard, Permission("manage-bot")],
    )
    async def topic_detail(
        self,
        request: Request,
        db_session: AsyncSession,
        topic_id: UUID,
    ) -> TemplateResponse:
        ctx = await get_admin_context(request, db_session)

        stmt = (
            select(CandidateBlogTopic, ChatAgentEngagement, User)
            .outerjoin(
                ChatAgentEngagement,
                ChatAgentEngagement.id == CandidateBlogTopic.engagement_id,
            )
            .outerjoin(
                User,
                User.id == CandidateBlogTopic.reviewed_by_user_id,
            )
            .where(CandidateBlogTopic.id == topic_id)
        )
        row = (await db_session.execute(stmt)).first()
        if row is None:
            raise NotFoundException(detail="Topic not found")
        topic, engagement, reviewer = row

        return TemplateResponse(
            "admin/blogging-agent/detail.html",
            context={
                "topic": topic,
                "engagement": engagement,
                "reviewer": reviewer,
                "valid_statuses": sorted(VALID_STATUSES),
                "flash_messages": get_flash_messages(request),
                **ctx,
            },
        )

    @post(
        "/topics/{topic_id:uuid}/status",
        guards=[auth_guard, Permission("manage-bot")],
    )
    async def topic_set_status(
        self,
        request: Request,
        db_session: AsyncSession,
        topic_id: UUID,
        data: Annotated[
            dict, Body(media_type=RequestEncodingType.URL_ENCODED)
        ],
    ) -> Redirect:
        new_status = (data.get("status") or "").strip()
        if new_status not in VALID_STATUSES:
            flash_error(request, f"Unknown status: {new_status!r}.")
            return Redirect(path=f"/admin/blogging-agent/topics/{topic_id}")

        topic = await db_session.get(CandidateBlogTopic, topic_id)
        if topic is None:
            raise NotFoundException(detail="Topic not found")

        ctx = await get_admin_context(request, db_session)
        reviewer = ctx["user"]
        topic.status = new_status
        # Going BACK to 'new' clears the review stamp; any other status records
        # who acted and when.
        if new_status == "new":
            topic.reviewed_at = None
            topic.reviewed_by_user_id = None
        else:
            topic.reviewed_at = datetime.now(timezone.utc)
            topic.reviewed_by_user_id = reviewer.id

        await db_session.commit()
        flash_success(request, f"Topic marked {new_status}.")
        return Redirect(path=f"/admin/blogging-agent/topics/{topic_id}")
