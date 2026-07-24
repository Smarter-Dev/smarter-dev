"""Native Litestar port of the chat-agent conversation bot API.

Ports the legacy FastAPI ``routers/chat_conversations.py`` (prefix
``/chat-conversations``) — part of unit U8 in
docs/v2/legacy-sunset/04-api-rewrite.md. Preserves the exact paths, verbs,
status codes, and request/response shapes of the FastAPI implementation so
``smarter_dev/bot/services/chat_conversation_persistence.py`` and
``smarter_dev/bot/plugins/bot_usage.py`` (and any external caller) need zero
changes:

- ``POST /api/chat-conversations/engagements`` → 201, engagement id + started_at.
- ``POST /api/chat-conversations/engagements/{engagement_id}/end`` → 200 or 404.
- ``POST /api/chat-conversations/errors`` → 201, error id + admin detail URL.
- ``POST /api/chat-conversations/turns`` → 201, turn id + cost breakdown.
- ``GET  /api/chat-conversations/usage-leaderboard`` → 200, per-channel token totals.

These endpoints write the Skrift DB (main database, ``skrift`` schema), which is
exactly what the Litestar-injected ``db_session`` targets — the legacy router
reached the same DB via ``get_skrift_db_session``.

Auth-scope parity: the legacy write endpoints called ``_require_bot_write`` to
demand a ``bot:write`` / ``admin:write`` scope (403 otherwise); the read
leaderboard required only a valid key. That split is expressed here through
guards — the three write handlers take :data:`BOT_API_ADMIN_GUARDS`
(``Permission("bot-api-admin")``), the read handler takes :data:`BOT_API_GUARDS`
(``Permission("bot-api")``). The bot's service key carries both permissions (see
``roles.py`` ``bot-service`` role and the phase-01 key-mint runbook).

Error-shape parity: the legacy 404 (unknown engagement) came from a bare
``HTTPException`` — a plain ``{"detail": "<string>"}`` body — reproduced via
:func:`errors.plain_error`. A malformed ``engagement_id`` path segment answers
422 (the FastAPI ``UUID`` path param validated before the handler ran),
reproduced via :func:`_parse_uuid_path`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

from litestar import Controller, get, post
from litestar.exceptions import ValidationException
from litestar.params import Parameter
from litestar.status_codes import HTTP_200_OK, HTTP_201_CREATED
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.guards import APIKeyOnly, Permission

from smarter_dev.shared.config import get_settings
from smarter_dev.web.api_native.schemas import (
    ChatAgentErrorCreate,
    ChatAgentErrorCreateResponse,
    ChatAgentEngagementEnd,
    ChatAgentEngagementStart,
    ChatAgentEngagementStartResponse,
    ChatAgentTurnCreate,
    ChatAgentTurnCreateResponse,
    ChatUsageLeaderboardEntry,
    ChatUsageLeaderboardResponse,
)
from smarter_dev.web.api_native.auth import bot_api_auth_guard
from smarter_dev.web.api_native.errors import (
    BOT_API_EXCEPTION_HANDLERS,
    plain_error,
)
from smarter_dev.web.llm_pricing import calc_cost
from smarter_dev.web.models import (
    CandidateBlogTopic,
    ChatAgentCompactionEvent,
    ChatAgentEngagement,
    ChatAgentError,
    ChatAgentTurn,
)

# Permissions granted to the bot's Skrift service key (see roles.py
# `bot-service` role and the phase-01 key-mint runbook). ``bot-api`` is the base
# key permission; ``bot-api-admin`` gates the write paths that the legacy router
# demanded a ``bot:write`` / ``admin:write`` scope for.
BOT_API_PERMISSION = "bot-api"
BOT_API_ADMIN_PERMISSION = "bot-api-admin"

# Guards are declared PER ROUTE (not only on the controller) because Skrift's
# ``auth_guard`` inspects ``route_handler.guards`` to find the ``APIKeyOnly``
# marker — controller-level guards do not populate that attribute. See the bytes
# controller and docs/v2/legacy-sunset/04-api-rewrite.md ("Auth model").
BOT_API_GUARDS = [bot_api_auth_guard, APIKeyOnly(), Permission(BOT_API_PERMISSION)]
BOT_API_ADMIN_GUARDS = [bot_api_auth_guard, APIKeyOnly(), Permission(BOT_API_ADMIN_PERMISSION)]


def _parse_uuid_path(value: str, field_name: str) -> UUID:
    """Parse a UUID path segment, matching FastAPI's 422 on bad format.

    The legacy route declared ``engagement_id`` as ``UUID``, so a malformed
    UUID produced a 422 ``RequestValidationError``. Declaring the Litestar param
    as ``str`` and parsing here reproduces that 422 (via
    :func:`errors.handle_validation_exception`) instead of a route-miss 404.
    """
    try:
        return UUID(value)
    except ValueError as parse_error:
        raise ValidationException(
            detail=f"Invalid {field_name} format",
            extra=[{"key": field_name, "message": "value is not a valid uuid"}],
        ) from parse_error


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


async def guild_total_tokens(
    db: AsyncSession, *, guild_id: str, since: datetime | None = None
) -> int:
    """The guild's summed chat tokens (input+output) across every channel.

    ``since`` bounds the window; None sums all time.
    """
    stmt = (
        select(
            func.coalesce(
                func.sum(
                    ChatAgentTurn.chat_tokens_input
                    + ChatAgentTurn.chat_tokens_output
                ),
                0,
            )
        )
        .select_from(ChatAgentTurn)
        .join(
            ChatAgentEngagement,
            ChatAgentTurn.engagement_id == ChatAgentEngagement.id,
        )
        .where(ChatAgentEngagement.guild_id == guild_id)
    )
    if since is not None:
        stmt = stmt.where(ChatAgentTurn.started_at >= since)
    return int(await db.scalar(stmt) or 0)


class ChatConversationController(Controller):
    """Chat-agent engagement/turn persistence plus the usage leaderboard."""

    path = "/api/chat-conversations"
    exception_handlers = BOT_API_EXCEPTION_HANDLERS

    @post("/engagements", status_code=HTTP_201_CREATED, guards=BOT_API_ADMIN_GUARDS)
    async def create_engagement(
        self,
        db_session: AsyncSession,
        data: ChatAgentEngagementStart,
    ) -> ChatAgentEngagementStartResponse:
        """Open a new chat-agent engagement for a channel."""
        engagement = ChatAgentEngagement(
            guild_id=data.guild_id,
            channel_id=data.channel_id,
            guild_name=data.guild_name,
            channel_name=data.channel_name,
            activation_user_id=data.activation_user_id,
            activation_username=data.activation_username,
            activation_message_id=data.activation_message_id,
        )
        db_session.add(engagement)
        await db_session.commit()
        await db_session.refresh(engagement)
        return ChatAgentEngagementStartResponse(
            id=engagement.id,
            started_at=engagement.started_at,
        )

    @post(
        "/engagements/{engagement_id:str}/end",
        status_code=HTTP_200_OK,
        guards=BOT_API_ADMIN_GUARDS,
    )
    async def end_engagement(
        self,
        db_session: AsyncSession,
        engagement_id: str,
        data: ChatAgentEngagementEnd,
    ) -> dict:
        """Close an engagement, recording why it deactivated."""
        parsed_engagement_id = _parse_uuid_path(engagement_id, "engagement_id")
        now = datetime.now(timezone.utc)
        result = await db_session.execute(
            update(ChatAgentEngagement)
            .where(ChatAgentEngagement.id == parsed_engagement_id)
            .values(ended_at=now, deactivation_reason=data.deactivation_reason)
        )
        if result.rowcount == 0:
            raise plain_error(404, "Engagement not found")
        await db_session.commit()
        return {"id": str(parsed_engagement_id), "ended_at": now.isoformat()}

    @post("/errors", status_code=HTTP_201_CREATED, guards=BOT_API_ADMIN_GUARDS)
    async def create_error(
        self,
        db_session: AsyncSession,
        data: ChatAgentErrorCreate,
    ) -> ChatAgentErrorCreateResponse:
        """Persist a failed chat run and return its protected admin URL."""
        error = ChatAgentError(
            engagement_id=data.engagement_id,
            request_id=data.request_id,
            guild_id=data.guild_id,
            channel_id=data.channel_id,
            model_name=data.model_name,
            reasoning_level=data.reasoning_level,
            error_type=data.error_type,
            error_message=data.error_message,
            traceback=data.traceback,
            provider_status_code=data.provider_status_code,
            provider_body=data.provider_body,
            error_context=data.error_context,
        )
        db_session.add(error)
        await db_session.commit()
        await db_session.refresh(error)
        admin_url = (
            f"{get_settings().site_base_url.rstrip('/')}/admin/chat-errors/{error.id}"
        )
        return ChatAgentErrorCreateResponse(
            id=error.id,
            occurred_at=error.occurred_at,
            admin_url=admin_url,
        )

    @post("/turns", status_code=HTTP_201_CREATED, guards=BOT_API_ADMIN_GUARDS)
    async def create_turn(
        self,
        db_session: AsyncSession,
        data: ChatAgentTurnCreate,
    ) -> ChatAgentTurnCreateResponse:
        """Persist one agent turn + its compaction events. Bumps engagement totals."""
        # Cost calculations — best-effort, returns 0 on unknown models.
        chat_cost = (
            calc_cost(
                data.chat_tokens_input,
                data.chat_tokens_output,
                data.chat_model_name,
                cache_read_tokens=data.chat_cache_read_tokens or 0,
                cache_write_tokens=data.chat_cache_write_tokens or 0,
            )
            if data.chat_model_name
            else Decimal("0")
        )
        voice_cost = (
            calc_cost(data.voice_tokens_input, data.voice_tokens_output, data.voice_model_name)
            if data.voice_model_name
            else Decimal("0")
        )
        summarizer_cost_total = Decimal("0")
        summarizer_in_total = 0
        summarizer_out_total = 0

        turn = ChatAgentTurn(
            engagement_id=data.engagement_id,
            request_id=data.request_id,
            turn_kind=data.turn_kind,
            output_kind=data.output_kind,
            triggering_messages=data.triggering_messages,
            agent_output=data.agent_output,
            model_messages_delta=data.model_messages_delta,
            duration_ms=data.duration_ms,
            chat_tokens_input=data.chat_tokens_input,
            chat_tokens_output=data.chat_tokens_output,
            chat_model_name=data.chat_model_name,
            chat_reasoning_level=data.chat_reasoning_level,
            chat_cache_read_tokens=data.chat_cache_read_tokens,
            chat_cache_write_tokens=data.chat_cache_write_tokens,
            chat_cost_usd=chat_cost,
            voice_tokens_input=data.voice_tokens_input,
            voice_tokens_output=data.voice_tokens_output,
            voice_model_name=data.voice_model_name,
            voice_cost_usd=voice_cost,
            voice_sent_ok=data.voice_sent_ok,
            voice_send_error=data.voice_send_error,
        )
        db_session.add(turn)
        await db_session.flush()  # populate turn.id for compaction-event FKs

        for ev in data.compaction_events:
            ev_cost = (
                calc_cost(
                    ev.summarizer_tokens_input,
                    ev.summarizer_tokens_output,
                    ev.summarizer_model_name,
                    cache_read_tokens=ev.summarizer_cache_read_tokens or 0,
                    cache_write_tokens=ev.summarizer_cache_write_tokens or 0,
                )
                if ev.summarizer_model_name
                else Decimal("0")
            )
            summarizer_cost_total += ev_cost
            summarizer_in_total += ev.summarizer_tokens_input
            summarizer_out_total += ev.summarizer_tokens_output
            db_session.add(
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
                    summarizer_reasoning_level=ev.summarizer_reasoning_level,
                    summarizer_cache_read_tokens=ev.summarizer_cache_read_tokens,
                    summarizer_cache_write_tokens=ev.summarizer_cache_write_tokens,
                    summarizer_cost_usd=ev_cost,
                )
            )

        # Blogging-agent capture: file any candidate blog topics the agent
        # surfaced this turn. Same neutral {headline, observation, scope,
        # evidence, category} shape Scout produces — Brainstorm forms
        # hypotheses from these claims downstream.
        if isinstance(data.agent_output, dict):
            for cand in data.agent_output.get("blog_topic_candidates") or []:
                headline = (cand.get("headline") or "").strip()
                observation = (cand.get("observation") or "").strip()
                if not headline or not observation:
                    continue
                scope = (cand.get("scope") or "").strip()
                evidence = cand.get("evidence") or []
                if not isinstance(evidence, list):
                    evidence = []
                db_session.add(
                    CandidateBlogTopic(
                        engagement_id=data.engagement_id,
                        turn_id=turn.id,
                        headline=headline[:255],
                        observation=observation,
                        scope=scope,
                        evidence=[str(e) for e in evidence if e],
                        category=cand.get("category"),
                    )
                )

        # Bump engagement aggregates + latest topic/notes denormalisation.
        last_topic = (
            data.agent_output.get("topic")
            if isinstance(data.agent_output, dict)
            else None
        )
        last_notes = (
            data.agent_output.get("notes")
            if isinstance(data.agent_output, dict)
            else None
        )
        total_cost_delta = chat_cost + voice_cost + summarizer_cost_total

        update_values: dict = {
            "total_chat_tokens_input": ChatAgentEngagement.total_chat_tokens_input
            + data.chat_tokens_input,
            "total_chat_tokens_output": ChatAgentEngagement.total_chat_tokens_output
            + data.chat_tokens_output,
            "total_voice_tokens_input": ChatAgentEngagement.total_voice_tokens_input
            + data.voice_tokens_input,
            "total_voice_tokens_output": ChatAgentEngagement.total_voice_tokens_output
            + data.voice_tokens_output,
            "total_compaction_tokens_input": ChatAgentEngagement.total_compaction_tokens_input
            + summarizer_in_total,
            "total_compaction_tokens_output": ChatAgentEngagement.total_compaction_tokens_output
            + summarizer_out_total,
            "total_chat_cost_usd": ChatAgentEngagement.total_chat_cost_usd + chat_cost,
            "total_voice_cost_usd": ChatAgentEngagement.total_voice_cost_usd + voice_cost,
            "total_compaction_cost_usd": ChatAgentEngagement.total_compaction_cost_usd
            + summarizer_cost_total,
            "total_cost_usd": ChatAgentEngagement.total_cost_usd + total_cost_delta,
        }
        if last_topic is not None:
            update_values["last_topic"] = last_topic
        if last_notes is not None:
            update_values["last_notes"] = last_notes

        await db_session.execute(
            update(ChatAgentEngagement)
            .where(ChatAgentEngagement.id == data.engagement_id)
            .values(**update_values)
        )

        await db_session.commit()
        await db_session.refresh(turn)

        return ChatAgentTurnCreateResponse(
            id=turn.id,
            started_at=turn.started_at,
            chat_cost_usd=str(chat_cost),
            voice_cost_usd=str(voice_cost),
            summarizer_cost_usd_total=str(summarizer_cost_total),
        )

    @get("/usage-leaderboard", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def usage_leaderboard(
        self,
        db_session: AsyncSession,
        guild_id: str,
        days: int = Parameter(default=1, ge=1, le=366),
        limit: int = Parameter(default=20, ge=1, le=100),
    ) -> ChatUsageLeaderboardResponse:
        """Top channels by chat-token usage over the last ``days`` days."""
        since = datetime.now(timezone.utc) - timedelta(days=days)
        rows = await channel_usage_leaderboard(
            db_session, guild_id=guild_id, since=since, limit=limit
        )
        return ChatUsageLeaderboardResponse(
            since=since,
            days=days,
            total_tokens_all_time=await guild_total_tokens(db_session, guild_id=guild_id),
            total_tokens_in_window=await guild_total_tokens(
                db_session, guild_id=guild_id, since=since
            ),
            entries=[
                ChatUsageLeaderboardEntry(
                    channel_id=row.channel_id,
                    channel_name=row.channel_name,
                    total_tokens=int(row.total_tokens or 0),
                )
                for row in rows
            ],
        )
