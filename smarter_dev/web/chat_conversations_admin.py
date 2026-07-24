"""Skrift admin: chat-agent conversations dashboard.

List and detail pages for engagements persisted by the bot's chat agent.
Plus a voice-replay endpoint that re-synthesises a turn's voice_summary
on demand for browser playback.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated
from uuid import UUID

from litestar import Controller, Request, get
from litestar.exceptions import NotFoundException
from litestar.params import Parameter
from litestar.response import Response
from litestar.response import Template as TemplateResponse
from sqlalchemy import and_, case, desc, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from skrift.admin.helpers import get_admin_context
from skrift.admin.navigation import ADMIN_NAV_TAG
from skrift.auth.guards import Permission, auth_guard
from skrift.flash import get_flash_messages

# Note: ``smarter_dev.bot.services.discord_voice`` (used in the
# voice-replay handler) pulls in ``google.genai`` + ``aiohttp`` which are
# heavy and used to push the web container over its 384Mi startup limit.
# Imported lazily inside ``voice_replay`` so the controller stays
# featherweight at module load.
from smarter_dev.shared.config import get_settings
from smarter_dev.web.models import (
    ChatAgentCompactionEvent,
    ChatAgentEngagement,
    ChatAgentError,
    ChatAgentTurn,
)

logger = logging.getLogger(__name__)


class ChatConversationsAdminController(Controller):
    """Operator dashboard for Discord chat-agent activity."""

    path = "/admin"
    guards = [auth_guard]

    @get(
        "/chat-conversations",
        tags=[ADMIN_NAV_TAG],
        guards=[auth_guard, Permission("manage-bot")],
        opt={
            "label": "Chat Conversations",
            "icon": "message-circle",
            "order": 65,
        },
    )
    async def list_engagements(
        self,
        request: Request,
        db_session: AsyncSession,
        guild_id: Annotated[str | None, Parameter(query="guild_id")] = None,
        channel_id: Annotated[str | None, Parameter(query="channel_id")] = None,
        user_id: Annotated[str | None, Parameter(query="user_id")] = None,
        page: Annotated[int, Parameter(query="page")] = 1,
    ) -> TemplateResponse:
        ctx = await get_admin_context(request, db_session)
        page_size = 25
        metrics = await _rolling_metrics(db_session, days=28)

        # Filter dropdowns
        guild_rows = await db_session.execute(
            select(distinct(ChatAgentEngagement.guild_id)).order_by(
                ChatAgentEngagement.guild_id
            )
        )
        guild_ids = [r[0] for r in guild_rows.all()]

        stmt = (
            select(ChatAgentEngagement, func.count(ChatAgentTurn.id))
            .outerjoin(ChatAgentTurn, ChatAgentTurn.engagement_id == ChatAgentEngagement.id)
            .group_by(ChatAgentEngagement.id)
            .order_by(desc(ChatAgentEngagement.started_at))
        )
        if guild_id:
            stmt = stmt.where(ChatAgentEngagement.guild_id == guild_id)
        if channel_id:
            stmt = stmt.where(ChatAgentEngagement.channel_id == channel_id)
        if user_id:
            stmt = stmt.where(ChatAgentEngagement.activation_user_id == user_id)

        # Pagination
        offset = max(0, (page - 1) * page_size)
        stmt = stmt.limit(page_size).offset(offset)

        rows = (await db_session.execute(stmt)).all()
        engagements = [
            {"engagement": eng, "turn_count": turn_count}
            for eng, turn_count in rows
        ]

        # Total count for pagination
        count_stmt = select(func.count(ChatAgentEngagement.id))
        if guild_id:
            count_stmt = count_stmt.where(ChatAgentEngagement.guild_id == guild_id)
        if channel_id:
            count_stmt = count_stmt.where(ChatAgentEngagement.channel_id == channel_id)
        if user_id:
            count_stmt = count_stmt.where(ChatAgentEngagement.activation_user_id == user_id)
        total = (await db_session.execute(count_stmt)).scalar() or 0

        return TemplateResponse(
            "admin/chat-conversations/list.html",
            context={
                "engagements": engagements,
                "guild_ids": guild_ids,
                "selected_guild_id": guild_id or "",
                "selected_channel_id": channel_id or "",
                "selected_user_id": user_id or "",
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": max(1, (total + page_size - 1) // page_size),
                "metrics": metrics,
                "flash_messages": get_flash_messages(request),
                **ctx,
            },
        )

    @get(
        "/chat-conversations/{engagement_id:uuid}",
        guards=[auth_guard, Permission("manage-bot")],
    )
    async def engagement_detail(
        self,
        request: Request,
        db_session: AsyncSession,
        engagement_id: UUID,
    ) -> TemplateResponse:
        ctx = await get_admin_context(request, db_session)
        stmt = (
            select(ChatAgentEngagement)
            .where(ChatAgentEngagement.id == engagement_id)
            .options(
                selectinload(ChatAgentEngagement.turns).selectinload(
                    ChatAgentTurn.compaction_events
                )
            )
        )
        engagement = (await db_session.execute(stmt)).scalar_one_or_none()
        if engagement is None:
            raise NotFoundException(detail="Engagement not found")

        return TemplateResponse(
            "admin/chat-conversations/detail.html",
            context={
                "engagement": engagement,
                "turns": engagement.turns,
                "flash_messages": get_flash_messages(request),
                **ctx,
            },
        )

    @get(
        "/chat-errors",
        tags=[ADMIN_NAV_TAG],
        guards=[auth_guard, Permission("manage-bot")],
        opt={
            "label": "Chat Errors",
            "icon": "alert-triangle",
            "order": 66,
        },
    )
    async def list_errors(
        self,
        request: Request,
        db_session: AsyncSession,
        guild_id: Annotated[str | None, Parameter(query="guild_id")] = None,
        model_name: Annotated[str | None, Parameter(query="model_name")] = None,
        page: Annotated[int, Parameter(query="page")] = 1,
    ) -> TemplateResponse:
        """Paginated operator log of failed Discord chat-agent runs."""
        ctx = await get_admin_context(request, db_session)
        page_size = 50
        filters = []
        if guild_id:
            filters.append(ChatAgentError.guild_id == guild_id)
        if model_name:
            filters.append(ChatAgentError.model_name == model_name)

        stmt = select(ChatAgentError).order_by(
            ChatAgentError.occurred_at.desc()
        )
        count_stmt = select(func.count(ChatAgentError.id))
        if filters:
            stmt = stmt.where(*filters)
            count_stmt = count_stmt.where(*filters)
        offset = max(0, (page - 1) * page_size)
        errors = (
            await db_session.execute(stmt.limit(page_size).offset(offset))
        ).scalars().all()
        total = int((await db_session.execute(count_stmt)).scalar() or 0)

        guild_rows = await db_session.execute(
            select(distinct(ChatAgentError.guild_id)).order_by(
                ChatAgentError.guild_id
            )
        )
        model_rows = await db_session.execute(
            select(distinct(ChatAgentError.model_name))
            .where(ChatAgentError.model_name.is_not(None))
            .order_by(ChatAgentError.model_name)
        )
        return TemplateResponse(
            "admin/chat-errors/list.html",
            context={
                "errors": errors,
                "guild_ids": [row[0] for row in guild_rows.all()],
                "model_names": [row[0] for row in model_rows.all()],
                "selected_guild_id": guild_id or "",
                "selected_model_name": model_name or "",
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": max(1, (total + page_size - 1) // page_size),
                "flash_messages": get_flash_messages(request),
                **ctx,
            },
        )

    @get(
        "/chat-errors/{error_id:uuid}",
        guards=[auth_guard, Permission("manage-bot")],
    )
    async def error_detail(
        self,
        request: Request,
        db_session: AsyncSession,
        error_id: UUID,
    ) -> TemplateResponse:
        """Full exception and run context for one protected diagnostic link."""
        ctx = await get_admin_context(request, db_session)
        error = await db_session.get(ChatAgentError, error_id)
        if error is None:
            raise NotFoundException(detail="Chat error not found")
        return TemplateResponse(
            "admin/chat-errors/detail.html",
            context={
                "error": error,
                "flash_messages": get_flash_messages(request),
                **ctx,
            },
        )

    @get(
        "/chat-conversations/turns/{turn_id:uuid}/voice",
        guards=[auth_guard, Permission("manage-bot")],
    )
    async def voice_replay(
        self,
        request: Request,
        db_session: AsyncSession,
        turn_id: UUID,
    ) -> Response:
        """Re-synthesise a turn's voice_summary and stream OGG back to the
        browser. Lets operators hear what a voice reply sounded like without
        needing the original Discord upload."""
        # Lazy import: pulls in google.genai + aiohttp, which the website
        # otherwise doesn't need and were tipping the pod over OOM at boot.
        from smarter_dev.bot.services.discord_voice import (
            clean_transcript_for_tts,
            convert_wav_to_opus_ogg,
            generate_tts,
            write_wave_file,
        )

        stmt = select(ChatAgentTurn).where(ChatAgentTurn.id == turn_id)
        turn = (await db_session.execute(stmt)).scalar_one_or_none()
        if turn is None:
            raise NotFoundException(detail="Turn not found")

        agent_output = turn.agent_output or {}
        voice_summary = (agent_output.get("voice_summary") or "").strip()
        if not voice_summary:
            raise NotFoundException(detail="Turn has no voice_summary")

        voice_instruction = agent_output.get("voice_instruction")

        settings = get_settings()
        text = clean_transcript_for_tts(voice_summary)[
            : settings.voice_max_input_chars
        ]
        try:
            tts_result = await asyncio.to_thread(
                generate_tts,
                text,
                settings.voice_tts_model,
                settings.voice_tts_voice,
                voice_instruction,
            )
        except Exception:
            logger.exception("Voice replay TTS generation failed for turn %s", turn_id)
            raise NotFoundException(detail="Voice synthesis failed")

        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "voice.wav"
            ogg = Path(tmp) / "voice.ogg"
            await asyncio.to_thread(
                write_wave_file,
                wav,
                tts_result.pcm,
                settings.voice_tts_sample_rate,
                settings.voice_tts_channels,
                settings.voice_tts_sample_width,
            )
            await convert_wav_to_opus_ogg(
                wav, ogg, settings.voice_opus_bitrate
            )
            audio_bytes = ogg.read_bytes()

        return Response(
            content=audio_bytes,
            media_type="audio/ogg",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": f'inline; filename="turn-{turn_id}.ogg"',
            },
        )


async def _rolling_metrics(
    db_session: AsyncSession, *, days: int
) -> dict[str, object]:
    """Return aggregate metrics over the last ``days`` for the list-view cards.

    ``activations`` counts engagements that STARTED in the window.
    ``voice_messages`` counts turns inside that window where the agent
    actually sent voice (``voice_sent_ok = true``). Token/cost rollups sum
    over the turn rows started in the window so the numbers reflect
    activity AT the time, not retroactive backfills.
    """
    since = datetime.now(UTC) - timedelta(days=days)

    # Engagement-level: activations
    activations_stmt = select(func.count(ChatAgentEngagement.id)).where(
        ChatAgentEngagement.started_at >= since
    )

    # Turn-level: chat / voice tokens + cost, voice messages sent
    turns_stmt = select(
        func.coalesce(
            func.sum(ChatAgentTurn.chat_tokens_input + ChatAgentTurn.chat_tokens_output),
            0,
        ),
        func.coalesce(func.sum(ChatAgentTurn.chat_cost_usd), Decimal("0")),
        func.coalesce(
            func.sum(
                ChatAgentTurn.voice_tokens_input + ChatAgentTurn.voice_tokens_output
            ),
            0,
        ),
        func.coalesce(func.sum(ChatAgentTurn.voice_cost_usd), Decimal("0")),
        func.coalesce(
            func.sum(
                case(
                    (ChatAgentTurn.voice_sent_ok.is_(True), 1),
                    else_=0,
                )
            ),
            0,
        ),
    ).where(ChatAgentTurn.started_at >= since)

    activations = (await db_session.execute(activations_stmt)).scalar() or 0
    (
        chat_tokens,
        chat_cost,
        voice_tokens,
        voice_cost,
        voice_messages,
    ) = (await db_session.execute(turns_stmt)).one()

    return {
        "days": days,
        "activations": int(activations),
        "voice_messages": int(voice_messages),
        "chat_tokens": int(chat_tokens),
        "chat_cost": Decimal(chat_cost),
        "voice_tokens": int(voice_tokens),
        "voice_cost": Decimal(voice_cost),
    }
