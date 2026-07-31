"""Canonical normalized usage ledger and live token/spend metrics."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import case
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.model_catalog import CatalogModel
from smarter_dev.web.chat.limits import settle_reservation_actual
from smarter_dev.web.llm_pricing import calc_cost
from smarter_dev.web.models import UsageCostRow
from smarter_dev.web.models import WebChatConversation

_PROVIDER_PREFIX = {
    "google": "google-gla",
    "openai": "openai",
    "anthropic": "anthropic",
    "digitalocean": "digitalocean",
    "openrouter": "openrouter",
    "opencode_zen": "opencode_zen",
}


def usage_cost(
    model: CatalogModel,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> Decimal:
    model_name = f"{_PROVIDER_PREFIX[model.provider.value]}:{model.model_id}"
    return calc_cost(
        max(input_tokens, 0),
        max(output_tokens, 0),
        model_name,
        max(cache_read_tokens, 0),
        max(cache_write_tokens, 0),
    )


async def record_usage(
    session: AsyncSession,
    *,
    operation_key: str,
    product_mode: str,
    operation_type: str,
    model: CatalogModel,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    user_id: UUID | None = None,
    discord_user_id: str | None = None,
    conversation_id: UUID | None = None,
    root_turn_id: UUID | None = None,
    subagent_id: UUID | None = None,
    reasoning_level: str | None = None,
    intelligence_mode: str | None = None,
    four_hour_window_id: UUID | None = None,
    overage_cost_usd: Decimal = Decimal("0"),
    metered_at: datetime | None = None,
    details: dict | None = None,
    cost_usd: Decimal | None = None,
) -> UsageCostRow:
    """Idempotently append one model or auxiliary operation."""
    existing = await session.scalar(
        select(UsageCostRow).where(UsageCostRow.operation_key == operation_key)
    )
    if existing is not None:
        return existing
    row = UsageCostRow(
        operation_key=operation_key,
        product_mode=product_mode,
        operation_type=operation_type,
        user_id=user_id,
        discord_user_id=discord_user_id,
        conversation_id=conversation_id,
        root_turn_id=root_turn_id,
        subagent_id=subagent_id,
        provider_key=model.provider.value,
        catalog_model_key=model.key,
        model_id=model.model_id,
        reasoning_level=reasoning_level,
        intelligence_mode=intelligence_mode,
        input_tokens=max(input_tokens, 0),
        output_tokens=max(output_tokens, 0),
        cache_read_tokens=max(cache_read_tokens, 0),
        cache_write_tokens=max(cache_write_tokens, 0),
        cost_usd=(
            usage_cost(
                model,
                input_tokens,
                output_tokens,
                cache_read_tokens,
                cache_write_tokens,
            )
            if cost_usd is None
            else max(Decimal(cost_usd), Decimal("0"))
        ),
        overage_cost_usd=max(Decimal(overage_cost_usd), Decimal("0")),
        four_hour_window_id=four_hour_window_id,
        metered_at=metered_at or datetime.now(UTC),
        details=details or {},
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        existing = await session.scalar(
            select(UsageCostRow).where(UsageCostRow.operation_key == operation_key)
        )
        if existing is None:
            raise
        return existing
    return row


async def record_settled_chat_usage(
    session: AsyncSession,
    *,
    operation_key: str,
    operation_type: str,
    model: CatalogModel,
    tier: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    user_id: UUID,
    conversation_id: UUID,
    root_turn_id: UUID,
    subagent_id: UUID | None = None,
    reasoning_level: str | None = None,
    intelligence_mode: str,
    details: dict | None = None,
) -> UsageCostRow:
    existing = await session.scalar(
        select(UsageCostRow).where(UsageCostRow.operation_key == operation_key)
    )
    if existing is not None:
        return existing
    cost = usage_cost(
        model,
        input_tokens,
        output_tokens,
        cache_read_tokens,
        cache_write_tokens,
    )
    overage, window_id, metered_at = await settle_reservation_actual(
        session, operation_key, actual_cost=cost, tier=tier
    )
    return await record_usage(
        session,
        operation_key=operation_key,
        product_mode="chat",
        operation_type=operation_type,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        user_id=user_id,
        conversation_id=conversation_id,
        root_turn_id=root_turn_id,
        subagent_id=subagent_id,
        reasoning_level=reasoning_level,
        intelligence_mode=intelligence_mode,
        four_hour_window_id=window_id,
        overage_cost_usd=overage,
        metered_at=metered_at,
        details=details,
        cost_usd=cost,
    )


async def reconcile_discord_usage(session: AsyncSession) -> int:
    """Catch up/normalize Discord usage written during rolling deployments."""
    from smarter_dev.web.api_native.chat_conversations import _normalized_model_identity
    from smarter_dev.web.models import ChatAgentCompactionEvent
    from smarter_dev.web.models import ChatAgentEngagement
    from smarter_dev.web.models import ChatAgentTurn

    repaired = 0
    turns = (
        await session.execute(
            select(ChatAgentTurn, ChatAgentEngagement.activation_user_id)
            .join(
                ChatAgentEngagement,
                ChatAgentEngagement.id == ChatAgentTurn.engagement_id,
            )
            .where(
                (ChatAgentTurn.chat_model_name.is_not(None))
                | (ChatAgentTurn.voice_model_name.is_not(None))
            )
            .with_for_update(skip_locked=True)
        )
    ).all()
    for turn, discord_user_id in turns:
        for operation_type, model_name, input_tokens, output_tokens, cost in (
            (
                "primary",
                turn.chat_model_name,
                turn.chat_tokens_input,
                turn.chat_tokens_output,
                turn.chat_cost_usd,
            ),
            (
                "voice",
                turn.voice_model_name,
                turn.voice_tokens_input,
                turn.voice_tokens_output,
                turn.voice_cost_usd,
            ),
        ):
            if not model_name:
                continue
            key = f"discord:turn:{turn.id}:{operation_type}"
            row = await session.scalar(
                select(UsageCostRow).where(UsageCostRow.operation_key == key)
            )
            provider, catalog_key, wire_id = _normalized_model_identity(model_name)
            if row is None:
                row = UsageCostRow(
                    operation_key=key,
                    product_mode="discord",
                    operation_type=operation_type,
                    discord_user_id=discord_user_id,
                    conversation_id=turn.engagement_id,
                    root_turn_id=turn.id,
                    provider_key=provider,
                    catalog_model_key=catalog_key,
                    model_id=wire_id,
                    reasoning_level=turn.chat_reasoning_level
                    if operation_type == "primary"
                    else None,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=(turn.chat_cache_read_tokens or 0)
                    if operation_type == "primary"
                    else 0,
                    cache_write_tokens=(turn.chat_cache_write_tokens or 0)
                    if operation_type == "primary"
                    else 0,
                    cost_usd=cost,
                    overage_cost_usd=Decimal("0"),
                    metered_at=turn.started_at,
                    details={"request_id": turn.request_id, "reconciled": True},
                )
                session.add(row)
                repaired += 1
            elif row.provider_key == "unknown":
                row.provider_key = provider
                row.catalog_model_key = catalog_key
                row.model_id = wire_id
                repaired += 1
    events = list(
        (
            await session.execute(
                select(ChatAgentCompactionEvent)
                .where(ChatAgentCompactionEvent.summarizer_model_name.is_not(None))
                .with_for_update(skip_locked=True)
            )
        ).scalars()
    )
    for event in events:
        key = f"discord:turn:{event.turn_id}:compaction:{event.id}"
        row = await session.scalar(
            select(UsageCostRow).where(UsageCostRow.operation_key == key)
        )
        provider, catalog_key, wire_id = _normalized_model_identity(
            event.summarizer_model_name
        )
        if row is None:
            turn = await session.get(ChatAgentTurn, event.turn_id)
            engagement = (
                await session.get(ChatAgentEngagement, turn.engagement_id)
                if turn is not None
                else None
            )
            row = UsageCostRow(
                operation_key=key,
                product_mode="discord",
                operation_type="compaction",
                discord_user_id=engagement.activation_user_id if engagement else None,
                conversation_id=turn.engagement_id if turn else None,
                root_turn_id=event.turn_id,
                provider_key=provider,
                catalog_model_key=catalog_key,
                model_id=wire_id,
                reasoning_level=event.summarizer_reasoning_level,
                input_tokens=event.summarizer_tokens_input,
                output_tokens=event.summarizer_tokens_output,
                cache_read_tokens=event.summarizer_cache_read_tokens or 0,
                cache_write_tokens=event.summarizer_cache_write_tokens or 0,
                cost_usd=event.summarizer_cost_usd,
                overage_cost_usd=Decimal("0"),
                metered_at=event.created_at,
                details={"event_kind": event.event_kind, "reconciled": True},
            )
            session.add(row)
            repaired += 1
        elif row.provider_key == "unknown":
            row.provider_key = provider
            row.catalog_model_key = catalog_key
            row.model_id = wire_id
            repaired += 1
    await session.commit()
    return repaired


async def spend_between(
    session: AsyncSession, user_id: UUID, start: datetime, end: datetime
) -> Decimal:
    value = await session.scalar(
        select(func.coalesce(func.sum(UsageCostRow.cost_usd), 0)).where(
            UsageCostRow.product_mode == "chat",
            UsageCostRow.user_id == user_id,
            UsageCostRow.metered_at >= start,
            UsageCostRow.metered_at < end,
        )
    )
    return Decimal(value or 0)


async def usage_metrics(
    session: AsyncSession,
    *,
    user_id: UUID,
    conversation_id: UUID,
    window_start: datetime,
    window_end: datetime,
    four_hour_limit: Decimal,
    window_id: UUID | None = None,
) -> dict:
    conversation = await session.get(WebChatConversation, conversation_id)
    totals = (
        await session.execute(
            select(
                func.coalesce(
                    func.sum(UsageCostRow.input_tokens + UsageCostRow.output_tokens), 0
                ),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                UsageCostRow.subagent_id.is_not(None),
                                UsageCostRow.input_tokens + UsageCostRow.output_tokens,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ),
            ).where(
                UsageCostRow.product_mode == "chat",
                UsageCostRow.conversation_id == conversation_id,
            )
        )
    ).one()
    now = datetime.now(UTC)
    if window_id is not None and window_start <= now < window_end:
        window_filter = UsageCostRow.four_hour_window_id == window_id
    else:
        window_filter = (UsageCostRow.metered_at >= now) & (
            UsageCostRow.metered_at < now
        )
    all_cost = Decimal(
        await session.scalar(
            select(func.coalesce(func.sum(UsageCostRow.cost_usd), 0)).where(
                UsageCostRow.product_mode == "chat",
                UsageCostRow.user_id == user_id,
                window_filter,
            )
        )
        or 0
    )
    conversation_window_cost = Decimal(
        await session.scalar(
            select(func.coalesce(func.sum(UsageCostRow.cost_usd), 0)).where(
                UsageCostRow.product_mode == "chat",
                UsageCostRow.conversation_id == conversation_id,
                window_filter,
            )
        )
        or 0
    )
    limit = Decimal(four_hour_limit)

    def percent(value: Decimal) -> float:
        return (
            float(value / limit * 100) if limit > 0 else (100.0 if value > 0 else 0.0)
        )

    return {
        "current_context_tokens": conversation.current_context_tokens
        if conversation
        else 0,
        "subagent_tokens": int(totals[1] or 0),
        "total_tokens": int(totals[0] or 0),
        # User-facing percentages show confirmed provider spend only. Active
        # reservations protect admission but are estimates and must not make the
        # UI claim spend that may never be billed.
        "four_hour_percent_conversation": percent(conversation_window_cost),
        "four_hour_percent_all_chat": percent(all_cost),
        "window_ends_at": window_end.isoformat()
        if window_id and window_start <= now < window_end
        else None,
    }
