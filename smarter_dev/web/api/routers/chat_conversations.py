"""Chat-agent conversation API endpoints.

POST endpoints used by the Discord bot to persist engagements and turns
into the Skrift DB (NOT the legacy DB), plus the usage-leaderboard read
backing ``/bot-usage-info``. The operator dashboard reads directly from
the DB.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.database import get_skrift_db_session
from smarter_dev.web.api.dependencies import APIKey
from smarter_dev.web.api.schemas import (
    ChatAgentEngagementEnd,
    ChatAgentEngagementStart,
    ChatAgentEngagementStartResponse,
    ChatAgentTurnCreate,
    ChatAgentTurnCreateResponse,
    ChatUsageLeaderboardEntry,
    ChatUsageLeaderboardResponse,
)
from smarter_dev.web.models import (
    CandidateBlogTopic,
    ChatAgentCompactionEvent,
    ChatAgentEngagement,
    ChatAgentTurn,
)
from smarter_dev.web.llm_pricing import calc_cost

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat-conversations", tags=["Chat Agent Conversations"])

_BOT_WRITE_SCOPES = {"bot:write", "admin:write"}

SkriftSession = Annotated[AsyncSession, Depends(get_skrift_db_session)]


def _require_bot_write(api_key: APIKey) -> None:
    if not any(scope in _BOT_WRITE_SCOPES for scope in api_key.scopes):
        raise HTTPException(
            status_code=403, detail="Bot write permissions required"
        )


@router.post(
    "/engagements",
    response_model=ChatAgentEngagementStartResponse,
    status_code=201,
)
async def create_engagement(
    request: Request,
    api_key: APIKey,
    body: ChatAgentEngagementStart,
    db: SkriftSession,
) -> ChatAgentEngagementStartResponse:
    _require_bot_write(api_key)
    engagement = ChatAgentEngagement(
        guild_id=body.guild_id,
        channel_id=body.channel_id,
        guild_name=body.guild_name,
        channel_name=body.channel_name,
        activation_user_id=body.activation_user_id,
        activation_username=body.activation_username,
        activation_message_id=body.activation_message_id,
    )
    db.add(engagement)
    await db.commit()
    await db.refresh(engagement)
    return ChatAgentEngagementStartResponse(
        id=engagement.id,
        started_at=engagement.started_at,
    )


@router.post("/engagements/{engagement_id}/end", status_code=200)
async def end_engagement(
    request: Request,
    api_key: APIKey,
    engagement_id: UUID,
    body: ChatAgentEngagementEnd,
    db: SkriftSession,
) -> dict:
    _require_bot_write(api_key)
    now = datetime.now(timezone.utc)
    result = await db.execute(
        update(ChatAgentEngagement)
        .where(ChatAgentEngagement.id == engagement_id)
        .values(ended_at=now, deactivation_reason=body.deactivation_reason)
    )
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Engagement not found")
    await db.commit()
    return {"id": str(engagement_id), "ended_at": now.isoformat()}


@router.post(
    "/turns",
    response_model=ChatAgentTurnCreateResponse,
    status_code=201,
)
async def create_turn(
    request: Request,
    api_key: APIKey,
    body: ChatAgentTurnCreate,
    db: SkriftSession,
) -> ChatAgentTurnCreateResponse:
    """Persist one agent turn + its compaction events. Bumps engagement totals."""
    _require_bot_write(api_key)

    # Cost calculations — best-effort, returns 0 on unknown models.
    chat_cost = (
        calc_cost(body.chat_tokens_input, body.chat_tokens_output, body.chat_model_name)
        if body.chat_model_name
        else Decimal("0")
    )
    voice_cost = (
        calc_cost(body.voice_tokens_input, body.voice_tokens_output, body.voice_model_name)
        if body.voice_model_name
        else Decimal("0")
    )
    summarizer_cost_total = Decimal("0")
    summarizer_in_total = 0
    summarizer_out_total = 0

    turn = ChatAgentTurn(
        engagement_id=body.engagement_id,
        request_id=body.request_id,
        turn_kind=body.turn_kind,
        output_kind=body.output_kind,
        triggering_messages=body.triggering_messages,
        agent_output=body.agent_output,
        model_messages_delta=body.model_messages_delta,
        duration_ms=body.duration_ms,
        chat_tokens_input=body.chat_tokens_input,
        chat_tokens_output=body.chat_tokens_output,
        chat_model_name=body.chat_model_name,
        chat_cost_usd=chat_cost,
        voice_tokens_input=body.voice_tokens_input,
        voice_tokens_output=body.voice_tokens_output,
        voice_model_name=body.voice_model_name,
        voice_cost_usd=voice_cost,
        voice_sent_ok=body.voice_sent_ok,
        voice_send_error=body.voice_send_error,
    )
    db.add(turn)
    await db.flush()  # populate turn.id for compaction-event FKs

    for ev in body.compaction_events:
        ev_cost = (
            calc_cost(
                ev.summarizer_tokens_input,
                ev.summarizer_tokens_output,
                ev.summarizer_model_name,
            )
            if ev.summarizer_model_name
            else Decimal("0")
        )
        summarizer_cost_total += ev_cost
        summarizer_in_total += ev.summarizer_tokens_input
        summarizer_out_total += ev.summarizer_tokens_output
        db.add(
            ChatAgentCompactionEvent(
                turn_id=turn.id,
                event_kind=ev.event_kind,
                tool_name=ev.tool_name,
                original_content=ev.original_content,
                summary=ev.summary,
                original_chars=ev.original_chars,
                summary_chars=ev.summary_chars,
                chars_saved=ev.original_chars - ev.summary_chars,
                summarizer_tokens_input=ev.summarizer_tokens_input,
                summarizer_tokens_output=ev.summarizer_tokens_output,
                summarizer_model_name=ev.summarizer_model_name,
                summarizer_cost_usd=ev_cost,
            )
        )

    # Blogging-agent capture: file any candidate blog topics the agent
    # surfaced this turn. Same neutral {headline, observation, scope,
    # evidence, category} shape Scout produces — Brainstorm forms
    # hypotheses from these claims downstream.
    if isinstance(body.agent_output, dict):
        for cand in body.agent_output.get("blog_topic_candidates") or []:
            headline = (cand.get("headline") or "").strip()
            observation = (cand.get("observation") or "").strip()
            if not headline or not observation:
                continue
            scope = (cand.get("scope") or "").strip()
            evidence = cand.get("evidence") or []
            if not isinstance(evidence, list):
                evidence = []
            db.add(
                CandidateBlogTopic(
                    engagement_id=body.engagement_id,
                    turn_id=turn.id,
                    headline=headline[:255],
                    observation=observation,
                    scope=scope,
                    evidence=[str(e) for e in evidence if e],
                    category=cand.get("category"),
                )
            )

    # Bump engagement aggregates + latest topic/notes denormalisation.
    last_topic = body.agent_output.get("topic") if isinstance(body.agent_output, dict) else None
    last_notes = body.agent_output.get("notes") if isinstance(body.agent_output, dict) else None
    total_cost_delta = chat_cost + voice_cost + summarizer_cost_total

    update_values: dict = {
        "total_chat_tokens_input": ChatAgentEngagement.total_chat_tokens_input
        + body.chat_tokens_input,
        "total_chat_tokens_output": ChatAgentEngagement.total_chat_tokens_output
        + body.chat_tokens_output,
        "total_voice_tokens_input": ChatAgentEngagement.total_voice_tokens_input
        + body.voice_tokens_input,
        "total_voice_tokens_output": ChatAgentEngagement.total_voice_tokens_output
        + body.voice_tokens_output,
        "total_compaction_tokens_input": ChatAgentEngagement.total_compaction_tokens_input
        + summarizer_in_total,
        "total_compaction_tokens_output": ChatAgentEngagement.total_compaction_tokens_output
        + summarizer_out_total,
        "total_chat_cost_usd": ChatAgentEngagement.total_chat_cost_usd + chat_cost,
        "total_voice_cost_usd": ChatAgentEngagement.total_voice_cost_usd + voice_cost,
        "total_compaction_cost_usd": ChatAgentEngagement.total_compaction_cost_usd + summarizer_cost_total,
        "total_cost_usd": ChatAgentEngagement.total_cost_usd + total_cost_delta,
    }
    if last_topic is not None:
        update_values["last_topic"] = last_topic
    if last_notes is not None:
        update_values["last_notes"] = last_notes

    await db.execute(
        update(ChatAgentEngagement)
        .where(ChatAgentEngagement.id == body.engagement_id)
        .values(**update_values)
    )

    await db.commit()
    await db.refresh(turn)

    return ChatAgentTurnCreateResponse(
        id=turn.id,
        started_at=turn.started_at,
        chat_cost_usd=str(chat_cost),
        voice_cost_usd=str(voice_cost),
        summarizer_cost_usd_total=str(summarizer_cost_total),
    )


async def channel_usage_leaderboard(
    db: AsyncSession, *, guild_id: str, since: datetime, limit: int
):
    """Top channels/threads by chat tokens spent since ``since``.

    Sums each turn's chat input+output tokens (voice/compaction buckets are
    excluded — this mirrors what the bot-side budget meter counts), grouped
    by the engagement's channel_id, descending. ``channel_name`` is the
    engagements' most recent non-null snapshot, for display fallback.
    """
    total_tokens = func.sum(
        ChatAgentTurn.chat_tokens_input + ChatAgentTurn.chat_tokens_output
    ).label("total_tokens")
    stmt = (
        select(
            ChatAgentEngagement.channel_id,
            func.max(ChatAgentEngagement.channel_name).label("channel_name"),
            total_tokens,
        )
        .select_from(ChatAgentTurn)
        .join(
            ChatAgentEngagement,
            ChatAgentTurn.engagement_id == ChatAgentEngagement.id,
        )
        .where(ChatAgentEngagement.guild_id == guild_id)
        .where(ChatAgentTurn.started_at >= since)
        .group_by(ChatAgentEngagement.channel_id)
        .order_by(total_tokens.desc())
        .limit(limit)
    )
    return (await db.execute(stmt)).all()


@router.get("/usage-leaderboard", response_model=ChatUsageLeaderboardResponse)
async def usage_leaderboard(
    request: Request,
    api_key: APIKey,
    db: SkriftSession,
    guild_id: str,
    days: int = Query(default=1, ge=1, le=366),
    limit: int = Query(default=20, ge=1, le=100),
) -> ChatUsageLeaderboardResponse:
    """Top channels by chat-token usage over the last ``days`` days."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = await channel_usage_leaderboard(
        db, guild_id=guild_id, since=since, limit=limit
    )
    return ChatUsageLeaderboardResponse(
        since=since,
        days=days,
        entries=[
            ChatUsageLeaderboardEntry(
                channel_id=row.channel_id,
                channel_name=row.channel_name,
                total_tokens=int(row.total_tokens or 0),
            )
            for row in rows
        ],
    )
