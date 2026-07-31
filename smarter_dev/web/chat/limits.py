"""Transactional admission, settlement, and overage attribution for web Chat."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from decimal import Decimal
from uuid import UUID
from uuid import uuid4

from skrift.db.models.user import User
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.chat.spend import SpendDecision
from smarter_dev.web.chat.spend import as_utc
from smarter_dev.web.chat.spend import daily_bounds
from smarter_dev.web.chat.spend import evaluate_spend
from smarter_dev.web.chat.spend import four_hour_bounds
from smarter_dev.web.chat.spend import shared_overage_used
from smarter_dev.web.chat.spend import weekly_bounds
from smarter_dev.web.models import ChatSpendLimit
from smarter_dev.web.models import ChatSpendReservation
from smarter_dev.web.models import ChatSpendWindow
from smarter_dev.web.models import UsageCostRow

ZERO = Decimal("0")


class OperationAlreadyReserved(RuntimeError):
    """An ambiguous/in-flight provider operation must never be sent twice."""


async def get_or_start_four_hour_window(
    session: AsyncSession,
    user_id: UUID,
    *,
    now: datetime | None = None,
) -> ChatSpendWindow:
    """Atomically start a user-relative, exact four-hour half-open window."""
    now = as_utc(now or datetime.now(UTC))
    user = await session.scalar(
        select(User).where(User.id == user_id).with_for_update()
    )
    if user is None:
        raise PermissionError("Chat user no longer exists")
    window = await session.scalar(
        select(ChatSpendWindow)
        .where(ChatSpendWindow.user_id == user_id)
        .order_by(ChatSpendWindow.starts_at.desc())
        .limit(1)
        .with_for_update()
    )
    if window is None or now >= as_utc(window.ends_at):
        starts_at, ends_at = four_hour_bounds(now)
        window = ChatSpendWindow(
            id=uuid4(),
            user_id=user_id,
            starts_at=starts_at,
            ends_at=ends_at,
            overage_cost_usd=ZERO,
        )
        session.add(window)
        await session.flush()
    return window


async def _sum_committed(
    session: AsyncSession, user_id: UUID, start: datetime, end: datetime
) -> Decimal:
    return Decimal(
        await session.scalar(
            select(func.coalesce(func.sum(UsageCostRow.cost_usd), 0)).where(
                UsageCostRow.product_mode == "chat",
                UsageCostRow.user_id == user_id,
                UsageCostRow.metered_at >= start,
                UsageCostRow.metered_at < end,
            )
        )
        or 0
    )


async def _sum_window_committed(session: AsyncSession, window_id: UUID) -> Decimal:
    return Decimal(
        await session.scalar(
            select(func.coalesce(func.sum(UsageCostRow.cost_usd), 0)).where(
                UsageCostRow.product_mode == "chat",
                UsageCostRow.four_hour_window_id == window_id,
            )
        )
        or 0
    )


async def _sum_reservations(
    session: AsyncSession,
    user_id: UUID,
    now: datetime,
    *,
    window_id: UUID | None = None,
) -> Decimal:
    filters = [
        ChatSpendReservation.user_id == user_id,
        ChatSpendReservation.status == "active",
        ChatSpendReservation.expires_at > now,
    ]
    if window_id is not None:
        filters.append(ChatSpendReservation.window_id == window_id)
    return Decimal(
        await session.scalar(
            select(func.coalesce(func.sum(ChatSpendReservation.reserved_usd), 0)).where(
                *filters
            )
        )
        or 0
    )


async def _decision(
    session: AsyncSession,
    *,
    user_id: UUID,
    tier: str,
    intelligence_mode: str,
    pending_cost: Decimal,
    now: datetime,
    window: ChatSpendWindow,
    exclude_reservation: UUID | None = None,
) -> SpendDecision:
    limits = await session.get(ChatSpendLimit, tier)
    if limits is None:
        raise PermissionError("Chat spend tier is not configured")
    day_start, day_end = daily_bounds(now)
    week_start, week_end = weekly_bounds(now)
    active_filters = [
        ChatSpendReservation.user_id == user_id,
        ChatSpendReservation.status == "active",
        ChatSpendReservation.expires_at > now,
    ]
    if exclude_reservation is not None:
        active_filters.append(ChatSpendReservation.id != exclude_reservation)

    async def reserved_between(start: datetime, end: datetime) -> Decimal:
        return Decimal(
            await session.scalar(
                select(
                    func.coalesce(func.sum(ChatSpendReservation.reserved_usd), 0)
                ).where(
                    *active_filters,
                    ChatSpendReservation.created_at >= start,
                    ChatSpendReservation.created_at < end,
                )
            )
            or 0
        )

    window_reserved = Decimal(
        await session.scalar(
            select(func.coalesce(func.sum(ChatSpendReservation.reserved_usd), 0)).where(
                *active_filters, ChatSpendReservation.window_id == window.id
            )
        )
        or 0
    )
    return evaluate_spend(
        four_hour_spend=await _sum_window_committed(session, window.id)
        + window_reserved,
        daily_spend=await _sum_committed(session, user_id, day_start, day_end)
        + await reserved_between(day_start, day_end),
        weekly_spend=await _sum_committed(session, user_id, week_start, week_end)
        + await reserved_between(week_start, week_end),
        four_hour_limit=limits.four_hour_usd,
        daily_limit=limits.daily_usd,
        weekly_limit=limits.weekly_usd,
        pending_cost=max(Decimal(pending_cost), ZERO),
        ultra=intelligence_mode == "ultra_intelligence",
    )


async def current_spend_decision(
    session: AsyncSession,
    *,
    user_id: UUID,
    tier: str,
    intelligence_mode: str,
    now: datetime | None = None,
    window_id: UUID | None = None,
) -> SpendDecision:
    now = as_utc(now or datetime.now(UTC))
    window = (
        await session.get(ChatSpendWindow, window_id)
        if window_id is not None
        else await get_or_start_four_hour_window(session, user_id, now=now)
    )
    if window is None or window.user_id != user_id:
        raise LookupError("Chat spend window is unavailable")
    return await _decision(
        session,
        user_id=user_id,
        tier=tier,
        intelligence_mode=intelligence_mode,
        pending_cost=ZERO,
        now=now,
        window=window,
    )


async def reserve_operation(
    session: AsyncSession,
    *,
    operation_key: str,
    operation_type: str = "primary",
    user_id: UUID,
    tier: str,
    intelligence_mode: str,
    estimate_usd: Decimal,
    conversation_id: UUID | None = None,
    root_turn_id: UUID | None = None,
    now: datetime | None = None,
    window_id: UUID | None = None,
    allow_hard_cutoff: bool = False,
) -> tuple[ChatSpendReservation | None, SpendDecision]:
    """Reserve one cost-producing operation under the durable user lock."""
    now = as_utc(now or datetime.now(UTC))
    await session.scalar(select(User).where(User.id == user_id).with_for_update())
    existing = await session.scalar(
        select(ChatSpendReservation)
        .where(ChatSpendReservation.operation_key == operation_key)
        .with_for_update()
    )
    if existing is not None:
        window = await session.get(ChatSpendWindow, existing.window_id)
        if existing.status == "active" and as_utc(existing.expires_at) > now:
            # There is no durable response to replay yet. Reusing this row would
            # send the same ambiguously accepted provider request twice while
            # accounting for it once. Keep the reservation charged and fail
            # closed until it settles or expires.
            raise OperationAlreadyReserved(operation_key)
        # Terminal operations are idempotent; callers can replay their durable
        # result. Expired/released operations must be admitted afresh.
        if existing.status == "settled":
            decision = await _decision(
                session,
                user_id=user_id,
                tier=tier,
                intelligence_mode=intelligence_mode,
                pending_cost=ZERO,
                now=now,
                window=window,
            )
            return existing, decision
        existing.status = "active"
        existing.reserved_usd = max(Decimal(estimate_usd), ZERO)
        # Ambiguous provider failures retain this reservation through every
        # fixed accounting window they can affect. It must not disappear after
        # two hours and silently restore daily/weekly capacity.
        existing.expires_at = weekly_bounds(now)[1]
        selected_window = (
            await session.get(ChatSpendWindow, window_id)
            if window_id is not None
            else await get_or_start_four_hour_window(session, user_id, now=now)
        )
        if selected_window is None or selected_window.user_id != user_id:
            raise LookupError("Chat spend window is unavailable")
        existing.window_id = selected_window.id
        existing.intelligence_mode = intelligence_mode
        existing.operation_type = operation_type
        existing.conversation_id = conversation_id
        existing.root_turn_id = root_turn_id
        window = await session.get(ChatSpendWindow, existing.window_id)
        decision = await _decision(
            session,
            user_id=user_id,
            tier=tier,
            intelligence_mode=intelligence_mode,
            pending_cost=existing.reserved_usd,
            now=now,
            window=window,
            exclude_reservation=existing.id,
        )
        if not decision.allowed and not allow_hard_cutoff:
            existing.status = "released"
            existing.reserved_usd = ZERO
            return None, decision
        return existing, decision

    window = (
        await session.get(ChatSpendWindow, window_id)
        if window_id is not None
        else await get_or_start_four_hour_window(session, user_id, now=now)
    )
    if window is None or window.user_id != user_id:
        raise LookupError("Chat spend window is unavailable")
    estimate = max(Decimal(estimate_usd), ZERO)
    decision = await _decision(
        session,
        user_id=user_id,
        tier=tier,
        intelligence_mode=intelligence_mode,
        pending_cost=estimate,
        now=now,
        window=window,
    )
    if not decision.allowed and not allow_hard_cutoff:
        return None, decision
    reservation = ChatSpendReservation(
        operation_key=operation_key,
        operation_type=operation_type,
        user_id=user_id,
        conversation_id=conversation_id,
        root_turn_id=root_turn_id,
        window_id=window.id,
        intelligence_mode=intelligence_mode,
        reserved_usd=estimate,
        status="active",
        expires_at=weekly_bounds(now)[1],
    )
    session.add(reservation)
    await session.flush()
    return reservation, decision


async def settle_reservation_actual(
    session: AsyncSession,
    operation_key: str,
    *,
    actual_cost: Decimal,
    tier: str,
) -> tuple[Decimal, UUID, datetime]:
    """Settle actual cost and return incremental overage, window, meter time."""
    # Preserve one lock order everywhere: user first, reservation second.
    # Reading the owner without a lock is safe; the second select revalidates
    # the reservation after the user lock is held.
    owner_id = await session.scalar(
        select(ChatSpendReservation.user_id).where(
            ChatSpendReservation.operation_key == operation_key
        )
    )
    if owner_id is None:
        raise LookupError(f"missing reservation {operation_key}")
    await session.scalar(select(User).where(User.id == owner_id).with_for_update())
    reservation = await session.scalar(
        select(ChatSpendReservation)
        .where(ChatSpendReservation.operation_key == operation_key)
        .with_for_update()
    )
    if reservation is None:
        raise LookupError(f"missing reservation {operation_key}")
    existing_usage = await session.scalar(
        select(UsageCostRow).where(UsageCostRow.operation_key == operation_key)
    )
    if reservation.status == "settled" and existing_usage is not None:
        return (
            Decimal(existing_usage.overage_cost_usd),
            reservation.window_id,
            existing_usage.metered_at,
        )
    limits = await session.get(ChatSpendLimit, tier)
    window = await session.get(ChatSpendWindow, reservation.window_id)
    if limits is None or window is None:
        raise LookupError("spend configuration disappeared during settlement")
    metered_at = as_utc(reservation.created_at)
    day_start, day_end = daily_bounds(metered_at)
    week_start, week_end = weekly_bounds(metered_at)
    four_before = await _sum_window_committed(session, window.id)
    daily_before = await _sum_committed(
        session, reservation.user_id, day_start, day_end
    )
    weekly_before = await _sum_committed(
        session, reservation.user_id, week_start, week_end
    )
    cost = max(Decimal(actual_cost), ZERO)
    before = shared_overage_used(
        four_hour_spend=four_before,
        daily_spend=daily_before,
        weekly_spend=weekly_before,
        four_hour_limit=limits.four_hour_usd,
        daily_limit=limits.daily_usd,
        weekly_limit=limits.weekly_usd,
    )
    after = shared_overage_used(
        four_hour_spend=four_before + cost,
        daily_spend=daily_before + cost,
        weekly_spend=weekly_before + cost,
        four_hour_limit=limits.four_hour_usd,
        daily_limit=limits.daily_usd,
        weekly_limit=limits.weekly_usd,
    )
    incremental = min(cost, max(after - before, ZERO))
    window.overage_cost_usd = Decimal(window.overage_cost_usd or 0) + incremental
    reservation.status = "settled"
    reservation.reserved_usd = ZERO
    return incremental, window.id, metered_at


async def settle_reservation(
    session: AsyncSession, operation_key: str, *, status: str = "settled"
) -> None:
    reservation = await session.scalar(
        select(ChatSpendReservation)
        .where(ChatSpendReservation.operation_key == operation_key)
        .with_for_update()
    )
    if reservation and reservation.status == "active":
        reservation.status = status
        reservation.reserved_usd = ZERO
