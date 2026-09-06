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

The bot still sends verbatim Discord message text on the turn write — the
retained artefact is the row, not the request — so the turn's triggering
messages, its model-message delta and each compaction event's original content
go through :mod:`smarter_dev.shared.message_content` on the way into the row.
Agent output, tool calls, summaries, char counts and every token/cost field are
ours, not Discord's, and are stored as sent.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from typing import NamedTuple
from uuid import UUID

from litestar import Controller
from litestar import get
from litestar import post
from litestar.exceptions import HTTPException
from litestar.exceptions import ValidationException
from litestar.params import Parameter
from litestar.status_codes import HTTP_200_OK
from litestar.status_codes import HTTP_201_CREATED
from skrift.auth.guards import APIKeyOnly
from skrift.auth.guards import Permission
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.config import get_settings
from smarter_dev.shared.message_content import redact_chat_agent_messages
from smarter_dev.shared.message_content import redact_model_message_parts
from smarter_dev.shared.message_content import redact_text
from smarter_dev.shared.model_catalog import MODEL_CATALOG
from smarter_dev.web.api_native.auth import bot_api_auth_guard
from smarter_dev.web.api_native.errors import BOT_API_EXCEPTION_HANDLERS
from smarter_dev.web.api_native.errors import plain_error
from smarter_dev.web.api_native.schemas import ChatAgentCompactionEventCreate
from smarter_dev.web.api_native.schemas import ChatAgentEngagementEnd
from smarter_dev.web.api_native.schemas import ChatAgentEngagementStart
from smarter_dev.web.api_native.schemas import ChatAgentEngagementStartResponse
from smarter_dev.web.api_native.schemas import ChatAgentErrorCreate
from smarter_dev.web.api_native.schemas import ChatAgentErrorCreateResponse
from smarter_dev.web.api_native.schemas import ChatAgentTurnCreate
from smarter_dev.web.api_native.schemas import ChatAgentTurnCreateResponse
from smarter_dev.web.api_native.schemas import ChatUsageLeaderboardEntry
from smarter_dev.web.api_native.schemas import ChatUsageLeaderboardResponse
from smarter_dev.web.llm_pricing import calc_cost
from smarter_dev.web.models import CandidateBlogTopic
from smarter_dev.web.models import ChatAgentCompactionEvent
from smarter_dev.web.models import ChatAgentEngagement
from smarter_dev.web.models import ChatAgentError
from smarter_dev.web.models import ChatAgentTurn
from smarter_dev.web.models import UsageCostRow

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
BOT_API_ADMIN_GUARDS = [
    bot_api_auth_guard,
    APIKeyOnly(),
    Permission(BOT_API_ADMIN_PERMISSION),
]


def _normalized_model_identity(model_name: str | None) -> tuple[str, str, str]:
    """Best-effort provider/catalog identity for normalized Discord usage."""
    if not model_name:
        return "unknown", "unknown", "unknown"
    provider_hint, _, wire = model_name.rpartition(":")
    if not wire:
        wire = model_name
        provider_hint = ""
    for model in MODEL_CATALOG:
        if wire == model.model_id or wire.startswith(model.model_id):
            return model.provider.value, model.key, model.model_id
    provider = {
        "google-gla": "google",
        "openai": "openai",
        "anthropic": "anthropic",
    }.get(provider_hint, provider_hint or "unknown")
    return provider, wire, wire


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
                    ChatAgentTurn.chat_tokens_input + ChatAgentTurn.chat_tokens_output
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


class _CompactionTotals(NamedTuple):
    """What a turn's compaction events add to the engagement's running totals."""

    cost_usd: Decimal
    tokens_input: int
    tokens_output: int


def _usage_cost_row(
    *,
    operation_key: str,
    operation_type: str,
    engagement: ChatAgentEngagement,
    turn_id: UUID,
    model_name: str,
    reasoning_level: str | None,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    cost_usd: Decimal,
    details: dict,
) -> UsageCostRow:
    """One metering row for a model that billed for this turn."""
    provider, catalog_key, wire_id = _normalized_model_identity(model_name)
    return UsageCostRow(
        operation_key=operation_key,
        product_mode="discord",
        operation_type=operation_type,
        discord_user_id=engagement.activation_user_id,
        conversation_id=engagement.id,
        root_turn_id=turn_id,
        provider_key=provider,
        catalog_model_key=catalog_key,
        model_id=wire_id,
        reasoning_level=reasoning_level,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        cost_usd=cost_usd,
        details=details,
    )


