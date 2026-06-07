"""Stripe webhook event handling for sudo memberships.

The webhook handler is the source of truth for role grants, founder seat
assignment, and membership lifecycle. Every handler is idempotent and keyed
by ``stripe_checkout_session_id`` / ``stripe_payment_intent_id`` so Stripe's
automatic retries are safe.

Event semantics (one-time payment mode):

* ``checkout.session.completed`` — first paid checkout. For rwx 0day it
  assigns a founder seat (atomic against the inventory cap); if the cap is
  already hit (race past the UI guardrail) the charge is refunded. Inserts
  the ``sudo_memberships`` row, grants the appropriate sudo role, and sets
  ``expires_at = purchased_at + 365 days``.
* ``charge.refunded`` — marks the membership refunded and revokes the role.
  The founder seat stays burned so the inventory cap stays consistent
  against historical purchases.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.services import assign_role_to_user, remove_role_from_user

from smarter_dev.web.billing import inventory
from smarter_dev.web.billing.client import get_stripe
from smarter_dev.web.billing.converge import converge
from smarter_dev.web.models import SudoMembership

logger = logging.getLogger(__name__)


# How long each one-time founder purchase grants access.
ACCESS_DURATION = timedelta(days=365)

# Map a tier slug to the Skrift role it grants.
TIER_TO_ROLE: dict[str, str] = {
    "execute": "sudo-rwx",
    "write":   "sudo-rw",
    "read":    "sudo-r",
}


async def _get_by_session_id(
    session: AsyncSession, checkout_session_id: str
) -> SudoMembership | None:
    result = await session.execute(
        select(SudoMembership).where(
            SudoMembership.stripe_checkout_session_id == checkout_session_id
        )
    )
    return result.scalar_one_or_none()


async def _get_by_payment_intent(
    session: AsyncSession, payment_intent_id: str
) -> SudoMembership | None:
    result = await session.execute(
        select(SudoMembership).where(
            SudoMembership.stripe_payment_intent_id == payment_intent_id
        )
    )
    return result.scalar_one_or_none()


async def _grant_role_for_tier(session: AsyncSession, user_id: UUID, tier: str) -> None:
    role_name = TIER_TO_ROLE.get(tier)
    if not role_name:
        logger.warning("No role mapping for tier %r; skipping grant.", tier)
        return
    assigned = await assign_role_to_user(session, user_id, role_name)
    if not assigned:
        logger.error(
            "Failed to assign role %s to user %s (user or role not found).",
            role_name,
            user_id,
        )


async def _revoke_role_for_tier(session: AsyncSession, user_id: UUID, tier: str) -> None:
    role_name = TIER_TO_ROLE.get(tier)
    if not role_name:
        return
    await remove_role_from_user(session, user_id, role_name)


async def handle_checkout_session_completed(
    session: AsyncSession, event_data: dict[str, Any]
) -> None:
    """Handle ``checkout.session.completed`` for a one-time founder purchase.

    For rwx 0day: assigns a founder seat atomically. If the cap is already
    hit (race past the UI guardrail) the charge is refunded and no role is
    granted.
    """
    session_obj = event_data["object"]
    checkout_session_id = session_obj.get("id")
    user_id_raw = session_obj.get("client_reference_id")
    if not user_id_raw:
        logger.error(
            "checkout.session.completed without client_reference_id; session=%s",
            checkout_session_id,
        )
        return

    user_id = UUID(user_id_raw)
    metadata = session_obj.get("metadata") or {}
    tier = metadata.get("tier")
    if tier not in TIER_TO_ROLE:
        logger.error(
            "checkout.session.completed with unknown tier %r; session=%s",
            tier, checkout_session_id,
        )
        return

    payment_intent_id = session_obj.get("payment_intent")
    stripe_customer_id = session_obj.get("customer")
    if not payment_intent_id or not stripe_customer_id:
        logger.error(
            "checkout.session.completed missing payment_intent/customer; session=%s",
            checkout_session_id,
        )
        return

    # Idempotency: if we've already recorded this checkout, do nothing.
    existing = await _get_by_session_id(session, checkout_session_id)
    if existing is not None:
        return

    # Pull amount + price from Stripe so we record the truth, not what the
    # client claimed in the form.
    stripe = get_stripe()
    line_items = stripe.checkout.Session.list_line_items(checkout_session_id, limit=1)
    if not line_items["data"]:
        logger.error("No line items on checkout session %s", checkout_session_id)
        return
    item = line_items["data"][0]
    price_id = item["price"]["id"]
    amount_paid_cents = int(session_obj.get("amount_total") or 0)

    founder_seat: int | None = None
    if tier == "execute":
        founder_seat = await inventory.reserve_next_seat(session)
        if founder_seat is None:
            # Lost the race — refund and abandon the grant.
            logger.error(
                "Founder rwx seats exhausted by the time webhook fired; "
                "refunding payment_intent %s for user %s",
                payment_intent_id, user_id,
            )
            try:
                stripe.Refund.create(payment_intent=payment_intent_id)
            except Exception:
                logger.exception("Refund failed for overflow founder seat.")
            return

    now = datetime.now(tz=timezone.utc)
    membership = SudoMembership(
        user_id=user_id,
        tier=tier,
        source="one_time",
        stripe_customer_id=stripe_customer_id,
        stripe_checkout_session_id=checkout_session_id,
        stripe_payment_intent_id=payment_intent_id,
        stripe_price_id=price_id,
        amount_paid_cents=amount_paid_cents,
        purchased_at=now,
        expires_at=now + ACCESS_DURATION,
        founder_seat_number=founder_seat,
    )
    session.add(membership)
    await session.commit()

    await _grant_role_for_tier(session, user_id, tier)
    await session.commit()

    # Project to Discord. Failures are logged inside converge and do not
    # block the response — the daily sweep + GUILD_MEMBER_ADD heal drift.
    try:
        await converge(session, user_id)
    except Exception:
        logger.exception("converge failed after fulfillment for user %s", user_id)


async def handle_charge_refunded(
    session: AsyncSession, event_data: dict[str, Any]
) -> None:
    """Handle ``charge.refunded`` — revoke the role; seat stays burned."""
    charge = event_data["object"]
    payment_intent_id = charge.get("payment_intent")
    if not payment_intent_id:
        return

    record = await _get_by_payment_intent(session, payment_intent_id)
    if record is None:
        logger.info(
            "charge.refunded for unknown payment_intent %s; ignoring.",
            payment_intent_id,
        )
        return

    if record.refunded_at is not None:
        # Already processed.
        return

    record.refunded_at = datetime.now(tz=timezone.utc)
    record.revoked_reason = "refund"
    await session.commit()

    await _revoke_role_for_tier(session, record.user_id, record.tier)
    await session.commit()

    try:
        await converge(session, record.user_id)
    except Exception:
        logger.exception("converge failed after refund for user %s", record.user_id)


async def expire_lapsed_memberships(session: AsyncSession) -> int:
    """Revoke roles for any memberships whose ``expires_at`` has passed.

    Intended to be called from a scheduled sweep. Returns the number of
    memberships expired in this pass.
    """
    now = datetime.now(tz=timezone.utc)
    result = await session.execute(
        select(SudoMembership).where(
            SudoMembership.expires_at < now,
            SudoMembership.refunded_at.is_(None),
        )
    )
    expired = 0
    expired_user_ids: list[UUID] = []
    for membership in result.scalars():
        role_name = TIER_TO_ROLE.get(membership.tier)
        if role_name is None:
            continue
        await remove_role_from_user(session, membership.user_id, role_name)
        expired_user_ids.append(membership.user_id)
        expired += 1
    if expired:
        await session.commit()
    # Project the lapse to Discord too. Inline serial calls — fine at
    # launch-cohort scale (a few dozen a day at most).
    for uid in expired_user_ids:
        try:
            await converge(session, uid)
        except Exception:
            logger.exception("converge failed during sweep for user %s", uid)
    return expired


_HANDLERS = {
    "checkout.session.completed": handle_checkout_session_completed,
    "charge.refunded": handle_charge_refunded,
}


async def dispatch(session: AsyncSession, event: dict[str, Any]) -> None:
    """Dispatch a verified Stripe event to its handler. No-ops for unknown types."""
    event_type = event.get("type")
    handler = _HANDLERS.get(event_type)
    if handler is None:
        return
    await handler(session, event.get("data") or {})
