"""Database operations for research sessions."""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.models import ResearchSession

logger = logging.getLogger(__name__)


class ResearchSessionOperations:
    """CRUD operations for research sessions."""

    async def create_session(
        self,
        session: AsyncSession,
        query: str,
        user_id: str,
        guild_id: str | None = None,
        channel_id: str | None = None,
        context: dict | None = None,
    ) -> ResearchSession:
        research = ResearchSession(
            query=query,
            user_id=user_id,
            guild_id=guild_id,
            channel_id=channel_id,
            status="running",
            context=context,
        )
        session.add(research)
        await session.flush()
        return research

    async def get_session(
        self, session: AsyncSession, session_id: UUID
    ) -> ResearchSession | None:
        result = await session.execute(
            select(ResearchSession).where(ResearchSession.id == session_id)
        )
        return result.scalar_one_or_none()

    async def update_session_result(
        self,
        session: AsyncSession,
        session_id: UUID,
        response: str,
        summary: str,
        sources: list[dict],
        tool_log: list[dict] | None = None,
    ) -> None:
        await session.execute(
            update(ResearchSession)
            .where(ResearchSession.id == session_id)
            .values(
                status="complete",
                response=response,
                summary=summary,
                sources=sources,
                tool_log=tool_log,
            )
        )
        await session.commit()

    async def update_session_error(
        self,
        session: AsyncSession,
        session_id: UUID,
        error_message: str,
    ) -> None:
        await session.execute(
            update(ResearchSession)
            .where(ResearchSession.id == session_id)
            .values(status="error", error_message=error_message)
        )
        await session.commit()

    async def add_followup(
        self,
        session: AsyncSession,
        session_id: UUID,
        followup_id: UUID,
        query: str,
    ) -> None:
        research = await self.get_session(session, session_id)
        if research:
            followups = list(research.followups or [])
            followups.append({
                "id": str(followup_id),
                "query": query,
            })
            await session.execute(
                update(ResearchSession)
                .where(ResearchSession.id == session_id)
                .values(followups=followups)
            )
            await session.commit()

    async def append_tool_log(
        self,
        session: AsyncSession,
        session_id: UUID,
        entry: dict,
    ) -> None:
        research = await self.get_session(session, session_id)
        if research:
            tool_log = list(research.tool_log or [])
            tool_log.append(entry)
            await session.execute(
                update(ResearchSession)
                .where(ResearchSession.id == session_id)
                .values(tool_log=tool_log)
            )
            await session.commit()

    async def list_sessions(
        self,
        session: AsyncSession,
        user_id: str,
        limit: int = 20,
        offset: int = 0,
    ) -> list[ResearchSession]:
        result = await session.execute(
            select(ResearchSession)
            .where(ResearchSession.user_id == user_id)
            .order_by(ResearchSession.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())