def _summarizer_cost(event: ChatAgentCompactionEventCreate) -> Decimal:
    """What the summarizer charged for this compaction; zero on unknown models."""
    if not event.summarizer_model_name:
        return Decimal("0")
    return calc_cost(
        event.summarizer_tokens_input,
        event.summarizer_tokens_output,
        event.summarizer_model_name,
        cache_read_tokens=event.summarizer_cache_read_tokens or 0,
        cache_write_tokens=event.summarizer_cache_write_tokens or 0,
    )


def _compaction_event_row(
    turn_id: UUID, event: ChatAgentCompactionEventCreate
) -> ChatAgentCompactionEvent:
    """One stored compaction event: its metrics kept, its content redacted."""
    return ChatAgentCompactionEvent(
        turn_id=turn_id,
        event_kind=event.event_kind,
        tool_name=event.tool_name,
        original_content=redact_text(event.original_content),
        summary=event.summary,
        original_chars=event.original_chars,
        summary_chars=event.summary_chars,
        chars_saved=event.original_chars - event.summary_chars,
        summarizer_tokens_input=event.summarizer_tokens_input,
        summarizer_tokens_output=event.summarizer_tokens_output,
        summarizer_model_name=event.summarizer_model_name,
        summarizer_reasoning_level=event.summarizer_reasoning_level,
        summarizer_cache_read_tokens=event.summarizer_cache_read_tokens,
        summarizer_cache_write_tokens=event.summarizer_cache_write_tokens,
        summarizer_cost_usd=_summarizer_cost(event),
    )


async def _persist_compaction_events(
    db_session: AsyncSession,
    *,
    turn_id: UUID,
    engagement: ChatAgentEngagement,
    events: list[ChatAgentCompactionEventCreate],
) -> _CompactionTotals:
    """Store each compaction event plus its metering row, and total the spend."""
    stored_events = []
    for event in events:
        stored_event = _compaction_event_row(turn_id, event)
        db_session.add(stored_event)
        await db_session.flush()  # populate stored_event.id for the metering key
        stored_events.append(stored_event)
        if event.summarizer_model_name:
            db_session.add(
                _usage_cost_row(
                    operation_key=(
                        f"discord:turn:{turn_id}:compaction:{stored_event.id}"
                    ),
                    operation_type="compaction",
                    engagement=engagement,
                    turn_id=turn_id,
                    model_name=event.summarizer_model_name,
                    reasoning_level=event.summarizer_reasoning_level,
                    input_tokens=event.summarizer_tokens_input,
                    output_tokens=event.summarizer_tokens_output,
                    cache_read_tokens=event.summarizer_cache_read_tokens or 0,
                    cache_write_tokens=event.summarizer_cache_write_tokens or 0,
                    cost_usd=stored_event.summarizer_cost_usd,
                    details={"event_kind": event.event_kind},
                )
            )
    return _CompactionTotals(
        cost_usd=sum(
            (stored.summarizer_cost_usd for stored in stored_events), Decimal("0")
        ),
        tokens_input=sum(event.summarizer_tokens_input for event in events),
        tokens_output=sum(event.summarizer_tokens_output for event in events),
    )


