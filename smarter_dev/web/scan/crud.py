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
        name: str | None = None,
        guild_id: str | None = None,
        channel_id: str | None = None,
        context: dict | None = None,
        pipeline_mode: str = "lite",
    ) -> ResearchSession:
        research = ResearchSession(
            query=query,
            name=name,
            user_id=user_id,
            guild_id=guild_id,
            channel_id=channel_id,
            status="running",
            context=context,
            pipeline_mode=pipeline_mode,
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
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        model_name: str | None = None,
        cost_usd: object = None,
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
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
                model_name=model_name,
                cost_usd=cost_usd,
            )
        )
        await session.commit()

    async def update_session_meta(
        self,
        session: AsyncSession,
        session_id: UUID,
        name: str,
        context: dict | None = None,
    ) -> None:
        values: dict = {"name": name}
        if context is not None:
            values["context"] = context
        await session.execute(
            update(ResearchSession)
            .where(ResearchSession.id == session_id)
            .values(**values)
        )
        await session.commit()

    async def merge_session_context(
        self,
        session: AsyncSession,
        session_id: UUID,
        extra: dict,
    ) -> None:
        """Merge extra keys into the session's existing context JSON."""
        research = await self.get_session(session, session_id)
        if research:
            ctx = dict(research.context or {})
            ctx.update(extra)
            await session.execute(
                update(ResearchSession)
                .where(ResearchSession.id == session_id)
                .values(context=ctx)
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

    async def add_usage(
        self,
        session: AsyncSession,
        session_id: UUID,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        cost_usd: object = None,
    ) -> None:
        """Atomically increment token counts and cost on a session."""
        research = await self.get_session(session, session_id)
        if not research:
            return
        values: dict = {
            "input_tokens": (research.input_tokens or 0) + input_tokens,
            "output_tokens": (research.output_tokens or 0) + output_tokens,
            "cache_read_tokens": (research.cache_read_tokens or 0) + cache_read_tokens,
            "cache_write_tokens": (research.cache_write_tokens or 0) + cache_write_tokens,
        }
        if cost_usd is not None:
            from decimal import Decimal
            current = research.cost_usd or Decimal(0)
            values["cost_usd"] = current + cost_usd
        await session.execute(
            update(ResearchSession)
            .where(ResearchSession.id == session_id)
            .values(**values)
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