def _candidate_blog_topics(
    engagement_id: UUID, turn_id: UUID, agent_output: dict
) -> list[CandidateBlogTopic]:
    """The blogging-agent topics this turn surfaced, minus the unusable ones.

    Same neutral {headline, observation, scope, evidence, category} shape Scout
    produces — Brainstorm forms hypotheses from these claims downstream. A
    candidate without both a headline and an observation claims nothing, and
    evidence that did not arrive as a list is dropped rather than guessed at.
    """
    topics = []
    for candidate in agent_output.get("blog_topic_candidates") or []:
        headline = (candidate.get("headline") or "").strip()
        observation = (candidate.get("observation") or "").strip()
        if not headline or not observation:
            continue
        evidence = candidate.get("evidence")
        topics.append(
            CandidateBlogTopic(
                engagement_id=engagement_id,
                turn_id=turn_id,
                headline=headline[:255],
                observation=observation,
                scope=(candidate.get("scope") or "").strip(),
                evidence=[str(item) for item in evidence if item]
                if isinstance(evidence, list)
                else [],
                category=candidate.get("category"),
            )
        )
    return topics


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
        now = datetime.now(UTC)
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
        # Serialize requests for an engagement before checking the request id.
        # This makes bot retries idempotent without imposing a new historical
        # table constraint on rows written before request ids were canonical.
        engagement = await db_session.scalar(
            select(ChatAgentEngagement)
            .where(ChatAgentEngagement.id == data.engagement_id)
            .with_for_update()
        )
        if engagement is None:
            raise HTTPException(status_code=404, detail="Engagement not found")
        existing = await db_session.scalar(
            select(ChatAgentTurn).where(
                ChatAgentTurn.engagement_id == data.engagement_id,
                ChatAgentTurn.request_id == data.request_id,
            )
        )
        if existing is not None:
            summarizer_cost = Decimal(
                await db_session.scalar(
                    select(
                        func.coalesce(
                            func.sum(ChatAgentCompactionEvent.summarizer_cost_usd), 0
                        )
                    ).where(ChatAgentCompactionEvent.turn_id == existing.id)
                )
                or 0
            )
            return ChatAgentTurnCreateResponse(
                id=existing.id,
                started_at=existing.started_at,
                chat_cost_usd=str(existing.chat_cost_usd),
                voice_cost_usd=str(existing.voice_cost_usd),
                summarizer_cost_usd_total=str(summarizer_cost),
            )
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
            calc_cost(
                data.voice_tokens_input, data.voice_tokens_output, data.voice_model_name
            )
            if data.voice_model_name
            else Decimal("0")
        )
        turn = ChatAgentTurn(
            engagement_id=data.engagement_id,
            request_id=data.request_id,
            turn_kind=data.turn_kind,
            output_kind=data.output_kind,
            triggering_messages=redact_chat_agent_messages(data.triggering_messages),
            agent_output=data.agent_output,
            model_messages_delta=redact_model_message_parts(data.model_messages_delta),
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

        if data.chat_model_name:
            db_session.add(
                _usage_cost_row(
                    operation_key=f"discord:turn:{turn.id}:primary",
                    operation_type="primary",
                    engagement=engagement,
                    turn_id=turn.id,
                    model_name=data.chat_model_name,
                    reasoning_level=data.chat_reasoning_level,
                    input_tokens=data.chat_tokens_input,
                    output_tokens=data.chat_tokens_output,
                    cache_read_tokens=data.chat_cache_read_tokens or 0,
                    cache_write_tokens=data.chat_cache_write_tokens or 0,
                    cost_usd=chat_cost,
                    details={"request_id": str(data.request_id)},
                )
            )
        if data.voice_model_name:
            db_session.add(
                _usage_cost_row(
                    operation_key=f"discord:turn:{turn.id}:voice",
                    operation_type="voice",
                    engagement=engagement,
                    turn_id=turn.id,
                    model_name=data.voice_model_name,
                    reasoning_level=None,
                    input_tokens=data.voice_tokens_input,
                    output_tokens=data.voice_tokens_output,
                    cache_read_tokens=0,
                    cache_write_tokens=0,
                    cost_usd=voice_cost,
                    details={"request_id": str(data.request_id)},
                )
            )

        compaction = await _persist_compaction_events(
            db_session,
            turn_id=turn.id,
            engagement=engagement,
            events=data.compaction_events,
        )

        for topic in _candidate_blog_topics(
            data.engagement_id, turn.id, data.agent_output
        ):
            db_session.add(topic)

        last_topic = data.agent_output.get("topic")
        last_notes = data.agent_output.get("notes")
        total_cost_delta = chat_cost + voice_cost + compaction.cost_usd

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
            + compaction.tokens_input,
            "total_compaction_tokens_output": ChatAgentEngagement.total_compaction_tokens_output
            + compaction.tokens_output,
            "total_chat_cost_usd": ChatAgentEngagement.total_chat_cost_usd + chat_cost,
            "total_voice_cost_usd": ChatAgentEngagement.total_voice_cost_usd
            + voice_cost,
            "total_compaction_cost_usd": ChatAgentEngagement.total_compaction_cost_usd
            + compaction.cost_usd,
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
            summarizer_cost_usd_total=str(compaction.cost_usd),
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
        since = datetime.now(UTC) - timedelta(days=days)
        rows = await channel_usage_leaderboard(
            db_session, guild_id=guild_id, since=since, limit=limit
        )
        return ChatUsageLeaderboardResponse(
            since=since,
            days=days,
            total_tokens_all_time=await guild_total_tokens(
                db_session, guild_id=guild_id
            ),
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
